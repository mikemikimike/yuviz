=================== ISSUE #174
# [medium] TTS failure on a whole reply leaves the caller in silence; one failed sentence is still stored as spoken [OPEN]

Make TTS raise on the reply: SttResult and AssistantResponse are sent, then no TtsStarted and no `is_final` chunk; the caller's next utterance is only answered 12 s later. Same for any reply starting with "-" on macOS `say`. When only the first of two sentences fails, `transcript_entries` and the model history say the full reply was spoken. Expected: spoken fallback or at least a terminal empty `TtsChunk`.

Found by adversarial QA of the conversation layer at `92aa06a` (origin/redesign). Report: `.sdlc/conversation-layer/07-qa-report.md` (defect #7). Stubbed providers, private Postgres/Redis.

## Comments
--- ckss2k17:
PR #200 partly fixes this: the terminal chunk now arrives in 0.09 s (base 16.6 s) and sentences whose TTS raised are not stored. Remaining: providers that return empty audio without raising (macOS confirmed live; from code reading Deepgram, Cartesia, ElevenLabs, Sarvam, Kokoro) still have the sentence stored as spoken, e.g. '-5 degrees outside today.' Keeping this open.
=================== ISSUE #219
# [low] Max call duration reached and the goodbye's TTS fails: goodbye stored as spoken on every turn past the cap, no EndCall [OPEN]

When the max call duration is reached and the goodbye's TTS fails, the goodbye is stored in the transcript and history as if spoken on every turn past the cap, and no EndCall is sent (pipeline max-duration branch). Expected: the unspoken goodbye is not stored as spoken and the call ends.

Found by the independent QA of the combined stack #198..#210 (`.sdlc/conversation-layer/14-qa-final-stack.md`).

## Comments

=================== ISSUE #220
# [low] CJK reply: when one sentence fails TTS the stored text is rejoined with spaces (我很好。 谢谢。) [OPEN]

`_spoken_reply` rejoins the remaining sentences with spaces when one sentence of a Chinese/Japanese reply fails TTS, so the stored text reads '我很好。 谢谢。'. Expected: no inserted spaces for languages that do not use them.

Found by the independent QA of the combined stack #198..#210 (`.sdlc/conversation-layer/14-qa-final-stack.md`).

## Comments

=================== ISSUE #205
# [low] Persistence leftovers: hangup counted as barge-in, interrupted rows store generated not spoken text, handoff row names only the IVR agent, caller words dropped from history on TTS failure, ringing dial shown as stage 'ai', transcript_entries.tool_calls never filled, TTS failure on END_CALL hangs up silently [OPEN]

1) A hangup mid-turn adds 1 to `barge_in_count` (base 0). 2) An interrupted row stores what the LLM had generated, not what was spoken (one sentence past the audio the caller got, sometimes a half sentence). 3) After an IVR handoff `agent_id` names the IVR agent and nothing records which agent took the call; the sales agent shows 0 calls though it answered 12 turns. 4) When a whole reply fails TTS the caller's words are also dropped from the model's history though the transcript keeps them. 5) Live Calls shows a call-flow Dial that is ringing a human as stage 'ai'. 6) (pre-existing) `transcript_entries.tool_calls` is never filled so an interrupted `pay_order` row doesn't show the payment was sent. 7) (pre-existing) TTS failure on a reply carrying END_CALL hangs up in silence; with TRANSFER the transfer goes out unannounced.

Found by the QA retest of PR #200 (`.sdlc/conversation-layer/11-qa-retest-persistence.md`).

## Comments

