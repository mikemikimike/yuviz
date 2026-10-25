"""ToolPolicyResolver — which registered tools an agent may use, from
agent_tool_policies/tool_provider_configs (direct Postgres + in-process TTL cache).
Note: RuntimeConfig.tools / get_tools() in libs/config_sdk is an unused stub for the same concept."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, replace
from typing import Any

import asyncpg

from libs.tenancy import TenantUnresolved, tenant_conn

from ..bounded import POOL_COMMAND_TIMEOUT_S, POOL_RELEASE_TIMEOUT_S, run_bounded
from .registry import ToolRegistry
from .types import ToolDefinition

log = logging.getLogger(__name__)

_DEFAULT_CACHE_TTL_S = 30.0
# Bound on the whole DB lookup (both acquires and both fetches); a slow Postgres must not stall a turn.
_LOOKUP_TIMEOUT_S = 2.0
# After a failed lookup, serve the fallback without touching the DB for this long.
_FALLBACK_BACKOFF_S = 30.0
# Past this age a cached list is no longer trusted during an outage.
_STALE_MAX_AGE_S = 900.0


@dataclass(frozen=True)
class ResolvedToolPolicy:
    definition:              ToolDefinition
    tool_provider_config_id: str
    engine:                  str
    api_key_ref:             str | None
    extra:                   dict[str, Any]
    timeout_ms:              int | None
    max_calls_per_turn:      int | None
    # execute_api only; None = platform default.
    max_chain_depth:         int | None = None
    # execute_api only — sensitive caller-param names across the agent's enabled APIs.
    sensitive_arg_keys:      frozenset[str] = frozenset()
    # execute_api only — lower-cased names of offered APIs whose whole chain is read-only
    # (custom_apis.side_effecting = false). Anything not listed is treated as a write.
    read_only_apis:          frozenset[str] = frozenset()
    # execute_api only — every offered write API takes the caller's number from the call itself
    # (custom_api_params.source = 'caller_id'), so the model never has to confirm or pass one.
    writes_use_call_number:  bool = False


class ToolPolicyResolver:
    def __init__(
        self, pool: asyncpg.Pool | None, registry: ToolRegistry, cache_ttl_s: float = _DEFAULT_CACHE_TTL_S,
    ) -> None:
        self._pool = pool
        self._registry = registry
        self._cache_ttl_s = cache_ttl_s
        # Keyed by tenant too, so a cross-tenant agent_id collision can't leak cached policy.
        self._cache: dict[tuple[str, str], tuple[float, list[ResolvedToolPolicy]]] = {}
        # Monotonic time before which a key that just fell back is not looked up again.
        self._backoff_until: dict[tuple[str, str], float] = {}
        self._inflight: dict[tuple[str, str], asyncio.Task] = {}

    @classmethod
    async def connect(cls, database_url: str | None, registry: ToolRegistry) -> "ToolPolicyResolver":
        if not database_url:
            return cls(None, registry)
        pool = await asyncpg.create_pool(
            database_url, min_size=1, max_size=5, command_timeout=POOL_COMMAND_TIMEOUT_S,
        )
        return cls(pool, registry)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def enabled_tools(
        self, agent_id: str, tenant_slug: str, only: list[str] | None = None,
    ) -> list[ResolvedToolPolicy]:
        """Enabled tools for agent_id in tenant_slug; `only` subsets by name (None = all, [] = none).
        An empty/unresolvable slug raises TenantUnresolved."""
        if not agent_id or self._pool is None:
            return []

        cache_key = (agent_id, tenant_slug)
        cached = self._cache.get(cache_key)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self._cache_ttl_s:
            return _narrow(cached[1], only)

        if now < self._backoff_until.get(cache_key, 0.0):
            return _narrow(self._stale(cache_key), only)

        # One lookup per key: concurrent callers share it instead of each hitting the DB and logging.
        lookup = self._inflight.get(cache_key)
        if lookup is None:
            lookup = asyncio.create_task(self._refresh(cache_key))
            self._inflight[cache_key] = lookup
            lookup.add_done_callback(lambda _: self._inflight.pop(cache_key, None))
        resolved = await asyncio.shield(lookup)
        return _narrow(self._stale(cache_key) if resolved is None else resolved, only)

    def _stale(self, cache_key: tuple[str, str]) -> list[ResolvedToolPolicy]:
        """The last cached list while it is young enough to trust, else no tools (fail closed)."""
        cached = self._cache.get(cache_key)
        if cached is None or time.monotonic() - cached[0] > _STALE_MAX_AGE_S:
            return []
        return cached[1]

    async def _refresh(self, cache_key: tuple[str, str]) -> list[ResolvedToolPolicy] | None:
        """Read and cache the full list; None when Postgres is slow or down (backoff armed)."""
        agent_id, tenant_slug = cache_key
        try:
            resolved = await run_bounded(self._fetch(agent_id, tenant_slug), _LOOKUP_TIMEOUT_S)
        except TenantUnresolved:
            raise
        except Exception as exc:
            # Never log str(exc) or exc_info: a driver error carries the DSN/host.
            self._backoff_until[cache_key] = time.monotonic() + _FALLBACK_BACKOFF_S
            log.warning(
                "ToolPolicyResolver: policy lookup fell back agent_id=%s tenant=%s reason=%s using=%s",
                agent_id, tenant_slug, "timeout" if isinstance(exc, TimeoutError) else type(exc).__name__,
                "stale-cache" if self._stale(cache_key) else "no-tools",
            )
            return None

        # Cache unnarrowed; `only` is applied on read (varies per node).
        self._cache[cache_key] = (time.monotonic(), resolved)
        self._backoff_until.pop(cache_key, None)
        return resolved

    async def _fetch(self, agent_id: str, tenant_slug: str) -> list[ResolvedToolPolicy]:
        """The full, unnarrowed policy list for the agent, read from Postgres."""
        async with tenant_conn(
            self._pool, explicit_tenant=tenant_slug, reason="conversation-tool-policy",
            acquire_timeout=POOL_RELEASE_TIMEOUT_S,
        ) as conn:
            rows = await conn.fetch(
                """
                SELECT atp.tool_name, atp.timeout_ms, atp.max_calls_per_turn, atp.max_chain_depth,
                       tpc.id AS tool_provider_config_id, tpc.engine, tpc.api_key_ref, tpc.extra
                FROM agent_tool_policies atp
                JOIN agents a ON a.id = atp.agent_id
                JOIN tool_provider_configs tpc
                  ON tpc.id = atp.tool_provider_config_id AND tpc.tenant_id = a.tenant_id
                WHERE atp.agent_id = $1 AND atp.enabled = true AND tpc.deleted_at IS NULL
                """,
                agent_id,
            )

        resolved: list[ResolvedToolPolicy] = []
        for row in rows:
            defn = self._registry.resolve(row["tool_name"])
            if defn is None:
                log.warning(
                    "ToolPolicyResolver: agent_tool_policies references unknown tool_name=%r agent_id=%s — skipping",
                    row["tool_name"], agent_id,
                )
                continue
            raw_extra = row["extra"]
            # asyncpg may return JSONB as str without a codec — accept either.
            extra = json.loads(raw_extra) if isinstance(raw_extra, str) else (raw_extra or {})
            resolved.append(ResolvedToolPolicy(
                definition=defn,
                tool_provider_config_id=str(row["tool_provider_config_id"]),
                engine=row["engine"],
                api_key_ref=row["api_key_ref"],
                extra=extra,
                timeout_ms=row["timeout_ms"],
                max_calls_per_turn=row["max_calls_per_turn"],
                max_chain_depth=row["max_chain_depth"],
            ))

        await self._specialize_execute_api_if_present(resolved, agent_id, tenant_slug)
        return resolved

    async def _specialize_execute_api_if_present(
        self, resolved: list[ResolvedToolPolicy], agent_id: str, tenant_slug: str,
    ) -> None:
        """Specialize execute_api if present; drop it when the agent has no enabled custom APIs."""
        for i, policy in enumerate(resolved):
            if policy.definition.name != "execute_api":
                continue
            specialized = await self._specialize_execute_api(policy.definition, agent_id, tenant_slug)
            if specialized is None:
                del resolved[i]
            else:
                defn, sensitive_arg_keys, read_only_apis, writes_use_call_number = specialized
                resolved[i] = replace(
                    policy, definition=defn, sensitive_arg_keys=sensitive_arg_keys, read_only_apis=read_only_apis,
                    writes_use_call_number=writes_use_call_number,
                )
            return

    async def _specialize_execute_api(
        self, defn: ToolDefinition, agent_id: str, tenant_slug: str,
    ) -> tuple[ToolDefinition, frozenset[str], frozenset[str]] | None:
        """Return (defn with api_name enum + param docs, sensitive caller-param names, read-only API names),
        or None if no APIs.
        Upstream-only APIs are hidden and caller params are gathered across each API's whole chain;
        the agent/API tenant join is the runtime tenant fence."""
        async with tenant_conn(
            self._pool, explicit_tenant=tenant_slug, reason="conversation-tool-policy",
        ) as conn:
            rows = await conn.fetch(
                """
                WITH RECURSIVE enabled AS (
                    SELECT ca.id, ca.name, ca.description
                    FROM agent_custom_apis aca
                    JOIN custom_apis ca ON ca.id = aca.custom_api_id AND ca.deleted_at IS NULL
                    JOIN agents      a  ON a.id  = aca.agent_id AND a.tenant_id = ca.tenant_id
                    -- A connector-backed API is offered only while its connection is
                    -- connected; the tenant predicate keeps another tenant's connected
                    -- row from re-enabling it.
                    LEFT JOIN oauth_connections oc ON oc.id = ca.oauth_connection_id
                                                  AND oc.tenant_id = ca.tenant_id AND oc.deleted_at IS NULL
                    WHERE aca.agent_id = $1 AND aca.enabled
                      AND (ca.oauth_connection_id IS NULL OR oc.status = 'connected')
                ),
                -- Every API reachable from each enabled API: itself, plus
                -- its transitive upstream dependencies. The depth cap is a
                -- termination guard for a cycle in the data, NOT the chain
                -- limit — services/toolexec's resolve_order() owns that and
                -- raises cycle_detected/depth_limit_exceeded for real.
                chain(root_id, api_id, depth) AS (
                    SELECT id, id, 1 FROM enabled
                  UNION ALL
                    SELECT c.root_id, p.upstream_api_id, c.depth + 1
                    FROM chain c
                    JOIN custom_api_params p
                      ON p.custom_api_id = c.api_id
                     AND p.source = 'upstream'
                     AND p.upstream_api_id IS NOT NULL
                    WHERE c.depth < 8
                )
                SELECT e.id, e.name, e.description,
                       EXISTS (
                           SELECT 1 FROM chain c2
                           WHERE c2.api_id = e.id AND c2.root_id <> e.id
                       ) AS is_intermediate,
                       -- A chain is a write if any API in it is; the column defaults to true.
                       EXISTS (
                           SELECT 1 FROM chain c3
                           JOIN custom_apis ca3 ON ca3.id = c3.api_id
                           WHERE c3.root_id = e.id AND ca3.side_effecting
                       ) AS chain_side_effecting,
                       EXISTS (
                           SELECT 1 FROM chain c4
                           JOIN custom_api_params p4 ON p4.custom_api_id = c4.api_id AND p4.source = 'caller_id'
                           WHERE c4.root_id = e.id
                       ) AS chain_uses_caller_id,
                       p.name        AS param_name,
                       p.description AS param_description,
                       p.json_type, p.required,
                       p.sensitive   AS param_sensitive
                FROM enabled e
                LEFT JOIN chain c ON c.root_id = e.id
                LEFT JOIN custom_api_params p
                       ON p.custom_api_id = c.api_id AND p.source = 'caller'
                ORDER BY e.name, p.name
                """,
                agent_id,
            )
        if not rows:
            return None

        offerable = {r["name"] for r in rows if not r["is_intermediate"]}
        if not offerable:
            # Only possible with a dependency cycle; offer all rather than an empty enum.
            log.warning(
                "ToolPolicyResolver: every enabled custom API for agent_id=%s is an upstream of "
                "another (dependency cycle?) — offering all of them unfiltered", agent_id,
            )
            offerable = {r["name"] for r in rows}

        apis: dict[str, dict[str, Any]] = {}
        sensitive_arg_keys: set[str] = set()
        read_only_apis: set[str] = set()
        writes_without_call_number: set[str] = set()
        for row in rows:
            if row["name"] not in offerable:
                continue
            api = apis.setdefault(row["name"], {"description": row["description"], "params": {}})
            # .get: a row without the column is treated as a write (fail closed).
            if not row.get("chain_side_effecting", True):
                read_only_apis.add(row["name"].lower())
            elif not row.get("chain_uses_caller_id", False):
                writes_without_call_number.add(row["name"].lower())
            # Dedup: a diamond in the chain reaches the same leaf twice.
            if row["param_name"] is not None and row["param_name"] not in api["params"]:
                api["params"][row["param_name"]] = row
                if row["param_sensitive"]:
                    sensitive_arg_keys.add(row["param_name"])

        api_docs = "\n".join(
            f"- {name}: {api['description']}" + "".join(
                f"\n    * {p['param_name']} ({p['json_type']}"
                f"{', required' if p['required'] else ''}): {p['param_description']}"
                for p in sorted(api["params"].values(), key=lambda r: r["param_name"])
            )
            for name, api in apis.items()
        )
        specialized = replace(
            defn,
            description=f"{defn.description}\n\nAvailable APIs:\n{api_docs}",
            parameters_schema={
                **defn.parameters_schema,
                "properties": {
                    **defn.parameters_schema["properties"],
                    "api_name": {"type": "string", "enum": sorted(apis)},
                },
            },
        )
        writes = {name.lower() for name in apis} - read_only_apis
        writes_use_call_number = bool(writes) and not (writes & writes_without_call_number)
        return specialized, frozenset(sensitive_arg_keys), frozenset(read_only_apis), writes_use_call_number


def _narrow(
    resolved: list[ResolvedToolPolicy], only: list[str] | None,
) -> list[ResolvedToolPolicy]:
    """Subset by name — never grants. None means unnarrowed."""
    if only is None:
        return resolved
    allowed = set(only)
    return [p for p in resolved if p.definition.name in allowed]
