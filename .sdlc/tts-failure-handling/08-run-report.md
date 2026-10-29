# Run report: tts-failure-handling
Status: shipped (CI green)
PR: https://github.com/yuviz-ai/yuviz/pull/228

| Stage | Verdict | Rounds | Output tokens (exact) | Est. cost |
|---|---|---|---|---|
| prd | GREEN | 1 (3 runs) | sonnet 24,974 · opus 22,044 | $1.39 est. |
| security_design | GREEN | 1 | opus 2,257 | $0.09 est. |
| design | AMBER | 1 | opus 45,528 | $1.78 est. |
| plan | GREEN | 1 | opus 8,779 | $0.34 est. |
| build | GREEN | 1 | haiku 7,953 · sonnet 51,007 | $1.17 est. |
| review | GREEN | 1 | opus 10,345 | $0.41 est. |
| security_code | GREEN | 1 | (billed to review) | — |
| test | GREEN | 1 (retry after halt) | sonnet 10,418 | $0.22 est. |
| independent | GREEN | 1 | opus 9,266 | $0.36 est. |
| _state_writes | — | — | haiku 81,039 | $0.86 est. |
| **Total** | | | | **≈ $6.63 est.** |

Outside the workflow: two follow-up fix passes (independent-review minors), a pre-push full-diff review (GREEN, 4 minors fixed), and one CI fix (new gateway test never started its AudioWorkerPool → SEGFAULT).

Open findings — decide:
- [minor] design — stage closed AMBER; its two minors (both-order AC13 test, Recognizing no-op) were addressed in the test retry and pre-push fixes.
- [note] #205 is only partly fixed (item 7); the other items belong to the parallel batch, so no "Fixes #205" was posted.
Lessons added: None
Workflow run: wf_0ed35fa9-4b5 (resume within this session with resumeFromRunId)
