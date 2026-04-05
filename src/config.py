"""
config.py - Centralised configuration for the HITL Payment Automation.

Loads environment variables via python-dotenv and exposes typed constants
used across every other module.  All settings can be overridden by the
corresponding environment variable in `.env`.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# ── Load .env from project root ─────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_env_path = PROJECT_ROOT / ".env"
load_dotenv(_env_path)


GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default


def _get_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value not in (None, "") else default


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_origins() -> list[str]:
    raw = os.getenv("CORS_ALLOW_ORIGINS", "*")
    origins = [item.strip() for item in raw.split(",") if item.strip()]
    return origins or ["*"]


def _resolve_project_path(raw_path: str) -> str:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return str(candidate)
    return str(PROJECT_ROOT / candidate)


# ── Google Sheets ────────────────────────────────────────────────────
SPREADSHEET_ID: str = os.getenv("SPREADSHEET_ID", "")
SHEET_NAME: str = os.getenv("SHEET_NAME", "Sheet1")
SHEETS_API_KEY: str = os.getenv("SHEETS_API_KEY", "")

# ── Service Account ─────────────────────────────────────────────────
SERVICE_ACCOUNT_PATH: str = _resolve_project_path(
    os.getenv("SERVICE_ACCOUNT_PATH", "service_account.json")
)

# ── Webhook ──────────────────────────────────────────────────────────
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")

# ── Server ───────────────────────────────────────────────────────────
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "8000"))
CORS_ALLOW_ORIGINS: list[str] = _get_origins()
ALLOW_CREDENTIALS: bool = _get_bool("ALLOW_CREDENTIALS", False)

# ── Persistence & Queueing ──────────────────────────────────────────
SESSION_DB_PATH: str = _resolve_project_path(os.getenv("SESSION_DB_PATH", "hitl_sessions.db"))
SESSION_RETENTION_HOURS: int = _get_int("SESSION_RETENTION_HOURS", 168)
SESSION_CLEANUP_INTERVAL_SECONDS: int = _get_int("SESSION_CLEANUP_INTERVAL_SECONDS", 1800)
SESSION_LIST_MAX_LIMIT: int = _get_int("SESSION_LIST_MAX_LIMIT", 500)
REVIEW_WORKER_COUNT: int = _get_int("REVIEW_WORKER_COUNT", 4)
REVIEW_WORKER_IDLE_SLEEP_SECONDS: float = _get_float("REVIEW_WORKER_IDLE_SLEEP_SECONDS", 0.5)
REVIEW_JOB_VISIBILITY_TIMEOUT_SECONDS: int = _get_int("REVIEW_JOB_VISIBILITY_TIMEOUT_SECONDS", 300)
REQUIRE_SERVICE_ACCOUNT: bool = _get_bool("REQUIRE_SERVICE_ACCOUNT", True)

# ── Sheets API Tuning ───────────────────────────────────────────────
SHEETS_API_CONCURRENT_LIMIT: int = _get_int("SHEETS_API_CONCURRENT_LIMIT", 5)
SHEETS_CLIENT_TTL_SECONDS: int = _get_int("SHEETS_CLIENT_TTL_SECONDS", 300)

# ── Reconciliation ──────────────────────────────────────────────────
RECONCILIATION_COOLDOWN_SECONDS: int = _get_int("RECONCILIATION_COOLDOWN_SECONDS", 15)

# ── Application constants ───────────────────────────────────────────
APP_NAME: str = "hitl_payment_automation"
USER_ID: str = "system"  # single-user PoC


# ── Logging ──────────────────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s"
LOG_LEVEL = logging.DEBUG if os.getenv("DEBUG") else logging.INFO


def setup_logging() -> None:
    """Configure structured console logging once at startup."""
    logging.basicConfig(
        level=LOG_LEVEL,
        format=LOG_FORMAT,
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    # Silence noisy third-party loggers
    for noisy in (
        "httpcore", "httpx", "urllib3",
        "googleapiclient.discovery_cache",
        "googleapiclient.discovery",
        "google.auth.transport.requests",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ── Startup validation ──────────────────────────────────────────────
def validate() -> None:
    """Fail fast with a clear message if critical env vars are missing."""
    missing: list[str] = []
    if not SPREADSHEET_ID:
        missing.append("SPREADSHEET_ID")
    if missing:
        sys.exit(
            f"Missing required environment variables: {', '.join(missing)}\n"
            f"Copy .env.example to .env and fill in the values."
        )

    sa_path = Path(SERVICE_ACCOUNT_PATH)
    if REQUIRE_SERVICE_ACCOUNT and not sa_path.exists():
        sys.exit(
            f"Service account file '{SERVICE_ACCOUNT_PATH}' not found.\n"
            f"Production writes require a valid Google service account key."
        )

    if not sa_path.exists():
        logging.getLogger(__name__).warning(
            "Service account file '%s' not found. "
            "Sheets API will use API-key auth (read-only). "
            "Set SERVICE_ACCOUNT_PATH in .env to fix.",
            SERVICE_ACCOUNT_PATH,
        )

    if REVIEW_WORKER_COUNT < 1:
        sys.exit("REVIEW_WORKER_COUNT must be at least 1.")

    if SESSION_RETENTION_HOURS < 1:
        sys.exit("SESSION_RETENTION_HOURS must be at least 1.")

    if ALLOW_CREDENTIALS and CORS_ALLOW_ORIGINS == ["*"]:
        sys.exit(
            "ALLOW_CREDENTIALS=true cannot be used with CORS_ALLOW_ORIGINS='*'."
        )
