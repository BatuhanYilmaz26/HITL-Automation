"""
config.py — Centralised configuration for the HITL Payment Automation.

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
_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_path)


GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")


# ── Google Sheets ────────────────────────────────────────────────────
SPREADSHEET_ID: str = os.getenv("SPREADSHEET_ID", "")
SHEET_NAME: str = os.getenv("SHEET_NAME", "Sheet1")
SHEETS_API_KEY: str = os.getenv("SHEETS_API_KEY", "")

# ── Service Account ─────────────────────────────────────────────────
SERVICE_ACCOUNT_PATH: str = os.getenv("SERVICE_ACCOUNT_PATH", "service_account.json")

# ── Webhook ──────────────────────────────────────────────────────────
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")

# ── Server ───────────────────────────────────────────────────────────
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "8000"))

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
    if not GOOGLE_API_KEY:
        missing.append("GOOGLE_API_KEY")
    if not SPREADSHEET_ID:
        missing.append("SPREADSHEET_ID")
    if missing:
        sys.exit(
            f"❌  Missing required environment variables: {', '.join(missing)}\n"
            f"   Copy .env.example → .env and fill in the values."
        )

    # Warn (don't crash) if the service account file is missing — API-key
    # fallback still works for read-only access.
    sa_path = Path(SERVICE_ACCOUNT_PATH)
    if not sa_path.exists():
        logging.getLogger(__name__).warning(
            "⚠️  Service account file '%s' not found. "
            "Sheets API will use API-key auth (read-only). "
            "Set SERVICE_ACCOUNT_PATH in .env to fix.",
            SERVICE_ACCOUNT_PATH,
        )
