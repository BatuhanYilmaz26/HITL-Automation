"""
tools.py — ADK tool definitions for the HITL payment workflow.

Provides one tool the agent calls for EVERY withdrawal:
  request_human_approval → LongRunningFnTool (pauses agent, writes Sheet row)

All withdrawals are escalated to a human reviewer via Google Sheets.
There is no auto-approve path.
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.tools import LongRunningFunctionTool

import sheets_service

logger = logging.getLogger(__name__)


# ─── Long-Running Tool: Request Human Approval ─────────────────────


def request_human_approval(
    session_id: str,
    player_id: str,
) -> dict[str, Any]:
    """
    Escalate a withdrawal for human review.

    Writes a row to the HITL Google Sheet dashboard and returns a
    pending status.  The ADK framework will pause the agent run after
    this tool returns.  The agent resumes when the webhook sends the
    human decision back as a FunctionResponse.

    This tool is called for EVERY withdrawal — all withdrawals require
    human verification before processing.

    Args:
        session_id: Unique session identifier for this withdrawal.
        player_id:  The player requesting the withdrawal.

    Returns:
        A dict with status="pending" so ADK knows to pause.
    """
    logger.info(
        "🔒 HITL escalation: session=%s player=%s",
        session_id, player_id,
    )

    # Write the review row to Google Sheets
    try:
        sheets_service.append_review_row(session_id, player_id)
    except Exception as exc:
        logger.error("❌ Sheet write failed for session=%s player=%s: %s", session_id, player_id, exc)
        return {
            "status": "error",
            "message": f"Failed to write review row for player {player_id}: {exc}",
            "session_id": session_id,
            "player_id": player_id,
        }

    return {
        "status": "pending",
        "message": f"Withdrawal for player {player_id} "
                   f"has been submitted for human review.",
        "session_id": session_id,
        "player_id": player_id,
    }


# ─── Wrapped tool instance (import this into agent.py) ──────────────

# LongRunningFunctionTool — ADK pauses the run after this tool returns
human_approval_tool = LongRunningFunctionTool(func=request_human_approval)
