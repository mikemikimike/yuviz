# Security review: .sdlc/pg-outage-resilience/02-design.md
VERDICT: GREEN

## Findings
1. [low] The Risks section says a stale policy lasts at most 30 s ("the first lookup 30 s after the fallback refetches and overwrites it"). That only holds if the DB is healthy by then. While the resolver's DB path keeps failing, every window re-arms the fallback and the stale list, including `api_key_ref` and the provider config id, is served with no time limit — Design: Risks (stale cached policy) / Interfaces step 7
   Attack: a tenant admin disables a tool or soft-deletes a `tool_provider_configs` row, for example after a key compromise, through the Config service. Meanwhile the conversation node's own pool is exhausted or partitioned (its pool has `max_size=5`, `services/conversation/tools/policy_resolver.py:62`). Live calls in that tenant keep offering and invoking the revoked tool for as long as the partial outage lasts, while the admin believes it was revoked. This stays inside one tenant, so it is not cross-tenant.
   Fix: either cap how old a cached entry may be before fallback refuses it (for example, refuse stale entries older than N x TTL and fall back to `[]`), or correct the Risks text to say the stale window is unbounded during an outage, so that acceptance is explicit.

## Verified controls
- The policy cache and the new `_backoff_until` are both keyed on `(agent_id, tenant_slug)` (`policy_resolver.py:54,75`). Test plan 5 checks that tenant A's cached policy and backoff never answer tenant B for the same agent_id.
- Both `enabled_tools` callers pass server-resolved identity, not caller input: `__main__.py:375-376` uses `runtime_config.agent.id` / `runtime_config.tenant.slug`, and `orchestrator.py:125` uses the handler's agent/tenant.
- `_fetch` keeps the code from lines 80-119 unchanged, so `tenant_conn(explicit_tenant=...)` and the `tpc.tenant_id = a.tenant_id` / `a.tenant_id = ca.tenant_id` joins are preserved. This design adds no new SQL.
- Tool fallback fails closed: with no cache it returns `[]`, and `TenantUnresolved` is still re-raised instead of being treated as an outage.
- Every fallback and backoff return goes through `_narrow(..., only)`. Test plan 4 covers `only=[]` and `only=[x]`, so a workflow node's tool narrowing cannot be bypassed during an outage.
- A fallback result is never written to `_cache`, so a failure cannot be stored as a fresh DB result.
- Peer error leak: the `str(exc)` sites at `servicer.py:671,679` are the only raw-text peer messages in the servicer. Both are replaced with fixed strings. The factory raise at `servicer.py:147`, which today escapes the stream's `try`, gets a new generic branch with a fixed message.
- The fallback WARNING logs `type(exc).__name__` / "timeout" without `str(exc)` or `exc_info`. The test asserts that no host, port or DSN appears in it.
- Heartbeat and reconcile run under `platform_conn` with no tenant input, so making them raise and putting them under timeouts adds no tenancy surface. `CancelledError` still propagates (Python >=3.11).
