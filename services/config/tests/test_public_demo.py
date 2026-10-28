"""POST /public/demo-call against real Postgres and Redis: origin allow-list, demo switch,
per-IP cap, and a credential that only ever opens the configured demo agent."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from libs.config_sdk.test_credentials import redeem_test_credential
from services.config import agents, cache
from services.config.app import app
from services.config.routers.public_demo import PublicDemoThrottle

SITE = "http://localhost:3002"
URL = "/public/demo-call"


@pytest_asyncio.fixture(loop_scope="session")
async def demo_agents(test_tenant, scoped):
    tenant_id, slug = test_tenant["id"], test_tenant["slug"]
    voice = await agents.create_agent(tenant_id=tenant_id, tenant_slug=slug, slug="demo-voice", name="Demo")
    other = await agents.create_agent(tenant_id=tenant_id, tenant_slug=slug, slug="other-voice", name="Other")
    await agents.create_agent(tenant_id=tenant_id, tenant_slug=slug, slug="demo-off", name="Off", status="inactive")
    await agents.create_agent(tenant_id=tenant_id, tenant_slug=slug, slug="demo-chat", name="Chat",
                              template_id="faq-support", template_version=4)
    return {"voice": voice, "other": other}


@pytest.fixture
def demo_env(monkeypatch, test_tenant):
    def configure(agent_slug: str | None = "demo-voice", origins: str = SITE, per_hour: str = "3"):
        if agent_slug is None:
            monkeypatch.delenv("PUBLIC_DEMO_AGENT", raising=False)
        else:
            monkeypatch.setenv("PUBLIC_DEMO_AGENT", f"{test_tenant['slug']}/{agent_slug}")
        monkeypatch.setenv("PUBLIC_DEMO_ORIGINS", origins)
        monkeypatch.setenv("PUBLIC_DEMO_PER_IP_PER_HOUR", per_hour)
        monkeypatch.setattr(app.state, "public_demo_throttle", PublicDemoThrottle())
    configure()
    return configure


async def _post(origin: str | None = SITE):
    headers = {"Origin": origin} if origin else {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(URL, headers=headers)


async def test_allowed_origin_gets_a_voice_credential_with_cors_header(demo_agents, demo_env, test_tenant):
    resp = await _post()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"credential", "expires_in", "tenant", "agent"}
    assert body["tenant"] == test_tenant["slug"] and body["agent"] == "demo-voice"
    assert body["expires_in"] == 60
    assert resp.headers["access-control-allow-origin"] == SITE
    assert "Origin" in resp.headers["vary"]


@pytest.mark.parametrize("origin", [None, "https://evil.example", "http://localhost:3000"])
async def test_origin_not_on_the_list_is_refused_without_cors_header(demo_agents, demo_env, origin):
    resp = await _post(origin)

    assert resp.status_code == 403
    assert resp.headers.get("access-control-allow-origin") in (None, origin if origin == "http://localhost:3000" else None)
    assert "credential" not in resp.text


async def test_empty_origin_list_refuses_everything(demo_agents, demo_env):
    demo_env(origins="")
    resp = await _post()
    assert resp.status_code == 403
    assert "access-control-allow-origin" not in resp.headers


async def test_demo_switched_off_is_404(demo_agents, demo_env):
    demo_env(agent_slug=None)
    resp = await _post()
    assert resp.status_code == 404
    assert resp.headers["access-control-allow-origin"] == SITE


@pytest.mark.parametrize("agent_slug", ["nope", "demo-off", "demo-chat"])
async def test_missing_inactive_or_chat_agent_is_404(demo_agents, demo_env, agent_slug):
    demo_env(agent_slug=agent_slug)
    resp = await _post()
    assert resp.status_code == 404
    assert "credential" not in resp.text


async def test_fourth_call_from_one_ip_in_an_hour_is_429(demo_agents, demo_env):
    statuses = [(await _post()).status_code for _ in range(4)]
    assert statuses == [200, 200, 200, 429]
    last = await _post()
    assert last.headers["access-control-allow-origin"] == SITE
    assert int(last.headers["retry-after"]) > 0


async def test_credential_opens_only_the_demo_agent_once(demo_agents, demo_env, test_tenant):
    credential = (await _post()).json()["credential"]
    redis = cache.get_client()

    grant = await redeem_test_credential(redis, credential, channel="voice", consume=True)
    assert grant is not None
    assert grant.tenant_slug == test_tenant["slug"]
    assert grant.agent_slug == "demo-voice"
    assert grant.agent_id == str(demo_agents["voice"]["id"])
    assert grant.agent_id != str(demo_agents["other"]["id"])
    assert await redeem_test_credential(redis, credential, channel="voice", consume=True) is None
