"""Tenant checks on agent_id-keyed routes (retrieval-policy GET/PUT, knowledge-bases GET) via the ASGI app."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from libs.tenancy import set_caller_tenant
from services.knowledge import knowledge_bases as kb_service
from services.knowledge.app import app
from services.knowledge.tests.rag_fakes import add_document


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _make_agent(pool, tenant):
    return dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sup', 'Support') RETURNING *", tenant["id"],
    ))


async def test_retrieval_policy_get_cross_tenant_is_403_unknown_agent_is_404(
    client, pool, test_tenant, test_admin, other_tenant_admin,
):
    # Standard convention here (unlike knowledge_bases.py): cross-tenant 403, unknown 404.
    agent = await _make_agent(pool, test_tenant)

    cross_resp = await client.get(
        f"/agents/{agent['id']}/retrieval-policy",
        headers={"Authorization": f"Bearer {other_tenant_admin['token']}"},
    )
    unknown_resp = await client.get(
        "/agents/00000000-0000-0000-0000-000000000000/retrieval-policy",
        headers={"Authorization": f"Bearer {test_admin['token']}"},
    )
    assert cross_resp.status_code == 403
    assert unknown_resp.status_code == 404


async def test_retrieval_policy_get_own_tenant_is_200(client, pool, test_tenant, test_admin):
    agent = await _make_agent(pool, test_tenant)
    resp = await client.get(
        f"/agents/{agent['id']}/retrieval-policy",
        headers={"Authorization": f"Bearer {test_admin['token']}"},
    )
    assert resp.status_code == 200


async def test_retrieval_policy_put_cross_tenant_is_403_not_written(
    client, pool, test_tenant, other_tenant_admin,
):
    agent = await _make_agent(pool, test_tenant)
    resp = await client.put(
        f"/agents/{agent['id']}/retrieval-policy",
        json={"top_k": 3},
        headers={"Authorization": f"Bearer {other_tenant_admin['token']}"},
    )
    assert resp.status_code == 403
    row = await pool.fetchrow("SELECT * FROM agent_retrieval_policies WHERE agent_id = $1", agent["id"])
    assert row is None


async def test_agent_documents_get_cross_tenant_is_404_like_unknown_agent(
    client, pool, test_tenant, test_admin, other_tenant_admin,
):
    agent = await _make_agent(pool, test_tenant)

    cross_resp = await client.get(
        f"/agents/{agent['id']}/documents",
        headers={"Authorization": f"Bearer {other_tenant_admin['token']}"},
    )
    unknown_resp = await client.get(
        "/agents/00000000-0000-0000-0000-000000000000/documents",
        headers={"Authorization": f"Bearer {test_admin['token']}"},
    )
    assert cross_resp.status_code == 404
    assert unknown_resp.status_code == 404


async def test_agent_documents_get_own_tenant_is_200(client, pool, test_tenant, test_admin):
    agent = await _make_agent(pool, test_tenant)
    resp = await client.get(
        f"/agents/{agent['id']}/documents",
        headers={"Authorization": f"Bearer {test_admin['token']}"},
    )
    assert resp.status_code == 200
    assert resp.json() == []


async def test_agent_documents_put_replaces_the_set_and_get_returns_it(client, pool, test_tenant, test_admin):
    agent = await _make_agent(pool, test_tenant)
    set_caller_tenant(str(test_tenant["id"]))
    kb = await kb_service.create_knowledge_base(tenant_id=test_tenant["id"], slug="docs", name="Docs")
    doc = await add_document(pool, test_tenant, kb, "Refunds", ["Refunds take 30 days."])
    auth = {"Authorization": f"Bearer {test_admin['token']}"}

    put = await client.put(f"/agents/{agent['id']}/documents", json={"document_ids": [str(doc["id"])]}, headers=auth)
    got = await client.get(f"/agents/{agent['id']}/documents", headers=auth)

    assert put.status_code == 200
    assert [row["document_id"] for row in got.json()] == [str(doc["id"])]


async def test_agent_documents_put_refuses_another_tenants_document_with_404(
    client, pool, test_tenant, test_admin, other_tenant_admin,
):
    agent = await _make_agent(pool, test_tenant)
    other = other_tenant_admin["tenant"]
    set_caller_tenant(str(other["id"]))
    kb = await kb_service.create_knowledge_base(tenant_id=other["id"], slug="theirs", name="Theirs")
    try:
        doc = await add_document(pool, other, kb, "Theirs", ["Their text."])
        resp = await client.put(
            f"/agents/{agent['id']}/documents", json={"document_ids": [str(doc["id"])]},
            headers={"Authorization": f"Bearer {test_admin['token']}"},
        )
        assert resp.status_code == 404
        assert await pool.fetchval("SELECT count(*) FROM agent_documents WHERE agent_id = $1", agent["id"]) == 0
    finally:
        await pool.execute("DELETE FROM kb_chunks WHERE tenant_id = $1", other["id"])
        await pool.execute("DELETE FROM kb_documents WHERE tenant_id = $1", other["id"])
        await pool.execute("DELETE FROM knowledge_bases WHERE tenant_id = $1", other["id"])


async def test_agent_documents_put_is_admin_only(client, pool, test_tenant, test_viewer):
    agent = await _make_agent(pool, test_tenant)
    resp = await client.put(
        f"/agents/{agent['id']}/documents", json={"document_ids": []},
        headers={"Authorization": f"Bearer {test_viewer['token']}"},
    )
    assert resp.status_code == 403


@pytest.mark.parametrize("method, path", [
    ("GET", "/knowledge-bases"), ("POST", "/knowledge-bases"),
    ("PATCH", "/knowledge-bases/00000000-0000-0000-0000-000000000000"),
    ("DELETE", "/knowledge-bases/00000000-0000-0000-0000-000000000000"),
])
async def test_old_knowledge_base_link_routes_are_gone(client, pool, test_tenant, test_admin, method, path):
    agent = await _make_agent(pool, test_tenant)
    resp = await client.request(
        method, f"/agents/{agent['id']}{path}", headers={"Authorization": f"Bearer {test_admin['token']}"},
    )
    assert resp.status_code == 410
