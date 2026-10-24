"""Transfer-destination policy: write-time 4xx and the tenant opt-in (#168)."""

from __future__ import annotations

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
async def test_premium_rate_is_refused_even_after_opt_in(admin_client, test_tenant, agent, destination):
    opt_in = await admin_client.patch(
        f"/tenants/{test_tenant['id']}/transfer-policy", json={"allow_international_transfers": True},
    )
    assert opt_in.status_code == 200, opt_in.text
    resp = await _set_destination(admin_client, test_tenant, agent, destination)
    assert resp.status_code == 422, resp.text


async def test_opt_in_allows_international_and_external_sip(admin_client, test_tenant, agent):
    await admin_client.patch(
        f"/tenants/{test_tenant['id']}/transfer-policy", json={"allow_international_transfers": True},
    )
    for destination in ("+442071234567", "sip:agent@pbx.example.com"):
        resp = await _set_destination(admin_client, test_tenant, agent, destination)
        assert resp.status_code == 200, resp.text


async def test_home_calling_code_decides_what_is_domestic(admin_client, test_tenant, agent):
    resp = await admin_client.patch(
        f"/tenants/{test_tenant['id']}/transfer-policy", json={"home_calling_code": "44"},
    )
    assert resp.status_code == 200, resp.text
    assert (await _set_destination(admin_client, test_tenant, agent, "+442071234567")).status_code == 200
    assert (await _set_destination(admin_client, test_tenant, agent, "+12125550100")).status_code == 422


async def test_admin_cannot_opt_in_another_tenant(admin_client, pool):
    other = await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ('Foreign', $1) RETURNING id",
        f"test-foreign-{uuid.uuid4().hex[:8]}",
    )
    try:
        resp = await admin_client.patch(
            f"/tenants/{other['id']}/transfer-policy", json={"allow_international_transfers": True},
        )
        assert resp.status_code == 404
        assert not await pool.fetchval(
            "SELECT allow_international_transfers FROM tenants WHERE id = $1", other["id"],
        )
    finally:
        await pool.execute("DELETE FROM tenants WHERE id = $1", other["id"])


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
