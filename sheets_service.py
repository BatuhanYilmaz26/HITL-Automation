"""
sheets_service.py — Google Sheets API wrapper (singleton).

Provides a reusable Sheets service object and a helper to append
a new HITL review row to the configured spreadsheet.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from googleapiclient.discovery import build

import config

logger = logging.getLogger(__name__)

# ── Singleton service ────────────────────────────────────────────────
_service = None


import os
from google.oauth2.service_account import Credentials

def get_sheets_service():
    """Return a cached Google Sheets API v4 service object."""
    global _service
    if _service is None:
        logger.info("Initialising Google Sheets API client …")
        
        if os.path.exists("service_account.json"):
            creds = Credentials.from_service_account_file(
                "service_account.json", 
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
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


# ── Public helpers ───────────────────────────────────────────────────


def append_review_row(
    session_id: str,
    player_id: str,
) -> dict[str, Any]:
    """
    Find the next available row in Column C (starting from row 5 due to headers)
    and update it with the Session ID and Player ID, leaving B blank for Apps Script.

    Returns the Sheets API response dict.
    """
    service = get_sheets_service()

    try:
        # Fetch existing Player IDs to find the first empty row
        col_c_data = service.spreadsheets().values().get(
            spreadsheetId=config.SPREADSHEET_ID,
            range=f"{config.SHEET_NAME}!C5:C"
        ).execute()

        values = col_c_data.get("values", [])
        offset = 0
        for row in values:
            if not row or not row[0].strip():
                break
            offset += 1

        next_row = 5 + offset
    except Exception as exc:
        logger.exception("Failed to fetch Column C to find empty row.")
        raise

    range_notation = f"{config.SHEET_NAME}!A{next_row}:C{next_row}"
    
    row_data = [
        session_id,      # A — Session ID (hidden/ignored by humans)
        "",              # B — Timestamp (filled by Apps Script)
        player_id,       # C — Player ID
    ]

    try:
        result = service.spreadsheets().values().update(
            spreadsheetId=config.SPREADSHEET_ID,
            range=range_notation,
            valueInputOption="USER_ENTERED",
            body={"values": [row_data]},
        ).execute()
        
        logger.info("✅ Sheet row updated at row %d (session=%s)", next_row, session_id)
        return result
    except Exception:
        logger.exception("❌ Failed to update row %d in Google Sheet", next_row)
        raise
