# Independent review: pg-outage-resilience (fresh context, full diff vs origin/redesign)

## Round 1 — AMBER
- [major] `wait_for` awaited the inner task's cancellation, and asyncpg's release can wait on a blackholed server with no timeout, so the 2 s / 5 s bounds could stretch to the TCP timeout. Fixed: `services/conversation/bounded.py` `run_bounded` (asyncio.wait, cancel without awaiting, a done-callback consumes the exception).
- [major] Concurrent lookups for one (agent, tenant) weren't single-flight: N DB attempts and N WARNINGs. Fixed: one in-flight task per key via `_inflight` and `asyncio.shield`.
- [major] The heartbeat recovery test could not fail (the outage raised instantly). Fixed: a real-time test with a hung write, asserting the gap is ≥ 5 s and ≤ 7.5 s.
- [minor] Stale fallback had no age limit. Fixed: `_STALE_MAX_AGE_S = 900`, after which the result is `[]`.
- [minor] No test for UNAVAILABLE rejection text. Added. [minor] Broad `pytest.raises(Exception)` narrowed to `asyncpg.InterfaceError`.

## Round 2 — GREEN
All six fixes verified. `pytest -q services/conversation`: 1436 passed.
Accepted minors:
- During a multi-minute blackhole, one abandoned (already cancelled) task per failed heartbeat or backoff window. The pool's max size caps lingering connections. A pool release or command timeout would be a possible follow-up.
- No explicit test that cancelling the first single-flight caller leaves the other waiters with the result. The code is correct by shield semantics.
- The `from .bounded import run_bounded` import is out of alphabetical order (cosmetic).

## Follow-up — closing the accepted minors (4 more rounds, ending GREEN)
- Pool slots held after a cancel on a blackholed DB. `command_timeout` alone didn't bound it (round AMBER). `tenant_conn`/`platform_conn` now take an opt-in `acquire_timeout`, which bounds asyncpg's release (AMBER: a cancel after BEGIN still hung in ROLLBACK). With the timeout set, a cancel during or after BEGIN now terminates the connection and still raises CancelledError. Passed only on `run_bounded` paths.
- One round went RED: a TimeoutError→CancelledError swap in `_acquire` misread every asyncpg timeout, because asyncpg chains an internal CancelledError onto each one. Reverted. A test now pins that an exhausted pool raises TimeoutError.
- Added: an in-process blackhole-Postgres test (cancel before and after BEGIN, a control case, an exhausted pool), a test for a cancelled single-flight caller, the import order fix, and test names that say what they check.
- Final: GREEN. Full suite minus the five Postgres-socket files: 5038 passed, 0 failed. Blackhole tests pass 3/3 runs.
