# Security review: working-tree diff vs 44458cd (guard-end-call-marker)
VERDICT: GREEN

## Findings
None. The change is limited to the conversation pipeline's handling of the end-call marker within a single call session. It adds no tenant-scoped reads or writes, roles, tokens, SQL, shell, path or redirect sinks, and no new endpoints.

## Verified controls
- No tenant or authorization surface: the diff touches only `services/conversation/pipeline.py`, `services/conversation/i18n/{__init__,en,hi}.py` and tests. It adds no data access, cache keys, ids or routes.
- No transcript leak in logs: `end_call_rejected` logs only a fixed reason token and session_id (`pipeline.py`, guard block after `stream_buf.flush()`). The i18n warning logs only the module code and attribute names.
- What the caller controls: `caller_text` (STT output) is only matched against fixed phrase tables, so it can only allow a marker the LLM already emitted. It cannot create one, and the result is never stricter than before the change. Prompt-injected `[[END_CALL]]` was already reachable, and this change makes it harder to use.
- No ReDoS: `_PHRASE_SPLIT_RE` is a single character class with `+`, so it runs in linear time. `_SENTENCE_RE` is unchanged and existed before.
- Call-length bound kept: a rejected marker cannot stretch a call without limit, because the max-duration hangup (`pipeline.py:779-797`) and guardrail escalation (`pipeline.py:1035`) do not depend on the guard.
- AC12 latch: `end_call` in `on_speech_ended` is now read from `directives` after the stream, so a mid-stream `end_call=True` that the guard later rejects is never acted on. The barge-in and transfer gates at `pipeline.py:952` are unchanged.
- i18n startup validation: a malformed `CLOSURE_PHRASES` or `FAREWELL_PHRASES` makes `ValueError` fail the import, and `en` must have both. This prevents a silent empty table that would reject every marker.
