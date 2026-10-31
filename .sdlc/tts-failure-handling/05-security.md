# Security review: tts-failure-handling (code, base a841bf1)
VERDICT: GREEN

## Findings
None.

Carried items: none are tagged security_design. The AC15 item is handled in the diff: `CallFSM::on_end_call_without_playback` now accepts Recognizing, with a matching kValidTransitions entry. The test-order and plan items are for the critics.

## Verified controls
- No tenant-scoped data path changed: the diff touches only TTS turn handling, EndCall signalling and FSM transitions. No DB queries, cache keys or tenant ids are added or changed.
- `end_call_without_audio` is set only by the max-duration cap (pipeline.py:1065). LLM or caller input cannot set it, so prompt-injected END_CALL directives still need audio_sent (servicer.py:645).
- The gateway reason string `max_duration_no_audio` (CallSession.cpp:14) comes only from the Conversation Service gRPC stream, with no new external or unauthenticated ingress. The handler is posted to the session's control thread like the existing EndCall path.
- `on_end_call_without_playback` no-ops in BargeIn, WaitingForHangup and terminal states (CallFSM.cpp:177-191). It cannot reopen a closed session or skip a barge-in cancel.
- New log lines (pipeline.py TTS no-audio and goodbye/transfer warnings; servicer.py:660) log only session_id and character counts, never transcript text, PII or credentials.
- Transcript records now store the goodbye actually spoken (a constant template or empty), not attacker-influenced content beyond the prior behaviour.
- The empty-chunk skip in `_synthesize_sentence_stream` and `_is_reply_audio` only change audio accounting. They add no new response fields and expose nothing to the caller.
