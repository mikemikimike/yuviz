# Review: 01-prd.md (TTS failure handling)
VERDICT: GREEN

1. [minor] Criterion 13 asks for "a subsequent turn's on_playback_finished" after a max-duration no-audio EndCall, but criteria 12 and 15 require that call to have already ended, so a test can only reach a later turn by assuming the hangup failed — 01-prd.md:33 — fix: restate 13 as a gateway-level assertion: after handling a no-playback max-duration EndCall, `pending_end_call_` is false and a later `on_playback_finished` on the same CallSession triggers zero hangups.
