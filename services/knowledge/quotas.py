"""Per-tenant knowledge limits: live documents and their stored bytes. A NULL tenant column means the platform default."""

from __future__ import annotations

from typing import Any

import asyncpg

from libs.tenancy import tenant_conn

from . import db
from .envparse import env_int

DEFAULT_MAX_DOCUMENTS = env_int("KNOWLEDGE_DEFAULT_MAX_DOCUMENTS", 10, lo=1, hi=100_000)
DEFAULT_MAX_STORAGE_BYTES = env_int("KNOWLEDGE_DEFAULT_MAX_STORAGE_BYTES", 10 * 1024 * 1024, lo=1, hi=10**12)


# Only documents the console lists, and so the tenant can delete: a soft-deleted folder's documents keep
# deleted_at NULL (knowledge_bases.soft_delete_knowledge_base) but are invisible, so they must not use up the quota.
_LISTED = (
    "JOIN knowledge_bases kb ON kb.id = d.kb_id AND kb.deleted_at IS NULL "
    "WHERE d.tenant_id = t.id AND d.deleted_at IS NULL "
)


# A live replacement whose original is still live: a replace the client has not finished.
_PENDING_REPLACEMENT_SQL = (
    "SELECT EXISTS (SELECT 1 FROM kb_documents r JOIN kb_documents o ON o.id = r.replaces_id "
    "WHERE r.tenant_id = $1 AND o.tenant_id = $1 AND r.deleted_at IS NULL AND o.deleted_at IS NULL)"
)


class QuotaExceeded(Exception):
    def __init__(self, kind: str, used: int, limit: int) -> None:
        self.kind, self.used, self.limit = kind, used, limit
        super().__init__(f"{kind} limit reached ({used} of {limit})")


async def usage(conn: asyncpg.Connection, tenant_id: Any) -> dict[str, int]:
    row = await conn.fetchrow(
        "SELECT t.knowledge_max_documents, t.knowledge_max_storage_bytes, "
        "  (SELECT count(*) FROM kb_documents d " + _LISTED + ") AS documents, "
        "  (SELECT coalesce(sum(d.byte_size), 0) FROM kb_documents d " + _LISTED + ") AS storage_bytes "
        "FROM tenants t WHERE t.id = $1",
        tenant_id,
    )
    if row is None:
        raise LookupError(f"tenant {tenant_id!r} not found")
    return {
        "documents": row["documents"],
        "max_documents": row["knowledge_max_documents"] or DEFAULT_MAX_DOCUMENTS,
        "storage_bytes": int(row["storage_bytes"]),
        "max_storage_bytes": row["knowledge_max_storage_bytes"] or DEFAULT_MAX_STORAGE_BYTES,
    }


async def tenant_usage(tenant_id: Any) -> dict[str, int]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        return await usage(conn, tenant_id)


async def check_upload(conn: asyncpg.Connection, tenant_id: Any, size: int, *, replaces: Any | None = None) -> None:
    """Raises QuotaExceeded. Call inside the transaction that inserts the document: the lock serialises a tenant's
    uploads to the end of it, so two parallel uploads cannot both take the last slot."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('kb-quota:' || $1::text, 0))", str(tenant_id))
    u = await usage(conn, tenant_id)
    # A replacement may go over the limits, so a tenant already over one (e.g. from before quotas) can still update its
    # files in place, but only while no other replacement is pending: the client deletes the original once the new file
    # is ready, and until then the tenant holds one extra document. Checked under the lock, so the overage is at most
    # one document, never as many as the tenant cares to upload with the same `replaces`.
    old_size = None
    if replaces is not None and not await conn.fetchval(_PENDING_REPLACEMENT_SQL, tenant_id):
        # Re-checked under the lock: an original deleted since the caller's check is no longer a replacement, so the
        # upload falls through to the normal checks.
        old_size = await conn.fetchval(
            "SELECT coalesce(byte_size, 0) FROM kb_documents WHERE id = $1 AND tenant_id = $2 AND deleted_at IS NULL",
            replaces, tenant_id,
        )
    if old_size is None and u["documents"] + 1 > u["max_documents"]:
        raise QuotaExceeded("documents", u["documents"], u["max_documents"])
    if u["storage_bytes"] + size > u["max_storage_bytes"] and (old_size is None or size > old_size):
        raise QuotaExceeded("storage", u["storage_bytes"], u["max_storage_bytes"])
