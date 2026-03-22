"""
agent.py — ADK Agent, Runner, and session management.

Creates the LLM agent with HITL-aware system instructions, manages
a shared InMemoryRunner, and provides start/resume functions for the
withdrawal workflow.

ALL withdrawals are escalated to human review — there is no auto-approve.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.genai import types

import config
from tools import human_approval_tool

logger = logging.getLogger(__name__)

# ── System instructions ──────────────────────────────────────────────

SYSTEM_INSTRUCTION = """\
You are the Payment Withdrawal Agent for a regulated gaming platform.

## Your Responsibility
Process every incoming withdrawal request by submitting it for
**mandatory human verification**.  No withdrawal is ever auto-approved.

## What To Do For Every Request
1. Receive the withdrawal details (session_id, player_id).
2. Immediately call `request_human_approval` with **both parameters**:
   session_id and player_id.
3. STOP and wait — a human reviewer will verify the request via the
   HITL dashboard.

## When You Receive a Human Decision
After a human reviews the withdrawal you will receive their decision.
Finalize accordingly:
- **Decision = "Yes"**  → Confirm: "Withdrawal for player
  [player_id] has been RELEASED.  Human notes: [notes]"
- **Decision = "No"**   → Reject: "Withdrawal for player
  [player_id] has been BLOCKED.  Human notes: [notes]"

## Important Rules
- ALWAYS call `request_human_approval` — every single withdrawal must
  be reviewed by a human.
- Always use the exact session_id provided in the withdrawal request.
- Never fabricate a decision — always wait for the human response.
- Be concise and professional in your replies.
- Include the player_id and decision in every
  final confirmation or rejection message.
"""

# ── Agent creation ───────────────────────────────────────────────────


def create_agent() -> LlmAgent:
    """Build and return the configured LLM agent."""
    return LlmAgent(
        model=config.MODEL_ID,
        name="payment_withdrawal_agent",
        instruction=SYSTEM_INSTRUCTION,
        tools=[human_approval_tool],
    )


# ── Shared runtime singletons ───────────────────────────────────────

agent = create_agent()

runner = InMemoryRunner(
    agent=agent,
    app_name=config.APP_NAME,
)

# Maps session_id → metadata needed to resume the agent after HITL.
# Each entry: {
#   "function_response": <types.FunctionResponse>,  # the pending tool response
#   "player_id": str,
# }
pending_sessions: dict[str, dict[str, Any]] = {}

# Maps player_id → dict with decision and notes. Helps ADA chatbot poll for results
player_status: dict[str, dict[str, str]] = {}

# ── Concurrency throttle ────────────────────────────────────────────
# Limits simultaneous LLM calls to prevent API rate exhaustion.
# Production (Vertex AI / Enterprise): 50 is safe for 1000+ RPM quota.
# Free tier: lower to 2-3 via LLM_CONCURRENCY_LIMIT env var.
_llm_semaphore = asyncio.Semaphore(config.LLM_CONCURRENCY_LIMIT)

# ── Retry settings ──────────────────────────────────────────────────
LLM_MAX_RETRIES = 3
LLM_RETRY_DELAYS = [15, 30, 60]  # seconds between retries


# ── Helper: extract long-running call/response from events ──────────


def _extract_long_running_function_call(
    event: Any,
) -> types.FunctionCall | None:
    """Return the FunctionCall if this event contains a long-running tool call."""
    if (
        not getattr(event, "long_running_tool_ids", None)
        or not event.content
        or not event.content.parts
    ):
        return None
    for part in event.content.parts:
        if (
            part
            and part.function_call
            and event.long_running_tool_ids
            and part.function_call.id in event.long_running_tool_ids
        ):
            return part.function_call
    return None


def _extract_function_response(
    event: Any,
    function_call_id: str,
) -> types.FunctionResponse | None:
    """Return the FunctionResponse matching *function_call_id*."""
    if not event.content or not event.content.parts:
        return None
    for part in event.content.parts:
        if (
            part
            and part.function_response
            and part.function_response.id == function_call_id
        ):
            return part.function_response
    return None


def _is_rate_limit_error(exc: Exception) -> bool:
    """Check if an exception is a rate-limit / quota error."""
    exc_str = str(exc)
    return "429" in exc_str or "RESOURCE_EXHAUSTED" in exc_str


# ── Public API ───────────────────────────────────────────────────────


async def start_withdrawal(
    session_id: str,
    player_id: str,
) -> dict[str, Any]:
    """
    Kick off a new withdrawal flow.

    Creates an ADK session, sends the initial prompt to the agent,
    and captures the pending FunctionResponse so the webhook can
    resume the flow later after human review.

    Every withdrawal is escalated — there is no auto-approve path.

    Uses a semaphore to throttle concurrent LLM calls, and retries
    on transient rate-limit errors.

    Returns a summary dict.
    """
    logger.info(
        "➡️  Starting withdrawal: session=%s player=%s",
        session_id, player_id,
    )

    player_status[player_id] = {"decision": "pending", "notes": "", "row_data": []}

    prompt = (
        f"Process withdrawal request:\n"
        f"- Session ID: {session_id}\n"
        f"- Player ID: {player_id}"
    )

    content = types.Content(
        role="user",
        parts=[types.Part(text=prompt)],
    )

    # Retry loop for LLM rate-limit (429) errors
    current_session_id = session_id

    for attempt in range(LLM_MAX_RETRIES + 1):
        # Create a fresh session for this attempt
        session = await runner.session_service.create_session(
            app_name=config.APP_NAME,
            user_id=config.USER_ID,
            session_id=current_session_id,
        )

        long_running_fc: types.FunctionCall | None = None
        long_running_fr: types.FunctionResponse | None = None
        agent_texts: list[str] = []

        try:
            # Semaphore throttles how many LLM calls run simultaneously
            async with _llm_semaphore:
                logger.debug(
                    "🔓 Semaphore acquired for session=%s (attempt %d)",
                    current_session_id, attempt + 1,
                )
                async for event in runner.run_async(
                    session_id=session.id,
                    user_id=config.USER_ID,
                    new_message=content,
                ):
                    # Try to capture the long-running function call
                    if not long_running_fc:
                        long_running_fc = _extract_long_running_function_call(event)
                    elif long_running_fc:
                        potential = _extract_function_response(event, long_running_fc.id)
                        if potential:
                            long_running_fr = potential

                    # Collect any text the agent emits
                    if event.content and event.content.parts:
                        for part in event.content.parts:
                            if part.text:
                                agent_texts.append(part.text)

            # If we got here without exception, break out of retry loop
            break

        except Exception as exc:
            if _is_rate_limit_error(exc) and attempt < LLM_MAX_RETRIES:
                delay = LLM_RETRY_DELAYS[attempt]
                logger.warning(
                    "⏳ LLM rate-limited (attempt %d/%d), retrying in %ds …",
                    attempt + 1, LLM_MAX_RETRIES + 1, delay,
                )
                await asyncio.sleep(delay)

                # Use a new session ID for the retry
                current_session_id = f"{session_id}-r{attempt + 1}"
                continue
            else:
                logger.exception("❌ LLM call failed for session %s", current_session_id)
                raise

    # Store the pending response for resumption via webhook
    if long_running_fr:
        pending_sessions[current_session_id] = {
            "function_response": long_running_fr,
            "player_id": player_id,
        }
        logger.info("⏸️  Session %s paused — awaiting human decision", current_session_id)
        return {
            "status": "pending_human_review",
            "session_id": current_session_id,
            "agent_message": " ".join(agent_texts) or "Submitted for human review.",
        }

    # Fallback — should not normally happen since every withdrawal is escalated
    logger.warning("⚠️  Session %s completed without HITL escalation", current_session_id)
    return {
        "status": "completed_unexpected",
        "session_id": current_session_id,
        "agent_message": " ".join(agent_texts) or "Withdrawal processed (no HITL).",
    }


async def resume_withdrawal(
    session_id: str,
    decision: str,
    notes: str,
    row_data: list[Any] | None = None,
) -> dict[str, Any]:
    """
    Resume a paused withdrawal after the human provides a decision.

    Looks up the stored FunctionResponse, clones it with the human's
    verdict, and feeds it back into the runner so the agent can
    finalize the transaction.

    Uses a semaphore to throttle concurrent LLM calls, and retries
    on transient rate-limit errors.

    Returns a summary dict.
    """
    logger.info(
        "▶️  Resuming session=%s decision=%s notes=%s",
        session_id, decision, notes,
    )

    session_data = pending_sessions.get(session_id)
    if not session_data:
        logger.warning("⚠️  No pending session found for %s", session_id)
        return {
            "status": "error",
            "message": f"No pending session found for session_id={session_id}",
        }

    original_fr: types.FunctionResponse = session_data["function_response"]

    # Build the updated FunctionResponse with the human decision
    updated_fr = original_fr.model_copy(deep=True)
    updated_fr.response = {
        "status": "approved" if decision.strip().lower() == "yes" else "rejected",
        "decision": decision,
        "notes": notes,
        "player_id": session_data["player_id"],
    }

    resume_content = types.Content(
        role="user",
        parts=[types.Part(function_response=updated_fr)],
    )

    agent_texts: list[str] = []

    # Retry loop for LLM rate-limit errors during resume
    for attempt in range(LLM_MAX_RETRIES + 1):
        try:
            async with _llm_semaphore:
                async for event in runner.run_async(
                    session_id=session_id,
                    user_id=config.USER_ID,
                    new_message=resume_content,
                ):
                    if event.content and event.content.parts:
                        for part in event.content.parts:
                            if part.text:
                                agent_texts.append(part.text)
            break  # Success

        except Exception as exc:
            if _is_rate_limit_error(exc) and attempt < LLM_MAX_RETRIES:
                delay = LLM_RETRY_DELAYS[attempt]
                logger.warning(
                    "⏳ Resume rate-limited (attempt %d/%d), retrying in %ds …",
                    attempt + 1, LLM_MAX_RETRIES + 1, delay,
                )
                await asyncio.sleep(delay)
                agent_texts.clear()  # Reset for clean retry
                continue
            else:
                logger.exception("❌ Resume LLM call failed for session %s", session_id)
                raise

    # Clean up
    del pending_sessions[session_id]
    player_status[session_data["player_id"]] = {
        "decision": decision,
        "notes": notes,
        "row_data": row_data or [],
    }
    final_message = " ".join(agent_texts) or "Transaction finalized."

    logger.info("🏁 Session %s finalized: %s", session_id, final_message[:120])
    return {
        "status": "finalized",
        "session_id": session_id,
        "decision": decision,
        "agent_message": final_message,
    }
