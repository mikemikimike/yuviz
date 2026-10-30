# Security review: .sdlc/tts-failure-handling/02-design.md
VERDICT: GREEN

## Findings
None. The design changes in-call audio and failure handling only. It adds no endpoint, no tenant-addressed read or write, no role or token logic, and no new input that a caller or tenant controls. The new `EndCall.reason = "max_duration_no_audio"` travels over the existing conversation-service-to-gateway stream. Only the conversation service can send it, and that service can already end any call it serves, so the new reason grants no new capability.

## Verified controls
- Empty TTS results are never cached: `phrase_audio.store` refuses an empty or all-`b""` result (`services/conversation/phrase_audio.py:46`), so a provider fault cannot poison replays.
- In-flight waiters never get an empty result as a cache hit: `finish` resolves the future to `None` when there is no audio (`services/conversation/phrase_audio.py:90`).
- The phrase cache is scoped to one TTS instance through a weakref, and the key includes the phrase text (`phrase_audio.py:31-53`). A hit only replays audio for the same text and the same voice instance, so no cross-session content can be substituted.
- The servicer's EndCall gate still requires `not cancelled` (`services/conversation/servicer.py:639`). The new branch only narrows `audio_sent` and is set by one pipeline source, so a barge-in still cannot end the call or trigger a transfer.
- The gateway's `on_end_call` handler (`gateway/src/session/CallSession.cpp:405-412`) is reached only from the conversation stream. The reason-keyed branch adds no exposure to callers or external input.
- The new log lines record only the session id, a character count and a fixed reason. No transcript text, caller speech or PII is logged.
