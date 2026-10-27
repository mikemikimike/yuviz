"""kb_documents CRUD. upload_document() keeps storage bytes, the document row and its ingestion job in sync."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from libs.tenancy import platform_conn, tenant_conn

from . import agent_kb, audit, db, quotas
from . import retrieval_plan as plans
from .storage import StorageProvider

_UPDATABLE_FIELDS = {"title", "language", "tags", "usage_mode"}

# What retry_document accepts, computed for the API so the console only offers Retry when it will work: a failed
# document, or a ready one whose last re-index failed, and in both cases no job already pending or running (an automatic
# transient retry is a pending job).
RETRYABLE_SQL = (
    "COALESCE((d.status = 'failed' OR (d.status IN ('ready', 'ready_degraded') AND d.error LIKE 're-index failed:%')) "
    "AND NOT EXISTS (SELECT 1 FROM kb_ingestion_jobs j WHERE j.document_id = d.id AND j.status IN ('pending', 'running')), false)"
)
# True while an upload, retry or reindex job is queued or running; a reindexed document stays 'ready' meanwhile.
JOB_ACTIVE_SQL = "EXISTS (SELECT 1 FROM kb_ingestion_jobs j WHERE j.document_id = d.id AND j.status IN ('pending', 'running'))"


async def get_document(document_id: Any, *, platform_scoped: bool = False) -> dict[str, Any] | None:
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="document-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        row = await conn.fetchrow(
            f"SELECT d.*, {RETRYABLE_SQL} AS retryable, {JOB_ACTIVE_SQL} AS job_active FROM kb_documents d WHERE d.id = $1 AND d.deleted_at IS NULL",
            document_id,
        )
    return dict(row) if row is not None else None


async def list_documents(kb_id: Any) -> list[dict[str, Any]]:
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        # Not parent rows (no embedding of their own), and regardless of version: during a re-ingest the document keeps
        # serving its old chunks, so the count stays what is searchable.
        rows = await conn.fetch(
            f"SELECT d.*, {RETRYABLE_SQL} AS retryable, {JOB_ACTIVE_SQL} AS job_active, "
            "       COALESCE(c.chunk_count, 0) AS chunk_count, COALESCE(c.token_count, 0) AS token_count "
            "FROM kb_documents d "
            "LEFT JOIN LATERAL (SELECT COUNT(*) AS chunk_count, COALESCE(SUM(token_count), 0) AS token_count "
            "                   FROM kb_chunks WHERE document_id = d.id AND NOT is_parent) c ON true "
            "WHERE d.kb_id = $1 AND d.deleted_at IS NULL ORDER BY d.created_at DESC",
            kb_id,
        )
    return [dict(row) for row in rows]


async def upload_document(
    *,
    tenant_id: Any,
    kb_id: Any,
    title: str,
    filename: str,
    content_type: str,
    content: bytes,
    storage: StorageProvider,
    language: str | None = None,
    tags: dict[str, Any] | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
    replaces: Any | None = None,
) -> dict[str, Any]:
    """`replaces` names the live document this upload will replace: the quota counts the net change and the new
    document is linked to the same agents. Raises LookupError for an unknown one, quotas.QuotaExceeded over a limit."""
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        # Also checks, under the quota lock, that the folder is still live (FolderGone) and that `replaces` is a counted
        # document of this tenant (LookupError).
        await quotas.check_upload(conn, tenant_id, len(content), kb_id=kb_id, replaces=replaces)
        row = await conn.fetchrow(
            "INSERT INTO kb_documents (kb_id, tenant_id, title, source_ref, content_type, language, tags, byte_size, "
            "replaces_id) VALUES ($1, $2, $3, '', $4, $5, $6::jsonb, $7, $8) RETURNING *",
            kb_id, tenant_id, title, content_type, language, json.dumps(tags or {}), len(content), replaces,
        )
        document = dict(row)

        # Save before committing source_ref: a crash leaves an orphan file, never a dangling row.
        source_ref = await storage.save(str(tenant_id), str(kb_id), filename, content)
        row = await conn.fetchrow(
            "UPDATE kb_documents SET source_ref = $2 WHERE id = $1 RETURNING *", document["id"], source_ref,
        )
        document = dict(row)

        job_row = await conn.fetchrow(
            "INSERT INTO kb_ingestion_jobs (document_id, kb_id) VALUES ($1, $2) RETURNING *",
            document["id"], kb_id,
        )
        relinked = 0
        if replaces is not None:
            relinked = int((await conn.execute(
                "INSERT INTO agent_documents (agent_id, document_id) "
                "SELECT agent_id, $2 FROM agent_documents WHERE document_id = $1 ON CONFLICT DO NOTHING",
                replaces, document["id"],
            )).split()[-1])

        await audit.write_audit(
            conn, entity_type="kb_document", entity_id=document["id"], action="created",
            user_id=user_id, user_email=user_email, new_value=document,
        )
    if relinked:
        plans.invalidate_tenant_id(tenant_id)  # those agents' plans must pick up the new document
    return {**document, "ingestion_job_id": job_row["id"]}


async def update_document(
    document_id: Any,
    *,
    platform_scoped: bool = False,
    stamp_tenant: Any | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    if not fields:
        raise ValueError("update_document() called with no fields to update")
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"update_document() got non-updatable field(s): {unknown}")

    if "tags" in fields:
        fields = {**fields, "tags": json.dumps(fields["tags"])}

    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="document-admin-by-id", stamp_tenant=stamp_tenant)
        if platform_scoped else tenant_conn(pool)
    )
    async with conn_cm as conn:
        old_row = await conn.fetchrow("SELECT * FROM kb_documents WHERE id = $1 FOR UPDATE", document_id)
        if old_row is None:
            raise LookupError(f"kb_document {document_id} not found")
        old = dict(old_row)

        columns = list(fields.keys())
        set_parts = []
        for i, col in enumerate(columns):
            cast = "::jsonb" if col == "tags" else ""
            set_parts.append(f"{col} = ${i + 2}{cast}")
        new_row = await conn.fetchrow(
            f"UPDATE kb_documents SET {', '.join(set_parts)}, updated_at = now() WHERE id = $1 RETURNING *",
            document_id, *(fields[col] for col in columns),
        )
        new = dict(new_row)

        await audit.write_audit(
            conn, entity_type="kb_document", entity_id=document_id, action="updated",
            user_id=user_id, user_email=user_email, old_value=old, new_value=new,
        )
    plans.invalidate_tenant_id(old["tenant_id"])  # usage_mode/title edits change the plan's prompt-mode documents
    return new


async def retry_document(
    document_id: Any,
    *,
    tenant_id: Any,
    platform_scoped: bool = False,
    stamp_tenant: Any | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any] | None:
    """Re-queue a failed document's stored file, or a ready one whose last RE-ingest failed (it still serves its old
    chunks and carries the reason in `error`, prefixed 're-index failed: '). None otherwise (or already re-queued)."""
    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="document-admin-by-id", stamp_tenant=stamp_tenant)
        if platform_scoped else tenant_conn(pool)
    )
    async with conn_cm as conn:
        row = await conn.fetchrow(
            "UPDATE kb_documents SET status = CASE WHEN status = 'failed' THEN 'pending' ELSE status END, "
            "error = CASE WHEN status = 'failed' THEN NULL ELSE error END, version = version + 1, updated_at = now() "
            "WHERE id = $1 AND tenant_id = $2 AND deleted_at IS NULL "
            "AND (status = 'failed' OR (status IN ('ready', 'ready_degraded') AND error LIKE 're-index failed:%' "
            "     AND NOT EXISTS (SELECT 1 FROM kb_ingestion_jobs j WHERE j.document_id = kb_documents.id "
            "                     AND j.status IN ('pending', 'running')))) RETURNING *",
            document_id, tenant_id,
        )
        if row is None:
            return None
        document = dict(row)
        job_row = await conn.fetchrow(
            "INSERT INTO kb_ingestion_jobs (document_id, kb_id) VALUES ($1, $2) RETURNING *",
            document["id"], document["kb_id"],
        )
        await audit.write_audit(
            conn, entity_type="kb_document", entity_id=document_id, action="updated",
            user_id=user_id, user_email=user_email, new_value=document,
        )
    return {**document, "ingestion_job_id": job_row["id"]}


async def reindex_document(
    document_id: Any,
    *,
    tenant_id: Any,
    platform_scoped: bool = False,
    stamp_tenant: Any | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> dict[str, Any] | None:
    """Re-queue a ready (or failed) document's stored file at normal priority. A ready one keeps serving its old
    chunks until the worker swaps in the new ones. None when it is in neither state or already has a job queued."""
    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="document-admin-by-id", stamp_tenant=stamp_tenant)
        if platform_scoped else tenant_conn(pool)
    )
    async with conn_cm as conn:
        # Two clicks at once must not queue two jobs: the NOT EXISTS below alone cannot see an uncommitted one.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('kb-reindex:' || $1::text, 0))", str(document_id))
        row = await conn.fetchrow(
            "UPDATE kb_documents SET status = CASE WHEN status = 'failed' THEN 'pending' ELSE status END, "
            "error = CASE WHEN status = 'failed' THEN NULL ELSE error END, version = version + 1, updated_at = now() "
            "WHERE id = $1 AND tenant_id = $2 AND deleted_at IS NULL "
            "AND status IN ('ready', 'ready_degraded', 'failed') "
            "AND NOT EXISTS (SELECT 1 FROM kb_ingestion_jobs j WHERE j.document_id = kb_documents.id "
            "                AND j.status IN ('pending', 'running')) RETURNING *",
            document_id, tenant_id,
        )
        if row is None:
            return None
        document = dict(row)
        job_row = await conn.fetchrow(
            "INSERT INTO kb_ingestion_jobs (document_id, kb_id) VALUES ($1, $2) RETURNING *",
            document["id"], document["kb_id"],
        )
        await audit.write_audit(
            conn, entity_type="kb_document", entity_id=document_id, action="updated",
            user_id=user_id, user_email=user_email, new_value=document,
        )
    return {**document, "ingestion_job_id": job_row["id"]}


TEXT_MAX_CHARS = 200_000
_MAX_OVERLAP_WORDS = 400


def _join_windows(chunks: list[str], limit: int | None = None) -> str:
    """Join fixed-window chunks, dropping the words each one repeats from the end of the one before it. Stops once the
    text passes `limit` characters, so a huge legacy document costs no more than the part the caller returns."""
    words: list[str] = []
    length = -1  # length of " ".join(words)
    for chunk in chunks:
        if limit is not None and length > limit:
            break
        new = chunk.split()
        for k in range(min(len(words), len(new), _MAX_OVERLAP_WORDS), 0, -1):
            if words[-k:] == new[:k]:
                new = new[k:]
                break
        words.extend(new)
        length += sum(len(w) + 1 for w in new)
    return " ".join(words)


async def get_document_text(document_id: Any, *, platform_scoped: bool = False) -> dict[str, Any]:
    """The text extracted when the document was indexed; for one indexed before that was kept, rebuilt from its chunks
    (each section once, short sections with no parent included, fixed windows joined without their overlap)."""
    pool = await db.get_pool()
    conn_cm = platform_conn(pool, reason="document-by-id") if platform_scoped else tenant_conn(pool)
    async with conn_cm as conn:
        stored = await conn.fetchrow("SELECT text, truncated FROM kb_document_texts WHERE document_id = $1", document_id)
        if stored is not None:
            return {
                "document_id": str(document_id), "text": stored["text"], "source": "extracted",
                "truncated": stored["truncated"],
            }
        chunks = await conn.fetch(
            "SELECT id, parent_id, content, is_parent, chunk_index FROM kb_chunks "
            "WHERE document_id = $1",
            document_id,
        )
    if not chunks:
        return {"document_id": str(document_id), "text": "", "source": "chunks", "truncated": False}
    has_parent = any(c["is_parent"] for c in chunks)
    if has_parent:
        chunk_map = {c["id"]: c for c in chunks}
        children = sorted([c for c in chunks if not c["is_parent"]], key=lambda c: c["chunk_index"])
        text_parts: list[str] = []
        seen_parents: set = set()
        for child in children:
            parent = chunk_map.get(child["parent_id"]) if child["parent_id"] else None
            if parent is None:
                text_parts.append(child["content"])  # a short section the chunker gave no parent
            elif parent["id"] not in seen_parents:
                text_parts.append(parent["content"])  # the whole section, which already holds this child's text
                seen_parents.add(parent["id"])
        text, source = "\n\n".join(text_parts), "sections"
    else:
        windows = sorted([c for c in chunks if not c["is_parent"]], key=lambda c: c["chunk_index"])
        # Off the event loop: it is CPU-bound and this worker also serves live-call /retrieve.
        text = await asyncio.to_thread(_join_windows, [c["content"] for c in windows], TEXT_MAX_CHARS)
        source = "chunks"
    return {
        "document_id": str(document_id), "text": text[:TEXT_MAX_CHARS], "source": source,
        "truncated": len(text) > TEXT_MAX_CHARS,
    }


async def soft_delete_document(
    document_id: Any,
    *,
    platform_scoped: bool = False,
    stamp_tenant: Any | None = None,
    user_id: Any | None = None,
    user_email: str | None = None,
) -> None:
    pool = await db.get_pool()
    conn_cm = (
        platform_conn(pool, reason="document-admin-by-id", stamp_tenant=stamp_tenant)
        if platform_scoped else tenant_conn(pool)
    )
    agent_ids: list[Any] = []
    async with conn_cm as conn:
        # The quota lock first, like an upload: a delete must not change what counts while an upload is being checked.
        tenant_id = await conn.fetchval("SELECT tenant_id FROM kb_documents WHERE id = $1", document_id)
        if tenant_id is None:
            raise LookupError(f"kb_document {document_id} not found")
        await quotas.lock_tenant(conn, tenant_id)
        old_row = await conn.fetchrow("SELECT * FROM kb_documents WHERE id = $1 FOR UPDATE", document_id)
        if old_row is None:
            raise LookupError(f"kb_document {document_id} not found")
        old = dict(old_row)
        agent_ids = [row["agent_id"] for row in await conn.fetch(
            "SELECT DISTINCT agent_id FROM agent_documents WHERE document_id = $1", document_id,
        )]

        await conn.execute("UPDATE kb_documents SET deleted_at = now() WHERE id = $1", document_id)
        await audit.write_audit(
            conn, entity_type="kb_document", entity_id=document_id, action="deleted",
            user_id=user_id, user_email=user_email, old_value=old,
        )
    plans.invalidate_tenant_id(old["tenant_id"])  # a deleted prompt-mode document must stop being served now
    for agent_id in agent_ids:
        await agent_kb._refresh_flag(agent_id)
