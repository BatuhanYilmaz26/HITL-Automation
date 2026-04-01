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

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config

logger = logging.getLogger(__name__)

# ── Thread-safe singleton service ────────────────────────────────────
_service = None
_service_lock = threading.Lock()


def get_sheets_service():
    """Return a cached Google Sheets API v4 service object (thread-safe)."""
    global _service
    if _service is not None:
        return _service

    with _service_lock:
        # Double-check after acquiring lock (another thread may have initialised)
        if _service is not None:
            return _service

        logger.info("Initialising Google Sheets API client …")

        sa_path = config.SERVICE_ACCOUNT_PATH
        if os.path.exists(sa_path):
            creds = Credentials.from_service_account_file(
                sa_path,
                scopes=["https://www.googleapis.com/auth/spreadsheets"],
            )
            svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
            logger.info("✅ Using service account '%s' for Sheets API authentication.", sa_path)
        else:
            svc = build(
                "sheets",
                "v4",
                developerKey=config.SHEETS_API_KEY or config.GOOGLE_API_KEY,
                cache_discovery=False,
            )
            logger.warning(
                "⚠️  No service account file at '%s'. Using API Key (may fail on writes).",
                sa_path,
            )
        _service = svc
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
            elif status == 403:
                logger.error(
                    "🔒 %s returned HTTP 403 — Permission denied. "
                    "Ensure the service account email has Editor access to the spreadsheet.",
                    description,
                )
                raise
            elif status == 404:
                logger.error(
                    "🔍 %s returned HTTP 404 — Spreadsheet or sheet tab not found. "
                    "Check SPREADSHEET_ID='%s' and SHEET_NAME='%s' in .env.",
                    description, config.SPREADSHEET_ID, config.SHEET_NAME,
                )
                raise
            else:
                raise
    raise RuntimeError(f"{description} failed after {MAX_RETRIES} retries")


# ── Public helpers ───────────────────────────────────────────────────

# Global lock to prevent race conditions when appending to Google Sheets.
# BackgroundTasks run in concurrent threads, so threading.Lock safely synchronizes writes.
_append_lock = threading.Lock()


def append_review_row(
    session_id: str,
    player_id: str,
    player_name: str = "",
    channel: str = "Chat",
) -> dict[str, Any]:
    """
    Find the next available row and update it with Session ID, Player ID, Name, and Channel.

    Column layout:
        A — Empty (legacy alignment)
        B — Timestamp (filled by Apps Script onChange trigger)
        C — Player ID
        D — Player Name
        E — Channel
        F-J — Reserved (Agent, Decision, Notes, etc.)
        K — Session ID (hidden reference column)

    Returns:
        dict with ``result`` (Sheets API response) and ``row_number`` (int).

    Raises:
        HttpError: On unrecoverable Sheets API failure.
    """
    service = get_sheets_service()

    with _append_lock:
        try:
            # Fetch entire range from A5:K to see if ANY column has data
            all_cols_data = _retry_api_call(
                lambda: service.spreadsheets().values().get(
                    spreadsheetId=config.SPREADSHEET_ID,
                    range=f"{config.SHEET_NAME}!A5:K",
                ).execute(),
                description="Fetch range A5:K",
            )

            values = all_cols_data.get("values", [])

            # Find the absolute last row that has ANY data in columns A-K
            last_filled_index = -1
            for i, row in enumerate(values):
                if any(cell.strip() for cell in row if isinstance(cell, str)):
                    last_filled_index = i

            next_row = 5 + last_filled_index + 1
        except Exception:
            logger.exception("Failed to fetch sheet data while finding next empty row.")
            raise

        range_notation = f"{config.SHEET_NAME}!A{next_row}:K{next_row}"

        # Mapping for the HITL dashboard
        row_data = [
            "",          # A — Empty
            "",          # B — Timestamp (filled by Apps Script)
            player_id,   # C — Player ID
            player_name, # D — Player Name
            channel,     # E — Channel
            "", "", "", "", "", # F through J (Empty)
            session_id,  # K — Session ID
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

            logger.info("✅ Sheet row updated at row %d (session=%s, player=%s)", next_row, session_id, player_id)
            return {"result": result, "row_number": next_row}
        except Exception:
            logger.exception("❌ Failed to update row %d in Google Sheet (session=%s)", next_row, session_id)
            raise
