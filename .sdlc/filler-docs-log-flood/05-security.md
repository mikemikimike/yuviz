# Security review: filler-docs-log-flood (code, base 92aa06a)
VERDICT: GREEN

## Findings
None.

## Verified controls
- Cache key is tenant-scoped: `_cache_key` returns (tenant id|slug, agent id|slug), so the newly cached starter graph cannot be served across tenants — `services/conversation/workflow/runner.py:215-218`.
- Staleness is bounded by version: `_cache_get` misses on `runtime_config.version` change, and `version = agents.config_version`, which the `bump_agent_config_version` trigger bumps on any agents-row change except `workflow_draft` — so a greeting/system_prompt edit or a publish invalidates the cached starter — `runner.py:221-229`, `database/schema.sql:679-699`.
- Stale tool names in a cached starter cannot widen tool access: the starter is single-stage, so `allowed_tool_names()` returns None and live `agent_tool_policies` decide — `runner.py:91-99`, `pipeline.py:1480`.
- Draft path is not cached, so an unpublished draft cannot poison the live (non-draft) cache entry — `runner.py:266-267`.
- Cache stays bounded at `_GRAPH_CACHE_MAX` with existing eviction; no new unbounded store reachable by callers — `runner.py:232-237`.
- Log change: the INFO line logs only the agent slug (no prompt, greeting, tenant secret or PII); parse-failure ERROR/exception paths unchanged — `runner.py:270-274`.
- Docs change names only an env var and default; no secrets exposed — `docs/voice-latency.md:33-37`.
