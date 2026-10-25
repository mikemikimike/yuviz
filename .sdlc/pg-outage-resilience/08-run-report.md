# Run report: pg-outage-resilience
Status: halted at build: combined-tree suite fails — services/clinic tests can't reach Postgres (asyncpg FileNotFoundError: test DSN doesn't point at the running server). services/conversation passes fully (1430 passed).
PR: none

| Stage | Verdict | Rounds | Output tokens (exact) | Est. cost (est.) |
|---|---|---|---|---|
| prd | GREEN | 2 | sonnet 21,228 · opus 16,321 | ~$1.08 |
| design | AMBER (passed) | 1 | opus 26,026 | ~$1.04 |
| security_design | GREEN | 1 | opus 4,587 | ~$0.18 |
| plan | AMBER (passed) | 2 | opus 13,788 | ~$0.55 |
| build | halted | 0 | sonnet 51,359 · haiku 1,038 | ~$1.15 |
| _state_writes | — | 0 | haiku 43,430 | ~$0.46 |

Open findings — decide:
- [minor] prd — criterion 3: the recovery bound should say the attempt starts within 7 s and succeeds within its 5 s timeout (12 s worst case)
- [minor] design — no test that an uncached routed call reaching deps-None → UNAVAILABLE carries no host/DSN text
- [minor] design — concurrent failing lookups each log a WARNING (criterion 18 wants exactly one)
- [minor] design — test 1's fake clock never exercises the real 5 s wait_for timeout; needs a real-time hang-then-recover test
- [minor] design — the heartbeat _spawn call needs the "heartbeat" name argument
- [low] security_design — the fallback can re-arm on every backoff window, so a stale policy (including disabled tools) can be served with no time limit during a long outage
- [minor] plan — T2: check that reconcile_inactive_calls still runs in an iteration where reconcile_dead_nodes raised
- [minor] plan — T5: add the _LOOKUP_TIMEOUT_S ≤ 3 tripwire, and check narrowing on the short-circuit path
Lessons added: None
Workflow run: wf_8e43b378-eb3 (resume within this session with resumeFromRunId)
