"""Unauthenticated website demo: mints a single-use voice credential for one configured agent.

POST /public/demo-call is a CORS "simple" request from the marketing site (no preflight), so the
route mirrors Access-Control-Allow-Origin itself, and only for PUBLIC_DEMO_ORIGINS.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from libs.ratelimit import FixedWindowCounter
from libs.tenancy import set_target_tenant

from .. import agent_testing, cache
from .. import agents as agents_service
from .. import tenants as tenants_service
from .agents import _agent_channel

log = logging.getLogger(__name__)

router = APIRouter(prefix="/public", tags=["public"])


class PublicDemoThrottle:
    """Per-IP hourly cap on demo passes, keyed on request.client.host."""

    def __init__(self) -> None:
        limit = int(os.environ.get("PUBLIC_DEMO_PER_IP_PER_HOUR") or 3)
        self.hour = FixedWindowCounter(limit=limit, window_seconds=3600)

    def allow(self, client_host: str) -> tuple[bool, int]:
        over, retry_after = self.hour.over_limit(client_host)
        if over:
            return False, retry_after
        self.hour.increment(client_host)
        return True, 0


def _allowed_origins() -> set[str]:
    return {o.strip() for o in os.environ.get("PUBLIC_DEMO_ORIGINS", "").split(",") if o.strip()}


def _demo_target() -> tuple[str, str] | None:
    tenant, _, agent = (os.environ.get("PUBLIC_DEMO_AGENT") or "").strip().partition("/")
    return (tenant, agent) if tenant and agent else None


def _reply(status: int, body: dict, origin: str, extra: dict[str, str] | None = None) -> JSONResponse:
    headers = {"Access-Control-Allow-Origin": origin, "Vary": "Origin", **(extra or {})}
    return JSONResponse(status_code=status, content=body, headers=headers)


@router.post("/demo-call")
async def create_demo_call(request: Request):
    origin = request.headers.get("origin", "")
    if origin not in _allowed_origins():
        return JSONResponse(status_code=403, content={"detail": "origin not allowed"})

    target = _demo_target()
    if target is None:
        return _reply(404, {"detail": "demo not available"}, origin)
    tenant_slug, agent_slug = target

    # client.host is the socket peer; behind a reverse proxy this needs trusted X-Forwarded-For handling.
    client_host = request.client.host if request.client else "unknown"
    allowed, retry_after = request.app.state.public_demo_throttle.allow(client_host)
    if not allowed:
        return _reply(429, {"detail": "too many demo calls; try again later"}, origin,
                      {"Retry-After": str(retry_after)})

    tenant = await tenants_service.get_tenant(tenant_slug)
    if tenant is None:
        return _reply(404, {"detail": "demo not available"}, origin)
    set_target_tenant(str(tenant["id"]))
    agent = await agents_service.get_agent(tenant_slug, agent_slug)
    if agent is None or agent.get("status") != "active" or _agent_channel(agent) != "voice":
        return _reply(404, {"detail": "demo not available"}, origin)

    minted = await agent_testing.mint_test_session(cache.get_client(), tenant=tenant, agent=agent, channel="voice")
    log.info("public demo: pass issued for %s/%s", tenant_slug, agent_slug)
    return _reply(200, {"credential": minted["credential"], "expires_in": minted["expires_in"],
                        "tenant": tenant_slug, "agent": agent_slug}, origin)
