# Run report: transcript-turn-accuracy
Status: halted at review: round 1 RED with 2 blocking findings. (1) calls.py:171: a NULL close_reason drops ended chat-test calls out of both the success and failed counts. (2) pipeline.py:1199: a hang-up at stream end still counts as a barge-in (AC14). Fixing (2) needs a hang-up signal from servicer.py/session.py, which is outside the fix agent's files. security_code GREEN; build GREEN.
PR: none

| Stage | Verdict | Rounds | Output tokens (exact) | Est. cost |
|---|---|---|---|---|
| prd | AMBER | 2 | sonnet 9,930 · opus 7,130 | $0.49 |
| security_design | GREEN | 1 | opus 6,035 | $0.24 |
| design | AMBER | 1 | opus 33,837 | $1.33 |
| plan | GREEN | 2 | opus 22,512 | $0.88 |
| build | GREEN | 1 | haiku 18,228 · sonnet 81,241 | $1.91 |
| review | RED (halted) | 1 | opus 24,744 · sonnet 2,557 | $1.02 |
| security_code | GREEN | 1 | (billed to review) | — |
| _state_writes | — | — | haiku 68,200 | $0.72 |
| **Total** | | | 261,406 | **≈ $6.61 est.** |

Cost is estimated: input taken as 20× output with 80% of it cache reads. Haiku input is priced at $1/M because the configured rate was unreadable.

Open findings — decide:
- [minor] prd — AC2 should also count TRANSFER_TIMEOUT (not only TRANSFER_FAILED) as failed for zero-turn calls.
- [minor] prd — AC15 answering-agent link needs a same-tenant criterion, both on write and in the count query.
- [minor] design — the disposition-drift tripwire must scan callflow/runner.py and handler.py together and assert the exact set of 4 Hangup reasons (transfer_refused lives in handler.py:117).
Lessons added: None
Workflow run: wf_6a72bc22-f35 (resume within this session with resumeFromRunId)
