# Review: 01-prd.md (pg-outage-resilience)
VERDICT: GREEN

1. [minor] Criterion 3's "heartbeat written within 7 s of recovery" leaves out how long the new heartbeat attempt itself takes (a fresh pool connect plus the write after a 5 s timeout and a 2 s backoff), so a test against the deployed values could fail on a correct fix — Acceptance criteria 3 / Timing budget "Recovery" — fix: say the first new heartbeat attempt starts within 7 s of recovery and succeeds within that attempt's 5 s timeout (12 s worst case), and assert that bound.
