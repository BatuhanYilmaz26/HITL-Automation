# HITL Payment Automation: Complete Codebase Guide

This document is a comprehensive guide to understanding the **Human-in-the-Loop (HITL) Payment Automation** project.

---

## 1. Project Overview & Architecture

### The Goal
The purpose of this system is to handle automated withdrawal requests from players (originating from a chatbot like Ada.cx) but with a mandatory **Human-in-the-Loop** step. The system operates as a zero-latency router.

### Core Flow
1. **Trigger:** A chatbot (e.g., Ada) sends a withdrawal request to our Python server (`main.py`).
2. **Fast Response:** The FastAPI server instantly creates an asynchronous `BackgroundTask`, generates a `session_id`, and returns `{"status": "processing", "session_id": "xyz"}` to the chatbot in under 10ms. This prevents the chatbot webhook from timing out.
3. **Escalation (Background):** The background task safely acquires a Python Thread Lock and inserts a new pending row into a Google Spreadsheet (`sheets_service.py`), then updates its internal status to `pending_human_review`.
4. **Human Action:** A human reviewer opens the Google Sheet, looks at the withdrawal request, and types "Yes" or "No" in the Decision column.
5. **Webhook to Backend:** A Google Apps Script (`apps_script.js`) attached to the Spreadsheet detects this edit and immediately fires an HTTP payload (webhook) back to our Python server.
6. **State Sync:** The Python server receives the webhook and instantly mutates the memory map `session_status[session_id]` to `approved` or `rejected` with the human notes. 
7. **Final State:** The chatbot cleanly polls our server via `/hitl/v1/status/session/{session_id}` and retrieves the final status to deliver to the user.

---

## 2. Setting Up the Mechanics

### Dependencies
*   `google-api-python-client` & `google-auth`: For communicating securely with the Google Sheets API.
*   `fastapi`, `uvicorn`: Web server.
*   `python-dotenv`: Environment var loader.
*   `pydantic`: Schema validation.

### Secure Sheets Client (`sheets_service.py`)
Because the system is asynchronous and multi-threaded by nature (FastAPI concurrency), writing to the Google Sheet must be strictly controlled to prevent two requests hitting the exact same row.

*   **`threading.Lock()` (`_append_lock`)**: This lock ensures that if 50 withdrawal requests hit at once, they queue up single-file to update the Google Sheet safely. The delay is entirely hidden from the chatbot because of `BackgroundTasks`.

---

## 3. The FastAPI Core (`main.py`)

`main.py` is the front door. 

*   **`session_status: dict`**: An ultra-fast in-memory State Machine. It stores `{session_id: {"status": "...", "decision": "...", "notes": "..."}}`.
*   **`/hitl/v1/request_review`**: Triggers the `process_withdrawal_background()` task. Returns `session_id` instantly.
*   **`/hitl/v1/status/session/{session_id}`**: Polling endpoint. It returns exactly what exists in `session_status` at that exact millisecond.
*   **`/webhook`**: Handles incoming HTTP POST requests from Google Apps Script to flip the boolean inside `session_status`.

---

## 4. Apps Script (`apps_script.js`)

Our final puzzle piece lives inside Google Sheets. Without this, the Python server would never know a human wrote "Yes" or "No".

1.  **`onChange(e)`**: When the Python backend API writes a new row, this silently stamps the current Date/Time into Column B.
2.  **`onEdit(e)`**: Whenever a user types something in the "Decision" or "Notes" column, it triggers. Once both are filled out, it grabs the hidden Session ID (Column K) and bundles a JSON payload. It uses `UrlFetchApp` with exponential retry bounds to dispatch the HTTP POST webhook back safely.

---

## 5. Stress Testing (`test_concurrent.py`)

Due to the concurrency constraints of Google API limits, this file tortures the system safely.
* It blasts `/hitl/v1/request_review` heavily.
* Measures initial response time (guaranteeing sub-1s).
* Continually pings `/hitl/v1/status/session/{session_id}` dynamically to ensure the server updates successfully as rows are locked and appended.
