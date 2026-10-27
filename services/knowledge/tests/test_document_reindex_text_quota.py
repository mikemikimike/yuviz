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

async def test_text_returns_sections_and_parentless_passages_in_order(client, pool, test_tenant, test_viewer):
    kb = await _kb(test_tenant)
    doc = await add_document(pool, test_tenant, kb, "Handbook", [])

    async def chunk(index, content, *, is_parent=False, parent_id=None):
        return await pool.fetchval(
            "INSERT INTO kb_chunks (document_id, kb_id, tenant_id, chunk_index, content, is_parent, parent_id) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
            doc["id"], kb["id"], test_tenant["id"], index, content, is_parent, parent_id,
        )

    intro = await chunk(-1, "Intro one. Intro two.", is_parent=True)
    refunds = await chunk(-2, "Refunds one. Refunds two.", is_parent=True)
    await chunk(0, "Intro one.", parent_id=intro)
    await chunk(1, "Intro two.", parent_id=intro)
    await chunk(2, "Hours: open 9 to 5.")
    await chunk(3, "Refunds one.", parent_id=refunds)
    await chunk(4, "Refunds two.", parent_id=refunds)

    body = (await client.get(f"/documents/{doc['id']}/text", headers=_auth(test_viewer))).json()

    assert body["source"] == "sections" and body["truncated"] is False
    assert body["text"] == "Intro one. Intro two.\n\nHours: open 9 to 5.\n\nRefunds one. Refunds two."


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


async def test_document_agents_lists_only_the_agents_linked_to_that_document(
    client, pool, test_tenant, test_viewer, other_tenant_admin,
):
    kb = await _kb(test_tenant)
    used = await add_document(pool, test_tenant, kb, "Used", ["used"])
    unused = await add_document(pool, test_tenant, kb, "Unused", ["unused"])
    agent = dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sup', 'Support') RETURNING *", test_tenant["id"],
    ))
    await agent_kb_service.replace_documents(agent["id"], [str(used["id"])])

    used_agents = (await client.get(f"/documents/{used['id']}/agents", headers=_auth(test_viewer))).json()
    unused_agents = (await client.get(f"/documents/{unused['id']}/agents", headers=_auth(test_viewer))).json()
    foreign = await client.get(f"/documents/{used['id']}/agents", headers=_auth(other_tenant_admin))

    assert [a["agent_name"] for a in used_agents] == ["Support"]
    assert unused_agents == []
    assert foreign.status_code == 404


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


async def test_documents_in_a_deleted_folder_do_not_use_up_the_quota(client, pool, test_tenant, test_admin):
    gone = await _kb(test_tenant, "old-folder")
    for i in range(3):
        await add_document(pool, test_tenant, gone, f"Old {i}", ["old"])
    await pool.execute("UPDATE kb_documents SET byte_size = 100 WHERE kb_id = $1", gone["id"])
    await kb_service.soft_delete_knowledge_base(gone["id"])
    kb = await _kb(test_tenant)
    await _limit(pool, test_tenant, documents=1, storage=150)

    used = await quotas.tenant_usage(test_tenant["id"])
    resp = await _upload(client, kb, test_admin, "a.txt")

    assert (used["documents"], used["storage_bytes"]) == (0, 0)
    assert resp.status_code == 201


async def test_a_tenant_over_its_limits_can_still_replace_a_file(client, pool, test_tenant, test_admin):
    kb = await _kb(test_tenant)
    original = (await _upload(client, kb, test_admin, "a.txt", body=b"0123456789")).json()
    await _upload(client, kb, test_admin, "b.txt", body=b"0123456789")
    await _upload(client, kb, test_admin, "c.txt", body=b"0123456789")
    await _limit(pool, test_tenant, documents=1, storage=15)

    same_size = await _upload(client, kb, test_admin, "a-v2.txt", body=b"abcdefghij", replaces=original["id"])
    pending = await _live_documents(pool, test_tenant)
    # The console deletes the original once the replacement is ready.
    await client.delete(f"/documents/{original['id']}", headers=_auth(test_admin))
    bigger = await _upload(client, kb, test_admin, "a-v3.txt", body=b"abcdefghijk", replaces=same_size.json()["id"])

    assert same_size.status_code == 201 and pending == 4
    assert bigger.status_code == 413  # growing storage past the limit is still refused
    assert await _live_documents(pool, test_tenant) == 3


async def _live_documents(pool, tenant) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM kb_documents WHERE tenant_id = $1 AND deleted_at IS NULL", tenant["id"],
    )


async def test_a_replacement_whose_original_was_deleted_meanwhile_is_refused(client, pool, test_tenant, test_admin):
    """`replaces` is checked under the quota lock; a DELETE can commit while the upload waits for it."""
    from libs.tenancy import tenant_conn

    kb = await _kb(test_tenant)
    x = (await _upload(client, kb, test_admin, "x.txt")).json()
    await _upload(client, kb, test_admin, "y.txt")
    await _limit(pool, test_tenant, documents=1)
    await client.delete(f"/documents/{x['id']}", headers=_auth(test_admin))

    with pytest.raises(LookupError):
        async with tenant_conn(pool) as conn:
            await quotas.check_upload(conn, test_tenant["id"], 5, replaces=x["id"])


async def test_only_one_same_size_replacement_may_be_pending_while_over_the_storage_limit(
    client, pool, test_tenant, test_admin,
):
    kb = await _kb(test_tenant)
    x = (await _upload(client, kb, test_admin, "x.txt", body=b"0123456789")).json()
    await _limit(pool, test_tenant, storage=5)

    first = await _upload(client, kb, test_admin, "x-v2.txt", body=b"abcdefghij", replaces=x["id"])
    second = await _upload(client, kb, test_admin, "x-v3.txt", body=b"abcdefghij", replaces=x["id"])

    assert (first.status_code, second.status_code) == (201, 413)
    assert (await quotas.tenant_usage(test_tenant["id"]))["storage_bytes"] == 20


async def _counted(tenant) -> int:
    return (await quotas.tenant_usage(tenant["id"]))["documents"]


async def test_a_document_in_a_deleted_folder_cannot_be_replaced(client, pool, test_tenant, test_admin):
    """It isn't counted, so replacing it would add a document past the limit; the console can't show it anyway."""
    hidden_kb = await _kb(test_tenant, "old-folder")
    hidden = [(await _upload(client, hidden_kb, test_admin, f"h{i}.txt")).json() for i in range(3)]
    await kb_service.soft_delete_knowledge_base(hidden_kb["id"])
    kb = await _kb(test_tenant)
    await _upload(client, kb, test_admin, "a.txt")
    await _limit(pool, test_tenant, documents=1)

    results = [await _upload(client, kb, test_admin, f"r{i}.txt", replaces=h["id"]) for i, h in enumerate(hidden)]
    for h in hidden:  # deleting a hidden original must not open a slot either
        await client.delete(f"/documents/{h['id']}", headers=_auth(test_admin))
    again = await _upload(client, kb, test_admin, "b.txt")

    assert [r.status_code for r in results] == [404, 404, 404]
    assert again.status_code == 409
    assert await _counted(test_tenant) == 1


async def test_deleting_folders_during_a_replacement_never_lets_usage_pass_one_over(
    client, pool, test_tenant, test_admin,
):
    f1, f2, f3 = await _kb(test_tenant, "f1"), await _kb(test_tenant, "f2"), await _kb(test_tenant, "f3")
    a = (await _upload(client, f1, test_admin, "a.txt")).json()
    b = (await _upload(client, f2, test_admin, "b.txt")).json()
    await _limit(pool, test_tenant, documents=2)
    seen = []

    # The replacement lands in another folder, and that folder is deleted: the replacement is hidden, so nothing is
    # pending and usage is back to the two originals.
    r1 = await _upload(client, f3, test_admin, "a-v2.txt", replaces=a["id"])
    seen.append(await _counted(test_tenant))
    await kb_service.soft_delete_knowledge_base(f3["id"])
    seen.append(await _counted(test_tenant))
    # The original's folder is deleted while its replacement is pending: the original is no longer counted.
    r2 = await _upload(client, f2, test_admin, "a-v3.txt", replaces=a["id"])
    seen.append(await _counted(test_tenant))
    await kb_service.soft_delete_knowledge_base(f1["id"])
    seen.append(await _counted(test_tenant))
    r3 = await _upload(client, f2, test_admin, "b-v2.txt", replaces=b["id"])
    seen.append(await _counted(test_tenant))
    extra = await _upload(client, f2, test_admin, "c.txt")
    seen.append(await _counted(test_tenant))

    assert [r.status_code for r in (r1, r2, r3)] == [201, 201, 201]
    assert extra.status_code == 409
    assert seen == [3, 2, 3, 2, 3, 3]
    assert max(seen) <= 2 + 1


async def test_concurrent_replacements_and_a_delete_of_the_original_stay_within_one_over(
    client, pool, test_tenant, test_admin,
):
    kb = await _kb(test_tenant)
    x = (await _upload(client, kb, test_admin, "x.txt")).json()
    await _limit(pool, test_tenant, documents=1)

    async def replace(i):
        return (await _upload(client, kb, test_admin, f"x-v{i}.txt", replaces=x["id"])).status_code

    async def delete():
        return (await client.delete(f"/documents/{x['id']}", headers=_auth(test_admin))).status_code

    for _ in range(3):
        codes = await asyncio.gather(replace(1), replace(2), delete(), replace(3), replace(4))
        assert codes[2] == 204
        assert sum(c == 201 for c in codes) <= 1
        assert await _counted(test_tenant) <= 1  # the original is gone, so at most its one replacement is left
        live = await pool.fetchval(
            "SELECT id FROM kb_documents WHERE tenant_id = $1 AND deleted_at IS NULL LIMIT 1", test_tenant["id"],
        )
        if live is None:
            x = (await _upload(client, kb, test_admin, "x.txt")).json()
        else:
            x = {"id": str(live)}


async def test_only_one_replacement_may_be_pending_while_over_the_limit(client, pool, test_tenant, test_admin):
    kb = await _kb(test_tenant)
    a = (await _upload(client, kb, test_admin, "a.txt")).json()
    b = (await _upload(client, kb, test_admin, "b.txt")).json()
    await _limit(pool, test_tenant, documents=1)

    first = await _upload(client, kb, test_admin, "a-v2.txt", replaces=a["id"])
    same_again = await _upload(client, kb, test_admin, "a-v3.txt", replaces=a["id"])
    other = await _upload(client, kb, test_admin, "b-v2.txt", replaces=b["id"])
    chained = await _upload(client, kb, test_admin, "a-v2-v2.txt", replaces=first.json()["id"])
    live_while_pending = await _live_documents(pool, test_tenant)
    await client.delete(f"/documents/{a['id']}", headers=_auth(test_admin))  # the first replacement completes
    after = await _upload(client, kb, test_admin, "b-v2.txt", replaces=b["id"])

    assert first.status_code == 201
    assert same_again.status_code == other.status_code == chained.status_code == 409
    assert live_while_pending == 3
    assert after.status_code == 201


async def test_concurrent_saves_of_an_agents_documents_leave_one_set_not_the_union(pool, test_tenant):
    kb = await _kb(test_tenant)
    docs = [str((await add_document(pool, test_tenant, kb, f"D{i}", [f"d{i}"]))["id"]) for i in range(4)]
    agent = await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sup', 'Support') RETURNING id", test_tenant["id"],
    )
    a, b = docs[:2], docs[2:]

    for _ in range(5):
        await agent_kb_service.replace_documents(agent["id"], [])
        await asyncio.gather(
            agent_kb_service.replace_documents(agent["id"], a), agent_kb_service.replace_documents(agent["id"], b),
        )
        linked = {str(r["document_id"]) for r in await agent_kb_service.list_for_agent(agent["id"])}
        assert linked in (set(a), set(b))


def _backfill_script():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "backfill_kb_byte_size", Path(__file__).resolve().parents[3] / "scripts/backfill_kb_byte_size.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_byte_size_backfill_sizes_legacy_documents_from_storage_and_is_rerunnable(pool, test_tenant, tmp_path):
    kb = await _kb(test_tenant)
    storage = LocalStorageProvider(str(tmp_path))
    legacy, missing, sized = [await add_document(pool, test_tenant, kb, t, ["x"]) for t in ("Legacy", "Missing", "Sized")]
    ref = await storage.save(str(test_tenant["id"]), str(kb["id"]), "legacy.txt", b"0123456789" * 3)
    await pool.execute("UPDATE kb_documents SET source_ref = $2 WHERE id = $1", legacy["id"], ref)
    await pool.execute("UPDATE kb_documents SET source_ref = $2 WHERE id = $1", missing["id"], str(tmp_path / "gone"))
    await pool.execute("UPDATE kb_documents SET byte_size = 7 WHERE id = $1", sized["id"])
    script = _backfill_script()

    async with pool.acquire() as conn:
        first = await script.backfill(conn, storage, tenant_id=test_tenant["id"])
        second = await script.backfill(conn, storage, tenant_id=test_tenant["id"])

    assert first == (1, [str(missing["id"])]) and second == (0, [str(missing["id"])])
    sizes = {r["id"]: r["byte_size"] for r in await pool.fetch(
        "SELECT id, byte_size FROM kb_documents WHERE tenant_id = $1", test_tenant["id"])}
    assert (sizes[legacy["id"]], sizes[missing["id"]], sizes[sized["id"]]) == (30, None, 7)
    assert (await quotas.tenant_usage(test_tenant["id"]))["storage_bytes"] == 37


def test_window_join_stops_once_past_the_limit():
    chunks = [f"w{i}a w{i}b" for i in range(10_000)]

    text = documents_service._join_windows(chunks, limit=50)

    assert 50 < len(text) < 70
    assert documents_service._join_windows(chunks[:3]) == "w0a w0b w1a w1b w2a w2b"
