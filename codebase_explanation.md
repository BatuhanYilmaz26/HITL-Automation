# HITL Payment Automation: Complete Codebase Guide

This document is a comprehensive guide to understanding the **Human-in-the-Loop (HITL) Payment Automation** project.

---

## 1. Project Overview & Architecture

### The Goal
The purpose of this system is to handle automated withdrawal requests from players (originating from a chatbot like Ada.cx) but with a mandatory **Human-in-the-Loop** step. The system operates as a low-latency router with durable state.

### Core Flow
1. **Trigger:** A chatbot (e.g., Ada) sends a withdrawal request to our Python server (`main.py`).
2. **Fast Response:** The FastAPI server instantly generates a `session_id`, stores both the session and a queued review job in SQLite, and returns `{"status": "processing", "session_id": "xyz"}` to the chatbot.
3. **Escalation (Workers):** Dedicated review workers claim queued jobs, append a new row into Google Sheets (`sheets_service.py`), then update the persisted session status to `pending_human_review`.
4. **Human Action:** A human reviewer opens the Google Sheet, looks at the withdrawal request, and types "Yes" or "No" in the Decision column.
5. **Webhook to Backend:** A Google Apps Script (`apps_script.js`) attached to the Spreadsheet detects this edit and immediately fires an HTTP payload (webhook) back to our Python server.
6. **State Sync:** The Python server receives the webhook and updates the persistent session record to `approved` or `rejected` with the human notes. 
7. **Final State:** The chatbot cleanly polls our server via `/hitl/v1/status/session/{session_id}` and retrieves the final status to deliver to the user.

---

## 2. Setting Up the Mechanics

### Dependencies
*   `google-api-python-client` & `google-auth`: For communicating securely with the Google Sheets API.
*   `fastapi`, `uvicorn`: Web server.
*   `python-dotenv`: Environment var loader.
*   `pydantic`: Schema validation.
*   `aiohttp`: Async HTTP client used by the concurrency stress test.

### Secure Sheets Client (`sheets_service.py`)
Because the system is asynchronous and multi-threaded by nature, the Google Sheets client must avoid shared `httplib2` state while also avoiding unnecessary rebuilds.

*   **Thread-local client cache**: Each worker thread keeps its own cached Sheets client for `SHEETS_CLIENT_TTL_SECONDS`, so `httplib2.Http()` is never shared across threads.
*   **Semaphore-based rate limiting**: All Sheets reads and writes pass through a concurrency limiter before retry logic, which reduces the chance of hitting Sheets API quotas under load.
*   **Atomic append**: Rows are written with a single `values.append` call, so the backend does not need to scan `A5:K` to find the next empty row.
*   **Backend timestamps**: Column B is written by the API itself in GMT+2, so Apps Script no longer needs a full-sheet `onChange` sweep.
*   **Canonical sheet contract**: The integration uses operational columns B through K. Column A is ignored so sheet-specific helper values cannot shift the payload.

---

## 3. The FastAPI Core (`main.py`)

`main.py` is the front door. 

*   **`session_store.py`**: A persistent SQLite-backed state machine for both sessions and queued review jobs.
*   **`/hitl/v1/request_review`**: Persists a new session and review job, then returns `session_id` instantly.
*   **Review workers**: Background worker coroutines claim queued jobs and process Sheets writes without blocking request handlers.
*   **`/hitl/v1/status/session/{session_id}`**: Polling endpoint. It returns the persisted session record and can reconcile pending rows from Google Sheets on a per-session cooldown.
*   **`/webhook`**: Handles incoming HTTP POST requests from Google Apps Script and updates persistent state using timing-safe secret comparison.

### Durable SQLite Store (`session_store.py`)

The demo uses SQLite as both a session store and a durable review-job queue.

*   **WAL mode**: Enables strong concurrent read behavior.
*   **Tuned pragmas**: `busy_timeout`, page-cache sizing, and `mmap_size` improve stability under concurrent access.
*   **Indexed cleanup path**: Session cleanup runs against an index on `updated_at` instead of forcing a full table scan as row counts grow.

### Configuration (`config.py`)

The application exposes its operational tuning through environment variables.

Key runtime controls:
*   `REVIEW_WORKER_COUNT`
*   `SHEETS_API_CONCURRENT_LIMIT`
*   `SHEETS_CLIENT_TTL_SECONDS`
*   `RECONCILIATION_COOLDOWN_SECONDS`
*   `SESSION_RETENTION_HOURS`
*   `SESSION_CLEANUP_INTERVAL_SECONDS`
*   `WEBHOOK_SECRET`

---

## 4. Apps Script (`apps_script.js`)

Our final puzzle piece lives inside Google Sheets. Without this, the Python server would never know a human wrote "Yes" or "No".

1.  **`onEdit(e)`**: Whenever a user types something in the "Decision" or "Notes" column, it triggers. Once both are filled out, it grabs the hidden Session ID (Column K) and bundles the operational row payload from Columns B through J. It uses `UrlFetchApp` with exponential retry bounds to dispatch the HTTP POST webhook back safely.
2.  **`onChange(e)`**: Retained as a compatibility no-op. Timestamping now happens in the backend append call.

The Apps Script also writes failed webhook attempts to an `ErrorLog` sheet so operator-visible decisions are not silently lost.

---

## 5. Stress Testing (`test_concurrent.py`)

Due to the concurrency constraints of Google API limits, this file tortures the system safely.
* It blasts `/hitl/v1/request_review` heavily.
* Measures initial response time while writes are queued durably.
* Continually pings `/hitl/v1/status/session/{session_id}` dynamically to ensure the server updates successfully as workers append rows.
* It supports both burst mode and staggered batches so you can test queue pressure without immediately exhausting the Sheets quota.
