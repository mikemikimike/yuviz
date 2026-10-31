# Code review
VERDICT: GREEN
Coverage: callers 6 checked (_synthesize_sentence_stream reply/max-duration/transfer/cache paths, servicer EndCall branches, CallSession on_end_call, CallFSM on_playback_finished/on_speech_started in WaitingForHangup) · raises 0 new (new no-op/early-return paths traced: FSM no-op in BargeIn/Idle/Connecting, servicer third EndCall branch) · promises 7 matched (zero-byte skip + WARNING + unspoken, _join_sentences, max-duration record/flag/WARNING, transfer WARNING, servicer max_duration_no_audio after terminal chunk, gateway reason-keyed immediate WaitingForHangup, end_call_pending accessor) · contracts 2 compared (HandlerResponse.end_call_without_audio → servicer latch; EndCall.reason literal servicer ↔ kMaxDurationNoAudioReason) · failure-visibility done · env: Python targeted suite 305 passed; gateway BLOCKED-ENV (no build/tests/gateway_tests binary, C++ tests not run)

Carried items:
- AC13 wire order: handled — call_session_end_call_test.cpp MaxDurationNoAudioOrderTest is parameterised over both orders.
- Recognizing no-op (AC15): handled — CallFSM accepts Recognizing (transition table + switch), pinned by EndCallWithoutPlayback_FromRecognizing_EntersWaitingForHangup. 02-design.md Changes/Test plan 5 still say Recognizing is a no-op (doc lag only).
- T1 done-when: handled — 03-tasks.md:11 now requires fail-on-base only for zero-chunk/only-b"" and pass-on-both for the raising control.

1. [minor] Servicer audio_sent narrowing is guarded only by a direct unit test of the new `_is_reply_audio` helper — `services/conversation/servicer.py:587`, `services/conversation/tests/test_servicer.py:57` — fails when: line 587 is reverted to `audio_sent or not response.filler`; every test stays green because the pipeline now filters b"" before the servicer, so no servicer-level test feeds `tts_payloads=[b""]` — fix: drive one Converse turn whose handler yields `HandlerResponse(tts_payloads=[b""], end_call=True)` and assert no EndCall is sent.
