# Code review
VERDICT: GREEN
Coverage: callers 3 checked (_llm_to_tts at pipeline.py:851 on_speech_ended with caller_text=stt_result.text, :1100 on_transfer_failed with silent_turn_text="transfer_failed_fallback" and caller_text=""; no other call sites in services/libs; _init_languages single caller) · raises 3 traced (guard language None -> fail-open at :250 before any table lookup; end_call_rejected -> fallback key at :1456, main turn only; marker-only rejection on the transfer path -> transfer_failed_fallback spoken once, any_audio=True so on_transfer_failed :1115 adds no second apology) · promises 5 matched (agents.language > STT > TTS > "en" at :570-574, same order as resolve_languages for single-language agents; free text "English"/"Hindi" falls through because normalize_language returns None for 4+ letter words; "multi" only reaches here from the resolved STT language (Deepgram), giving None; caller's last sentence at :255, also splits on the Hindi danda (probe: "धन्यवाद। मुझे एक और बुकिंग बदलनी है।" -> no_closure); end_call_check only when silent_turn_text == "fallback_llm_error") · contracts 2 compared (end_call_check exists in en and hi, the only two languages with phrase tables, so a rejection never speaks an English fallback through t()'s en fallback for a table language it guarded; test_i18n hi-keys-in-en holds) · failure-visibility done (rejection log is reason token + session id only, no transcript or reply text; the guard runs only when a marker was parsed, and _guard_language is computed once at init, so the no-marker path adds one bool) · env ok (pytest services/conversation: 1267 passed)

Previous findings:
- #1 (transfer-failed apology replaced by end_call_check): fixed at pipeline.py:1456. Covered by test_on_transfer_failed_marker_only_reply_speaks_the_transfer_apology_once, which fails on the old code (count would be 0).
- #2 (free-text agents.language switches the guard off): fixed at pipeline.py:570-574. Covered by test_legacy_free_text_agent_language_keeps_the_end_call_guard, which fails on the old code (guard None means accept).

No new findings.

Notes, not defects:
- The `hi-IN` case in test_guard_uses_the_provider_row_language_when_the_agent_has_none would also pass with an "en" guard, because both reject that caller. The `es` case is the one that proves the row language is used.
- A single-language agent whose resolved STT language is Deepgram "multi" now fails open (before: "en"). The design asks for this explicitly (02-design.md:14).
