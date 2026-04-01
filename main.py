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
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import config
import sheets_service

logger = logging.getLogger(__name__)

# ── Runtime metrics ──────────────────────────────────────────────────
_startup_time: float = 0.0
_metrics = {
    "requests_total": 0,
    "requests_succeeded": 0,
    "requests_failed": 0,
    "webhooks_received": 0,
    "webhooks_corrections": 0,
}

# ── In-Memory State ──────────────────────────────────────────────────
# Tracks the state of all sessions. Format:
# session_id: {
#     "status": "processing" | "pending_human_review" | "approved" | "rejected" | "error",
#     "player_id": str,
#     "row_number": int | None,
#     "decision": str | None,
#     "notes": str | None,
#     "row_data": list | None,
# }
session_status: dict[str, dict[str, Any]] = {}


# ── Pydantic models ──────────────────────────────────────────────────

class WebhookPayload(BaseModel):
    session_id: str = Field(..., description="Session ID from Column K")
    decision: str = Field(..., description="'Yes' or 'No' from Column I")
    notes: str = Field("", description="Context/notes from Column J")
    row_number: int | None = Field(None, description="The specific row being edited")
    row_data: list[Any] = Field(default_factory=list, description="Array of columns A to J")

class WithdrawalRequest(BaseModel):
    session_id: str
    player_id: str

class AdaWithdrawalRequest(BaseModel):
    playerUid: str | None = None
    player_id: str | None = None
    player_name: str | None = ""
    channel: str | None = "Chat"


# ── Lifespan ─────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _startup_time
    _startup_time = time.monotonic()
    config.setup_logging()
    config.validate()
    logger.info("🚀 Asynchronous HITL Payment Automation starting …")
    logger.info("   Sheet   : %s", config.SPREADSHEET_ID[:12] + "…")
    logger.info("   Mode    : Pure webhook router (No AI)")
    yield
    logger.info("👋 Server shutting down")

# ── FastAPI app ──────────────────────────────────────────────────────

app = FastAPI(
    title="HITL Payment Automation",
    description="Zero-latency async webhook router for Google Sheets Human-in-the-Loop workflows.",
    version="3.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Background Tasks logic ──────────────────────────────────────────

def process_withdrawal_background(session_id: str, player_id: str, player_name: str, channel: str):
    """
    Synchronously appends the row to Google Sheets via thread lock, but
    runs in the background to prevent blocking the HTTP response.
    """
    try:
        logger.info("⚙️  Background Task started for session=%s", session_id)
        sheet_res = sheets_service.append_review_row(session_id, player_id, player_name, channel)
        row_number = sheet_res["row_number"]
        
        session_status[session_id].update({
            "status": "pending_human_review",
            "row_number": row_number
        })
        logger.info("✅ Background Task finished for session=%s (row=%d)", session_id, row_number)
    except Exception as exc:
        logger.exception("❌ Background Task failed for session=%s", session_id)
        session_status[session_id]["status"] = "error"
        session_status[session_id]["notes"] = str(exc)

# ── Endpoints ────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    uptime_seconds = time.monotonic() - _startup_time if _startup_time else 0
    return {
        "status": "ok",
        "uptime_seconds": round(uptime_seconds, 1),
        "tracked_sessions": len(session_status),
    }

@app.get("/metrics")
async def metrics():
    uptime_seconds = time.monotonic() - _startup_time if _startup_time else 0
    return {
        "uptime_seconds": round(uptime_seconds, 1),
        "tracked_sessions": len(session_status),
        **_metrics,
    }

@app.get("/sessions")
async def list_sessions():
    return {
        "count": len(session_status),
        "sessions": session_status,
    }

@app.post("/webhook")
async def webhook(payload: WebhookPayload, x_webhook_secret: str | None = Header(None)):
    """Receives the human decision from Google Apps Script."""
    _metrics["webhooks_received"] += 1
    if config.WEBHOOK_SECRET and x_webhook_secret != config.WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    logger.info("📩 Webhook received: session=%s decision=%s notes=%s", 
                payload.session_id, payload.decision, payload.notes)

    if payload.session_id not in session_status:
        # Create it if it doesn't exist (handle restarts)
        session_status[payload.session_id] = {
            "status": "pending_human_review",
            "player_id": payload.row_data[2] if len(payload.row_data) > 2 else "Unknown",
            "row_number": payload.row_number,
        }
        
    status = "approved" if payload.decision.strip().lower() == "yes" else "rejected"
    
    session_status[payload.session_id].update({
        "status": status,
        "decision": payload.decision,
        "notes": payload.notes,
        "row_data": payload.row_data,
    })

    _metrics["requests_succeeded"] += 1
    return {"status": "finalized", "message": "Updated session status."}

@app.post("/test/withdrawal")
async def test_withdrawal(req: WithdrawalRequest, background_tasks: BackgroundTasks):
    _metrics["requests_total"] += 1
    session_id = req.session_id
    
    session_status[session_id] = {
        "status": "processing",
        "player_id": req.player_id,
        "row_number": None,
        "decision": None,
        "notes": "",
    }
    
    background_tasks.add_task(process_withdrawal_background, session_id, req.player_id, "Test-User", "Test")
    return {"status": "processing", "session_id": session_id}

@app.post("/hitl/v1/request_review")
async def ada_request_review(req: AdaWithdrawalRequest, background_tasks: BackgroundTasks):
    """
    ADA Chatbot endpoint. Instantly returns 'processing' and specific session_id.
    """
    _metrics["requests_total"] += 1
    actual_player_id = req.playerUid or req.player_id
    
    if not actual_player_id:
        raise HTTPException(status_code=422, detail="Missing player_id or playerUid")

    # Generate a chronologically sortable, short session ID. Example: W-260401-185807-a3f2
    now_str = time.strftime("%y%m%d-%H%M%S")
    short_uuid = uuid.uuid4().hex[:4]
    session_id = f"W-{now_str}-{short_uuid}"
    
    logger.info("🤖 ADA Request via Chatbot: player=%s", actual_player_id)
    logger.info("   … Generated new sequence session=%s", session_id)
    
    session_status[session_id] = {
        "status": "processing",
        "player_id": actual_player_id,
        "row_number": None,
        "decision": None,
        "notes": "",
    }

    background_tasks.add_task(
        process_withdrawal_background, 
        session_id=session_id, 
        player_id=actual_player_id, 
        player_name=req.player_name or "", 
        channel=req.channel or "Chat"
    )

    return {
        "status": "processing",
        "session_id": session_id,
        "message": "Enqueued directly to Google Sheets layer."
    }

@app.get("/hitl/v1/status/session/{session_id}")
async def ada_check_status_session(session_id: str):
    """New endpoint: Check status using session_id."""
    if session_id not in session_status:
        return {"status": "not_found", "message": "Unknown or expired session ID."}
    return session_status[session_id]

# Legacy compat if they still use it temporarily, though it will just fail as row_number isn't known ahead of time
@app.get("/hitl/v1/status/{player_id}/{row_number}")
async def ada_check_status_legacy(player_id: str, row_number: int):
    # Try to find a matching session
    for sid, data in session_status.items():
        if data.get("player_id") == player_id and data.get("row_number") == row_number:
            return data
    return {"status": "not_found", "decision": "not_found", "notes": ""}

if __name__ == "__main__":
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=False, log_level="info")
