# Security review: turn1-neutral-filler (code, base d5f9839)
VERDICT: GREEN

## Findings
None. The change selects static, import-validated phrase strings and adds no endpoint, query, credential path, or tenant-scoped state.

## Verified controls
- Tenant isolation: the turn-1 filler's latency average is still read through `agent_average_ms(self._tenant_id, agent_id)`. That store is nested tenant-first and returns None on a blank tenant or agent (`services/conversation/tool_latency.py:88-95`), so the change adds no cross-tenant read.
- Shared selector state: `FillerSelector` stays stateless and uses random choice. `last_phrase` lives in per-session state (`pipeline.py:819-826`), so nothing leaks between concurrent calls or tenants.
- Input reach: the language code is normalized by `normalize_language` and only used as a dict key into the static `_TABLES`. Module discovery happens once at import, so no caller-controlled value reaches import, path or template code (`services/conversation/i18n/__init__.py:117-135`).
- Startup validation: the optional `FIRST_TURN_FILLERS` gets the same strict tuple, type and finiteness check as `TOOL_FILLERS`, so a malformed table fails at startup instead of at runtime (`services/conversation/i18n/__init__.py:30-62`).
- Fail-safe: `select_first_turn_filler` catches any exception and returns a static shortest phrase. Its log line carries no transcript or PII (`services/conversation/fillers.py:61-72`).
- End-call guard: the language without phrase tables now trusts the model's end-call marker. Only the agent's own call ends, and the caller can always hang up anyway, so there is no privilege or data gain. The `end_call_rejected` log includes only the reason and session id (`services/conversation/pipeline.py:236-258, 1419-1431`).
