"""Per-tenant knowledge limits: live documents and their stored bytes. A NULL tenant column means the platform default."""

from __future__ import annotations

from typing import Any

import asyncpg

from libs.tenancy import tenant_conn

from . import db
from .envparse import env_int

DEFAULT_MAX_DOCUMENTS = env_int("KNOWLEDGE_DEFAULT_MAX_DOCUMENTS", 100, lo=1, hi=100_000)
DEFAULT_MAX_STORAGE_BYTES = env_int("KNOWLEDGE_DEFAULT_MAX_STORAGE_BYTES", 100 * 1024 * 1024, lo=1, hi=10**12)


class QuotaExceeded(Exception):
    def __init__(self, kind: str, used: int, limit: int) -> None:
        self.kind, self.used, self.limit = kind, used, limit
        super().__init__(f"{kind} limit reached ({used} of {limit})")


async def usage(conn: asyncpg.Connection, tenant_id: Any, *, excluding: Any | None = None) -> dict[str, int]:
    """`excluding` leaves one document out, so replacing a file counts the net change."""
    row = await conn.fetchrow(
        "SELECT t.knowledge_max_documents, t.knowledge_max_storage_bytes, "
        "  (SELECT count(*) FROM kb_documents d "
        "    WHERE d.tenant_id = t.id AND d.deleted_at IS NULL AND d.id IS DISTINCT FROM $2::uuid) AS documents, "
        "  (SELECT coalesce(sum(d.byte_size), 0) FROM kb_documents d "
        "    WHERE d.tenant_id = t.id AND d.deleted_at IS NULL AND d.id IS DISTINCT FROM $2::uuid) AS storage_bytes "
        "FROM tenants t WHERE t.id = $1",
        tenant_id, excluding,
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
    u = await usage(conn, tenant_id, excluding=replaces)
    if u["documents"] + 1 > u["max_documents"]:
        raise QuotaExceeded("documents", u["documents"], u["max_documents"])
    if u["storage_bytes"] + size > u["max_storage_bytes"]:
        raise QuotaExceeded("storage", u["storage_bytes"], u["max_storage_bytes"])
