"""
Gemini agentic loop.

Flow per solve request:
  1. Build Gemini client (Vertex AI in prod, API-key locally)
  2. Process file attachments → extract text
  3. Build initial user message (task prompt + file contents)
  4. Enter the tool-calling loop:
       a. Call Gemini with all Tripletex tools available
       b. If model returns function calls → execute each → feed results back
       c. If model returns no function calls → done
  5. Return (caller always returns {"status": "completed"})

Design:
  • Synchronous — FastAPI runs sync endpoints in a thread pool, so blocking
    requests calls are fine here.
  • Hard iteration cap (MAX_AGENT_ITERATIONS) prevents infinite loops.
  • Time budget guard stops the loop before the 300 s Cloud Run deadline.
  • All validation errors from Tripletex are fed back to the model as tool
    results so it can self-correct in one additional call.
"""
import logging
import time
from datetime import date

from google import genai
from google.genai import types

from . import config
from .files import process_files
from .schemas import SolveFile
from .tools import TOOLS, execute_tool
from .tripletex.client import TripletexClient

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are an expert Tripletex accounting agent competing in a contest \
where correctness and API efficiency determine your score.

TASK: Complete the accounting task in the user message by calling Tripletex API tools. \
The prompt may be in Norwegian, English, Spanish, Portuguese, Nynorsk, German, or French \
— understand it in any language.

CRITICAL RULES (every violation reduces your score):

1. PLAN FIRST: Read the entire prompt and any file contents before making any API call. \
   Mentally plan all required steps.

2. REUSE IDs: When you create an entity (POST), the response contains its ID. \
   Use that ID directly in subsequent calls. NEVER call GET to fetch something you just POSTed.

3. MINIMAL CALLS: Every extra API call reduces your efficiency bonus. \
   Make only the calls strictly necessary to complete the task.

4. NO TRIAL-AND-ERROR: Every 4xx error reduces your score. \
   Validate parameters carefully before calling. If you get a validation error, \
   fix it precisely and retry once — do not make multiple guesses.

5. EMPTY SANDBOX: The account is brand new — nothing exists. \
   Always create prerequisites first:
   • Customer must exist before creating an order
   • Order must exist before creating an invoice
   • Product must exist before adding order lines

6. STOP WHEN DONE: After completing the task, do not make verification calls \
   unless you have specific reason for uncertainty.

7. EMPLOYEE ROLES: If an employee should be 'kontoadministrator' or 'administrator', \
   this is worth many scoring points. After creating the employee, use \
   tripletex_api_call to grant the role (e.g. PUT /employee/{id}/loggedInUser).

8. TODAY'S DATE: {today}

COMMON PATTERNS:
• Create employee → POST /employee (+ grant role if required)
• Create customer → POST /customer
• Create invoice → POST /customer → POST /product → POST /order → POST /invoice
• Register payment → POST /invoice/{id}/:payment
• Delete travel expense → GET /travelExpense → DELETE /travelExpense/{id}
• Update entity → GET /{resource}/{id} (for version) → PUT /{resource}/{id}
"""


def _build_gemini_client() -> genai.Client:
    if config.USE_VERTEX_AI:
        if not config.GOOGLE_CLOUD_PROJECT:
            raise ValueError("GOOGLE_CLOUD_PROJECT must be set when USE_VERTEX_AI=true")
        return genai.Client(
            vertexai=True,
            project=config.GOOGLE_CLOUD_PROJECT,
            location=config.VERTEX_AI_LOCATION,
        )
    if not config.GEMINI_API_KEY:
        raise ValueError(
            "Either set GEMINI_API_KEY (local dev) or USE_VERTEX_AI=true (production)"
        )
    return genai.Client(api_key=config.GEMINI_API_KEY)


def run_agent(
    prompt: str,
    files: list[SolveFile],
    base_url: str,
    session_token: str,
) -> None:
    """
    Execute the full agent loop for one solve request.
    Raises nothing — all errors are logged and we always return to the caller.
    """
    deadline = time.time() + config.TIME_BUDGET_SECONDS
    gemini_client = _build_gemini_client()
    tripletex_client = TripletexClient(base_url, session_token)

    # Process attachments
    file_text = process_files(files, gemini_client, config.GEMINI_FLASH_MODEL)

    # Build initial user message
    user_message = prompt
    if file_text:
        user_message = f"{prompt}\n\n---\nATTACHED FILES:\n{file_text}"

    system_instruction = _SYSTEM_PROMPT.format(today=date.today().isoformat())

    contents: list[types.Content] = [
        types.Content(role="user", parts=[types.Part(text=user_message)])
    ]

    gen_config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=[TOOLS],
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode="AUTO")
        ),
    )

    logger.info(
        "Agent loop starting",
        extra={
            "model": config.GEMINI_MODEL,
            "has_files": bool(files),
            "prompt_length": len(prompt),
        },
    )

    for iteration in range(config.MAX_AGENT_ITERATIONS):
        if time.time() > deadline:
            logger.warning(f"Time budget exhausted at iteration {iteration}, stopping.")
            break

        try:
            response = gemini_client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=contents,
                config=gen_config,
            )
        except Exception as exc:
            logger.error(f"Gemini generate_content failed: {exc}", exc_info=True)
            break

        if not response.candidates:
            logger.warning("Gemini returned no candidates — stopping loop.")
            break

        model_content = response.candidates[0].content

        # Use SDK shortcut which handles thinking tokens and mixed parts correctly
        function_calls = response.function_calls or []

        # Log what the model said (text parts) for debugging
        for part in (model_content.parts or []):
            if hasattr(part, "text") and part.text:
                logger.info(f"Model text: {part.text[:300]}")

        if not function_calls:
            logger.info(
                f"Agent finished after {iteration + 1} Gemini call(s) — "
                f"no tool calls in response."
            )
            break

        # Add the model's response to conversation history
        contents.append(model_content)

        # Execute every function call and collect results
        result_parts: list[types.Part] = []
        for fc in function_calls:
            if time.time() > deadline:
                logger.warning("Time budget hit while executing tool calls.")
                break
            result = execute_tool(tripletex_client, fc.name, dict(fc.args))
            result_parts.append(
                types.Part.from_function_response(name=fc.name, response=result)
            )

        # Feed all tool results back in a single user turn
        contents.append(types.Content(role="user", parts=result_parts))

    else:
        logger.warning(
            f"Agent hit MAX_AGENT_ITERATIONS ({config.MAX_AGENT_ITERATIONS}) — stopping."
        )

    # Log summary
    logger.info(
        "Agent loop complete",
        extra={
            "total_tripletex_calls": len(tripletex_client.call_log),
            "call_log": tripletex_client.call_log,
        },
    )
