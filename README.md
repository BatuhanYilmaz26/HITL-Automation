# HITL Payment Automation — Google ADK + FastAPI + Google Sheets

A local proof-of-concept for **Human-in-the-Loop (HITL) payment automation** using Google's Agent Development Kit (ADK), integrated with an ADA Chatbot workflow.

An AI agent processes player withdrawals. **Every withdrawal** is submitted to a Google Sheet for mandatory human review. The ADA Chatbot simply provides the `player_id`. The backend inserts the review row, Apps Script stamps the time, and when the human decides (Yes/No), an Apps Script webhook resumes the agent. The ADA Chatbot can then poll for the decision.

## Architecture

![GoogleADK-HITL Infographics](GoogleADK-HITL%20Infographics.png)

```text
┌──────────────┐     ┌──────────────────┐     ┌────────────────┐
│ ADA Chatbot  │────▶│  FastAPI Server  │────▶│   ADK Agent    │
│ (Automated)  │     │  POST /ada/...   │     │  (Gemini LLM)  │
└──────────────┘     └──────────────────┘     └───────┬────────┘
        ▲                     ▲                       │
        │                     │             ┌─────────┘
   GET /status                │             ▼ (Requests Review)
   (Polling)                  │        ┌──────────────┐ 
        │                 POST /webhook│ Google Sheet │◀──┐
        │                (decision,    │ (HITL Dash)  │   │ onChange
        │                 notes,       └──────────────┘───┘ (Timestamp)
        │                 row_data)           │
        │                     │               ▼
        │               ┌─────┴────────────┐  Human reviews row,
 (Returns full JSON     │   Apps Script    │  types Col I (Decision),
  array of Col A-J)     │ (onEdit Trigger) │  types Col J (Notes)
        └───────────────│                  │
                        └──────────────────┘
```

## Reliability Features

This PoC is designed so that **zero withdrawal requests are missed or skipped**, even under high concurrency:

| Feature | File | Description |
|---------|------|-------------|
| **LLM Semaphore** | `agent.py` | `asyncio.Semaphore` throttles concurrent LLM calls (default: 50, configurable via `LLM_CONCURRENCY_LIMIT`). Excess requests queue instead of failing. |
| **LLM Retry** | `agent.py` | Both `start_withdrawal` and `resume_withdrawal` auto-retry on 429/RESOURCE_EXHAUSTED errors with progressive delays (15s, 30s, 60s). |
| **Sheets Lock** | `sheets_service.py` | `threading.Lock` serializes Google Sheets writes, preventing race conditions on row assignment. |
| **Sheets Retry** | `sheets_service.py` | Exponential backoff (up to 5 retries) for Sheets API 429/5xx errors. |
| **Gap-tolerant Row Finder** | `sheets_service.py` | Appends after the last filled row, not the first empty gap. |
| **Error Propagation** | `tools.py`, `main.py` | Sheet write failures and agent errors are returned to the caller (never silently swallowed). |
| **Webhook Retry** | `apps_script.js` | Apps Script retries up to 3× with 2s/4s/8s backoff if the webhook fails, ensuring human decisions are never lost. |
| **Correction Fallback** | `main.py` | If a webhook arrives for a session that was already finalized, the decision is applied directly via `player_id` from `row_data`. |

## Quick Start

### 1. Clone & Virtual Environment

```bash
cd GoogleADK-HITL
python -m venv venv
venv\Scripts\activate      # Windows
# source venv/bin/activate  # macOS/Linux
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment

```bash
copy .env.example .env     # Windows
# cp .env.example .env      # macOS/Linux
```

Edit `.env` and fill in:

| Variable               | Description                                                           |
| ---------------------- | --------------------------------------------------------------------- |
| `GOOGLE_API_KEY`       | Your Gemini API key (from [AI Studio](https://aistudio.google.com/))  |
| `SPREADSHEET_ID`       | The long ID from your Google Sheet URL                                |
| `SHEET_NAME`           | Tab name (default: `Sheet1`)                                          |
| `SHEETS_API_KEY`       | Can be the same key if Sheets API is enabled on the project           |
| `LLM_CONCURRENCY_LIMIT`| Max simultaneous LLM calls (default: `50` for production, `2` for free tier) |

### 4. Google Service Account Setup

To allow the backend to edit your Google Sheet securely, you need a Service Account credentials file:

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) -> **Service Accounts** (for your project).
2. Click **+ CREATE SERVICE ACCOUNT**, name it (e.g., `hitl-bot`), and create.
3. Copy the generated Service Account Email address (e.g., `hitl-bot@...gserviceaccount.com`).
4. Click on the new service account, go to the **Keys** tab, click **ADD KEY** -> **Create new key** -> **JSON**.
5. Move the downloaded file into this project's folder and rename it exactly to `service_account.json`.

### 5. Google Sheet Setup

Create a Google Sheet with **4 header rows**. Data must start at Row 5. Note: The backend will auto-insert into Column A (Session ID) and Column C (Player ID).

**Important:** Click **Share** on your Google Sheet and share it with the Service Account email you copied in Step 4, setting the role to **Editor**.

| A          | B         | C         | D           | E              | F  | G  | H     | I        | J     |
| ---------- | --------- | --------- | ----------- | -------------- | -- | -- | ----- | -------- | ----- |
| Session ID | Timestamp | Player ID | Player Name | Contact Method | — | — | Agent | Decision | Notes |

- **Rows 1-4**: Reserved for your descriptive headers, warnings, and spreadsheet titles. Data starts at Row 5.
- **Column B**: Automatically stamped by the Apps Script `onChange` trigger when Player ID populates.
- **Column I**: Add a data-validation dropdown → `Yes, No`

### 6. Apps Script Setup

1. Open the Sheet → **Extensions → Apps Script**
2. Paste the contents of `apps_script.js`
3. Replace `WEBHOOK_URL` with your ngrok URL
4. Create **two installable triggers**: Edit → Triggers → Add
   - **Webhook Trigger**: Function: `onEdit`, Event: `From spreadsheet`, `On edit`
   - **Timestamp Trigger**: Function: `onChange`, Event: `From spreadsheet`, `On change`
5. Authorize the script

### 7. Run the Server

```bash
python main.py
```

Server starts at `http://localhost:8000`.

### 8. Expose via ngrok (for Apps Script)

```bash
ngrok http 8000
```

Copy the `https://xxxx.ngrok-free.app` URL into `apps_script.js`.

## ADA Chatbot Integration Guide

To connect your ADA Chatbot to this automation system, you need to configure two API calls inside ADA:

### 1. Trigger the Request (POST)
When a player requests a withdrawal, ADA sends the `player_id` to the backend.
- **Endpoint**: `POST http://<your-server-ip>:8000/ada/v1/request_review`
- **Headers**: `"Content-Type": "application/json"`
- **JSON Body**: 
  ```json
  { "player_id": "P100" }
  ```
*Action*: This creates a new session and instantly adds the `P100` row to the Google Sheet for human review.

### 2. Poll for the Decision (GET)
ADA should be set up to poll this endpoint automatically (e.g., every 5-10 seconds) to check if the human payment agent has finished reviewing the sheet.
- **Endpoint**: `GET http://<your-server-ip>:8000/ada/v1/status/{player_id}`
  *(Replace `{player_id}` dynamically with the player's ID, e.g., `/ada/v1/status/P100`)*

*Action*: While the human is still reviewing, this returns `"decision": "pending"`. The exact moment the human agent finishes typing their Notes in Column J, the Google Sheet sends a webhook. The very next time ADA polls this GET endpoint, it will instantly receive the finalized JSON with all 10 columns:
```json
{
  "player_id": "P100",
  "decision": "Yes",
  "notes": "Verified manually",
  "row_data": [
    "ada-xxxx",
    "2026-03-22T19:00:00.000Z",
    "P100",
    "", "", "", "", "",
    "Yes",
    "Verified manually"
  ]
}
```

---

## Testing

### Single Request

```powershell
# Submit a withdrawal via ADA endpoint
Invoke-RestMethod -Uri "http://localhost:8000/ada/v1/request_review" `
  -Method Post `
  -Headers @{"Content-Type"="application/json"} `
  -Body '{"player_id":"P100"}'

# Poll status for the player (will return "pending" initially)
Invoke-RestMethod -Uri "http://localhost:8000/ada/v1/status/P100"

# Simulate human approval (Alternatively, edit Column I in the Sheet directly!)
Invoke-RestMethod -Uri "http://localhost:8000/webhook" `
  -Method Post `
  -Headers @{"Content-Type"="application/json"} `
  -Body '{"session_id":"<SESSION_ID_FROM_RESPONSE>","decision":"Yes","notes":"Verified manually"}'
```

### Concurrency Stress Test

Use `test_concurrent.py` to simulate multiple simultaneous withdrawal requests:

```powershell
# Install test dependency (if not already)
pip install aiohttp

# Default: 15 requests in staggered batches (safe for free tier)
python test_concurrent.py

# Burst mode: all at once (requires paid Gemini API / Vertex AI)
python test_concurrent.py --mode burst --count 15

# Custom batch settings
python test_concurrent.py --count 20 --batch-size 4 --batch-delay 65
```

The test script will:
1. Fire all requests (concurrently or in batches)
2. Report per-request status codes and timing
3. Poll each player's status to confirm `pending`
4. Show total pending sessions on the server

## API Reference

| Method   | Endpoint                       | Description                                                                        |
| -------- | ------------------------------ | ---------------------------------------------------------------------------------- |
| `GET`  | `/health`                    | Health check + pending count                                                       |
| `GET`  | `/sessions`                  | List pending session IDs                                                           |
| `POST` | `/ada/v1/request_review`     | **ADA Integration**: Trigger a new withdrawal check (requires `player_id`) |
| `GET`  | `/ada/v1/status/{player_id}` | **ADA Integration**: Poll the human decision for a player                    |
| `POST` | `/test/withdrawal`           | Dev endpoint: Trigger with `session_id` and `player_id`                        |
| `POST` | `/webhook`                   | Receive human decision (from Apps Script)                                          |

## Tech Stack

- **Google ADK** — Agent framework with `LongRunningFnTool` for HITL
- **Gemini 3 Flash Preview** — Fast, cost-effective LLM
- **FastAPI + Uvicorn** — Async HTTP server
- **Google Sheets API** — HITL dashboard bridge
- **Google Apps Script** — `onEdit` and `onChange` spreadsheet triggers
