/**
 * apps_script.js — Google Apps Script for the HITL Dashboard Sheet.
 *
 * Paste this into Extensions → Apps Script in your Google Sheet.
 * It handles:
 *  1. onEdit(e): Watches for Human Decision in Column I and sends a webhook to backend.
 *  2. onChange(e): Optional no-op hook kept only for backward compatibility.
 *
 * IMPORTANT:
 *  1. Replace WEBHOOK_URL with your actual ngrok URL.
 *  2. If using WEBHOOK_SECRET, set it to match the .env value.
 *  3. This must be deployed as an "installable trigger"
 *     (Edit → Triggers → Add → onEdit) for UrlFetchApp to work.
 */

// ─── Configuration ──────────────────────────────────────────────────

const WEBHOOK_URL = "https://testurl.com/webhook";
const WEBHOOK_SECRET = "";  // Must match .env WEBHOOK_SECRET (leave "" to disable)

// Column indices (1-based)
const COL_SESSION_ID = 11;   // Column K (Hidden Session ID)
const COL_DECISION = 9;   // Column I
const COL_NOTES = 10;  // Column J
const COL_PAYLOAD_START = 2; // Column B
const COL_PAYLOAD_COUNT = 9; // Columns B through J

// Retry settings for webhook delivery
const WEBHOOK_MAX_RETRIES = 3;
const WEBHOOK_RETRY_DELAYS = [2000, 4000, 8000]; // milliseconds

// Dead-letter sheet name for failed webhook deliveries
const ERROR_LOG_SHEET = "ErrorLog";

// ─── Configuration Validation ───────────────────────────────────────

/**
 * Checks that WEBHOOK_URL has been changed from the default placeholder.
 * Returns true if configuration is valid.
 */
function _validateConfig() {
  if (!WEBHOOK_URL || WEBHOOK_URL === "https://testurl.com/webhook") {
    Logger.log(
      "⛔ WEBHOOK_URL is still set to the default placeholder. " +
      "Update it to your ngrok/Cloud Run URL before using triggers."
    );
    return false;
  }
  return true;
}

// ─── Dead-Letter Logging ────────────────────────────────────────────

/**
 * Logs a failed webhook delivery to a dedicated "ErrorLog" sheet tab.
 * Creates the tab automatically if it does not exist.
 *
 * @param {number} row - The sheet row that triggered the webhook.
 * @param {string} sessionId - The session ID from Column K.
 * @param {string} errorMsg - Description of the failure.
 */
function _logDeadLetter(row, sessionId, errorMsg) {
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var errorSheet = ss.getSheetByName(ERROR_LOG_SHEET);

    if (!errorSheet) {
      errorSheet = ss.insertSheet(ERROR_LOG_SHEET);
      errorSheet.appendRow(["Timestamp", "Row", "Session ID", "Error", "Webhook URL"]);
      errorSheet.getRange(1, 1, 1, 5).setFontWeight("bold");
    }

    var timestamp = Utilities.formatDate(
      new Date(), Session.getScriptTimeZone(), "yyyy-MM-dd, HH:mm:ss"
    );
    errorSheet.appendRow([timestamp, row, sessionId, errorMsg, WEBHOOK_URL]);
    Logger.log("📋 Dead-letter logged for row " + row + " (session=" + sessionId + ")");
  } catch (logErr) {
    Logger.log("⚠️ Failed to write dead-letter log: " + logErr.message);
  }
}

// ─── Triggers ───────────────────────────────────────────────────────

/**
 * Optional backward-compatible no-op hook.
 * Timestamping is now handled directly by the backend append call, which avoids
 * scanning the sheet on every insert.
 */
function onChange(e) {
  Logger.log("onChange hook invoked; timestamping is handled by the backend and no action is required.");
}

/**
 * Fires whenever any cell is edited by a user.
 * We act when Column I is edited (the human Decision).
 *
 * Includes retry logic: if the webhook fails, retries up to 3 times
 * with exponential backoff to prevent lost human decisions.
 *
 * @param {GoogleAppsScript.Events.SheetsOnEdit} e - The edit event.
 */
function onEdit(e) {
  try {
    // Pre-flight: ensure WEBHOOK_URL is configured
    if (!_validateConfig()) return;

    var range = e.range;
    var sheet = range.getSheet();
    var row = range.getRow();
    var col = range.getColumn();

    // Skip edits on the ErrorLog sheet
    if (sheet.getName() === ERROR_LOG_SHEET) return;

    // Trigger on Column I (Decision) or Column J (Notes) edits, skipping headers
    if ((col !== COL_DECISION && col !== COL_NOTES) || row <= 4) return;

    var sessionId = sheet.getRange(row, COL_SESSION_ID).getValue();
    var decision = sheet.getRange(row, COL_DECISION).getValue();
    var notes = sheet.getRange(row, COL_NOTES).getValue();

    // Guard: Both decision and notes must be filled before webhook is sent!
    if (!sessionId || !decision || !notes) {
      Logger.log("Waiting for both Decision and Notes to be filled. Skipping webhook.");
      return;
    }

    // Capture the operational payload columns (B through J). Column A is not
    // part of the integration contract and may contain sheet-specific helpers.
    var fullRowData = sheet.getRange(row, COL_PAYLOAD_START, 1, COL_PAYLOAD_COUNT).getValues()[0];

    // Convert Date objects to the spreadsheet timezone for stable ADA payloads.
    var scriptTimezone = Session.getScriptTimeZone();
    for (var i = 0; i < fullRowData.length; i++) {
      if (Object.prototype.toString.call(fullRowData[i]) === '[object Date]') {
        fullRowData[i] = Utilities.formatDate(fullRowData[i], scriptTimezone, "yyyy-MM-dd HH:mm:ss");
      }
    }

    // Build the JSON payload to include decision, notes, and the full row data array
    var payload = {
      session_id: String(sessionId),
      decision: String(decision),
      notes: String(notes),
      row_number: Number(row),
      row_data: fullRowData // Array of columns B(0) to J(8)
    };

    var options = {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify(payload),
      muteHttpExceptions: true,
    };

    // Add the shared secret header if configured
    if (WEBHOOK_SECRET) {
      options.headers = { "X-Webhook-Secret": WEBHOOK_SECRET };
    }

    // Retry loop for webhook delivery — ensures human decisions are never lost
    var success = false;
    var lastError = "";
    for (var attempt = 0; attempt < WEBHOOK_MAX_RETRIES; attempt++) {
      try {
        var response = UrlFetchApp.fetch(WEBHOOK_URL, options);
        var code = response.getResponseCode();
        var body = response.getContentText();

        if (code >= 200 && code < 300) {
          Logger.log("✅ Webhook success (HTTP " + code + "): " + body);
          success = true;
          break;
        } else if (code === 404) {
          // 404 = session not found, but the fallback correction logic in the
          // backend will handle it via player_id from row_data. Log and stop.
          Logger.log("⚠️ Webhook 404 (session not pending): " + body);
          success = true;
          break;
        } else {
          lastError = "HTTP " + code + ": " + body;
          Logger.log(
            "⚠️ Webhook attempt " + (attempt + 1) + "/" + WEBHOOK_MAX_RETRIES +
            " failed (" + lastError + ")"
          );
        }
      } catch (fetchErr) {
        lastError = fetchErr.message;
        Logger.log(
          "⚠️ Webhook attempt " + (attempt + 1) + "/" + WEBHOOK_MAX_RETRIES +
          " error: " + lastError
        );
      }

      // Wait before retrying (unless this was the last attempt)
      if (attempt < WEBHOOK_MAX_RETRIES - 1) {
        Utilities.sleep(WEBHOOK_RETRY_DELAYS[attempt]);
      }
    }

    if (!success) {
      var criticalMsg =
        "Webhook FAILED after " + WEBHOOK_MAX_RETRIES + " attempts for row " + row +
        " (session=" + sessionId + "). Last error: " + lastError;
      Logger.log("❌ CRITICAL: " + criticalMsg);

      // Write to dead-letter sheet so no human decision is ever silently lost
      _logDeadLetter(row, sessionId, criticalMsg);
    }

  } catch (err) {
    Logger.log("❌ onEdit error: " + err.message);
  }
}
