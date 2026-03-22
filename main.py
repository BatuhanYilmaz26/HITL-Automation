"""
main.py — FastAPI server with webhook endpoint and dev utilities.

Run with:
    python main.py
or:
    uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field

import config
import agent as agent_module

logger = logging.getLogger(__name__)


# ── Pydantic models ──────────────────────────────────────────────────


class WebhookPayload(BaseModel):
    """Payload sent by Google Apps Script when a human edits the Sheet."""

    session_id: str = Field(..., description="ADK session ID from Column A")
    decision: str = Field(..., description="'Yes' or 'No' from Column I")
    notes: str = Field("", description="Context/notes from Column J")
    row_data: list = Field(default_factory=list, description="Array of columns A to J")


class WithdrawalRequest(BaseModel):
    """Dev/test endpoint payload to trigger a new withdrawal."""

    session_id: str
    player_id: str


class AdaWithdrawalRequest(BaseModel):
    """Payload sent by ADA Chatbot to trigger a withdrawal check."""

    player_id: str


# ── Lifespan ─────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks."""
    config.setup_logging()
    config.validate()

    # Set the Gemini API key in the environment for google-genai SDK
    os.environ["GOOGLE_API_KEY"] = config.GOOGLE_API_KEY

    logger.info("🚀 HITL Payment Automation server starting …")
    logger.info("   Model   : %s", config.MODEL_ID)
    logger.info("   Sheet   : %s", config.SPREADSHEET_ID[:12] + "…")
    logger.info("   Mode    : ALL withdrawals require human approval")
    yield
    logger.info("👋 Server shutting down")


# ── FastAPI app ──────────────────────────────────────────────────────

app = FastAPI(
    title="HITL Payment Automation",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Endpoints ────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    """Health-check endpoint."""
    return {
        "status": "ok",
        "pending_sessions": len(agent_module.pending_sessions),
    }


@app.get("/sessions")
async def list_sessions():
    """List all pending HITL sessions (for debugging)."""
    return {
        "pending_count": len(agent_module.pending_sessions),
        "session_ids": list(agent_module.pending_sessions.keys()),
    }


@app.post("/webhook")
async def webhook(
    payload: WebhookPayload,
    x_webhook_secret: str | None = Header(None),
):
    """
    Receive the human decision from Google Apps Script.

    Apps Script sends:
      { "session_id": "...", "decision": "Yes|No", "notes": "..." }

    This endpoint resumes the paused ADK agent with that decision.
    """
    # Optional shared-secret validation
    if config.WEBHOOK_SECRET and x_webhook_secret != config.WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    logger.info(
        "📩 Webhook received: session=%s decision=%s notes=%s",
        payload.session_id,
        payload.decision,
        payload.notes[:80],
    )

    if payload.session_id not in agent_module.pending_sessions:
        # If the human agent corrects a typo after it was already approved,
        # we can forcefully update the ADA chatbot polling dictionary directly
        # by extracting the Player ID (Column C, index 2).
        if len(payload.row_data) > 2:
            player_id = payload.row_data[2]
            if player_id:
                agent_module.player_status[player_id] = {
                    "decision": payload.decision,
                    "notes": payload.notes,
                    "row_data": payload.row_data,
                }
                logger.info("📝 Applied human correction to already-finalized session %s (player=%s)", payload.session_id, player_id)
                return {"status": "corrected", "message": f"Updated existing record for {player_id}"}
                
        raise HTTPException(
            status_code=404,
            detail=f"No pending session found for session_id={payload.session_id}",
        )

    try:
        result = await agent_module.resume_withdrawal(
            session_id=payload.session_id,
            decision=payload.decision,
            notes=payload.notes,
            row_data=payload.row_data,
        )
        return result
    except Exception as exc:
        logger.exception("Error resuming session %s", payload.session_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/test/withdrawal")
async def test_withdrawal(req: WithdrawalRequest):
    """
    Dev-only endpoint — trigger a new withdrawal flow via HTTP.

    Useful for testing without an external caller.
    """
    logger.info(
        "🧪 Test withdrawal: session=%s player=%s",
        req.session_id,
        req.player_id,
    )

    try:
        result = await agent_module.start_withdrawal(
            session_id=req.session_id,
            player_id=req.player_id,
        )
        return result
    except Exception as exc:
        logger.exception("Error starting withdrawal %s", req.session_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/ada/v1/request_review")
async def ada_request_review(req: AdaWithdrawalRequest):
    """
    ADA Chatbot endpoint. ADA usually supplies only the player ID.
    """
    session_id = f"ada-{uuid.uuid4().hex[:8]}"
    logger.info("🤖 ADA Request via Chatbot: player=%s (generated session=%s)", req.player_id, session_id)
    
    try:
        result = await agent_module.start_withdrawal(
            session_id=session_id,
            player_id=req.player_id,
        )

        # Check if the agent actually succeeded
        status = result.get("status", "")
        if status == "pending_human_review":
            return {
                "status": "pending_human_review",
                "session_id": result.get("session_id", session_id),
            }
        elif status == "completed_unexpected":
            logger.error("Agent did not escalate to HITL for player=%s", req.player_id)
            raise HTTPException(
                status_code=500,
                detail=f"Agent failed to escalate withdrawal for {req.player_id} to human review.",
            )
        else:
            logger.error("Unexpected result status '%s' for player=%s", status, req.player_id)
            raise HTTPException(status_code=500, detail=f"Unexpected status: {status}")

    except HTTPException:
        raise  # Re-raise our own HTTPExceptions
    except Exception as exc:
        logger.exception("Error starting ADA withdrawal %s", session_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/ada/v1/status/{player_id}")
async def ada_check_status(player_id: str):
    """
    ADA Chatbot endpoint to poll for the human decision.
    Returns: 'pending', 'Yes', 'No', or 'not_found' and any notes
    """
    status_data = agent_module.player_status.get(player_id, {"decision": "not_found", "notes": ""})
    return {
        "player_id": player_id,
        "decision": status_data["decision"],
        "notes": status_data["notes"],
        "row_data": status_data.get("row_data", []),
    }


# ── Entry-point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=config.HOST,
        port=config.PORT,
        reload=False,
        log_level="info",
    )
