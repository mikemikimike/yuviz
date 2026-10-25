"""Reindex, the extracted-text view, per-document token totals and per-tenant quotas, through the real routes."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from libs.tenancy import set_caller_tenant
from services.knowledge import agent_kb as agent_kb_service
from services.knowledge import documents as documents_service
from services.knowledge import knowledge_bases as kb_service
from services.knowledge import quotas
from services.knowledge.app import app
from services.knowledge.routers import documents as documents_router
from services.knowledge.storage import LocalStorageProvider

from rag_fakes import add_document


@pytest.fixture(autouse=True)
def _uploads_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(documents_router, "_storage", LocalStorageProvider(str(tmp_path)))


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {user['token']}"}


async def _kb(tenant, slug="docs"):
    set_caller_tenant(str(tenant["id"]))
    return await kb_service.create_knowledge_base(tenant_id=tenant["id"], slug=slug, name=slug)


async def _upload(client, kb, user, name="a.txt", body=b"Refunds take thirty days.", **form):
    return await client.post(
        f"/knowledge-bases/{kb['id']}/documents", files={"file": (name, body, "text/plain")},
        data={"title": name, **form}, headers=_auth(user),
    )


# ── reindex ─────────────────────────────────────────────────────────────────────

async def test_reindex_queues_a_ready_document_once_and_keeps_it_ready(client, pool, test_tenant, test_admin):
    kb = await _kb(test_tenant)
    doc = await add_document(pool, test_tenant, kb, "Refunds", ["Refunds take thirty days."])

    first = await client.post(f"/documents/{doc['id']}/reindex", headers=_auth(test_admin))
    second = await client.post(f"/documents/{doc['id']}/reindex", headers=_auth(test_admin))

    assert first.status_code == 202 and first.json()["status"] == "ready"
    assert first.json()["version"] == doc["version"] + 1
    assert second.status_code == 409
    assert await pool.fetchval(
        "SELECT count(*) FROM kb_ingestion_jobs WHERE document_id = $1 AND status = 'pending'", doc["id"],
    ) == 1


async def test_two_simultaneous_reindexes_queue_one_job(pool, test_tenant):
    kb = await _kb(test_tenant)
    doc = await add_document(pool, test_tenant, kb, "Refunds", ["Refunds take thirty days."])

    results = await asyncio.gather(*(
        documents_service.reindex_document(doc["id"], tenant_id=test_tenant["id"]) for _ in range(2)
    ))

    assert sum(r is not None for r in results) == 1
    assert await pool.fetchval("SELECT count(*) FROM kb_ingestion_jobs WHERE document_id = $1", doc["id"]) == 1


async def test_reindex_of_a_failed_document_puts_it_back_to_pending(client, pool, test_tenant, test_admin):
    kb = await _kb(test_tenant)
    doc = await add_document(pool, test_tenant, kb, "Broken", ["x"], status="failed")

    resp = await client.post(f"/documents/{doc['id']}/reindex", headers=_auth(test_admin))

    assert resp.status_code == 202 and resp.json()["status"] == "pending"


async def test_reindex_is_admin_only_and_a_foreign_document_is_404(
    client, pool, test_tenant, test_admin, test_viewer, other_tenant_admin,
):
    kb = await _kb(test_tenant)
    doc = await add_document(pool, test_tenant, kb, "Refunds", ["Refunds take thirty days."])

    viewer = await client.post(f"/documents/{doc['id']}/reindex", headers=_auth(test_viewer))
    foreign = await client.post(f"/documents/{doc['id']}/reindex", headers=_auth(other_tenant_admin))
    unknown = await client.post(f"/documents/{uuid.uuid4()}/reindex", headers=_auth(test_admin))

    assert viewer.status_code == 403
    assert foreign.status_code == unknown.status_code == 404


# ── extracted text ──────────────────────────────────────────────────────────────

async def test_text_returns_sections_in_order(client, pool, test_tenant, test_viewer):
    kb = await _kb(test_tenant)
    doc = await add_document(pool, test_tenant, kb, "Handbook", ["child"])
    for ordinal, section in enumerate(["Intro section.", "Refunds section.", "Contact section."]):
        await pool.execute(
            "INSERT INTO kb_chunks (document_id, kb_id, tenant_id, chunk_index, content, is_parent) "
            "VALUES ($1, $2, $3, $4, $5, true)", doc["id"], kb["id"], test_tenant["id"], -1 - ordinal, section,
        )

    body = (await client.get(f"/documents/{doc['id']}/text", headers=_auth(test_viewer))).json()

    assert body["source"] == "sections" and body["truncated"] is False
    assert body["text"] == "Intro section.\n\nRefunds section.\n\nContact section."


async def test_text_joins_windows_without_repeating_their_overlap(client, pool, test_tenant, test_viewer):
    kb = await _kb(test_tenant)
    doc = await add_document(pool, test_tenant, kb, "Policy", [
        "one two three four five six", "five six seven eight nine", "nine ten eleven",
    ])

    body = (await client.get(f"/documents/{doc['id']}/text", headers=_auth(test_viewer))).json()

    assert body["source"] == "chunks"
    assert body["text"] == "one two three four five six seven eight nine ten eleven"


async def test_text_is_truncated_past_the_cap(pool, test_tenant, monkeypatch):
    monkeypatch.setattr(documents_service, "TEXT_MAX_CHARS", 10)
    kb = await _kb(test_tenant)
    doc = await add_document(pool, test_tenant, kb, "Long", ["a much longer passage than ten characters"])

    body = await documents_service.get_document_text(doc["id"])

    assert body["truncated"] is True and len(body["text"]) == 10


async def test_text_of_a_foreign_document_is_404(client, pool, test_tenant, other_tenant_admin):
    kb = await _kb(test_tenant)
    doc = await add_document(pool, test_tenant, kb, "Refunds", ["Refunds take thirty days."])

    resp = await client.get(f"/documents/{doc['id']}/text", headers=_auth(other_tenant_admin))

    assert resp.status_code == 404


# ── token totals ────────────────────────────────────────────────────────────────

async def test_the_document_list_sums_child_chunk_tokens(client, pool, test_tenant, test_viewer):
    kb = await _kb(test_tenant)
    await add_document(pool, test_tenant, kb, "Refunds", ["one two three", "four five"], parent="one two three four five")

    listed = (await client.get(f"/knowledge-bases/{kb['id']}/documents", headers=_auth(test_viewer))).json()

    assert [(d["chunk_count"], d["token_count"]) for d in listed] == [(2, 5)]


# ── quotas and usage ────────────────────────────────────────────────────────────

async def _limit(pool, tenant, documents=None, storage=None):
    await pool.execute(
        "UPDATE tenants SET knowledge_max_documents = $2, knowledge_max_storage_bytes = $3 WHERE id = $1",
        tenant["id"], documents, storage,
    )


async def test_an_upload_past_the_document_limit_is_409(client, pool, test_tenant, test_admin):
    kb = await _kb(test_tenant)
    await _limit(pool, test_tenant, documents=1)

    first = await _upload(client, kb, test_admin, "a.txt")
    second = await _upload(client, kb, test_admin, "b.txt")

    assert first.status_code == 201
    assert second.status_code == 409 and "1 of 1" in second.json()["detail"]


async def test_an_upload_past_the_storage_limit_is_413(client, pool, test_tenant, test_admin):
    kb = await _kb(test_tenant)
    await _limit(pool, test_tenant, storage=10)

    resp = await _upload(client, kb, test_admin, body=b"more than ten bytes of text")

    assert resp.status_code == 413


async def test_a_replacement_counts_the_net_change_and_keeps_the_agents(client, pool, test_tenant, test_admin):
    kb = await _kb(test_tenant)
    original = (await _upload(client, kb, test_admin, "a.txt")).json()
    agent = dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sup', 'Support') RETURNING *", test_tenant["id"],
    ))
    await agent_kb_service.replace_documents(agent["id"], [original["id"]])
    await _limit(pool, test_tenant, documents=1)

    replacement = await _upload(client, kb, test_admin, "a-v2.txt", replaces=original["id"])

    assert replacement.status_code == 201
    linked = {str(r["document_id"]) for r in await agent_kb_service.list_for_agent(agent["id"])}
    assert linked == {original["id"], replacement.json()["id"]}


async def test_replacing_an_unknown_or_foreign_document_is_404(client, pool, test_tenant, test_admin, other_tenant_admin):
    kb = await _kb(test_tenant)
    other_kb = await _kb(other_tenant_admin["tenant"], "theirs")
    try:
        theirs = await add_document(pool, other_tenant_admin["tenant"], other_kb, "Theirs", ["theirs"])
        foreign = await _upload(client, kb, test_admin, replaces=str(theirs["id"]))
        unknown = await _upload(client, kb, test_admin, replaces=str(uuid.uuid4()))
        malformed = await _upload(client, kb, test_admin, replaces="not-a-uuid")
        assert foreign.status_code == unknown.status_code == malformed.status_code == 404
    finally:
        other = other_tenant_admin["tenant"]["id"]
        await pool.execute("DELETE FROM kb_chunks WHERE tenant_id = $1", other)
        await pool.execute("DELETE FROM kb_documents WHERE tenant_id = $1", other)
        await pool.execute("DELETE FROM knowledge_bases WHERE tenant_id = $1", other)


async def test_parallel_uploads_at_the_last_slot_admit_exactly_one(pool, test_tenant, tmp_path):
    kb = await _kb(test_tenant)
    await _limit(pool, test_tenant, documents=1)

    async def upload(name):
        try:
            return await documents_service.upload_document(
                tenant_id=test_tenant["id"], kb_id=kb["id"], title=name, filename=name, content_type="text/plain",
                content=b"text", storage=LocalStorageProvider(str(tmp_path)),
            )
        except quotas.QuotaExceeded:
            return None

    results = await asyncio.gather(upload("a.txt"), upload("b.txt"))

    assert sum(r is not None for r in results) == 1


async def test_usage_reports_counts_limits_and_the_upload_cap(client, pool, test_tenant, test_viewer):
    kb = await _kb(test_tenant)
    await add_document(pool, test_tenant, kb, "Refunds", ["Refunds take thirty days."])
    await pool.execute("UPDATE kb_documents SET byte_size = 25 WHERE tenant_id = $1", test_tenant["id"])
    await _limit(pool, test_tenant, documents=7)

    body = (await client.get(f"/tenants/{test_tenant['id']}/knowledge-usage", headers=_auth(test_viewer))).json()

    assert body == {
        "documents": 1, "max_documents": 7, "storage_bytes": 25,
        "max_storage_bytes": quotas.DEFAULT_MAX_STORAGE_BYTES, "max_upload_bytes": body["max_upload_bytes"],
    }
    assert body["max_upload_bytes"] > 0


async def test_usage_of_another_tenant_is_refused(client, test_tenant, other_tenant_admin):
    resp = await client.get(f"/tenants/{test_tenant['id']}/knowledge-usage", headers=_auth(other_tenant_admin))
    assert resp.status_code in (403, 404)
