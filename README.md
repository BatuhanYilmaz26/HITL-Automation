# HITL Payment Automation — FastAPI + Google Sheets

![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python&logoColor=white) ![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white)

## Overview

![HITL Automation](hitl_automation.png)

This system delivers **end-to-end withdrawal automation** - from the moment a player requests a withdrawal in the chatbot, to the final decision being relayed back, with zero manual data entry in between.

- **Fast & Durable**: The request path persists session and job state immediately, then dedicated workers process Google Sheets writes without blocking ADA.
- **Chatbot-native**: Players initiate withdrawals directly through the ADA chatbot — no context switching for the player or the agent.
- **Instant dashboard logging**: Every request is appended to the HITL Google Sheet with player details, backend GMT+2 timestamps, and session tracking.
- **Human-only decisions**: The system routes the requests and **never** approves or rejects a payment automatically — that authority stays with the human reviewer.
- **Real-time feedback loop**: The moment a reviewer types their decision, the chatbot is updated within seconds via an automated webhook pipeline.
- **Full audit trail**: Every request, decision, and note is captured with timestamps — ready for compliance and reporting.
- **Concurrent & resilient**: Session state survives restarts, review jobs are recoverable, and writes avoid full-sheet scans as volume grows.

---

## Architecture

### Sequence Flow

```mermaid
sequenceDiagram
    participant Player
    participant ADA as ADA Chatbot
    participant API as FastAPI Server
  participant Store as SQLite Store
  participant Worker as Review Worker
    participant Sheet as Google Sheet
    participant Human as Human Reviewer
    participant Script as Apps Script

    Player->>ADA: "I want to withdraw"
    ADA->>API: POST /hitl/v1/request_review
  API->>Store: Create session + enqueue review job
    API-->>ADA: {status: "processing", session_id: "xyz"}
  Note right of API: Request returns immediately after durable persistence
    
  Worker->>Store: Claim queued review job
  Worker->>Sheet: Append row (GMT+2 Timestamp, Player ID, Name, Channel)

    loop Every 10-15 seconds
        ADA->>API: GET /hitl/v1/status/session/{session_id}
        API-->>ADA: {status: "pending_human_review"}
    end

    Human->>Sheet: Types "Yes" in Col I, notes in Col J
    Sheet-->>Script: onEdit trigger
    Script->>API: POST /webhook {decision, notes, row_data}
    API->>Store: Update session state
    API-->>Script: 200 OK

    ADA->>API: GET /hitl/v1/status/session/{session_id}
    API-->>ADA: {status: "approved", notes: "Verified", row_data: [...]}
    ADA-->>Player: "Your withdrawal has been approved!"
```

---

## Reliability Features

This system is designed so that **zero withdrawal requests are missed or skipped**, even under high concurrency:

| Feature                           | File                      | Description                                                                                                                     |
| --------------------------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| **Sheets Retry**            | `src/sheets_service.py`     | Truncated exponential backoff with jitter covers quota spikes, 5xx responses, and transient transport errors such as Windows socket aborts.                    |
| **Thread-Local Sheets Client Cache** | `src/sheets_service.py` | Reuses a cached Sheets client per thread with a TTL, avoiding cross-thread reuse of `httplib2.Http()` without rebuilding the client on every call. |
| **Sheets Concurrency Limit** | `src/sheets_service.py` | Limits concurrent Sheets API calls with a semaphore so worker bursts and reconciliation reads do not overwhelm API quota. |
| **Atomic Sheets Append**    | `src/sheets_service.py`     | Uses a single `values.append` call instead of scanning `A5:K` to find the next row.                                           |
| **Durable Session Store**   | `src/session_store.py`      | Persists session status in SQLite so polling, webhook updates, and restarts stay in sync.                                      |
| **Durable Review Queue**    | `src/main.py`               | Review jobs are queued in SQLite and claimed by worker tasks, so requests are recoverable after restarts.                      |
| **Reconciliation Cooldown** | `src/main.py` | Pending sessions reconcile from Google Sheets only after a per-session cooldown, preventing polling traffic from exhausting read quota. |
| **SQLite Tuning** | `src/session_store.py` | WAL mode plus `busy_timeout`, cache sizing, memory-mapped I/O, and cleanup indexing improve durability and performance under concurrency. |
| **Backend Timestamping**    | `src/sheets_service.py`     | The API writes Column B directly in GMT+2, removing the expensive Apps Script full-sheet `onChange` scan.                               |
| **Webhook Retry**           | `src/apps_script.js`        | Apps Script retries up to 3x with exponential backoff if the webhook fails.                                                    |
| **Timing-Safe Webhook Auth** | `src/main.py` | The webhook secret is validated with `hmac.compare_digest()` instead of a simple string comparison. |
| **Operational Metrics**     | `src/main.py`               | `/metrics` exposes queue depth, worker counts, session status counts, and review job outcomes.                                |

---

## Security Model

| Layer                            | Implementation                                                                                     |
| -------------------------------- | -------------------------------------------------------------------------------------------------- |
| **Webhook Authentication** | Shared secret (`WEBHOOK_SECRET`) in `X-Webhook-Secret`, validated inside the `/webhook` handler with constant-time comparison |
| **Sheets API Auth**        | Service account with narrow scope (`spreadsheets` only) — no OAuth user consent needed          |
| **Credential Management**  | All secrets in `.env` (gitignored), service account key in `service_account.json` (gitignored) |
| **CORS**                   | Configurable middleware — restrict to specific domains in production                              |
| **Input Validation**       | Pydantic models enforce strict typing on all request payloads                                      |

---

## Quick Start
1. Clone & Setup Venv. Install requirements.
2. Copy `.env.example` to `.env` and fill in the current runtime values.
3. Share the Google Sheet with the service account email as an Editor.
4. Run `python -m src.main`
5. Expose localhost with ngrok: `ngrok http 8000`
6. Open `http://localhost:8000/docs` or `http://localhost:8000/redoc` to inspect the live FastAPI surface.

## Production Notes
- Keep `REQUIRE_SERVICE_ACCOUNT=true` in production so writes never fall back to API-key mode.
- Put `SESSION_DB_PATH` on a durable disk path that survives restarts and deployments.
- Tune `REVIEW_WORKER_COUNT` carefully against Google Sheets quota rather than CPU core count.
- Keep `SHEETS_API_CONCURRENT_LIMIT` aligned with your Sheets API quota and review worker count.
- Keep `SHEETS_CLIENT_TTL_SECONDS` high enough to avoid unnecessary client rebuild churn, but low enough that auth/config changes roll through predictably.
- Tune `RECONCILIATION_COOLDOWN_SECONDS` high enough that ADA polling cannot turn into sustained Sheets read pressure.
- Set `CORS_ALLOW_ORIGINS` to the exact ADA domains you expect instead of `*`.
- Only the Apps Script `onEdit` trigger is required now; backend writes Column B timestamps directly in GMT+2.
- SQLite WAL mode intentionally creates `hitl_sessions.db`, `hitl_sessions.db-wal`, and `hitl_sessions.db-shm` while the app is active; those files belong to the same database and should not be deleted manually.

## Recommended Profiles

The values in `.env.example` are demo-oriented starter defaults. If you want clearer operational profiles while keeping Google Sheets as the review surface, use the following starting points.

| Profile | Use case | REVIEW_WORKER_COUNT | REVIEW_WORKER_IDLE_SLEEP_SECONDS | REVIEW_JOB_VISIBILITY_TIMEOUT_SECONDS | SHEETS_API_CONCURRENT_LIMIT | SHEETS_CLIENT_TTL_SECONDS | RECONCILIATION_COOLDOWN_SECONDS |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `demo` | Shareholder demo, low concurrent pending volume | 4 | 0.5 | 300 | 5 | 300 | 15 |
| `pilot` | Controlled rollout with moderate pending volume | 4 | 0.5 | 600 | 3 | 600 | 60 |
| `enterprise-on-sheets` | Higher-volume production while keeping Google Sheets as the reviewer UI | 4 | 1.0 | 900 | 2 | 900 | 180 |

### Profile Notes
- `REVIEW_WORKER_COUNT` should not scale linearly with traffic. Google Sheets quota is the real constraint, so adding more workers mainly increases burst pressure.
- `SHEETS_API_CONCURRENT_LIMIT` is only a concurrency cap. It reduces burst intensity but does not guarantee a fixed requests-per-minute ceiling.
- `RECONCILIATION_COOLDOWN_SECONDS` is the most important safety control for polling-heavy setups. Higher values reduce fallback freshness but protect Sheets read quota.
- `enterprise-on-sheets` assumes the Apps Script webhook is the primary state-sync path and that reconciliation from Google Sheets is a fallback, not the main flow.
- These are starting profiles, not guaranteed limits. Validate them against your actual Google Workspace edition, Sheets quota, and real pending-session behavior.

### Quick Math
- With a default Sheets read quota of about `300/min`, the maximum continuously pending sessions supported by reconciliation is roughly `5 * RECONCILIATION_COOLDOWN_SECONDS`.
- Example: `15s` supports about `75` continuously pending sessions before read pressure becomes dangerous.
- Example: `60s` supports about `300` continuously pending sessions.
- Example: `180s` supports about `900` continuously pending sessions.

## Deployment Checklist
- Copy `.env.example` to `.env` and set `SPREADSHEET_ID`, `SHEET_NAME`, `SERVICE_ACCOUNT_PATH`, and `WEBHOOK_SECRET`.
- Keep `REQUIRE_SERVICE_ACCOUNT=true` anywhere the app must append to Google Sheets.
- Share the target spreadsheet with the configured service account as an Editor.
- Point `SESSION_DB_PATH` to a durable disk location for any environment that should survive restarts.
- Set `CORS_ALLOW_ORIGINS` to the exact ADA domains you expect; only enable `ALLOW_CREDENTIALS=true` with explicit origins.
- Tune `REVIEW_WORKER_COUNT`, `SHEETS_API_CONCURRENT_LIMIT`, and `RECONCILIATION_COOLDOWN_SECONDS` to match expected traffic and Google Sheets quota.
- Update `src/apps_script.js` with the active `/webhook` URL and the same `WEBHOOK_SECRET` value.
- Install the Apps Script `onEdit` trigger and confirm the `ErrorLog` sheet is empty after a test run.
- Verify `/health`, `/metrics`, one end-to-end append, and one end-to-end human decision before presenting the demo.

## API Surface

The current FastAPI application exposes the following endpoints:

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/hitl/v1/request_review` | `POST` | Main ADA chatbot entry point. Creates a durable session, queues a review job, and returns `status: processing` immediately. |
| `/hitl/v1/status/session/{session_id}` | `GET` | Primary polling endpoint. Returns the persisted session and can reconcile pending rows from Google Sheets on a cooldown. |
| `/hitl/v1/status/{player_id}/{row_number}` | `GET` | Legacy lookup path for older integrations that still key status checks by player ID and row number. |
| `/webhook` | `POST` | Receives Apps Script callbacks after a human reviewer fills Decision and Notes. |
| `/health` | `GET` | Lightweight health and queue-depth check for operations and demos. |
| `/metrics` | `GET` | Operational counters, queue depth, worker count, session status counts, and job counts. |
| `/sessions` | `GET` | Admin/debug endpoint with pagination and optional `status` filtering. |
| `/test/withdrawal` | `POST` | Manual test endpoint that accepts a caller-provided `session_id`. Useful for local verification and scripted testing. |

Notes:
- `/sessions` supports `limit`, `offset`, and `status` query parameters.
- `/test/withdrawal` is a testing helper, not the primary ADA integration path.

### Testing
```powershell
# 1. Submit a withdrawal with Name and Channel
Invoke-RestMethod -Uri "http://localhost:8000/hitl/v1/request_review" `
  -Method Post `
  -Headers @{"Content-Type"="application/json"} `
  -Body '{"player_id":"P100", "player_name":"Batuhan", "channel":"Chat"}'

# 2. Extract session_id from response

# 3. Go to Google Sheets and simulate human review:
#   - Column B timestamp is written automatically in GMT+2
#   - Decision (Column I) = 'Yes'
#   - Notes (Column J) = 'Verified manually'

# 4. Poll status
Invoke-RestMethod -Uri "http://localhost:8000/hitl/v1/status/session/YOUR_SESSION_ID"
```

## Tools Available
Run `python -m src.test_concurrent` to exercise the durable queue path with burst or staggered request loads.
