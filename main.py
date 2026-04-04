"""
main.py - FastAPI server with durable HITL queue workers and webhook endpoints.

Run with:
    python main.py
or:
    uvicorn main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import config
import session_store
import sheets_service

logger = logging.getLogger(__name__)

# ── Runtime metrics ──────────────────────────────────────────────────
_startup_time: float = 0.0
_worker_tasks: list[asyncio.Task[Any]] = []
_metrics_lock = threading.Lock()
_metrics = {
    "requests_total": 0,
    "requests_succeeded": 0,
    "requests_failed": 0,
    "review_jobs_completed": 0,
    "review_jobs_failed": 0,
    "webhooks_received": 0,
    "webhooks_corrections": 0,
    "sessions_expired": 0,
}


def _increment_metric(name: str, delta: int = 1) -> None:
    with _metrics_lock:
        _metrics[name] = _metrics.get(name, 0) + delta


def _metrics_snapshot() -> dict[str, int]:
    with _metrics_lock:
        return dict(_metrics)


def _normalise_player_id(player_id: str | None) -> str:
    return (player_id or "").strip()


def _status_from_decision(decision: str) -> str:
    if decision.strip().lower() in {"yes", "y", "approved", "approve"}:
        return "approved"
    return "rejected"


def _safe_row_value(row_data: list[Any], index: int, default: str = "") -> str:
    if index >= len(row_data):
        return default
    value = row_data[index]
    return str(value).strip() if value is not None else default


def _extract_player_fields(row_data: list[Any]) -> tuple[str, str, str]:
    if len(row_data) >= 10:
        # Legacy payload shape from Apps Script captured columns A:J.
        return (
            _safe_row_value(row_data, 2, "Unknown"),
            _safe_row_value(row_data, 3, ""),
            _safe_row_value(row_data, 4, "Chat"),
        )

    # Canonical payload shape captures operational columns B:J only.
    return (
        _safe_row_value(row_data, 1, "Unknown"),
        _safe_row_value(row_data, 2, ""),
        _safe_row_value(row_data, 3, "Chat"),
    )


# ── Reconciliation throttle ──────────────────────────────────────────────
_reconciliation_timestamps: dict[str, float] = {}
_reconciliation_lock = threading.Lock()


def _needs_sheet_reconciliation(session: dict[str, Any]) -> bool:
    """Check if a pending session should be reconciled from Google Sheets.

    Applies a per-session cooldown to avoid excessive Sheets API reads
    when ADA polls at high frequency across thousands of sessions.
    """
    if not session.get("row_number") or session.get("status") != "pending_human_review":
        return False

    session_id = session["session_id"]
    now = time.monotonic()

    with _reconciliation_lock:
        last_check = _reconciliation_timestamps.get(session_id, 0.0)
        if (now - last_check) < config.RECONCILIATION_COOLDOWN_SECONDS:
            return False
        _reconciliation_timestamps[session_id] = now

    return True


def _reconcile_session_from_sheet(session: dict[str, Any]) -> dict[str, Any]:
    row_number = session.get("row_number")
    if not row_number:
        return session

    row_snapshot = sheets_service.get_review_row(int(row_number))
    if row_snapshot is None:
        return session

    sheet_session_id = row_snapshot.get("session_id", "")
    if sheet_session_id and sheet_session_id != session["session_id"]:
        logger.warning(
            "Skipped sheet reconciliation for session=%s because row %s belongs to session=%s",
            session["session_id"],
            row_number,
            sheet_session_id,
        )
        return session

    row_data = row_snapshot["row_data"]
    decision = row_snapshot["decision"]
    notes = row_snapshot["notes"]
    player_id, player_name, channel = _extract_player_fields(row_data)

    update_fields: dict[str, Any] = {
        "row_data": row_data,
        "player_id": player_id,
        "player_name": player_name,
        "channel": channel,
    }

    if decision != (session.get("decision") or ""):
        update_fields["decision"] = decision or None
    if notes != (session.get("notes") or ""):
        update_fields["notes"] = notes

    if decision and notes:
        update_fields["status"] = _status_from_decision(decision)

    has_changes = any(session.get(key) != value for key, value in update_fields.items())
    if not has_changes:
        return session

    session_store.update_session(session["session_id"], **update_fields)
    logger.info(
        "Reconciled session=%s from Google Sheets row=%s (status=%s)",
        session["session_id"],
        row_number,
        update_fields.get("status", session.get("status")),
    )
    refreshed = session_store.get_session(session["session_id"])
    return refreshed if refreshed is not None else session


def _run_review_job(session_id: str, player_id: str, player_name: str, channel: str) -> None:
    try:
        logger.info("Review job started for session=%s", session_id)
        sheet_res = sheets_service.append_review_row(session_id, player_id, player_name, channel)
        row_number = sheet_res["row_number"]
        session_store.update_session(
            session_id,
            status="pending_human_review",
            row_number=row_number,
            notes="",
        )
        session_store.complete_review_job(session_id)
        _increment_metric("review_jobs_completed")
        logger.info("Review job finished for session=%s (row=%d)", session_id, row_number)
    except Exception as exc:
        error_message = str(exc)
        session_store.increment_session_error(session_id, error_message)
        session_store.fail_review_job(session_id, error_message)
        _increment_metric("requests_failed")
        _increment_metric("review_jobs_failed")
        logger.exception("Review job failed for session=%s", session_id)


async def _review_worker(worker_number: int) -> None:
    worker_id = f"review-worker-{worker_number}"
    logger.info("Started %s", worker_id)
    try:
        while True:
            job = await asyncio.to_thread(session_store.claim_next_review_job, worker_id)
            if job is None:
                await asyncio.sleep(config.REVIEW_WORKER_IDLE_SLEEP_SECONDS)
                continue

            await asyncio.to_thread(
                _run_review_job,
                job["session_id"],
                job["player_id"],
                job["player_name"],
                job["channel"],
            )
    except asyncio.CancelledError:
        logger.info("Stopped %s", worker_id)
        raise


async def _cleanup_worker() -> None:
    try:
        while True:
            await asyncio.sleep(config.SESSION_CLEANUP_INTERVAL_SECONDS)
            removed = await asyncio.to_thread(session_store.cleanup_expired_sessions)
            if removed:
                _increment_metric("sessions_expired", removed)
                logger.info("Expired %d old session(s)", removed)

            # Prune stale reconciliation cache entries
            now = time.monotonic()
            cutoff = config.RECONCILIATION_COOLDOWN_SECONDS * 10
            with _reconciliation_lock:
                stale = [sid for sid, ts in _reconciliation_timestamps.items() if (now - ts) > cutoff]
                for sid in stale:
                    del _reconciliation_timestamps[sid]
                if stale:
                    logger.debug("Pruned %d stale reconciliation cache entries", len(stale))
    except asyncio.CancelledError:
        logger.info("Stopped session cleanup worker")
        raise


# ── Pydantic models ──────────────────────────────────────────────────

class WebhookPayload(BaseModel):
    session_id: str = Field(..., description="Session ID from Column K")
    decision: str = Field(..., description="'Yes' or 'No' from Column I")
    notes: str = Field("", description="Context/notes from Column J")
    row_number: int | None = Field(None, description="The specific row being edited")
    row_data: list[Any] = Field(default_factory=list, description="Array of columns B to J")


class WithdrawalRequest(BaseModel):
    session_id: str
    player_id: str
    player_name: str = "Test-User"
    channel: str = "Test"


class AdaWithdrawalRequest(BaseModel):
    playerUid: str | None = None
    player_id: str | None = None
    player_name: str | None = ""
    channel: str | None = "Chat"


# ── Lifespan ─────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _startup_time, _worker_tasks
    _startup_time = time.monotonic()
    config.setup_logging()
    config.validate()
    await asyncio.to_thread(session_store.init_db)
    expired = await asyncio.to_thread(session_store.cleanup_expired_sessions)
    if expired:
        _increment_metric("sessions_expired", expired)

    logger.info("Durable HITL Payment Automation starting")
    logger.info("   Sheet   : %s", config.SPREADSHEET_ID[:12] + "...")
    logger.info("   Store   : %s", config.SESSION_DB_PATH)
    logger.info("   Workers : %d", config.REVIEW_WORKER_COUNT)

    _worker_tasks = [
        asyncio.create_task(_review_worker(index), name=f"review-worker-{index}")
        for index in range(1, config.REVIEW_WORKER_COUNT + 1)
    ]
    _worker_tasks.append(asyncio.create_task(_cleanup_worker(), name="session-cleanup"))

    try:
        yield
    finally:
        for task in _worker_tasks:
            task.cancel()
        await asyncio.gather(*_worker_tasks, return_exceptions=True)
        await asyncio.to_thread(session_store.close_all_connections)
        logger.info("Server shutting down")


# ── FastAPI app ──────────────────────────────────────────────────────

app = FastAPI(
    title="HITL Payment Automation",
    description="Durable webhook router for Google Sheets human-in-the-loop workflows.",
    version="4.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ALLOW_ORIGINS,
    allow_credentials=config.ALLOW_CREDENTIALS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Webhook-Secret"],
)


# ── Endpoints ────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    uptime_seconds = time.monotonic() - _startup_time if _startup_time else 0
    tracked_sessions, queue_depth = await asyncio.gather(
        asyncio.to_thread(session_store.count_sessions),
        asyncio.to_thread(session_store.get_review_queue_depth),
    )
    return {
        "status": "ok",
        "uptime_seconds": round(uptime_seconds, 1),
        "tracked_sessions": tracked_sessions,
        "review_queue_depth": queue_depth,
        "workers": config.REVIEW_WORKER_COUNT,
        "storage": "sqlite",
    }


@app.get("/metrics")
async def metrics():
    uptime_seconds = time.monotonic() - _startup_time if _startup_time else 0
    tracked_sessions, queue_depth, status_counts, job_counts = await asyncio.gather(
        asyncio.to_thread(session_store.count_sessions),
        asyncio.to_thread(session_store.get_review_queue_depth),
        asyncio.to_thread(session_store.get_status_counts),
        asyncio.to_thread(session_store.get_review_job_counts),
    )
    return {
        "uptime_seconds": round(uptime_seconds, 1),
        "tracked_sessions": tracked_sessions,
        "review_queue_depth": queue_depth,
        "review_workers": config.REVIEW_WORKER_COUNT,
        "session_status_counts": status_counts,
        "review_job_counts": job_counts,
        **_metrics_snapshot(),
    }


@app.get("/sessions")
async def list_sessions(
    limit: int = Query(100, ge=1, le=config.SESSION_LIST_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    status_filter: str | None = Query(None, alias="status"),
):
    total = await asyncio.to_thread(session_store.count_sessions, status_filter)
    sessions = await asyncio.to_thread(
        session_store.list_sessions,
        limit=limit,
        offset=offset,
        status=status_filter,
    )
    return {
        "count": total,
        "limit": limit,
        "offset": offset,
        "sessions": sessions,
    }


@app.post("/webhook")
async def webhook(payload: WebhookPayload, x_webhook_secret: str | None = Header(None)):
    """Receives the human decision from Google Apps Script."""
    _increment_metric("webhooks_received")
    if config.WEBHOOK_SECRET:
        if not x_webhook_secret or not hmac.compare_digest(x_webhook_secret, config.WEBHOOK_SECRET):
            raise HTTPException(status_code=403, detail="Invalid webhook secret")

    logger.info(
        "Webhook received: session=%s decision=%s notes=%s",
        payload.session_id,
        payload.decision,
        payload.notes,
    )

    existing = await asyncio.to_thread(session_store.get_session, payload.session_id)
    player_id, player_name, channel = _extract_player_fields(payload.row_data)

    if existing is None:
        _increment_metric("webhooks_corrections")
        await asyncio.to_thread(
            session_store.create_session,
            payload.session_id,
            player_id,
            player_name,
            channel,
            "pending_human_review",
        )

    update_fields: dict[str, Any] = {
        "status": _status_from_decision(payload.decision),
        "decision": payload.decision,
        "notes": payload.notes,
        "row_data": payload.row_data,
        "player_id": player_id,
        "player_name": player_name,
        "channel": channel,
    }
    if payload.row_number is not None:
        update_fields["row_number"] = payload.row_number

    await asyncio.to_thread(session_store.update_session, payload.session_id, **update_fields)
    _increment_metric("requests_succeeded")
    return {"status": "finalized", "message": "Updated session status."}


@app.post("/test/withdrawal")
async def test_withdrawal(req: WithdrawalRequest):
    _increment_metric("requests_total")
    player_id = _normalise_player_id(req.player_id)
    await asyncio.to_thread(
        session_store.create_session,
        req.session_id,
        player_id,
        req.player_name,
        req.channel,
        "processing",
    )
    await asyncio.to_thread(
        session_store.enqueue_review_job,
        req.session_id,
        player_id,
        req.player_name,
        req.channel,
    )
    return {"status": "processing", "session_id": req.session_id}


@app.post("/hitl/v1/request_review")
async def ada_request_review(req: AdaWithdrawalRequest):
    """ADA chatbot endpoint. Instantly returns 'processing' and a specific session_id."""
    _increment_metric("requests_total")
    actual_player_id = _normalise_player_id(req.playerUid or req.player_id)
    if not actual_player_id:
        raise HTTPException(status_code=422, detail="Missing player_id or playerUid")

    now_str = time.strftime("%y%m%d-%H%M%S")
    short_uuid = uuid.uuid4().hex[:6]
    session_id = f"W-{now_str}-{short_uuid}"

    logger.info("ADA Request via Chatbot: player=%s", actual_player_id)
    logger.info("   Generated new sequence session=%s", session_id)

    await asyncio.to_thread(
        session_store.create_session,
        session_id,
        actual_player_id,
        req.player_name or "",
        req.channel or "Chat",
        "processing",
    )
    await asyncio.to_thread(
        session_store.enqueue_review_job,
        session_id,
        actual_player_id,
        req.player_name or "",
        req.channel or "Chat",
    )

    return {
        "status": "processing",
        "session_id": session_id,
        "message": "Queued for durable Google Sheets processing.",
    }


@app.get("/hitl/v1/status/session/{session_id}")
async def ada_check_status_session(session_id: str):
    session = await asyncio.to_thread(session_store.get_session, session_id)
    if session is None:
        return {"status": "not_found", "message": "Unknown or expired session ID."}

    if _needs_sheet_reconciliation(session):
        try:
            session = await asyncio.to_thread(_reconcile_session_from_sheet, session)
        except Exception:
            logger.exception("Failed to reconcile session=%s from Google Sheets", session_id)

    return session


@app.get("/hitl/v1/status/{player_id}/{row_number}")
async def ada_check_status_legacy(player_id: str, row_number: int):
    session = await asyncio.to_thread(
        session_store.find_session_by_player_and_row,
        player_id,
        row_number,
    )
    if session is None:
        return {"status": "not_found", "decision": "not_found", "notes": ""}
    return session


if __name__ == "__main__":
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=False, log_level="info")
