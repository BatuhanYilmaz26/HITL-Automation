"""
test_concurrent.py — Concurrency stress test for the HITL Payment Automation.

Fires multiple POST requests to /ada/v1/request_review and verifies
all sessions land correctly.

Supports two modes:
  - burst:     all requests fire simultaneously (may hit LLM rate limits)
  - staggered: fires in batches with a pause between batches (respects rate limits)

Usage:
    python test_concurrent.py                                  # 15 requests, staggered
    python test_concurrent.py --count 20                       # 20 requests, staggered
    python test_concurrent.py --mode burst                     # all at once
    python test_concurrent.py --batch-size 4 --batch-delay 15  # 4 per batch, 15s gap
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field

import aiohttp


# ── Configuration ────────────────────────────────────────────────────

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_COUNT = 15
DEFAULT_BATCH_SIZE = 4       # Gemini free tier = 5 RPM: stay safe with 4
DEFAULT_BATCH_DELAY = 65     # seconds between batches (to reset the 1-min window)
REQUEST_TIMEOUT = 180        # seconds per request (LLM + Sheets can be slow)


@dataclass
class TestResult:
    player_id: str
    status_code: int = 0
    body: dict = field(default_factory=dict)
    error: str = ""
    elapsed_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status_code == 200 and not self.error


# ── Core HTTP helpers ────────────────────────────────────────────────


async def fire_request(
    session: aiohttp.ClientSession,
    base_url: str,
    player_id: str,
) -> TestResult:
    """Send a single POST /ada/v1/request_review and return the result."""
    url = f"{base_url}/ada/v1/request_review"
    result = TestResult(player_id=player_id)
    t0 = time.perf_counter()
    try:
        async with session.post(
            url,
            json={"player_id": player_id},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            result.status_code = resp.status
            result.body = await resp.json()
    except Exception as exc:
        result.error = str(exc)
    result.elapsed_ms = (time.perf_counter() - t0) * 1000
    return result


async def poll_status(
    session: aiohttp.ClientSession,
    base_url: str,
    player_id: str,
) -> dict:
    """Poll GET /ada/v1/status/{player_id}."""
    url = f"{base_url}/ada/v1/status/{player_id}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            return await resp.json()
    except Exception as exc:
        return {"player_id": player_id, "error": str(exc)}


async def get_sessions(
    session: aiohttp.ClientSession,
    base_url: str,
) -> dict:
    """Call GET /sessions."""
    url = f"{base_url}/sessions"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            return await resp.json()
    except Exception as exc:
        return {"error": str(exc)}


# ── Batch helpers ────────────────────────────────────────────────────


async def fire_batch(
    session: aiohttp.ClientSession,
    base_url: str,
    player_ids: list[str],
) -> list[TestResult]:
    """Fire a batch of requests concurrently and return results."""
    tasks = [fire_request(session, base_url, pid) for pid in player_ids]
    raw = await asyncio.gather(*tasks, return_exceptions=True)
    results: list[TestResult] = []
    for i, r in enumerate(raw):
        if isinstance(r, BaseException):
            results.append(TestResult(player_id=player_ids[i], error=str(r)))
        else:
            results.append(r)
    return results


# ── Main test runner ─────────────────────────────────────────────────


async def run_test(
    base_url: str,
    count: int,
    mode: str,
    batch_size: int,
    batch_delay: float,
) -> None:
    """Main test runner."""

    player_ids = [f"PTEST{i:03d}" for i in range(1, count + 1)]

    print("=" * 70)
    print("  HITL Concurrency Stress Test")
    print(f"  Target : {base_url}")
    print(f"  Players: {count} requests ({player_ids[0]} – {player_ids[-1]})")
    print(f"  Mode   : {mode}" + (f" (batch={batch_size}, delay={batch_delay}s)" if mode == "staggered" else ""))
    print("=" * 70)
    print()

    all_results: list[TestResult] = []

    async with aiohttp.ClientSession() as session:
        t_start = time.perf_counter()

        if mode == "burst":
            # ── Burst mode: all at once ──────────────────────────────
            print(f"🚀 Phase 1: Firing ALL {count} requests simultaneously ...")
            print()
            all_results = await fire_batch(session, base_url, player_ids)
        else:
            # ── Staggered mode: batches with delay ───────────────────
            batches = [player_ids[i:i + batch_size] for i in range(0, len(player_ids), batch_size)]
            total_batches = len(batches)
            print(f"🚀 Phase 1: Firing {count} requests in {total_batches} batches of ≤{batch_size} ...")
            print()

            for batch_num, batch in enumerate(batches, 1):
                print(f"  📦 Batch {batch_num}/{total_batches}: {', '.join(batch)}")
                batch_results = await fire_batch(session, base_url, batch)

                for r in batch_results:
                    status_str = f"{r.status_code}" if r.status_code else "ERR"
                    body_str = r.body.get("status", r.error)[:40] if not r.error else r.error[:40]
                    marker = "✅" if r.ok else "❌"
                    print(f"     {marker} {r.player_id:<10} HTTP {status_str:<5} {r.elapsed_ms:>8.0f} ms  {body_str}")

                all_results.extend(batch_results)

                if batch_num < total_batches:
                    print(f"  ⏳ Waiting {batch_delay}s before next batch (rate limit cooldown) ...")
                    await asyncio.sleep(batch_delay)
                print()

        t_total = (time.perf_counter() - t_start) * 1000

        # ── Summary ──────────────────────────────────────────────────
        successes = [r for r in all_results if r.ok]
        failures = [r for r in all_results if not r.ok]

        if mode == "burst":
            print(f"{'Player ID':<12} {'Status':<8} {'Time (ms)':<12} {'Response'}")
            print("-" * 70)
            for r in all_results:
                status_str = f"{r.status_code}" if r.status_code else "ERR"
                body_str = r.body.get("status", r.error)[:45] if not r.error else r.error[:45]
                marker = "✅" if r.ok else "❌"
                print(f"{marker} {r.player_id:<10} {status_str:<8} {r.elapsed_ms:>8.0f} ms   {body_str}")
            print()

        print(f"  Total wall time : {t_total:,.0f} ms ({t_total / 1000:.1f}s)")
        print(f"  Succeeded       : {len(successes)} / {count}")
        print(f"  Failed          : {len(failures)} / {count}")
        print()

        if failures:
            print("⚠️  FAILURES:")
            for r in failures:
                print(f"   {r.player_id}: HTTP {r.status_code} — {r.error or r.body}")
            print()

        # ── Phase 2: Poll status ─────────────────────────────────────
        print(f"🔍 Phase 2: Polling /ada/v1/status for each player ...")
        print()

        statuses = await asyncio.gather(
            *[poll_status(session, base_url, pid) for pid in player_ids]
        )

        pending_count = 0
        for s in statuses:
            decision = s.get("decision", "?")
            marker = "⏳" if decision == "pending" else ("✅" if decision in ("Yes", "No") else "❓")
            if decision == "pending":
                pending_count += 1
            print(f"  {marker} {s.get('player_id', '?'):<12} → {decision}")

        print()
        print(f"  Pending : {pending_count} / {count}")
        print()

        # ── Phase 3: Check sessions ──────────────────────────────────
        print("📋 Phase 3: Fetching /sessions ...")
        sessions_data = await get_sessions(session, base_url)
        ses_count = sessions_data.get("pending_count", "?")
        ses_ids = sessions_data.get("session_ids", [])
        print(f"  Pending sessions on server: {ses_count}")
        if ses_ids:
            for sid in ses_ids:
                print(f"    • {sid}")
        print()

    # ── Final verdict ────────────────────────────────────────────────
    print("=" * 70)
    if len(successes) == count and pending_count == count:
        print("✅ ALL TESTS PASSED — All requests succeeded and are pending review.")
    elif len(successes) == count:
        print("⚠️  All requests succeeded but some statuses are unexpected.")
    else:
        print(f"❌ TEST FAILED — {len(failures)} request(s) did not succeed.")
    print("=" * 70)


# ── CLI entry-point ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="HITL Concurrency Stress Test")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Server base URL")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT, help="Number of requests")
    parser.add_argument(
        "--mode", choices=["burst", "staggered"], default="staggered",
        help="burst = all at once; staggered = batches with delay (default)",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Requests per batch (staggered mode)")
    parser.add_argument("--batch-delay", type=float, default=DEFAULT_BATCH_DELAY, help="Seconds between batches")
    args = parser.parse_args()

    asyncio.run(run_test(args.base_url, args.count, args.mode, args.batch_size, args.batch_delay))


if __name__ == "__main__":
    main()
