"""
main.py — FastAPI server with webhook endpoint and dev utilities.

Run with:
    python main.py
or:
    uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks
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
    row_data: list[Any] = Field(default_factory=list, description="Array of columns A to J")


class WithdrawalRequest(BaseModel):
    """Dev/test endpoint payload to trigger a new withdrawal."""

    session_id: str
    player_id: str


class AdaWithdrawalRequest(BaseModel):
    """Payload sent by ADA Chatbot to trigger a withdrawal check."""

    player_id: str
    player_name: str = ""
    channel: Literal["Chat", "Email"] = "Chat"


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
        payload.notes,
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
            # We add dummy name/channel for the test endpoint
            player_name="Test-User",
            channel="Chat",
        )
        return result
    except Exception as exc:
        logger.exception("Error starting withdrawal %s", req.session_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/ada/v1/request_review")
async def ada_request_review(req: AdaWithdrawalRequest, background_tasks: BackgroundTasks):
    """
    ADA Chatbot endpoint. ADA usually supplies only the player ID.
    Using BackgroundTasks to ensure API responds < 15s to satisfy Ada webhook requirements.
    """
    logger.info("🤖 ADA Request via Chatbot: player=%s", req.player_id)

    # Note: We've relaxed the idempotency check to support the user's "Delete & Retry" testing flow.
    # If a duplicate request for the same player arrived, we proceed with a new session ID
    existing_status = agent_module.player_status.get(req.player_id)
    if existing_status and existing_status.get("decision") == "pending":
        logger.info("⏳ Withdrawal already pending for player=%s, but starting new session for tester retry", req.player_id)

    session_id = f"ada-{uuid.uuid4().hex}"
    logger.info("   ... Generated new session=%s", session_id)
    
    # Initialize the status synchronously so that polling works immediately
    agent_module.player_status[req.player_id] = {
        "decision": "pending",
        "notes": "",
        "row_data": []
    }

    async def bg_task(pid: str = req.player_id, pname: str = req.player_name, pchannel: str = req.channel, base_sid: str = session_id):
        max_attempts = 3
        for attempt in range(max_attempts):
            # Append attempt number to avoid "session already exists" errors on retry
            current_sid = f"{base_sid}-{attempt}" if attempt > 0 else base_sid
            
            try:
                result = await agent_module.start_withdrawal(
                    session_id=current_sid,
                    player_id=pid,
                    player_name=pname,
                    channel=pchannel,
                )

                # Check if the agent actually succeeded
                status = result.get("status", "")
                if status == "pending_human_review":
                    logger.info("✅ Agent pending human review for player=%s (attempt %d)", pid, attempt + 1)
                    return  # Success, exit the background task
                elif status == "completed_unexpected":
                    logger.error("❌ Agent did not escalate to HITL for player=%s", pid)
                    agent_module.player_status[pid] = {
                        "decision": "error",
                        "notes": "Agent failed to escalate withdrawal to human review.",
                        "row_data": []
                    }
                    return
                else:
                    logger.error("❌ Unexpected result status '%s' for player=%s", status, pid)
                    agent_module.player_status[pid] = {
                        "decision": "error",
                        "notes": f"Unexpected status: {status}",
                        "row_data": []
                    }
                    return

            except Exception as exc:
                if attempt < max_attempts - 1:
                    logger.warning("⚠️ Transient error for ADA withdrawal %s (attempt %d). Retrying...: %s", current_sid, attempt + 1, str(exc))
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
                else:
                    logger.exception("🚨 Error starting ADA withdrawal %s after %d attempts", current_sid, max_attempts)
                    agent_module.player_status[pid] = {
                        "decision": "error",
                        "notes": f"Internal server error: {exc}",
                        "row_data": []
                    }

    # Offload the LLM call to async background task
    background_tasks.add_task(bg_task)

    return {
        "status": "pending_human_review",
        "session_id": session_id,
    }


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
