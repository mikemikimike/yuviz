from __future__ import annotations

import uuid

import pytest

from libs.tenancy import set_caller_tenant
from services.config import provider_configs
from services.knowledge import agent_kb as agent_kb_service
from services.knowledge import cache, knowledge_bases as kb_service

from rag_fakes import add_document


async def _make_kb(tenant):
    set_caller_tenant(str(tenant["id"]))
    embedding_cfg = await provider_configs.create_provider_config(
        tenant_id=tenant["id"], name="Embed", role="embedding", engine="ollama",
        allow_pointer_schemes=False,
    )
    return await kb_service.create_knowledge_base(
        tenant_id=tenant["id"], slug="policies", name="Policies", embedding_config_id=embedding_cfg["id"],
    )


async def _doc(pool, tenant, kb, title="Refunds"):
    return await add_document(pool, tenant, kb, title, [f"{title} text"])


async def test_has_enabled_kb_false_when_nothing_linked(tenant_agent):
    tenant, agent = tenant_agent
    set_caller_tenant(str(tenant["id"]))
    assert await agent_kb_service.has_enabled_kb(tenant["slug"], agent["slug"]) is False


async def test_linking_a_document_makes_has_enabled_kb_true_and_writes_redis_flag(pool, tenant_agent):
    tenant, agent = tenant_agent
    kb = await _make_kb(tenant)
    doc = await _doc(pool, tenant, kb)

    linked = await agent_kb_service.replace_documents(agent["id"], [str(doc["id"])])

    assert [str(row["document_id"]) for row in linked] == [str(doc["id"])]
    assert await agent_kb_service.has_enabled_kb(tenant["slug"], agent["slug"]) is True
    assert await cache.get_has_enabled_kb(tenant["slug"], agent["slug"]) is True


async def test_replace_with_an_empty_set_unlinks_everything_and_flips_flag_false(pool, tenant_agent):
    tenant, agent = tenant_agent
    kb = await _make_kb(tenant)
    doc = await _doc(pool, tenant, kb)
    await agent_kb_service.replace_documents(agent["id"], [str(doc["id"])])

    assert await agent_kb_service.replace_documents(agent["id"], []) == []

    assert await agent_kb_service.list_for_agent(agent["id"]) == []
    assert await cache.get_has_enabled_kb(tenant["slug"], agent["slug"]) is False


async def test_deleting_the_agents_only_document_flips_the_flag_false(pool, tenant_agent):
    from services.knowledge import documents as documents_service
    tenant, agent = tenant_agent
    kb = await _make_kb(tenant)
    doc = await _doc(pool, tenant, kb)
    await agent_kb_service.replace_documents(agent["id"], [str(doc["id"])])

    await documents_service.soft_delete_document(doc["id"])

    assert await cache.get_has_enabled_kb(tenant["slug"], agent["slug"]) is False


async def test_replace_keeps_only_the_given_documents(pool, tenant_agent):
    tenant, agent = tenant_agent
    kb = await _make_kb(tenant)
    a, b, c = [await _doc(pool, tenant, kb, t) for t in ("A", "B", "C")]
    await agent_kb_service.replace_documents(agent["id"], [str(a["id"]), str(b["id"])])

    linked = await agent_kb_service.replace_documents(agent["id"], [str(b["id"]), str(c["id"])])

    assert {str(row["document_id"]) for row in linked} == {str(b["id"]), str(c["id"])}


@pytest.mark.parametrize("kind", ["missing", "deleted", "foreign"])
async def test_replace_refuses_a_document_that_is_not_a_live_document_of_the_agents_tenant(pool, tenant_agent, kind):
    tenant, agent = tenant_agent
    kb = await _make_kb(tenant)
    kept = await _doc(pool, tenant, kb)
    await agent_kb_service.replace_documents(agent["id"], [str(kept["id"])])
    if kind == "missing":
        bad = str(uuid.uuid4())
    elif kind == "deleted":
        bad = str((await add_document(pool, tenant, kb, "Gone", ["gone"], deleted=True))["id"])
    else:
        other = dict(await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ('Other', $1) RETURNING *", f"other-{uuid.uuid4().hex[:8]}",
        ))
        other_kb = await kb_service.create_knowledge_base(tenant_id=other["id"], slug="x", name="X")
        bad = str((await add_document(pool, other, other_kb, "Theirs", ["theirs"]))["id"])

    with pytest.raises(LookupError):
        await agent_kb_service.replace_documents(agent["id"], [str(kept["id"]), bad])

    # Nothing changed: the refused call is all or nothing.
    assert [str(row["document_id"]) for row in await agent_kb_service.list_for_agent(agent["id"])] == [str(kept["id"])]
    if kind == "foreign":
        await pool.execute("DELETE FROM kb_chunks WHERE tenant_id = $1", other["id"])
        await pool.execute("DELETE FROM kb_documents WHERE tenant_id = $1", other["id"])
        await pool.execute("DELETE FROM knowledge_bases WHERE tenant_id = $1", other["id"])
        await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])


async def test_inactive_kb_does_not_count_as_enabled(pool, tenant_agent):
    tenant, agent = tenant_agent
    kb = await _make_kb(tenant)
    doc = await _doc(pool, tenant, kb)
    await agent_kb_service.replace_documents(agent["id"], [str(doc["id"])])
    await kb_service.update_knowledge_base(kb["id"], status="inactive")

    assert await agent_kb_service.has_enabled_kb(tenant["slug"], agent["slug"]) is False


async def test_list_for_kb_returns_empty_when_no_document_is_linked(tenant_agent):
    tenant, _agent = tenant_agent
    kb = await _make_kb(tenant)

    assert await agent_kb_service.list_for_kb(kb["id"]) == []


async def test_two_agents_share_a_document_and_unlinking_one_leaves_the_other(pool, tenant_agent):
    tenant, agent = tenant_agent
    kb = await _make_kb(tenant)
    doc = await _doc(pool, tenant, kb)
    other_agent = dict(await pool.fetchrow(
        "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sales', 'Sales') RETURNING *", tenant["id"],
    ))

    await agent_kb_service.replace_documents(agent["id"], [str(doc["id"])])
    await agent_kb_service.replace_documents(other_agent["id"], [str(doc["id"])])

    both = await agent_kb_service.list_for_kb(kb["id"])
    assert {row["agent_id"] for row in both} == {agent["id"], other_agent["id"]}

    await agent_kb_service.replace_documents(agent["id"], [])

    remaining = await agent_kb_service.list_for_kb(kb["id"])
    assert [row["agent_id"] for row in remaining] == [other_agent["id"]]
    # The KB row and its documents are untouched by an unlink.
    assert await kb_service.get_knowledge_base(kb["id"]) is not None


async def test_list_for_kb_excludes_soft_deleted_agent(pool, tenant_agent):
    tenant, agent = tenant_agent
    kb = await _make_kb(tenant)
    doc = await _doc(pool, tenant, kb)
    await agent_kb_service.replace_documents(agent["id"], [str(doc["id"])])

    await pool.execute("UPDATE agents SET deleted_at = now() WHERE id = $1", agent["id"])

    assert await agent_kb_service.list_for_kb(kb["id"]) == []


async def test_replace_is_audited_with_the_old_and_new_document_sets(pool, tenant_agent):
    tenant, agent = tenant_agent
    kb = await _make_kb(tenant)
    doc = await _doc(pool, tenant, kb)

    await agent_kb_service.replace_documents(agent["id"], [str(doc["id"])], user_email="admin@example.com")

    row = await pool.fetchrow(
        "SELECT old_value, new_value, user_email FROM audit_log "
        "WHERE entity_type = 'agent_documents' AND entity_id = $1 ORDER BY changed_at DESC LIMIT 1", agent["id"],
    )
    assert row["user_email"] == "admin@example.com"
    assert '"document_ids": []' in row["old_value"] and str(doc["id"]) in row["new_value"]
