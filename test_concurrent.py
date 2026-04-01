"""
test_concurrent.py — Concurrency stress test for the HITL Payment Automation.

Fires multiple POST requests to /hitl/v1/request_review and verifies
all sessions land correctly.

Supports two modes:
  - burst:     all requests fire simultaneously
  - staggered: fires in batches with a pause between batches

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
DEFAULT_BATCH_SIZE = 10      # requests per batch
DEFAULT_BATCH_DELAY = 10     # seconds between batches
REQUEST_TIMEOUT = 180        # seconds per request


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
    index: int = 0,
) -> TestResult:
    """Send a single POST /hitl/v1/request_review and return the result."""
    url = f"{base_url}/hitl/v1/request_review"
    result = TestResult(player_id=player_id)
    t0 = time.perf_counter()

    # Alternate between Chat and Email for testing
    channel = "Chat" if index % 2 == 0 else "Email"

    try:
        async with session.post(
            url,
            json={
                "player_id": player_id,
                "player_name": f"Tester-{player_id}",
                "channel": channel,
            },
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
    session_id: str,
) -> dict:
    """Poll GET /hitl/v1/status/session/{session_id}."""
    url = f"{base_url}/hitl/v1/status/session/{session_id}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            return await resp.json()
    except Exception as exc:
        return {"session_id": session_id, "error": str(exc)}


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


# ── Pre-flight check ────────────────────────────────────────────────


async def preflight_check(base_url: str) -> bool:
    """Verify the server is reachable before starting the test suite."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{base_url}/health",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    print(f"  ✅ Server reachable — status={data.get('status')}, "
                          f"tracked={data.get('tracked_sessions')}")
                    return True
                else:
                    print(f"  ❌ Server returned HTTP {resp.status}")
                    return False
    except Exception as exc:
        print(f"  ❌ Cannot reach server at {base_url}: {exc}")
        return False


# ── Batch helpers ────────────────────────────────────────────────────


async def fire_batch(
    session: aiohttp.ClientSession,
    base_url: str,
    player_ids: list[str],
    start_index: int = 0,
) -> list[TestResult]:
    """Fire a batch of requests concurrently and return results."""
    tasks = [fire_request(session, base_url, pid, start_index + i) for i, pid in enumerate(player_ids)]
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
    duplicate_player: str | None = None,
) -> None:
    """Main test runner."""

    if duplicate_player:
        player_ids = [duplicate_player] * count
    else:
        player_ids = [f"PTEST{i:03d}" for i in range(1, count + 1)]

    print("=" * 70)
    print("  HITL Concurrency Stress Test")
    print(f"  Target : {base_url}")
    print(f"  Players: {count} requests ({player_ids[0]} – {player_ids[-1]})")
    print(f"  Mode   : {mode}" + (f" (batch={batch_size}, delay={batch_delay}s)" if mode == "staggered" else ""))
    print("=" * 70)
    print()

    # Pre-flight: ensure server is alive
    print("🔍 Pre-flight check …")
    if not await preflight_check(base_url):
        print("\n❌ Aborting — server is not reachable. Start it with: python main.py")
        sys.exit(1)
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

            for batch_num, batch in enumerate(batches, 0):
                print(f"  📦 Batch {batch_num + 1}/{total_batches}: {', '.join(batch)}")
                batch_results = await fire_batch(session, base_url, batch, batch_num * batch_size)

                for r in batch_results:
                    status_str = f"{r.status_code}" if r.status_code else "ERR"
                    body_str = r.body.get("status", r.error)[:40] if not r.error else r.error[:40]
                    marker = "✅" if r.ok else "❌"
                    print(f"     {marker} {r.player_id:<10} HTTP {status_str:<5} {r.elapsed_ms:>8.0f} ms  {body_str}")

                all_results.extend(batch_results)

                # Only delay between batches, not after the last one
                if batch_num < total_batches - 1:
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
        print(f"🔍 Phase 2: Polling /hitl/v1/status for each player ...")
        print()

        poll_tasks = []
        for r in all_results:
            if r.ok:
                session_id = r.body.get("session_id")
                poll_tasks.append(poll_status(session, base_url, session_id))

        statuses = await asyncio.gather(*poll_tasks)

        pending_count = 0
        for s in statuses:
            decision = s.get("status", "?")
            marker = "⏳" if decision in ("processing", "pending_human_review") else ("✅" if decision in ("approved", "rejected") else "❓")
            if decision in ("processing", "pending_human_review"):
                pending_count += 1
            print(f"  {marker} {s.get('player_id', '?'):<12} → {decision} (row: {s.get('row_number', '?')})")

        print()
        print(f"  Pending : {pending_count} / {count}")
        print()

        # ── Phase 3: Check sessions ──────────────────────────────────
        print("📋 Phase 3: Fetching /sessions ...")
        sessions_data = await get_sessions(session, base_url)
        ses_count = sessions_data.get("count", "?")
        ses_data = sessions_data.get("sessions", {})
        print(f"  Tracked sessions on server: {ses_count}")
        if ses_data:
            for sid, stats in ses_data.items():
                print(f"    • {sid} ({stats.get('status')})")
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
    parser.add_argument("--duplicate", type=str, default=None, help="Test multiple requests for ONE player (e.g. --duplicate P100)")
    args = parser.parse_args()

    asyncio.run(run_test(args.base_url, args.count, args.mode, args.batch_size, args.batch_delay, args.duplicate))


if __name__ == "__main__":
    main()
