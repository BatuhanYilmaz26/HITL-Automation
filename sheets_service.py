"""
sheets_service.py — Google Sheets API wrapper (singleton).

Provides a reusable Sheets service object and a helper to append
a new HITL review row to the configured spreadsheet.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from google.auth.credentials import Credentials as BaseCredentials
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config

logger = logging.getLogger(__name__)

# ── Singleton service ────────────────────────────────────────────────
_service = None


def get_sheets_service():
    """Return a cached Google Sheets API v4 service object."""
    global _service
    if _service is None:
        logger.info("Initialising Google Sheets API client …")

        if os.path.exists("service_account.json"):
            creds = Credentials.from_service_account_file(
                "service_account.json",
                scopes=["https://www.googleapis.com/auth/spreadsheets"],
            )
            _service = build("sheets", "v4", credentials=creds, cache_discovery=False)
            logger.info("Using service_account.json for Sheets API authentication.")
        else:
            _service = build(
                "sheets",
                "v4",
                developerKey=config.SHEETS_API_KEY or config.GOOGLE_API_KEY,
                cache_discovery=False,
            )
            logger.warning("No service_account.json found. Using API Key (may fail on writes).")
    return _service


# ── Retry helper ─────────────────────────────────────────────────────

MAX_RETRIES = 5
INITIAL_BACKOFF = 1.0  # seconds


def _retry_api_call(fn, *, description: str = "API call"):
    """
    Execute *fn()* with exponential backoff on 429/5xx errors.

    Returns the result of fn() on success, raises on exhaustion.
    """
    backoff = INITIAL_BACKOFF
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn()
        except HttpError as exc:
            status = exc.resp.status if exc.resp else 0
            if status in (429, 500, 502, 503) and attempt < MAX_RETRIES:
                logger.warning(
                    "⏳ %s failed (HTTP %d), retrying in %.1fs (attempt %d/%d)",
                    description, status, backoff, attempt, MAX_RETRIES,
                )
                time.sleep(backoff)
                backoff *= 2
            else:
                raise
    raise RuntimeError(f"{description} failed after {MAX_RETRIES} retries")


# ── Public helpers ───────────────────────────────────────────────────

# Global lock to prevent race conditions when appending to Google Sheets.
# Tool functions run in threads (via ADK), so threading.Lock is correct.
_append_lock = threading.Lock()


def append_review_row(
    session_id: str,
    player_id: str,
) -> dict[str, Any]:
    """
    Find the next available row in Column C (starting from row 5 due to headers)
    and update it with the Session ID and Player ID, leaving B blank for Apps Script.

    Uses a thread lock to ensure concurrent requests don't overwrite the same row,
    and retries with exponential backoff for transient Sheets API errors.
    """
    service = get_sheets_service()

    with _append_lock:
        try:
            col_c_data = _retry_api_call(
                lambda: service.spreadsheets().values().get(
                    spreadsheetId=config.SPREADSHEET_ID,
                    range=f"{config.SHEET_NAME}!C5:C",
                ).execute(),
                description="Fetch Column C",
            )

            values = col_c_data.get("values", [])

            # Find the first truly empty row after ALL filled rows.
            # We scan the entire column and take the position after the
            # last non-empty cell, so gaps in the middle are skipped.
            last_filled_index = -1
            for i, row in enumerate(values):
                if row and row[0].strip():
                    last_filled_index = i

            next_row = 5 + last_filled_index + 1
        except Exception:
            logger.exception("Failed to fetch Column C to find empty row.")
            raise

        range_notation = f"{config.SHEET_NAME}!A{next_row}:C{next_row}"

        row_data = [
            session_id,  # A — Session ID
            "",          # B — Timestamp (filled by Apps Script)
            player_id,   # C — Player ID
        ]

        try:
            result = _retry_api_call(
                lambda: service.spreadsheets().values().update(
                    spreadsheetId=config.SPREADSHEET_ID,
                    range=range_notation,
                    valueInputOption="USER_ENTERED",
                    body={"values": [row_data]},
                ).execute(),
                description=f"Update row {next_row}",
            )

            logger.info("✅ Sheet row updated at row %d (session=%s)", next_row, session_id)
            return result
        except Exception:
            logger.exception("❌ Failed to update row %d in Google Sheet", next_row)
            raise
