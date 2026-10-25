# Design: Conversation service survives a slow or down Postgres (#187, #188, #189)

## Approach
Contain every Postgres failure at the one function that talks to the DB, so callers never see an exception they were not built for. The heartbeat loop becomes a module-level coroutine in `__main__.py` that runs each step under `asyncio.wait_for` and catches `Exception` (never `CancelledError`) per step. `ToolPolicyResolver.enabled_tools` runs its whole DB path (both `tenant_conn` acquires and both fetches) under one `wait_for`. On failure it returns the last cached list for that `(agent_id, tenant_slug)`, stale or not, or `[]`, always through `_narrow`, and arms a per-key backoff. This one change also fixes "new call refused while config is cached": the refusal came from `enabled_tools` raising at `__main__.py:375` out of `handler_factory`. That call sits at `servicer.py:147`, outside the stream's `try`, so gRPC sent the peer the raw driver text. The servicer gets fixed peer messages on every generic path. I chose this over wrapping each caller in its own try/timeout: there are two callers today, and per-caller handling would leave the fail-closed and narrowing rules duplicated, with one copy free to drift.

## Changes
| File | Change | Why |
|---|---|---|
| `services/conversation/__main__.py` | Move the nested `_heartbeat_loop` (lines 508-516) to module level as `_run_heartbeat_loop`. Module constants: `HEARTBEAT_INTERVAL_S = 15`, `REAP_WINDOW_S = 3 * HEARTBEAT_INTERVAL_S`, `INACTIVE_CALL_TIMEOUT_S = 300`, `HEARTBEAT_STEP_TIMEOUT_S = 5`, `HEARTBEAT_RETRY_BACKOFF_S = 2`. Add a helper `_bounded_step`. `serve()` spawns `_run_heartbeat_loop(transcripts)` through the existing `_spawn`. Shutdown stays as it is (`task.cancel()` + `_await_stopped`). | Criteria 1-4, 19, 20, 24. Module level so `tests/test_startup_load.py`-style tests can import it, the same way they import `_spawn` now. |
| `services/conversation/transcript_builder.py` | `heartbeat()` (lines 102-114): remove the internal `try/except Exception` so a failed write raises to the loop. Keep the no-op return for no pool or no node id. | Today the error is swallowed, so the loop cannot tell a failed write from a good one. Criteria 1 and 24 need that signal. The loop becomes the only place that logs it. |
| `services/conversation/tools/policy_resolver.py` | Move lines 80-119 into `_fetch()`. `enabled_tools` calls it under `asyncio.wait_for(timeout=_LOOKUP_TIMEOUT_S)`. Add `_LOOKUP_TIMEOUT_S = 2.0`, `_FALLBACK_BACKOFF_S = 30.0`, and `self._backoff_until: dict[tuple[str, str], float]`. Add the fallback branch and the backoff short-circuit, both returning `_narrow(...)`. | Criteria 7-13, 17, 18, 21-23. |
| `services/conversation/servicer.py` | Add module constants `_INTERNAL_MESSAGE = "internal error"` and `_REJECTED_MESSAGE = "call rejected"`. Add `except Exception` after `except AgentUnavailable` at line 148: `log.exception`, yield `ServiceError(code="INTERNAL", message=_INTERNAL_MESSAGE, fatal=True)`, then `return`. Lines 668-682: replace `message=str(exc)` with these constants. `log.info`/`log.exception` keep the full exception. | Criteria 6, 14-16. Same pattern as the existing `AGENT_UNAVAILABLE`/`"agent unavailable"` branch at `servicer.py:148-156`. |
| `services/conversation/tests/test_heartbeat_loop.py` (new) | Loop tests (see Test plan). | |
| `services/conversation/tests/test_policy_resolver_outage.py` (new) | Resolver timeout, fallback, backoff, narrowing and tenancy tests. | |
| `services/conversation/tests/test_servicer_agent_unavailable.py` | Add cases for factory-raise and mid-call raise leaks, and a `RejectedCallHangup` case. | Already holds the stream/factory harness that asserts `(code, message, fatal)`. |
| `services/conversation/tests/test_transcript_builder.py` | Add a case: `heartbeat()` raises when the pool is closed. | Pins the new contract that the loop depends on. |

## Data
None.

## Interfaces
- `__main__._run_heartbeat_loop(transcripts: TranscriptBuilder, *, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None`. Runs forever and exits only on cancel. `clock`/`sleep` are injected so tests can drive a 60 s outage with the deployed constants (lesson 25). Constants are never parameters.
  - One iteration:
    1. `ok = await _bounded_step("heartbeat", transcripts.heartbeat())`.
    2. If not ok: `healthy_since = None`, `await sleep(HEARTBEAT_RETRY_BACKOFF_S)`, `continue`. Reconciles are skipped for that attempt.
    3. If ok and `healthy_since is None`: `healthy_since = clock()`.
    4. Run `reconcile_dead_nodes(stale_after_seconds=REAP_WINDOW_S)` only if `clock() - healthy_since >= REAP_WINDOW_S`, via `_bounded_step`.
    5. Always run `reconcile_inactive_calls` via `_bounded_step`.
    6. `await sleep(HEARTBEAT_INTERVAL_S)`.
  - `healthy_since` starts at `-inf`, so a node that has never failed reconciles on its first iteration, exactly as today. Criterion 24 applies only after a failed write.
  - State is held in locals of the single loop task. No second writer.
- `__main__._bounded_step(name: str, coro: Awaitable[object]) -> bool`
  - Returns True on success.
  - On `TimeoutError`: logs a WARNING with the step name and timeout, returns False.
  - On `Exception`: `log.exception` with the step name (server log, full detail allowed by criterion 16), returns False.
  - `CancelledError` is a `BaseException` on Python 3.11+ (`pyproject.toml` requires >=3.11), so it propagates and shutdown stays prompt (criterion 4).
- `ToolPolicyResolver.enabled_tools(agent_id, tenant_slug, only=None) -> list[ResolvedToolPolicy]`. Signature unchanged. Order:
  1. Existing no-pool/no-agent guard.
  2. Fresh cache hit (unchanged, line 77).
  3. If `time.monotonic() < self._backoff_until.get(key, 0)`, return `_narrow(stale_or_empty, only)` with no DB call and no log.
  4. `await asyncio.wait_for(self._fetch(agent_id, tenant_slug), _LOOKUP_TIMEOUT_S)`.
  5. On success: write the cache (unchanged, line 122), `self._backoff_until.pop(key, None)`, return `_narrow`.
  6. `except TenantUnresolved: raise`. The existing contract in the docstring at line 71 stays: a tenancy bug is not an outage.
  7. `except (TimeoutError, Exception) as exc`:
     - Set `self._backoff_until[key] = now + _FALLBACK_BACKOFF_S`.
     - Log one `log.warning("ToolPolicyResolver: policy lookup fell back agent_id=%s tenant=%s reason=%s using=%s", ...)`. `reason` is `"timeout"` or `type(exc).__name__`, never `str(exc)` and never `exc_info`, so no DSN, host or credentials (criterion 18). `using` is `"stale-cache"` or `"no-tools"`.
     - Return `_narrow(cached[1] if cached else [], only)`. The fallback is never written to `self._cache` (criterion 13).
- `ToolPolicyResolver._fetch(agent_id: str, tenant_slug: str) -> list[ResolvedToolPolicy]` holds lines 80-119 verbatim: the full, unnarrowed, specialized list. Tenancy is unchanged: `tenant_conn(explicit_tenant=tenant_slug, reason="conversation-tool-policy")`, and the explicit `tpc.tenant_id = a.tenant_id` / `a.tenant_id = ca.tenant_id` predicates stay (lesson 36).
- Fallback trace through every caller of `enabled_tools` (lesson 49):
  - `__main__.py:375`: recovery happens inside `enabled_tools` (step 7). The caller receives a list. `[]` gives `has_booking_tool=False`, and the call proceeds conversation-only and greets (criteria 5, 8). Anything else escaping `handler_factory` is caught by the new `servicer.py:148` generic branch.
  - `orchestrator.py:125`: same recovery (step 7). `policies` is a list, so `taint_refused` filtering and schema building run unchanged. `[]` matches the fail-closed shape at `orchestrator.py:122`.
  - Teardown (criterion 12) awaits at most an in-flight turn. That turn's only DB wait is the bounded `enabled_tools`, at most 2.0 s plus scheduling, or 0 s inside the backoff window. No teardown path calls `enabled_tools`.
- Peer error contract: `INTERNAL` / `"internal error"` and `CALL_REJECTED` / `"call rejected"`. Codes are unchanged, so gateway handling keyed on the code is unaffected.

## Risks
- Criterion 6 (uncached config during an outage) is interpreted as follows. A routed call already gets the curated `RejectionHandler(UNAVAILABLE)` at `__main__.py:412-417`, because `agent_resolver.py` turns `ConfigUnavailableError` into `None`, and that path is kept. Only an exception that escapes the factory gets the generic `INTERNAL` message. The mitigation is a test asserting neither path carries exception text; if the PRD meant to replace the spoken refusal with `INTERNAL`, the critic should flag it.
- Removing the swallow in `TranscriptBuilder.heartbeat()` changes its contract. Mitigation: its only caller is the heartbeat loop (`__main__.py:511`), which now catches every failure, and a new test pins "raises on DB error, no-op without a node id".
- `wait_for` cancelling an in-flight `conn.fetch` or pool acquire could leave a connection in a bad state. Mitigation: asyncpg's pool resets or terminates a connection released mid-query on cancel, and `tenant_conn`'s `async with` releases on `CancelledError`. No new pool or reconnect logic (out of scope).
- Concurrent lookups for one key during an outage may each wait the full 2.0 s before the first one arms the backoff. Mitigation: the bound is per lookup, so no turn ever exceeds 2.0 s, and after the first failure everything returns at once. Single-flight was skipped as extra machinery the PRD does not need.
- A stale cached policy can offer a tool that an admin disabled during the outage. Mitigation: the PRD accepts this (stale over none). The window is bounded: the first lookup 30 s after the fallback refetches and overwrites it.
- `_backoff_until` grows by one entry per distinct (agent, tenant) that has fallen back. Mitigation: entries are popped on the next successful fetch. Size is bounded by the agents served, the same bound as the existing unevicted `_cache`.
- `_LOOKUP_TIMEOUT_S = 2.0` (not 3.0). Mitigation: a timed-out turn then stays near the ~2.1 s turn latency from #133 rather than doubling it. Normal lookups are tens of ms, so 2.0 s does not false-trip.
- Cross-node reaping right after recovery. Mitigation: `reconcile_dead_nodes` waits for `REAP_WINDOW_S` of successful writes after any failed write (criterion 24). A peer still in a 5 s timeout plus 2 s backoff has written well within that 45 s.

## Test plan
Unit tests with fakes. Each states what would make it fail (lesson 12). All use the deployed constants; time is driven by injected `clock`/`sleep` or a monkeypatched `policy_resolver.time.monotonic`, never by shrinking a constant (lessons 20 and 25).

1. **Heartbeat loop: outage and recovery, two nodes** (`test_heartbeat_loop.py`).
   - Setup: a fake transcripts object over a virtual clock. `heartbeat` raises `ConnectionRefusedError` for a 60 s window, and `reconcile_dead_nodes` raises once. A fake heartbeat table records node A's writes and a peer B that starts writing at recovery + 7 s.
   - Assert: the loop never exits. The retry gap during the outage is `HEARTBEAT_RETRY_BACKOFF_S` and not 15 s. The first successful write is no more than 7 s after recovery. `reconcile_dead_nodes` is first called at least `REAP_WINDOW_S` after A's first successful write, and B is never reaped. `reconcile_inactive_calls` still runs in the iteration where `reconcile_dead_nodes` raised.
   - Fails if: reconcile runs on the first successful write, the loop dies, or the 15 s sleep is used for retry.
2. **Heartbeat loop: hang and cancel.**
   - A real `wait_for` hang test: `heartbeat` awaits a never-set Event. Assert it returns False after 5 s real time, logs a timeout, and the next attempt runs.
   - A constants tripwire: `HEARTBEAT_STEP_TIMEOUT_S <= 5`, `HEARTBEAT_RETRY_BACKOFF_S <= 2`, and `2*HEARTBEAT_STEP_TIMEOUT_S + HEARTBEAT_INTERVAL_S + HEARTBEAT_STEP_TIMEOUT_S <= 2*HEARTBEAT_INTERVAL_S`.
   - Cancel the loop task during a failing iteration: assert `task.cancelled()` within 0.5 s.
   - Fails if: `CancelledError` is caught, or a constant drifts past the budget.
3. **Resolver bound on every stall point** (`test_policy_resolver_outage.py`).
   - Monkeypatch `policy_resolver.tenant_conn` with a fake for each of three cases: (a) the acquire hangs; (b) the first fetch hangs; (c) the first fetch returns an `execute_api` row and the specialization fetch hangs.
   - Assert each `enabled_tools` call returns within `_LOOKUP_TIMEOUT_S + 0.5` s and returns `[]` with no cache, or the stale list with a cache. `_LOOKUP_TIMEOUT_S <= 3`.
   - Fails if: any fetch sits outside the `wait_for`.
4. **Fallback semantics.**
   - Seed a cached entry, then expire it via monotonic.
   - With the DB failing (`OSError("connect to 10.0.0.5:5432 postgresql://u:p@h/db")`): `only=None` returns the full stale list, `only=[]` returns `[]`, and `only=["x"]` returns a subset of `["x"]`. This holds both on the fallback call and on an in-window call.
   - In-window calls make zero `tenant_conn` entries and log no additional WARNING.
   - Exactly one WARNING is logged, and it contains neither `10.0.0.5`, `5432` nor `postgresql://`.
   - At window + 1 s with the DB healthy: one fetch happens, the cache is refreshed and the backoff entry cleared.
   - Fails if: any return path skips `_narrow`, the fallback is cached as fresh, or the DB is hit inside the window.
5. **Tenant isolation and peer leak.**
   - Cache tenant A's policy for `agent_id=X` and fail the DB. Tenant B / `X` gets `[]`, and B's lookup is not short-circuited by A's backoff (B still attempts the DB). Fails if the key drops the tenant.
   - In `test_servicer_agent_unavailable.py`: a factory raising, and a mid-call handler raising, the same host/port/DSN exception both yield `("INTERNAL", "internal error", True)`, and the full exception appears in `caplog`. A `RejectedCallHangup("... session=abc")` yields `("CALL_REJECTED", "call rejected", True)`. A factory using a resolver whose DB raises still yields `ServiceReady` and the greeting (criterion 5).
   - Fails if: any peer message contains the exception text.
