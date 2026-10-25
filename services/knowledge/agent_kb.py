"""agent_documents CRUD — sole writer of the agent_kb:{tenant}:{agent} Redis flag.
Every mutation rewrites the flag (write-through, no TTL)."""

from __future__ import annotations

from typing import Any

from libs.tenancy import platform_conn, tenant_conn

from . import audit, cache, db
from . import retrieval_plan as plans

# Live linked documents in an active KB of the agent's own tenant: what the flag and retrieval count.
_HAS_DOCUMENTS_SQL = (
    "SELECT EXISTS ("
    "  SELECT 1 FROM agent_documents ad "
    "  JOIN agents a ON a.id = ad.agent_id AND a.deleted_at IS NULL "
    "  JOIN kb_documents d ON d.id = ad.document_id AND d.tenant_id = a.tenant_id AND d.deleted_at IS NULL "
    "  JOIN knowledge_bases kb ON kb.id = d.kb_id AND kb.deleted_at IS NULL AND kb.status = 'active' "
    "  {where}"
    ") AS has_kb"
)


async def get_agent_tenant_id(agent_id: Any, *, platform_scoped: bool = False) -> str | None:
    """Tenant of agent_id, for authorizing junction-table routes (which have no tenant column)."""
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="agent-kb-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "SELECT tenant_id FROM agents WHERE id = $1 AND deleted_at IS NULL", agent_id,
        )
    return str(row["tenant_id"]) if row is not None else None


async def _tenant_and_agent_slugs(agent_id: Any) -> tuple[str, str] | None:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT a.slug AS agent_slug, t.slug AS tenant_slug "
            "FROM agents a JOIN tenants t ON t.id = a.tenant_id WHERE a.id = $1",
            agent_id,
        )
    return (row["tenant_slug"], row["agent_slug"]) if row is not None else None


async def _refresh_flag(agent_id: Any) -> None:
    slugs = await _tenant_and_agent_slugs(agent_id)
    if slugs is None:
        return
    tenant_slug, agent_slug = slugs
    plans.invalidate_agent(tenant_slug, agent_slug)
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(_HAS_DOCUMENTS_SQL.format(where="WHERE ad.agent_id = $1"), agent_id)
    await cache.set_has_enabled_kb(tenant_slug, agent_slug, bool(row["has_kb"]))


async def list_for_agent(agent_id: Any) -> list[dict[str, Any]]:
    """The agent's linked, non-deleted documents."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            "SELECT d.id AS document_id, d.kb_id, d.title, d.content_type, d.byte_size, d.status, "
            "       ad.created_at AS linked_at "
            "FROM agent_documents ad "
            "JOIN agents a ON a.id = ad.agent_id "
            "JOIN kb_documents d ON d.id = ad.document_id AND d.tenant_id = a.tenant_id AND d.deleted_at IS NULL "
            "WHERE ad.agent_id = $1 ORDER BY d.title, d.id",
            agent_id,
        )
    return [dict(row) for row in rows]


async def list_for_kb(kb_id: Any, *, platform_scoped: bool = False) -> list[dict[str, Any]]:
    """Agents using any live document of kb_id; platform_scoped must come from deps.is_platform_scoped()."""
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="kb-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        rows = await conn.fetch(
            "SELECT a.id AS agent_id, d.kb_id, true AS enabled, min(ad.created_at) AS created_at, "
            "       a.slug AS agent_slug, a.name AS agent_name, a.tenant_id "
            "FROM agent_documents ad "
            "JOIN agents a  ON a.id = ad.agent_id  AND a.deleted_at IS NULL "
            "JOIN tenants t ON t.id = a.tenant_id   AND t.deleted_at IS NULL "
            "JOIN kb_documents d ON d.id = ad.document_id AND d.tenant_id = a.tenant_id AND d.deleted_at IS NULL "
            "WHERE d.kb_id = $1 "
            "GROUP BY a.id, d.kb_id, a.slug, a.name, a.tenant_id "
            "ORDER BY a.name",
            kb_id,
        )
    return [dict(row) for row in rows]


async def replace_documents(
    agent_id: Any, document_ids: list[str], *, user_id: Any | None = None, user_email: str | None = None,
) -> list[dict[str, Any]]:
    """Make the agent's links exactly document_ids, in one transaction. Raises LookupError unless every id is a
    live document of the agent's own tenant, so a foreign id and a missing one look the same."""
    ids = list(dict.fromkeys(str(i) for i in document_ids))
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        async with conn.transaction():
            found = await conn.fetchval(
                "SELECT count(*) FROM kb_documents d JOIN agents a ON a.tenant_id = d.tenant_id "
                "WHERE a.id = $1 AND d.id = ANY($2::uuid[]) AND d.deleted_at IS NULL",
                agent_id, ids,
            )
            if found != len(ids):
                raise LookupError("document not found")
            old = [str(r["document_id"]) for r in await conn.fetch(
                "SELECT document_id FROM agent_documents WHERE agent_id = $1 ORDER BY document_id", agent_id,
            )]
            await conn.execute(
                "DELETE FROM agent_documents WHERE agent_id = $1 AND NOT (document_id = ANY($2::uuid[]))",
                agent_id, ids,
            )
            await conn.execute(
                "INSERT INTO agent_documents (agent_id, document_id) SELECT $1, unnest($2::uuid[]) "
                "ON CONFLICT DO NOTHING",
                agent_id, ids,
            )
            await audit.write_audit(
                conn, entity_type="agent_documents", entity_id=agent_id, action="updated",
                user_id=user_id, user_email=user_email,
                old_value={"document_ids": old}, new_value={"document_ids": sorted(ids)},
            )
    await _refresh_flag(agent_id)
    return await list_for_agent(agent_id)


async def has_enabled_kb(tenant_slug: str, agent_slug: str) -> bool:
    """Postgres fallback for a Redis miss, by slugs; the router sets the target tenant first."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        row = await conn.fetchrow(
            _HAS_DOCUMENTS_SQL.format(
                where="JOIN tenants t ON t.id = a.tenant_id AND t.deleted_at IS NULL WHERE t.slug = $1 AND a.slug = $2",
            ),
            tenant_slug, agent_slug,
        )
    # Refill with SET NX: this value predates any writer that committed while we were reading (see cache.fill_...).
    await cache.fill_has_enabled_kb(tenant_slug, agent_slug, bool(row["has_kb"]))
    return bool(row["has_kb"])
