/**
 * apps_script.js — Google Apps Script for the HITL Dashboard Sheet.
 *
 * Paste this into Extensions → Apps Script in your Google Sheet.
 * It handles:
 *  1. onChange(e): Watches for backend API inserts and adds a timestamp to Column B.
 *  2. onEdit(e): Watches for Human Decision in Column I and sends a webhook to backend.
 *
 * IMPORTANT:
 *  1. Replace WEBHOOK_URL with your actual ngrok URL.
 *  2. If using WEBHOOK_SECRET, set it to match the .env value.
 *  3. This must be deployed as an "installable trigger"
 *     (Edit → Triggers → Add → onEdit) for UrlFetchApp to work.
 *     You will ALSO need an installable trigger for onChange.
 */

// ─── Configuration ──────────────────────────────────────────────────

const WEBHOOK_URL = "https://your-ngrok-url.ngrok-free.app/webhook"; // Replace with your ngrok(webhook) URL
const WEBHOOK_SECRET = "";  // Must match .env WEBHOOK_SECRET (leave "" to disable)

// Column indices (1-based)
const COL_SESSION_ID = 1;   // Column A
const COL_DECISION = 9;   // Column I
const COL_NOTES = 10;  // Column J

// Retry settings for webhook delivery
const WEBHOOK_MAX_RETRIES = 3;
const WEBHOOK_RETRY_DELAYS = [2000, 4000, 8000]; // milliseconds

// ─── Triggers ───────────────────────────────────────────────────────

/**
 * Fires whenever the spreadsheet structure or content changes (including API edits).
 * Detects new Player ID insertions and adds timestamps.
 * Creates an Installable Trigger for "On change".
 */
function onChange(e) {
  try {
    var sheet = e.source.getActiveSheet();
    var lastRow = sheet.getLastRow();

    if (lastRow <= 4) return;

    var startRow = 5;
    var numRows = lastRow - startRow + 1;
    if (numRows < 1) return;

    // Range: from row `startRow`, 2nd column, for `numRows` rows, 2 columns wide (B and C)
    var range = sheet.getRange(startRow, 2, numRows, 2);
    var values = range.getValues();

    var timestamp = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), "yyyy-MM-dd HH:mm:ss");

    for (var i = 0; i < values.length; i++) {
      var rowNum = startRow + i;
      var bVal = values[i][0]; // Column B (Timestamp)
      var cVal = values[i][1]; // Column C (Player ID)

      // If Player ID exists but Timestamp is empty
      if (cVal && !bVal) {
        sheet.getRange(rowNum, 2).setValue(timestamp);
        Logger.log("Added timestamp for row " + rowNum);
      }
    }
  } catch (err) {
    Logger.log("onChange Error: " + err.message);
  }
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
    var range = e.range;
    var sheet = range.getSheet();
    var row = range.getRow();
    var col = range.getColumn();

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

    // Capture ALL column values (Columns A through J) for the ADA chatbot
    var numCols = 10;
    var fullRowData = sheet.getRange(row, 1, 1, numCols).getValues()[0];

    // Convert Date objects to GMT+1 strings so they don't get stringified to GMT (Z)
    for (var i = 0; i < fullRowData.length; i++) {
      if (Object.prototype.toString.call(fullRowData[i]) === '[object Date]') {
        fullRowData[i] = Utilities.formatDate(fullRowData[i], "GMT+01:00", "yyyy-MM-dd HH:mm:ss");
      }
    }

    // Build the JSON payload to include decision, notes, and the full row data array
    var payload = {
      session_id: String(sessionId),
      decision: String(decision),
      notes: String(notes),
      row_data: fullRowData // Array of all columns A(0) to J(9)
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
          Logger.log(
            "⚠️ Webhook attempt " + (attempt + 1) + "/" + WEBHOOK_MAX_RETRIES +
            " failed (HTTP " + code + "): " + body
          );
        }
      } catch (fetchErr) {
        Logger.log(
          "⚠️ Webhook attempt " + (attempt + 1) + "/" + WEBHOOK_MAX_RETRIES +
          " error: " + fetchErr.message
        );
      }

      // Wait before retrying (unless this was the last attempt)
      if (attempt < WEBHOOK_MAX_RETRIES - 1) {
        Utilities.sleep(WEBHOOK_RETRY_DELAYS[attempt]);
      }
    }

    if (!success) {
      Logger.log(
        "❌ CRITICAL: Webhook FAILED after " + WEBHOOK_MAX_RETRIES + " attempts for row " + row +
        " (session=" + sessionId + "). Human decision may be LOST!"
      );
    }

  } catch (err) {
    Logger.log("❌ onEdit error: " + err.message);
  }
}
