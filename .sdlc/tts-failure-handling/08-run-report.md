# Run report: tts-failure-handling
Status: halted at test: 1 still failing after 2 rounds (330 passed, 1 failed)
PR: none

| Stage | Verdict | Rounds | Output tokens (exact) | Est. cost |
|---|---|---|---|---|
| prd | GREEN | 1 (3 runs) | sonnet 24,974 · opus 22,044 | $1.39 est. |
| security_design | GREEN | 1 | opus 2,257 | $0.09 est. |
| design | AMBER | 1 | opus 45,528 | $1.78 est. |
| plan | GREEN | 1 | opus 8,779 | $0.34 est. |
| build | GREEN | 1 | haiku 7,953 · sonnet 51,007 | $1.17 est. |
| review | GREEN | 1 | opus 10,345 | $0.41 est. |
| security_code | GREEN | 1 | (billed to review) | — |
| test | RED | 2 | sonnet 6,241 | $0.13 est. |
| independent | not run | 0 | — | — |
| _state_writes | — | — | haiku 76,532 | $0.81 est. |
| **Total** | | | | **≈ $6.13 est.** |

Failing test: test_phrase_audio.py::test_the_cache_dies_with_the_tts_instance — WeakKeyDictionary still holds 1 entry; GC/order dependent (passes alone); failure is in test code; not verified against base.

Open findings — decide:
- [medium] test — Gateway C++ tests (call_fsm_test.cpp, call_session_end_call_test.cpp) written but not executed: cmake not installed locally (BLOCKED-ENV); CI must run them.
- [medium] test — T1 done-when wording in 03-tasks.md:11 is a task-file fix, not a test.
- [minor] prd — Criterion 13: assert at gateway level (pending_end_call_ false; later on_playback_finished → zero hangups).
- [minor] design — AC13 gateway test should run both EndCall/terminal-chunk orders (servicer sends EndCall after the terminal chunk).
- [minor] design — on_end_call_without_playback is a silent no-op in Recognizing; accept it there or log WARNING, and pin with an FSM test.
- [minor] plan — T1 "fail on base" should exclude the raising control (which passes on base).
- [minor] review:backend — servicer.py:587 narrowed audio_sent check lacks a servicer-level test (Converse turn with tts_payloads=[b""] + end_call=True → no EndCall).
Lessons added: None
Workflow run: wf_db85d3ea-47f (resume within this session with resumeFromRunId)
