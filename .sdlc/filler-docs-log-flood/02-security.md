# Security review: .sdlc/filler-docs-log-flood/02-design.md
VERDICT: GREEN

## Findings
None. This design adds no endpoint, query, token or permission path. It sends the non-draft "no published graph" fallback through the graph cache that already exists, and it changes one log level and a docs page. Each trust assumption the cache relies on is checked below.

## Verified controls
- Cache key is scoped by tenant. `_cache_key` returns (tenant id or slug, agent id or slug) (`services/conversation/workflow/runner.py:217-220`, redesign). A cached starter graph built from tenant A's greeting and system_prompt cannot be returned for tenant B, even when the agent slug is the same. Design test 2 covers this.
- Stale content is invalidated. `_cache_get` rejects an entry when `runtime_config.version` differs. `bump_agent_config_version` (`database/schema.sql:650-672`) bumps `agents.config_version` on any change to an agent column except `workflow_draft`. So an edit to greeting or system_prompt (for example, removing sensitive text from a prompt) evicts the cached starter on the next resolve.
- A cached starter cannot widen tool access. The starter graph has a single stage, so `allowed_tool_names()` returns None (`runner.py:91-98`). Tool access then comes from the live `agent_tool_policies` through `ToolPolicyResolver` (`pipeline.py:1203`). A tool name left in the cached graph after a tool is revoked grants nothing.
- Draft content cannot leak into the production cache. The `draft=True` empty path stays uncached, and the draft parse-failure path re-enters with `draft=False` and reads only published `raw`.
- The legacy YAML path is not affected. `to_runtime_config` always sets `workflow` (`services/conversation/agent_config.py:124`), so it never reaches the empty branch. Its key is still (tenant_slug, script_id).
- No memory DoS. Once-only state reuses `_GRAPH_CACHE`, which is capped at `_GRAPH_CACHE_MAX = 256` with the existing eviction (`runner.py:235-240`). An attacker who creates many agents cannot grow it.
- Logs expose no new data. The INFO message carries only the agent slug, with no PII, secrets or prompt text. The ERROR level and `log.exception` stay on the parse-failure paths, so real misconfiguration is still alerted.
- The docs change (`docs/voice-latency.md`) only renames an env var and fixes a default. It exposes no secrets or internal endpoints.
