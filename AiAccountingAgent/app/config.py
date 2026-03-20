"""
Central configuration — all env vars in one place.

Local dev:  set GEMINI_API_KEY in .env (USE_VERTEX_AI defaults to false)
Production: set USE_VERTEX_AI=true, GOOGLE_CLOUD_PROJECT, VERTEX_AI_LOCATION
"""
import os
from pathlib import Path

# Load .env if present (local dev only — no-op in Cloud Run)
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file)
    except ImportError:
        pass  # dotenv not installed — env vars must be set manually

# ── Gemini / Vertex AI ────────────────────────────────────────────────────────
USE_VERTEX_AI: bool = os.environ.get("USE_VERTEX_AI", "false").lower() == "true"
GOOGLE_CLOUD_PROJECT: str = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
VERTEX_AI_LOCATION: str = os.environ.get("VERTEX_AI_LOCATION", "europe-west1")
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")

# Model to use for the main reasoning + tool-calling loop
GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")
# Lighter model for file extraction only
GEMINI_FLASH_MODEL: str = os.environ.get("GEMINI_FLASH_MODEL", "gemini-2.5-flash")

# ── Agent behaviour ───────────────────────────────────────────────────────────
# Hard cap on tool-call iterations per solve request
MAX_AGENT_ITERATIONS: int = int(os.environ.get("MAX_AGENT_ITERATIONS", "25"))
# Stop the agent loop this many seconds before the 300 s hard deadline
TIME_BUDGET_SECONDS: int = int(os.environ.get("TIME_BUDGET_SECONDS", "240"))
