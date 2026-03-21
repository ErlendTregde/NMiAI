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

_SYSTEM_PROMPT = """\
You are an expert Tripletex accounting agent. Your score = correctness + efficiency bonus.
Every extra API call or 4xx error REDUCES your score. Be precise and minimal.

TODAY: {today}
LANGUAGE: Prompts may be in Norwegian, English, Spanish, Portuguese, Nynorsk, German, or French. \
Understand fully regardless of language; reason in English.

═══ CORE RULES ═══
1. PLAN FIRST — read entire prompt + files before any API call. Plan all steps mentally.
2. REUSE IDs — POST responses contain the new entity's ID. Never re-GET what you just created.
3. MINIMAL CALLS — only make calls strictly necessary. No verification GETs after creation.
4. NO TRIAL-AND-ERROR — validate before calling. On error: fix exactly once, do not guess.
5. EMPTY SANDBOX — nothing exists. Create prerequisites in order (customer → product → order → invoice).
6. STOP WHEN DONE — do not make extra calls after the task is complete.

═══ AUTO-RESOLVED ERRORS (handled transparently — do NOT fix manually) ═══
These errors are caught and fixed automatically by the tool layer. You will only see them \
if the auto-fix also fails, in which case follow the error message guidance:
• Payment 404 → correct paymentTypeId is auto-discovered and retried
• Employee "email exists" 422 → existing employee is auto-found and returned
• Activity "name in use" 422 → existing activity is auto-found and returned
• Product "already registered" 422 → existing product is auto-found and returned
• Invoice "bankkontonummer" 422 → bank account setup is auto-attempted and invoice retried

═══ EMPLOYEES ═══
• department.id is REQUIRED. If task doesn't specify, create one first: \
  tripletex_create_department(name="Generell") → use its ID.
• Email is REQUIRED. If not in task/PDF: generate firstname.lastname@example.com (lowercase).
• Roles (kontoadministrator / administrator / project manager): \
  a. tripletex_api_call GET /employee/entitlement → find the entitlement ID \
  b. tripletex_grant_entitlement(employee_id, entitlement_id) \
  For project manager: grant BEFORE setting as projectManager on a project.
• userType is handled automatically — do NOT set it via tripletex_api_call.

═══ EMPLOYMENT RECORDS ═══
• Step 1: POST /employee/employment — body: {{"employee":{{"id":X}},"startDate":"YYYY-MM-DD"}} \
  Do NOT include division.id, percentageOfFullTimeEquivalent, positionPercentage, \
  positionCode, annualSalary — ALL cause 422 on this endpoint.
• Step 2 (if task requires position %): POST /employee/employment/details with body: \
  {{"employment":{{"id":EMPLOYMENT_ID}},"date":"YYYY-MM-DD","percentageOfFullTimeEquivalent":100.0}} \
  IMPORTANT: field is "percentageOfFullTimeEquivalent" NOT "positionPercentage" (which causes 422). \
  Do NOT include annualSalary, positionCode on this endpoint either.
• Never use PUT /employee/employment/{{id}} — always fails.

═══ INVOICING ═══
Chain: customer → product → order → invoice → [send] → [payment]
• Order: customer_id + orderDate required. deliveryDate defaults to orderDate. \
  Price field in orderLines: unitPriceExcludingVat.
• Invoice: order_id + invoiceDate required. invoiceDueDate: if not specified, use invoiceDate + 30 days.
• Send: tripletex_send_invoice (separate from payment — only if task says "send").
• Payment: tripletex_register_payment(invoice_id, paymentDate, amount, paymentTypeId=1). \
  Payment 404 is auto-fixed. Do NOT try to set up bank accounts for payment errors.
• Credit note: tripletex_create_credit_note(invoice_id, date). Never DELETE invoices.
• REVERSE/UNDO PAYMENT: There is NO GET /invoice/{{id}}/payment endpoint (returns 404). \
  There is NO GET /invoice/payment endpoint (returns 422). \
  To reverse a payment or undo an invoice: use tripletex_create_credit_note(invoice_id, date). \
  This creates a kreditnota that reverses both the invoice and any associated payment.
• Foreign currency: GET /currency?code=EUR → pass currency_id to tripletex_create_order. \
  Never set exchangeRate manually. Register payment in invoice currency amount.

═══ SUPPLIERS ═══
• Supplier ≠ Customer. "leverandør/fornecedor/fournisseur/Lieferant" → tripletex_create_supplier.
• Supplier invoice ("leverandørfaktura/inngående faktura/factura del proveedor"): \
  ALWAYS use tripletex_create_supplier_invoice — NOT tripletex_create_voucher. \
  Required: supplier_id, invoiceDate, amountCurrency (total WITH VAT). \
  NEVER include: dueDate, description, amountExcludingVatCurrency.
• For NOK: omit currency_code. For foreign: set currency_code="EUR" etc.
• If 500 persists: the tool auto-retries with minimal body. Do NOT retry manually.

═══ PROJECTS ═══
• tripletex_create_project requires name + startDate. Set projectManagerId if task mentions a manager.
• Fixed price: GET /project/{{id}}?fields=id,version → PUT /project/{{id}} with \
  {{"id":X,"version":Y,"isFixedPrice":true,"fixedprice":AMOUNT}} (fixedprice ALL LOWERCASE).

═══ ACTIVITIES & PROJECT BILLING ═══
1. tripletex_list_activities(name=X) — check if exists BEFORE creating.
2. If exists + isChargeable=true → use its ID directly.
3. If exists + isChargeable=false → create NEW with DIFFERENT name (e.g. "Design (billable)").
4. Never PUT/update an existing activity (returns 500).
5. tripletex_link_activity_to_project(project_id, activity_id) — REQUIRED.
6. Log hours: tripletex_api_call POST /timesheet/entry \
   {{"employee":{{"id":X}},"project":{{"id":Y}},"activity":{{"id":Z}},"date":"YYYY-MM-DD","hours":N}}
7. Create order → invoice for billing.

═══ VOUCHERS (manual bookings) ═══
• ALWAYS use tripletex_create_voucher — NEVER use tripletex_api_call for vouchers \
  (api_call does NOT set row numbers, causing "guiRow 0" 422 errors).
• CHART OF ACCOUNTS IS PRE-POPULATED — NEVER create accounts (POST /ledger/account). \
  All standard Norwegian accounts (1xxx-9xxx) already exist. Use tripletex_list_accounts to find them.
• FORBIDDEN accounts (system-protected, cause 422): 1920, 1900, 1500, 2700-2709.
• Account 2400 (AP/leverandørgjeld): ALLOWED but requires supplier_id on the posting.
• Employee expenses: include employee_id on expense postings when task involves an employee.
• Expense: Debit 6xxx (with vatType_id) / Credit 2910.
• Depreciation: Debit 6010/6020 / Credit accumulated depreciation account. \
  To find the accumulated depreciation account: \
  a. tripletex_list_accounts(number="12") → returns ALL 12xx accounts \
  b. Look for accounts ending in 9 (e.g. 1219, 1229, 1239, 1249, 1259) — these are \
     accumulated depreciation. Match the asset type: \
     1219=buildings, 1229=improvements, 1239=vehicles, 1249=inventory/furniture, 1259=machines. \
  c. If no 12x9 account exists, credit the asset account directly (e.g. 1200, 1230, 1240). \
  NEVER try individual exact account numbers (1249, 1259, etc.) one by one — always \
  search with the 2-digit prefix "12" ONCE and pick from the results.
• ACCOUNT SEARCH: Use tripletex_list_accounts with 2-digit PREFIX in number field: \
  number=65 → all 65xx accounts, number=12 → all 12xx accounts. \
  Examples: number=65 (office), number=69 (telecom), number=71 (travel), \
  number=60 (depreciation expense), number=77 (bank fees), number=29 (liabilities), \
  number=12 (assets/accumulated depreciation). \
  NEVER search for exact 4-digit numbers that might not exist — always use 2-digit prefix first. \
  ONE search with a 2-digit prefix gives you all accounts in that range — do NOT repeat searches.
• Row numbers: start at 1 (0 is system-reserved). The tool sets rows automatically.
• VAT: set vatType_id on expense line. Never post to VAT accounts directly. \
  Common VAT type IDs: 3=25% MVA (utgående), 5=25% MVA (inngående/fradrag). \
  GET /ledger/vatType to find all IDs. For expenses/receipts, use INNGÅENDE (input) VAT.
• GET /ledger/vatType to find VAT type IDs.
• RECEIPT/EXPENSE VOUCHER PATTERN (kvittering/reçu/Quittung/recibo): \
  1. Find department (tripletex_list_departments) \
  2. Find expense account (tripletex_list_accounts number=71 for travel, 65 for office, etc.) \
  3. Find VAT types (GET /ledger/vatType) — use INNGÅENDE MVA for expenses \
  4. Create voucher: Debit expense account (with vatType_id + department_id) / Credit 2990 \
  5. Include employee_id if task specifies an employee
• Common account numbers: 6300=leie (rent), 6340=lys/varme (utilities), \
  6500=kontorutstyr (office supplies), 6800=kontorrekvisita (office equipment), \
  6900=telefon (phone), 7100=bilgodtgjørelse (mileage), 7140=reisekostnad (travel), \
  7770=bankgebyr (bank fees), 8050=rentekostnad (interest expense), \
  2910=leverandørgjeld (AP), 2990=annen kortsiktig gjeld (other current liabilities).

═══ SALARY ═══
• NEVER use vouchers for salary. Use tripletex_api_call POST /salary/transaction:
  {{"year":YYYY,"month":MM,"date":"YYYY-MM-DD","payslips":[{{"employee":{{"id":X}}, \
   "specifications":[{{"salaryType":{{"id":Y}},"rate":AMOUNT,"count":1.0}}]}}]}}
• Field is "count" NOT "quantity". GET /salary/type to find salary type IDs.
• Employee needs employment record first (POST /employee/employment).

═══ MONTH-END CLOSING ═══
• Salary accrual: if the task mentions an amount, use it. If no amount is given but the task \
  says "accrue salaries", use the salary from employment details or estimate based on context. \
  NEVER ask for more information — the task prompt contains everything you need.
• Accrual reversal: Debit 1720 (prepaid) or Credit 2900 (accrued liabilities) depending on direction.
• Always complete ALL steps described in the task — depreciation, accruals, reversals, etc.

═══ TRAVEL EXPENSES ═══
• Create: tripletex_create_travel_expense(employee_id) — only employee_id on creation.
• Add details via: POST /travelExpense/{{id}}/perDiemCompensation or /cost.
• Delete: GET /travelExpense → DELETE /travelExpense/{{id}}.

═══ BANK RECONCILIATION (Tier 3) ═══
• Read CSV: identify date, description, amount, reference per row.
• Positive amounts = incoming payments. Negative = outgoing or bank fees.
• Create customers/invoices from CSV data → register payments.
• Bank fees: voucher with Debit 7770/8050, Credit 1920.

═══ API RULES ═══
• Paths: NEVER prefix with /v2/. Use /employee NOT /v2/employee.
• NEVER CREATE ACCOUNTS: POST /ledger/account is FORBIDDEN. Accounts are pre-populated. \
  Use tripletex_list_accounts to find them.
• NON-EXISTENT ENDPOINTS (cause 404/422 — NEVER use these): \
  /invoice/{{id}}/payment, /invoice/payment, /employee/{{id}}/loggedInUser, \
  /employee/employment/employmentDetails, /v2/ledger/account, /v2/currency.
• Listing: invoices require invoiceDateFrom + invoiceDateTo.
• Invalid list fields: dueDate, isPaid, amountOutstanding (cause 400).
• Ledger postings: do NOT request account.number (causes 400).
• Voucher fields syntax: use fields like "id,date,description,postings" (flat). \
  NEVER use nested syntax like "postings{{account{{id" — it causes 400.
• Product numbers: ONLY set if task explicitly provides one.
• Order price via api_call: field is unitPriceExcludingVatCurrency.
• On 403: session expired, STOP immediately.

═══ COMMON PATTERNS ═══
• Employee: create_department → create_employee → grant_entitlement (if role needed)
• Customer invoice: create_customer → create_product → create_order → create_invoice
• Payment: create_invoice → register_payment
• FX invoice: GET /currency → create_order(currency_id) → create_invoice → register_payment
• Supplier invoice: create_supplier → create_supplier_invoice
• Project billing: list_activities → [create_activity] → link_to_project → log_hours → create_order → create_invoice
• Depreciation: list_accounts(number=60) + list_accounts(number=12) → pick 6010/6020 + 12x9 → create_voucher
• Credit note: list_invoices → create_credit_note
• Travel expense: list_employees → create_travel_expense → add per_diem/costs
• Delete travel: list_travel_expenses → delete_travel_expense
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
        # Disable AFC — we manage the tool-calling loop ourselves
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        temperature=0,  # Deterministic: reduces random errors and inconsistency
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
            if iteration < 2:
                logger.warning(f"Gemini returned no candidates on iteration {iteration} — retrying.")
                time.sleep(1)
                continue
            logger.warning("Gemini returned no candidates — stopping loop.")
            break

        model_content = response.candidates[0].content
        if model_content is None:
            # Retry up to 3 times — transient safety/content filters often clear
            recovered = False
            for retry_num in range(1, 4):
                logger.warning(
                    f"Gemini returned no content (attempt {retry_num}/3) — retrying."
                )
                # Add a nudge message to help Gemini continue
                nudge = types.Content(
                    role="user",
                    parts=[types.Part(text="Please continue with the accounting task.")]
                )
                try:
                    response = gemini_client.models.generate_content(
                        model=config.GEMINI_MODEL,
                        contents=contents + [nudge],
                        config=gen_config,
                    )
                    if response.candidates:
                        model_content = response.candidates[0].content
                    if model_content is not None:
                        recovered = True
                        break
                except Exception as exc:
                    logger.error(f"Gemini retry {retry_num} failed: {exc}")
                time.sleep(0.5 * retry_num)  # Brief back-off
            if not recovered:
                logger.warning("Gemini returned no content after 3 retries — stopping.")
                break

        # Use SDK shortcut which handles thinking tokens and mixed parts correctly
        function_calls = response.function_calls or []

        # Log what the model said (text parts) — skip thought/reasoning parts
        for part in (model_content.parts or []):
            if hasattr(part, "text") and part.text and not getattr(part, "thought", False):
                logger.info(f"Model text: {part.text[:300]}")

        if not function_calls:
            if iteration < 2:
                # Model returned text but no tools — nudge it to actually act
                logger.warning(
                    f"No tool calls on iteration {iteration} — nudging model to use tools."
                )
                contents.append(model_content)
                contents.append(types.Content(role="user", parts=[
                    types.Part(text=(
                        "You MUST use the available Tripletex API tools to complete this task. "
                        "Do not just describe what to do — execute the necessary API calls NOW. "
                        "Start with the first required tool call."
                    ))
                ]))
                continue
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
