"""Transfer-destination policy: write-time 4xx and the tenant opt-in (#168)."""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from services.config import agents
from services.config.app import app


def _client(token: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.fixture
async def admin_client(test_admin):
    async with _client(test_admin["token"]) as c:
        yield c


@pytest.fixture
async def superadmin_client(test_superadmin):
    async with _client(test_superadmin["token"]) as c:
        yield c


@pytest.fixture
async def viewer_client(test_viewer):
    async with _client(test_viewer["token"]) as c:
        yield c


@pytest.fixture
async def agent(test_tenant, scoped):
    return await agents.create_agent(tenant_id=test_tenant["id"], slug="desk", name="Desk")


async def _set_destination(client, tenant, agent, destination):
    return await client.patch(
        f"/tenants/{tenant['slug']}/agents/{agent['id']}",
        json={"transfer_type": "cold", "transfer_destination": destination},
    )


@pytest.mark.parametrize("destination", [
    "+442071234567", "00442071234567", "sip:agent@attacker.example", "+8821234567890",
])
async def test_agent_update_refuses_destination_outside_default_policy(
    admin_client, test_tenant, agent, destination,
):
    resp = await _set_destination(admin_client, test_tenant, agent, destination)
    assert resp.status_code == 422, resp.text
    assert "transfer_destination" in resp.json()["detail"]


@pytest.mark.parametrize("destination", ["1001", "+18005550100", "+12125550100"])
async def test_agent_update_keeps_domestic_destinations_working(admin_client, test_tenant, agent, destination):
    resp = await _set_destination(admin_client, test_tenant, agent, destination)
    assert resp.status_code == 200, resp.text
    assert resp.json()["transfer_destination"] == destination


@pytest.mark.parametrize("destination", [
    "+19005550100", "+19765550100", "+8821234567890", "+8835551234", "+870123456789",
    "+9791234567", "+449091234567", "00882123456789", "sip:+8821234567890@pbx.example.com",
])
async def test_premium_rate_is_refused_even_after_opt_in(
    admin_client, superadmin_client, test_tenant, agent, destination,
):
    opt_in = await superadmin_client.patch(
        f"/tenants/{test_tenant['id']}/transfer-policy", json={"allow_international_transfers": True},
    )
    assert opt_in.status_code == 200, opt_in.text
    resp = await _set_destination(admin_client, test_tenant, agent, destination)
    assert resp.status_code == 422, resp.text


async def test_opt_in_allows_international_and_external_sip(admin_client, superadmin_client, test_tenant, agent):
    await superadmin_client.patch(
        f"/tenants/{test_tenant['id']}/transfer-policy", json={"allow_international_transfers": True},
    )
    for destination in ("+442071234567", "sip:agent@pbx.example.com"):
        resp = await _set_destination(admin_client, test_tenant, agent, destination)
        assert resp.status_code == 200, resp.text


async def test_home_calling_code_decides_what_is_domestic(admin_client, superadmin_client, test_tenant, agent):
    resp = await superadmin_client.patch(
        f"/tenants/{test_tenant['id']}/transfer-policy", json={"home_calling_code": "44"},
    )
    assert resp.status_code == 200, resp.text
    assert (await _set_destination(admin_client, test_tenant, agent, "+442071234567")).status_code == 200
    assert (await _set_destination(admin_client, test_tenant, agent, "+12125550100")).status_code == 422


@pytest.mark.parametrize("body", [
    {"allow_international_transfers": True},
    {"home_calling_code": "3"},
    {"home_calling_code": "44", "allow_international_transfers": True},
])
async def test_tenant_admin_cannot_change_the_policy_of_any_tenant(admin_client, test_tenant, pool, body):
    other = await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ('Foreign', $1) RETURNING id",
        f"test-foreign-{uuid.uuid4().hex[:8]}",
    )
    try:
        for tenant_id in (test_tenant["id"], other["id"]):
            resp = await admin_client.patch(f"/tenants/{tenant_id}/transfer-policy", json=body)
            assert resp.status_code == 403, resp.text
            row = await pool.fetchrow(
                "SELECT home_calling_code, allow_international_transfers FROM tenants WHERE id = $1", tenant_id,
            )
            assert (row["home_calling_code"], row["allow_international_transfers"]) == ("1", False)
    finally:
        await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])


async def test_superadmin_sets_the_policy_and_it_is_audited(superadmin_client, test_superadmin, test_tenant, pool):
    resp = await superadmin_client.patch(
        f"/tenants/{test_tenant['id']}/transfer-policy",
        json={"home_calling_code": "91", "allow_international_transfers": True},
    )
    assert resp.status_code == 200, resp.text
    row = await pool.fetchrow(
        "SELECT home_calling_code, allow_international_transfers FROM tenants WHERE id = $1", test_tenant["id"],
    )
    assert (row["home_calling_code"], row["allow_international_transfers"]) == ("91", True)
    audit = await pool.fetchrow(
        "SELECT user_id, old_value, new_value FROM audit_log WHERE entity_type = 'tenant' AND entity_id = $1 "
        "ORDER BY id DESC LIMIT 1", test_tenant["id"],
    )
    assert audit["user_id"] == test_superadmin["user"]["id"]
    assert json.loads(audit["old_value"])["home_calling_code"] == "1"
    assert json.loads(audit["new_value"])["home_calling_code"] == "91"


@pytest.mark.parametrize("code", ["3", "4", "5", "0", "999", "882", "800", "1234", "+91", "abc", ""])
async def test_home_calling_code_must_be_a_real_country_code(superadmin_client, test_tenant, pool, code):
    resp = await superadmin_client.patch(
        f"/tenants/{test_tenant['id']}/transfer-policy", json={"home_calling_code": code},
    )
    assert resp.status_code == 422, resp.text
    assert await pool.fetchval("SELECT home_calling_code FROM tenants WHERE id = $1", test_tenant["id"]) == "1"


async def test_viewer_cannot_opt_in(viewer_client, test_tenant, pool):
    resp = await viewer_client.patch(
        f"/tenants/{test_tenant['id']}/transfer-policy", json={"allow_international_transfers": True},
    )
    assert resp.status_code == 403
    assert not await pool.fetchval(
        "SELECT allow_international_transfers FROM tenants WHERE id = $1", test_tenant["id"],
    )


async def test_tenant_admin_cannot_use_the_superadmin_patch_to_opt_in(admin_client, test_tenant):
    resp = await admin_client.patch(
        f"/tenants/{test_tenant['id']}", json={"allow_international_transfers": True},
    )
    assert resp.status_code == 403
