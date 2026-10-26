# Design: Filler docs match code; no ERROR flood for agents without a workflow

## Approach
Route the "no published graph" path through the existing `_GRAPH_CACHE` (`_cache_get`/`_cache_put` in `services/conversation/workflow/runner.py:223-240`), the same way the parse-failure path already caches its fallback. Today the empty-graph path skips the cache, so it logs and rebuilds the fallback on every call. After the change, a cache hit returns the stored starter graph silently. A miss logs once at INFO, builds the fallback and stores it. The once-only guard is therefore the cache entry itself. It is keyed by `_cache_key` (tenant, agent) and capped at `_GRAPH_CACHE_MAX = 256` with the existing eviction, so no second store, no new bound and no extra cleanup in `conftest.py`. This is better than a separate `_WARNED` set: a set would duplicate the cache's key, bound and lifecycle and still rebuild the fallback every turn. Docs: one timer, `_with_filler_timer(..., _filler_delay_s(), ...)` at `services/conversation/pipeline.py:1533-1535`, handles both the first-reply and tool fillers, so `docs/voice-latency.md` changes to name `VOICEAI_FILLER_DELAY_MS` with default 1200 for both.

## Changes
| File | Change | Why |
|---|---|---|
| `services/conversation/workflow/runner.py` | In `graph_for`'s `if not raw:` branch, when `not draft`: return `_cache_get(runtime_config)` if it is not None. Otherwise `log.info` the existing message, then build `_fallback_graph(runtime_config, raw=None)`, `_cache_put` it and return it. The `draft=True` empty path stays as it is: no log, no cache. The `WorkflowInvalid` `log.error` and the `log.exception` paths stay as they are. | AC3-8. Reuses the existing bounded store and key. |
| `docs/voice-latency.md` | Line 33: change "default 400" to "default 1200". Line 36: replace `VOICEAI_TOOL_FILLER_DELAY_MS` (default 400) with `VOICEAI_FILLER_DELAY_MS` (default 1200), and state that it is the same delay as the first-turn filler. | AC1-2. `.env.example`, `services/` and the rest of `docs/` already have zero mentions of the bogus name (grepped). |
| `services/conversation/tests/test_workflow_runner.py` | Add the tests listed in the Test plan. Build `RuntimeConfig` the same way as `test_graph_for_fallback_seeds_greeting_and_system_prompt` (`:245`), using a small local builder that takes tenant id/slug, agent id/slug and workflow. | AC3-8, AC11. |

## Data
None.

## Interfaces
No signature changes. `graph_for(runtime_config: RuntimeConfig, *, draft: bool = False) -> WorkflowGraph` keeps its "never raises" contract: `_cache_get`/`_cache_put` are plain dict operations on values that already exist. The non-draft empty path now returns a cached `WorkflowGraph` instance shared across calls, just as the published and parse-failure paths already do.

Callers checked (lesson 17):
- `services/conversation/pipeline.py:593` (per call, non-draft).
- `services/conversation/__main__.py:186` (prewarm, non-draft; this now seeds the cache, so the first real call is silent).
- `services/conversation/tests/test_startup_load.py:100` (monkeypatched).
- `graph_for` has no callers in `admin-ui/`, `scripts/` or `docs/`, and none use `draft=True` outside the runner itself.

## Risks
- A version bump (`runtime_config.version` changes) makes `_cache_get` miss, so the INFO line repeats once per config change. This is accepted: it falls under "once while retained" and is at INFO level, and it matches how a republished graph is handled today.
- Eviction at 256 agents can repeat the INFO line for an evicted agent. AC4 accepts this explicitly, and it is the same eviction the published path already uses.
- Callers now share one fallback `WorkflowGraph` instance per (tenant, agent, version) instead of getting a fresh one per call. The published and parse-failure fallback graphs are already shared this way, so the runner already treats graphs as read-only. Test 5 below checks that the cached instance and a fresh build are equal.
- A published graph added later must not be hidden by the cached starter. Publishing bumps `version`, so the old entry misses and the new graph parses and replaces it. Test 4 covers this.

## Test plan
Unit tests in `services/conversation/tests/test_workflow_runner.py`, using `caplog` on the `services.conversation.workflow.runner` logger. The autouse `_clear_graph_cache` fixture in `conftest.py` already resets the only store. All tests use the shipped `_GRAPH_CACHE_MAX`; none changes a constant (lesson 25).
1. **No ERROR, single record (AC3, AC4, AC11).** Call `graph_for(rc)` three times with `workflow=None`. Assert there are 0 records at ERROR or above, exactly 1 record containing "no published graph", and that the returned graph's greeting is the seeded one. Fails if: the level stays ERROR, or the cache hit is removed (that gives 3 records). Asserting `== 1` rather than `<= 1` also catches a guard that silences the first call.
2. **Tenant-scoped key (AC5).** Use the same agent slug and id `a1` under tenant `t1` and then tenant `t2`, then a different agent in `t1`. Expect 3 records in total. Fails if the key collapses to the agent slug or id alone.
3. **Parse failure still ERROR (AC6, AC7).** Published `workflow={"nodes": "bad"}` (or whatever raises `WorkflowInvalid`, reusing the broken JSON from the test at `:290`) must emit 1 ERROR record naming the slug and return a fallback. Monkeypatch `runner.parse_graph` to raise `RuntimeError` and assert that a record carries `exc_info` and the fallback is returned.
4. **Draft path unchanged and no shadowing (AC8).** With `draft=True`, `workflow=None` and `workflow_draft=None`, expect 0 records at WARNING or above, and `_GRAPH_CACHE` stays empty. Then call `graph_for` non-draft at version 1 (starter cached), then with a valid published workflow at version 2, and assert the published graph is returned.
5. **Shared instance equals a fresh build.** On the second call, `graph_for(rc)` returns an object equal to `_fallback_graph(rc, raw=None)`.

Existing `services/conversation/tests/test_latency_settings.py` covers AC9 unchanged. AC1 is checked with `grep -rn VOICEAI_TOOL_FILLER_DELAY_MS docs .env.example services`, which must return zero lines. AC10: run `pytest -q services/conversation/tests`.
