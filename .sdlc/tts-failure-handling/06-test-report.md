# Test report
COMMAND: venv/bin/python -m pytest -q services/conversation/tests
RESULT: 1645 passed, 0 failed

- test_the_cache_dies_with_the_tts_instance — now compares against a post-gc baseline, same assertion on this instance's entry — pass (full suite, no order dependence)
- test_end_call_is_withheld_when_the_only_tts_payload_is_empty (servicer, tts_payloads=[b""] + end_call) — reverting servicer.py:587 turns it red — pass
- FSM EndCallWithoutPlayback_FromRecognizing / InBargeIn_IsNoOp, CallSession MaxDurationNoAudioOrderTest both orders — written, NOT executed (no cmake; CI)
- CallFSM.cpp: WARNING added on the ignored-state path of on_end_call_without_playback (Recognizing already accepted); compile-unverified
- 03-tasks.md T1 done-when already reads correctly
FAILURES: None
UNCOVERED: Gateway C++ tests not executed locally (BLOCKED-ENV: no cmake)
