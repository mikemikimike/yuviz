# Run report: turn1-neutral-filler
Status: shipped
PR: https://github.com/yuviz-ai/yuviz/pull/144 (stacked on #143, base fix/128-guard-end-call-marker)

| Stage | Verdict | Rounds | Output tokens (exact) | Est. cost |
|---|---|---|---|---|
| security_design | GREEN | 1 | opus 2,431 | est. $0.10 |
| prd | GREEN | 1 | sonnet 3,687 / opus 4,065 | est. $0.24 |
| _state_writes | pending | 0 | haiku 36,593 | est. $0.39 |
| design | GREEN | 1 | opus 26,095 | est. $1.02 |
| plan | AMBER | 1 | opus 5,758 | est. $0.23 |
| build | GREEN | 1 | haiku 3,311 / sonnet 26,005 | est. $0.59 |
| review | AMBER | 1 | opus 9,792 | est. $0.38 |
| security_code | GREEN | 1 | none | est. $0.00 |
| test | GREEN | 1 | sonnet 1,345 | est. $0.03 |

Open findings — decide:
- [minor] prd — criterion 10 wording and the English fallback for a language with no i18n module (criterion 7) are still unstated in the PRD
- [minor] plan — T5 grep check can never fail
- [info] AC3: native Hindi spot-check of hi.FIRST_TURN_FILLERS still pending
- Post-ship: independent re-reviews GREEN/AMBER-minor, all findings fixed; wording reworked in 4f1787d and 3adda82

Lessons added: None
Workflow run: wf_fdeb8493-611 (resume within this session with resumeFromRunId)
