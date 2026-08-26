"""Environment-backed configuration, read once at import time."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"Missing required environment variable {name!r}. "
            "See .env.example for the full list."
        )
    return value


DATABASE_URL = _require("DATABASE_URL")

# The co-pilot talks to any OpenAI-compatible chat-completions endpoint, so the
# provider is configuration rather than a code change. Leave AI_BASE_URL unset
# for OpenAI itself; point it at Gemini's compatibility layer, Groq, or a local
# server to switch. Only the model needs to support tool calling.
AI_API_KEY = os.environ.get("AI_API_KEY", "").strip()
AI_BASE_URL = os.environ.get("AI_BASE_URL", "").strip() or None
AI_MODEL = os.environ.get("AI_MODEL", "gemini-2.5-flash").strip()

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Stash <onboarding@resend.dev>").strip()

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").strip().rstrip("/")
APP_ENV = os.environ.get("APP_ENV", "development").strip().lower()

IS_PRODUCTION = APP_ENV == "production"

SESSION_COOKIE_NAME = "stash_session"
SESSION_TTL_HOURS = 24 * 7
RESET_TOKEN_TTL_MINUTES = 30

# Upper bound on LLM -> tool -> LLM round trips for a single chat turn.
MAX_TOOL_HOPS = 5

# MCP sessions are cached per user, because starting the subprocess costs ~1s
# locally and ~7s on a small instance — which dominated chat latency when every
# turn paid it. A cached session is an idle Python process holding no database
# connection, so the ceiling is memory: keep it low enough that the web app and
# its subprocesses fit the instance. Raise it on a larger plan.
MCP_MAX_LIVE_SESSIONS = int(os.environ.get("MCP_MAX_LIVE_SESSIONS", "4"))
MCP_SESSION_IDLE_SECONDS = float(os.environ.get("MCP_SESSION_IDLE_SECONDS", "300"))
MCP_SESSION_SWEEP_SECONDS = 30.0
