# End-to-End ADA.cx Integration Guide

This guide provides the exact steps to connect your local FastAPI server to the **ADA.cx** chatbot platform using your ngrok tunnel.

> [!IMPORTANT]
> **Base URL**: `https://<your-ngrok-url>.ngrok-free.app`
> All endpoints below must be prefixed with this URL.

---

## 1. Google Apps Script Configuration

Before configuring ADA, ensure your Google Sheet can talk back to your local server.

1. Open your Google Sheet.
2. Go to **Extensions** → **Apps Script**.
3. In `apps_script.js`, locate the `WEBHOOK_URL` constant:
   ```javascript
   const WEBHOOK_URL = "https://<your-ngrok-url>.ngrok-free.app/webhook";
   ```
4. Click **Save** and ensure you have the `onEdit` trigger set up as described in the README.

> [!TIP]
> The Apps Script now validates the webhook URL at runtime. If you forget to update it from the default `testurl.com` placeholder, it will log a warning and skip the webhook call instead of silently failing.

---

## 2. ADA Action 1: Trigger Withdrawal Request

This action initializes the process and writes a row to Google Sheets.

### **Endpoint Tab**

- **Method**: `POST`
- **URL**: `https://<your-ngrok-url>.ngrok-free.app/hitl/v1/request_review`
- **This API uses**: `JSON`

### **Headers Tab**

- **Parameter**: `Content-Type`
- **Value**: `application/json`

### **Body Tab**

- **Content**:

  ```json
  {
    "player_id": "[player_id]",
    "player_name": "[player_name]",
    "channel": "Chat"
  }
  ```

  *(Ensure `[player_id]` and `[player_name]` are mapped to your ADA variables.)*

### **Response Handling**

ADA needs to capture the `session_id` from the API response to use in the polling step.

- Map the JSON key `session_id` to a new ADA variable named `meta_session_id`.

---

## 3. ADA Action 2: Poll for Decision

This action retrieves the current session state from the backend. For pending sessions, the backend can also reconcile the row from Google Sheets on a cooldown if the webhook has not arrived yet.

### **Endpoint Tab**

- **Method**: `GET`
- **URL**: `https://<your-ngrok-url>.ngrok-free.app/hitl/v1/status/session/[meta_session_id]`
  - *Note: Replace `[meta_session_id]` with the variable in ADA.*

### **Response Handling**

- Map the JSON key `status` to an ADA variable (e.g., `withdrawal_status`).
- Map the JSON key `notes` to an ADA variable (e.g., `withdrawal_notes`).

**How these values are retrieved:**

1. **Human Review**: A human payment agent visits the Google Sheet and reviews the request.
2. **Decision Entry**: The agent enters a decision ("Yes" or "No") in **Column I** and adds mandatory notes in **Column J**.
3. **Automated Webhook**: Once both columns are filled, the Google Apps Script `onEdit` trigger automatically sends a webhook to the local server.
4. **State Sync**: The backend receives the webhook and updates the durable session record.
5. **ADA Final Poll**: The next time ADA polls this action, the `status` changes from `processing` or `pending_human_review` to the human's final decision, and the `notes` field is populated.

---

## 4. Chat Workflow Logic

To make the automation feel seamless for the player:

1. **Call Action 1**: When the player requests a withdrawal.
2. **Wait Step**: Add a "Wait" block or a small delay.
3. **Call Action 2**: Periodically poll for the status.
4. **Condition**:
   - If `withdrawal_status` is `processing` or `pending_human_review`, loop back to a wait step.
   - If `withdrawal_status` is `approved` or `rejected`, show the `withdrawal_notes` to the player.

## 5. API Status Reference

Below is the complete breakdown of all API statuses returned by the system:

### Core Workflow Endpoints
*   **`POST /hitl/v1/request_review`**
    *   `processing`: Returned instantly. The request was received and a sequence `session_id` was generated.
*   **`GET /hitl/v1/status/session/{session_id}`** (ADA Polling)
    *   `processing`: Request stored durably and waiting for a review worker to write to Google Sheets.
    *   `pending_human_review`: Row successfully appended; system is waiting for the human reviewer to enter a decision.
    *   `approved`: Human reviewer entered "Yes" (Column I) and Notes (Column J).
    *   `rejected`: Human reviewer entered anything other than "Yes" in Column I and Notes (Column J).
    *   `error`: The review job failed to write to Google Sheets.
    *   `not_found`: `session_id` does not exist or has expired from retention cleanup.
*   **`GET /hitl/v1/status/{player_id}/{row_number}`** (Legacy Polling)
    *   Same statuses as above, plus `not_found` if the lookup fails.

### Webhook Endpoint
*   **`POST /webhook`** (Apps Script to Backend)
    *   `finalized`: Confirms the Apps Script payload was received and parsed.

### System & Admin Endpoints
*   **`GET /health`**: Returns `ok` when server is alive.
*   **`GET /sessions`**: Provides paginated persistent session state for admin inspection.

---

## 6. Troubleshooting

### Common Issues

| Symptom                                        | Likely Cause                               | Fix                                                                   |
| ---------------------------------------------- | ------------------------------------------ | --------------------------------------------------------------------- |
| `❌ Missing required environment variables`  | `.env` file is incomplete                | Copy `.env.example` → `.env` and fill in values                  |
| `HTTP 403` on Sheet write                    | Service account doesn't have Editor access | Share the Google Sheet with the service account email as Editor       |
| `HTTP 404` on Sheet write                    | Wrong `SPREADSHEET_ID` or `SHEET_NAME` | Double-check both values in `.env`                                  |
| Webhook never fires                          | Apps Script triggers not set up            | Create installable triggers in Apps Script → Triggers                |
| Webhook fires but 404                          | Session expired or wrong webhook URL       | Usually means retention cleanup or a bad target URL; verify the active backend instance |
| `⛔ WEBHOOK_URL is still set to the default` | Forgot to update Apps Script               | Replace `testurl.com` with your actual ngrok URL                    |
| `ErrorLog` sheet has entries                 | All 3 webhook retries failed               | Check server is running; manually replay the decision                 |
| Timestamp not appearing in Col B               | Backend write failed before append         | Check server logs and `/metrics` for review job failures |
| Status remains `pending_human_review` for too long | Webhook did not arrive yet or human review is incomplete | Ensure both Decision and Notes are filled; the status endpoint only reconciles pending rows periodically to protect Sheets quota |
| `pending_human_review` status never changes | Notes (Col J) is empty or the human decision is incomplete | Both Decision AND Notes must be filled for the webhook to fire        |

### Server Logs to Look For

| Log Message                                          | Meaning                                     |
| ---------------------------------------------------- | ------------------------------------------- |
| `Durable HITL Payment Automation starting`        | Server booted successfully                  |
| `ADA Request via Chatbot: player=...`             | ADA chatbot triggered a withdrawal          |
| `Review job started`                              | A worker claimed a queued Sheets write      |
| `Sheet row appended at row ...`                   | The row was successfully inserted into Google Sheets |
| `Webhook received: session=... decision=...`      | Human decision received from Apps Script    |

---

## 7. Testing Checklist

Use this checklist before your first end-to-end demonstration:

- [ ] Server starts without errors: `python main.py`
- [ ] Health check returns OK: `GET /health`
- [ ] ngrok tunnel is active and URL updated in Apps Script
- [ ] Google Sheet is shared with the service account email (Editor role)
- [ ] Apps Script has the `onEdit` installable trigger
- [ ] Column B is formatted as **Plain Text** (Format → Number → Plain Text)
- [ ] Single request test: `POST /hitl/v1/request_review` returns `status: processing` and `session_id`
- [ ] Sheet shows new row with Player ID, Name, and Channel
- [ ] Timestamp is written automatically in Column B by the backend append in GMT+2
- [ ] Typing Decision + Notes fires webhook (check server logs for `Webhook received`)
- [ ] Poll status returns `"status": "approved"` with full `row_data`
- [ ] Concurrency test passes: `python test_concurrent.py --count 10`

---

## 8. Verification

To ensure everything is working correctly:

1. Run `python main.py` on your local machine.
2. Trigger the flow via the ADA chatbot.
3. Observe the server logs—you should see:
  - `ADA Request via Chatbot: player=...`
  - `Review job started`
  - `Sheet row appended at row ...`
4. Edit the Google Sheet (Columns I & J) and verify the chat updates.
5. Check the `/metrics` endpoint for request counts and success rates.
