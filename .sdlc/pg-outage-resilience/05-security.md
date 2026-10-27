# Security review: pg-outage-resilience (code, base 0fc91a8)
VERDICT: GREEN

## Findings
None.

## Verified controls
- Policy cache and new `_backoff_until` are both keyed by `(agent_id, tenant_slug)`; a tenant A outage fallback or backoff cannot serve or short-circuit tenant B for a colliding agent_id — `services/conversation/tools/policy_resolver.py:58-60,82-108`.
- The fallback fails closed: it returns that key's own stale list or `[]`, always through `_narrow`, which only subsets and never grants — `policy_resolver.py:88,104,296-303`.
- `TenantUnresolved` is re-raised before the generic `except`, so a tenancy bug is not hidden as an outage (`wait_for` passes the original exception through) — `policy_resolver.py:94-95`.
- `_fetch` keeps the `tenant_conn(explicit_tenant=tenant_slug)` scoping and tenant join predicates unchanged; only moved — `policy_resolver.py:110+`.
- The fallback WARNING logs only agent_id, tenant slug and exception type name, never `str(exc)` or `exc_info`, so no DSN, host or credentials — `policy_resolver.py:98-103`.
- The peer-facing gRPC errors on the factory-failure, mid-call INTERNAL and CALL_REJECTED paths now use fixed texts; driver and DSN strings and session details stay in the server log. This also closes the old raw-exception leak to the peer — `services/conversation/servicer.py:39-40,162-172,683-697`.
- Factory failures now give the same `INTERNAL`/"internal error" for every cause, so error text is no longer an oracle on which tenant or agent lookup failed — `servicer.py:162-172`.
- The heartbeat loop runs as the platform role (`platform_conn`) with no tenant input. Reaping peers is delayed until REAP_WINDOW_S of healthy writes, so a node that was briefly unhealthy cannot close other nodes' live calls (an integrity guard against a self-caused mass hangup) — `services/conversation/__main__.py:244-268`, `transcript_builder.py:102-113`.
- `_bounded_step` lets `CancelledError` through (it catches only `Exception`), so shutdown still stops the loop — `__main__.py:230-241`.
