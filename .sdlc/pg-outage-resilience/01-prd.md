# PRD: Conversation service survives a slow or down Postgres (#187, #188, #189)

## Problem
When Postgres is slow or unreachable, the conversation service degrades far worse than its design intends. A ~30 s outage permanently kills the node heartbeat loop, so the node is later reaped as dead and its calls are reconciled away. New calls are refused even though agent config was served from the last-known-good cache, and the refusal text sent to the caller side exposes the DB host and port. The per-turn tool-policy lookup has no timeout, so a stalled DB freezes live turns and call teardown. Callers, and operators who need an outage to look like an outage and not a leak, are affected.

Market research skipped: this is codebase-specific wiring (heartbeat task, cache-aside config, tool-policy resolver), not a solved-elsewhere product feature.

## Scope
- In: The heartbeat loop keeps running through DB errors and timeouts, logs each failure, retries a failed heartbeat after a short backoff, and resumes the node heartbeat and reconcile work once Postgres returns.
- In: Every heartbeat and reconcile step has a bounded timeout, and the worst-case gap between successful heartbeat writes (all step timeouts plus the sleep) stays well under the node reap window, so a hung DB call cannot stall the loop or get the node reaped.
- In: A new call is accepted during a Postgres outage when its agent config comes from the last-known-good cache.
- In: When the tool-policy lookup fails or times out, the call uses the last cached policy for that agent and tenant, even if the cache entry is past its normal TTL. With no cached policy it uses no remote tools (fail closed), and the call proceeds as conversation only.
- In: The whole tool-policy lookup (`enabled_tools`), per turn and at call setup, has one bounded timeout. It covers the pool acquire, the policy fetch and the execute_api specialization fetch, so a turn never waits longer than that bound on the DB.
- In: After a policy-lookup fallback, later lookups for the same agent and tenant skip the DB for a fixed backoff window and return the fallback at once, so a long outage does not add the full timeout of dead air to every turn.
- In: Errors sent to the gRPC peer from the generic failure path carry a fixed code and message and never the raw exception text, so no DB host, port, DSN or driver message is exposed. Full detail goes to server logs only.
- Out: Changes to the Config service, the DB schema, or Redis and HTTP config repositories.
- Out: Reconnecting or replacing the asyncpg pool beyond what the pool already does.
- Out: Resilience of other DB consumers (transcripts persistence, knowledge, campaigns) beyond the heartbeat and reconcile calls.
- Out: Fail-open tool behaviour. A tool never runs under a policy the service cannot show was enabled.

## Timing budget (referenced by criteria 1, 3, 19, 21)
Deployed values: HEARTBEAT_INTERVAL_S = 15 s, reap window = 3 x 15 = 45 s. Required per-step timeouts: heartbeat at most 5 s, each of the two reconcile steps at most 5 s, failed-heartbeat retry backoff at most 2 s.
- Steady state (DB reachable, reconcile steps hitting their timeouts): gap between two successful heartbeat writes is at most reconcile 5 + reconcile 5 + sleep 15 + heartbeat 5 = 30 s, which leaves at least 15 s (one full interval) of margin under 45 s.
- Recovery: after Postgres returns, the loop is either inside a heartbeat attempt (at most 5 s) or inside the backoff (at most 2 s), so the first successful write lands within 7 s of recovery, not after a full interval.
- Any change to these values must keep the steady-state sum at or below 2 x HEARTBEAT_INTERVAL_S (30 s).

## Acceptance criteria
1. Given the heartbeat loop is running, when `transcripts.heartbeat()` raises or times out, then the loop logs the error, waits the short retry backoff (at most 2 s, not the 15 s interval), skips the reconcile steps for that attempt, and calls `heartbeat()` again.
2. Given `reconcile_dead_nodes` or `reconcile_inactive_calls` raises, when the loop iterates, then the other steps still run in that iteration and the loop continues.
3. Given Postgres was unreachable for 30 s and then recovers, when it recovers, then the node heartbeat is written within 7 s (heartbeat timeout 5 s plus backoff 2 s) with no service restart, per the timing budget.
4. Given the loop task is cancelled at shutdown, when cancellation arrives during a failing iteration, then the task exits promptly. Cancellation is not swallowed as a retryable error.
5. Given Postgres is down and the agent config is in the cache, when a new call starts, then the call is accepted and a greeting is produced.
6. Given Postgres is down and the agent config is not cached, when a new call starts, then the call is refused with the generic error in criterion 14 and not a raw exception.
7. Given Postgres is down and a policy for the agent and tenant is cached, including past its TTL, when a call starts or a turn runs, then the cached tool policy is used, narrowed per criterion 23.
8. Given Postgres is down and no policy is cached for the agent and tenant, when a call starts or a turn runs, then no remote tools are offered to the LLM, and the call continues as conversation only.
9. Given the `enabled_tools` call does not return within the timeout because the policy fetch hangs, when a turn runs, then the turn continues using criterion 7 or 8 within that timeout plus a small margin. The timeout is a named constant or setting with a default of at most 3 s.
10. Given the pool acquire stalls (pool exhausted or DB blackholed) and the policy fetch is never reached, when a turn runs or a call starts, then the same bound applies and the same fallback is used.
11. Given the policy fetch succeeds but the execute_api specialization fetch hangs, when a turn runs, then the bound still applies to the whole call and the fallback is used.
12. Given the DB is stalled, when a call ends, then teardown completes without waiting on a policy lookup.
13. Given a policy lookup that fell back, when Postgres recovers and the first lookup after the backoff window (criterion 21) runs, then fresh policy is fetched and cached again. A fallback result is never stored as if it were a fresh DB result.
14. Given a failure in the generic `except Exception` path while setting up or running a call, when the service sends an error to the gRPC peer, then the code and message are fixed strings with no exception text. A test with an exception containing a host, a port and a DSN asserts none of them appear in the message.
15. Given a `RejectedCallHangup`, when the service sends its message to the peer, then the text is curated and a test asserts it embeds no raw exception text.
16. Given an unexpected exception during a call, when the error is sent, then the full exception is still written to the server log.
17. Given a tenant-A agent and a tenant-B agent with the same agent_id, when either falls back to cache during an outage, then it never receives the other tenant's policy. The cache and the backoff window stay keyed by agent and tenant.
18. Given a tool-policy fallback or timeout, when it happens, then one WARNING is logged naming the agent and the reason, and the log line does not contain the DSN or credentials. Lookups answered from the backoff window do not log a further WARNING each turn.
19. Given a heartbeat or reconcile step whose DB call hangs instead of raising, when its per-step timeout elapses (at most 5 s each), then the step is cancelled, a timeout is logged, the remaining steps run, and the loop carries on. Given all steps hit their timeouts in one iteration, then the gap between two successful heartbeat writes is at most 30 s, below the 45 s reap window with at least 15 s of margin, per the timing budget.
20. Given the deployed timeout and interval values, when the tests run, then they use those values and do not shrink them to make the scenario reachable.
21. Given a policy lookup for an (agent, tenant) fell back, when further lookups for the same pair arrive within the 30 s backoff window, then they return the fallback (criterion 7 or 8, narrowed per criterion 23) without touching the DB or waiting, and the first lookup after the window tries the DB again. Given a different (agent, tenant) pair, then it is not affected by that window.
22. Given a long outage with a cached policy past its TTL, when a call runs several consecutive turns, then only the first turn after the fallback waits up to the lookup timeout (at most 3 s); every later turn within the backoff window adds no DB wait.
23. Given a workflow node narrows tools with `only`, when the lookup falls back to a cached policy (stale, or inside the backoff window), then the returned list is the cached full list narrowed by that node's `only` exactly as on a DB hit, never the unnarrowed cached list. Test: during an outage with a cached policy, `only=[]` returns `[]` and `only=[x]` returns at most `[x]`; the test fails if any fallback or backoff return skips the narrowing.
24. Given a node has had one or more failed heartbeat writes, when Postgres returns, then `reconcile_dead_nodes` is skipped until this node has written heartbeats successfully for at least one full reap window (45 s), so it cannot reap live peers that are still in timeout or backoff. Test: two nodes, a 60 s outage, node A recovers first; node A reaps no live node B until 45 s of successful writes have elapsed, and the test fails if reconcile runs on the first successful write.

## Constraints
- The conversation service uses `services/conversation/__main__.py`: the heartbeat loop at about lines 504-516, spawned through `_spawn` (done-callback at about line 207), and the call-setup policy read at about line 375.
- The policy cache is keyed by `(agent_id, tenant_slug)` in `services/conversation/tools/policy_resolver.py` (lines 54 and 75-78, fetch at line 83, `_specialize_execute_api` fetch at about line 150, called from about line 119). The cache stores the full unnarrowed list (about lines 121-122), so any fallback must apply `_narrow(cached, only)` before returning. The normal cache TTL is 30 s (`_DEFAULT_CACHE_TTL_S`, line 22). Both fetches go through `tenant_conn`, whose pool acquire is where a stalled pool hangs. Keep tenant scoping through `tenant_conn(explicit_tenant=...)`, and keep the explicit tenant predicate (lesson 36).
- The per-turn consumer is `services/conversation/tools/orchestrator.py:125`. Fail closed means `policies = []`, as at line 122. Trace the fallback through every caller of `enabled_tools` (lesson 49): `services/conversation/__main__.py:375` and `services/conversation/tools/orchestrator.py:125`. Each must name the line that recovers when the lookup times out or raises.
- The error leak is the `except Exception` branch at `services/conversation/servicer.py:673-682`, which sends `message=str(exc)`. The `RejectedCallHangup` branch at line 668-671 also sends `message=str(exc)`. Criterion 15 requires its text to be curated, not raw.
- Config comes from `CacheAsideConfigProvider` in `libs/config_sdk`. Do not change its contract.
- Anything in an async path must not block the event loop and must use explicit timeouts (lesson 18). Any new task or executor must have a stated shutdown (lesson 26).
- Tests must exercise real tuning values (lesson 25), and each must state what would make it fail (lesson 12).
- No new schema. RLS is inert under the superuser DSN (lesson 36).

## Open questions
None. Assumption: the policy timeout is at most 3 s, because it must fit inside a voice turn. Assumption: the fallback backoff window is 30 s (equal to the normal cache TTL), long enough to stop per-turn dead air and short enough that recovery is noticed within about one TTL. Assumption: a stale cached policy is preferred over no tools, since it was valid when cached. Assumption: the heartbeat, reconcile and retry-backoff values are 5 s, 5 s and 2 s as in the timing budget. Assumption: the base branch is `redesign` as the request says, although the checked-out branch is `fix/187-189-pg-resilience`. Assumption: `08-stress-report.md` is absent, so the GitHub issue text is authoritative, and I could not read the issues with the tools available.
