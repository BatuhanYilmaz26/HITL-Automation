"""
session_store.py - Persistent SQLite storage for HITL sessions and review jobs.

This module keeps request state durable across restarts and provides a small,
process-safe job queue for the Google Sheets write path.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from src import config

logger = logging.getLogger(__name__)

_thread_local = threading.local()
_schema_lock = threading.Lock()
_connection_registry_lock = threading.Lock()
_connections: list[sqlite3.Connection] = []


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat(timespec="seconds")


def _get_connection() -> sqlite3.Connection:
    conn = getattr(_thread_local, "connection", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.Error:
            _thread_local.connection = None

    conn = sqlite3.connect(
        config.SESSION_DB_PATH,
        timeout=30,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA cache_size=-32000")  # ~32 MB
    conn.execute("PRAGMA mmap_size=268435456")  # 256 MB
    _thread_local.connection = conn
    with _connection_registry_lock:
        _connections.append(conn)
    return conn


def close_all_connections() -> None:
    with _connection_registry_lock:
        while _connections:
            conn = _connections.pop()
            try:
                conn.close()
            except sqlite3.Error:
                logger.debug("Ignoring SQLite close error during shutdown.", exc_info=True)

    if hasattr(_thread_local, "connection"):
        _thread_local.connection = None


def init_db() -> None:
    with _schema_lock:
        conn = _get_connection()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                player_id TEXT NOT NULL,
                player_name TEXT NOT NULL DEFAULT '',
                channel TEXT NOT NULL DEFAULT 'Chat',
                status TEXT NOT NULL,
                row_number INTEGER,
                decision TEXT,
                notes TEXT NOT NULL DEFAULT '',
                row_data_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_status_updated
                ON sessions(status, updated_at);

            CREATE INDEX IF NOT EXISTS idx_sessions_player_row
                ON sessions(player_id, row_number);

            CREATE TABLE IF NOT EXISTS review_jobs (
                session_id TEXT PRIMARY KEY,
                player_id TEXT NOT NULL,
                player_name TEXT NOT NULL DEFAULT '',
                channel TEXT NOT NULL DEFAULT 'Chat',
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                worker_id TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                available_at TEXT NOT NULL,
                locked_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_review_jobs_claim
                ON review_jobs(status, available_at, created_at);

            CREATE INDEX IF NOT EXISTS idx_sessions_updated_at
                ON sessions(updated_at);
            """
        )
        conn.commit()


def _row_to_session(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None

    row_data = row["row_data_json"]
    return {
        "session_id": row["session_id"],
        "status": row["status"],
        "player_id": row["player_id"],
        "player_name": row["player_name"],
        "channel": row["channel"],
        "row_number": row["row_number"],
        "decision": row["decision"],
        "notes": row["notes"],
        "row_data": json.loads(row_data) if row_data else [],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "error_count": row["error_count"],
    }


def create_session(
    session_id: str,
    player_id: str,
    player_name: str = "",
    channel: str = "Chat",
    status: str = "processing",
) -> None:
    now = _utcnow_iso()
    conn = _get_connection()
    conn.execute(
        """
        INSERT INTO sessions (
            session_id, player_id, player_name, channel, status,
            notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, '', ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            player_id=excluded.player_id,
            player_name=excluded.player_name,
            channel=excluded.channel,
            status=excluded.status,
            updated_at=excluded.updated_at
        """,
        (session_id, player_id, player_name, channel, status, now, now),
    )
    conn.commit()


def get_session(session_id: str) -> dict[str, Any] | None:
    conn = _get_connection()
    row = conn.execute(
        """
        SELECT session_id, player_id, player_name, channel, status,
               row_number, decision, notes, row_data_json,
               created_at, updated_at, error_count
        FROM sessions
        WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    return _row_to_session(row)


def update_session(session_id: str, **updates: Any) -> None:
    if not updates:
        return

    allowed_fields = {
        "player_id",
        "player_name",
        "channel",
        "status",
        "row_number",
        "decision",
        "notes",
        "row_data",
        "error_count",
    }

    assignments: list[str] = []
    values: list[Any] = []

    for key, value in updates.items():
        if key not in allowed_fields:
            raise ValueError(f"Unsupported session field update: {key}")
        if key == "row_data":
            assignments.append("row_data_json = ?")
            values.append(json.dumps(value))
        else:
            assignments.append(f"{key} = ?")
            values.append(value)

    assignments.append("updated_at = ?")
    values.append(_utcnow_iso())
    values.append(session_id)

    conn = _get_connection()
    conn.execute(
        f"UPDATE sessions SET {', '.join(assignments)} WHERE session_id = ?",
        values,
    )
    conn.commit()


def increment_session_error(session_id: str, notes: str) -> None:
    conn = _get_connection()
    conn.execute(
        """
        UPDATE sessions
        SET status = 'error',
            notes = ?,
            error_count = error_count + 1,
            updated_at = ?
        WHERE session_id = ?
        """,
        (notes, _utcnow_iso(), session_id),
    )
    conn.commit()


def list_sessions(
    *,
    limit: int = 100,
    offset: int = 0,
    status: str | None = None,
) -> list[dict[str, Any]]:
    conn = _get_connection()
    params: list[Any] = []
    where_clause = ""
    if status:
        where_clause = "WHERE status = ?"
        params.append(status)

    params.extend([limit, offset])
    rows = conn.execute(
        f"""
        SELECT session_id, player_id, player_name, channel, status,
               row_number, decision, notes, row_data_json,
               created_at, updated_at, error_count
        FROM sessions
        {where_clause}
        ORDER BY updated_at DESC
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()
    return [_row_to_session(row) for row in rows if row is not None]


def count_sessions(status: str | None = None) -> int:
    conn = _get_connection()
    if status:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM sessions WHERE status = ?",
            (status,),
        ).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS total FROM sessions").fetchone()
    return int(row["total"] if row else 0)


def get_status_counts() -> dict[str, int]:
    conn = _get_connection()
    rows = conn.execute(
        "SELECT status, COUNT(*) AS total FROM sessions GROUP BY status"
    ).fetchall()
    return {str(row["status"]): int(row["total"]) for row in rows}


def find_session_by_player_and_row(player_id: str, row_number: int) -> dict[str, Any] | None:
    conn = _get_connection()
    row = conn.execute(
        """
        SELECT session_id, player_id, player_name, channel, status,
               row_number, decision, notes, row_data_json,
               created_at, updated_at, error_count
        FROM sessions
        WHERE player_id = ? AND row_number = ?
        LIMIT 1
        """,
        (player_id, row_number),
    ).fetchone()
    return _row_to_session(row)


def cleanup_expired_sessions() -> int:
    cutoff = (_utcnow() - timedelta(hours=config.SESSION_RETENTION_HOURS)).isoformat(timespec="seconds")
    conn = _get_connection()
    cur = conn.execute(
        "DELETE FROM sessions WHERE updated_at < ?",
        (cutoff,),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def enqueue_review_job(
    session_id: str,
    player_id: str,
    player_name: str = "",
    channel: str = "Chat",
) -> None:
    now = _utcnow_iso()
    conn = _get_connection()
    conn.execute(
        """
        INSERT INTO review_jobs (
            session_id, player_id, player_name, channel, status,
            attempts, last_error, available_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'queued', 0, '', ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            player_id=excluded.player_id,
            player_name=excluded.player_name,
            channel=excluded.channel,
            status='queued',
            worker_id=NULL,
            last_error='',
            available_at=excluded.available_at,
            locked_at=NULL,
            updated_at=excluded.updated_at
        """,
        (session_id, player_id, player_name, channel, now, now, now),
    )
    conn.commit()


def claim_next_review_job(worker_id: str) -> dict[str, Any] | None:
    conn = _get_connection()
    now = _utcnow_iso()
    stale_before = (
        _utcnow() - timedelta(seconds=config.REVIEW_JOB_VISIBILITY_TIMEOUT_SECONDS)
    ).isoformat(timespec="seconds")

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE review_jobs
            SET status = 'queued',
                worker_id = NULL,
                locked_at = NULL,
                updated_at = ?
            WHERE status = 'in_progress'
              AND locked_at IS NOT NULL
              AND locked_at < ?
            """,
            (now, stale_before),
        )

        row = conn.execute(
            """
            SELECT session_id, player_id, player_name, channel, attempts
            FROM review_jobs
            WHERE status = 'queued' AND available_at <= ?
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (now,),
        ).fetchone()

        if row is None:
            conn.commit()
            return None

        conn.execute(
            """
            UPDATE review_jobs
            SET status = 'in_progress',
                worker_id = ?,
                attempts = attempts + 1,
                locked_at = ?,
                updated_at = ?
            WHERE session_id = ?
            """,
            (worker_id, now, now, row["session_id"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "session_id": row["session_id"],
        "player_id": row["player_id"],
        "player_name": row["player_name"],
        "channel": row["channel"],
        "attempts": int(row["attempts"]) + 1,
    }


def complete_review_job(session_id: str) -> None:
    conn = _get_connection()
    conn.execute("DELETE FROM review_jobs WHERE session_id = ?", (session_id,))
    conn.commit()


def fail_review_job(session_id: str, error_message: str) -> None:
    conn = _get_connection()
    conn.execute(
        """
        UPDATE review_jobs
        SET status = 'failed',
            worker_id = NULL,
            locked_at = NULL,
            last_error = ?,
            updated_at = ?
        WHERE session_id = ?
        """,
        (error_message, _utcnow_iso(), session_id),
    )
    conn.commit()


def get_review_queue_depth() -> int:
    conn = _get_connection()
    row = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM review_jobs
        WHERE status IN ('queued', 'in_progress')
        """
    ).fetchone()
    return int(row["total"] if row else 0)


def get_review_job_counts() -> dict[str, int]:
    conn = _get_connection()
    rows = conn.execute(
        "SELECT status, COUNT(*) AS total FROM review_jobs GROUP BY status"
    ).fetchall()
    return {str(row["status"]): int(row["total"]) for row in rows}