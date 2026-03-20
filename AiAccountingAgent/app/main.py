"""
FastAPI application — the contest-facing entry point.

Endpoints:
  POST /solve   — receives a task, runs the agent, returns {"status": "completed"}
  GET  /health  — liveness check for Cloud Run

The /solve endpoint is synchronous (def, not async) so FastAPI runs it in
a thread pool. This lets the agent use blocking requests calls freely without
blocking the event loop.

The endpoint ALWAYS returns {"status": "completed"} with HTTP 200 — even
if the agent hits an error internally. Returning 200 is required by the
contest spec; any exception is logged for debugging.
"""
import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .agent import run_agent
from .logging_setup import setup_logging
from .schemas import SolveRequest, SolveResponse

setup_logging()
logger = logging.getLogger(__name__)

app = FastAPI(title="Tripletex AI Accounting Agent", version="1.0.0")


@app.post("/solve", response_model=SolveResponse)
def solve(request: SolveRequest) -> SolveResponse:
    request_id = str(uuid.uuid4())[:8]
    t0 = time.time()

    logger.info(
        "Solve request received",
        extra={
            "request_id": request_id,
            "prompt_length": len(request.prompt),
            "num_files": len(request.files),
            "base_url": request.tripletex_credentials.base_url,
        },
    )

    try:
        run_agent(
            prompt=request.prompt,
            files=request.files,
            base_url=request.tripletex_credentials.base_url,
            session_token=request.tripletex_credentials.session_token,
        )
    except Exception as exc:
        # Log but never propagate — the contest requires HTTP 200
        logger.error(
            f"Agent raised an unhandled exception: {exc}",
            exc_info=True,
            extra={"request_id": request_id},
        )

    elapsed = round(time.time() - t0, 2)
    logger.info(
        "Solve request complete",
        extra={"request_id": request_id, "elapsed_s": elapsed},
    )

    return SolveResponse(status="completed")


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/test")
def test_gemini() -> JSONResponse:
    """Debug endpoint — tests Gemini connectivity and returns the exact error if any."""
    from . import config
    from .agent import _build_gemini_client
    try:
        client = _build_gemini_client()
        # Try models in order of preference until one works
        candidates = [
            "gemini-2.5-pro-preview-03-25",
            "gemini-2.5-flash-preview-04-17",
            "gemini-2.0-flash-001",
            "gemini-1.5-pro-latest",
            "gemini-1.5-flash-latest",
        ]
        working_model = None
        last_error = None
        for model in candidates:
            try:
                response = client.models.generate_content(
                    model=model,
                    contents="Reply with the single word: working",
                )
                working_model = model
                break
            except Exception as e:
                last_error = str(e)
                continue

        # List available models regardless of above outcome
        available = []
        try:
            for m in client.models.list():
                available.append(m.name)
        except Exception as e:
            available = [f"list_error: {e}"]

        if working_model:
            return JSONResponse({
                "success": True,
                "working_model": working_model,
                "response": response.text,
                "available_models": available[:20],
            })
        else:
            return JSONResponse({
                "success": False,
                "tried": candidates,
                "last_error": last_error,
                "available_models": available[:20],
                "key_prefix": config.GEMINI_API_KEY[:8] + "..." if config.GEMINI_API_KEY else "NOT SET",
            })
    except Exception as exc:
        return JSONResponse({
            "success": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
        })


@app.middleware("http")
async def log_requests(request: Request, call_next):
    t0 = time.time()
    response = await call_next(request)
    elapsed_ms = round((time.time() - t0) * 1000)
    logger.info(
        f"{request.method} {request.url.path} → {response.status_code} ({elapsed_ms}ms)"
    )
    return response
