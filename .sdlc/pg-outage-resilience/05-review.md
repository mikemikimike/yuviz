# Code review
VERDICT: GREEN
Coverage: callers 5 checked · raises 3 traced · promises 9 matched · contracts 2 compared · failure-visibility done · env ok

Callers: `enabled_tools` at `__main__.py:426` and `orchestrator.py:125` both receive a list on every DB failure (only `TenantUnresolved` re-raises, as before). `TranscriptBuilder.heartbeat()` has one caller, `_run_heartbeat_loop`, which goes through `_bounded_step`. Nothing keys on the old `CALL_REJECTED`/`INTERNAL` message text (checked services, libs, scripts and admin-ui).
Raises: the `heartbeat()` raise is recovered at `__main__.py:_bounded_step`. A factory exception is recovered by the new generic branch at `servicer.py:162`. A mid-call exception is recovered at `servicer.py:688`.
Promises: wait_for covers both acquires and both fetches. Backoff short-circuits with no DB call or log. Every return goes through `_narrow`. The fallback is never cached. The log carries `reason=` with the class name only. The tenant is part of the key. Reaping is held off for REAP_WINDOW_S after a failed write. healthy_since starts at -inf. CancelledError propagates.
Contracts: peer error (code, message, fatal) is unchanged except the fixed message text. Resolver return type is unchanged.
Tests: 57 passed across the 4 scoped test files.
Criterion 6: the design's reading (routed uncached calls keep the curated RejectionHandler, and nothing escapes with raw text) meets PRD criterion 6. No drift.

No findings.
