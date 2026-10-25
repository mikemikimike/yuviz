"""DB fixtures. Point REALESTATE_TEST_DSN at a database that has schema.sql + realestate_schema.sql
applied (default: POSTGRES_DSN, else the local scratch database `clinic_scratch`)."""

from __future__ import annotations

import datetime
import os
import uuid
from zoneinfo import ZoneInfo

os.environ["POSTGRES_DSN"] = (
    os.environ.get("REALESTATE_TEST_DSN") or os.environ.get("CLINIC_TEST_DSN") or os.environ.get("POSTGRES_DSN")
    or "postgresql:///clinic_scratch?host=/var/run/postgresql"
)
os.environ.setdefault("JWT_SECRET", "dev-only-insecure-secret-do-not-deploy-" * 2)

import pytest_asyncio  # noqa: E402

from libs.tenancy import tenant_conn  # noqa: E402
from services.realestate import core, db  # noqa: E402

TABLES = ("re_callbacks", "re_leads", "re_site_visits", "re_projects", "re_api_keys", "re_settings")


async def _make_tenant(pool, capacity: int = 2) -> dict:
    slug = f"re-test-{uuid.uuid4().hex[:8]}"
    tenant_id = await pool.fetchval("INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING id", slug, slug)
    # Every day visits run 10:00-14:00 in one-hour slots so "tomorrow" always has times.
    await pool.execute(
        "INSERT INTO re_settings (tenant_id, company_name, visit_days, visit_start_hour, visit_end_hour, visit_capacity) "
        "VALUES ($1, 'Test Realty', '{1,2,3,4,5,6,7}', 10, 14, $2)", tenant_id, capacity)
    await pool.execute(
        "INSERT INTO re_projects (tenant_id, code, name, locality, meeting_point) "
        "VALUES ($1, 'GFH', 'Greenfield Heights', 'Noida Sector 150', 'the model flat'), "
        "       ($1, 'AVL', 'Aaravi Villas', 'Greater Noida West', 'the gate')", tenant_id)
    return {"id": str(tenant_id)}


async def _drop_tenant(pool, tenant_id: str) -> None:
    for table in TABLES:
        await pool.execute(f"DELETE FROM {table} WHERE tenant_id = $1", tenant_id)
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant_id)


@pytest_asyncio.fixture(loop_scope="session")
async def pool():
    yield await db.get_pool()


@pytest_asyncio.fixture(loop_scope="session")
async def tenant(pool):
    t = await _make_tenant(pool)
    yield t
    await _drop_tenant(pool, t["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def other_tenant(pool):
    t = await _make_tenant(pool)
    yield t
    await _drop_tenant(pool, t["id"])


@pytest_asyncio.fixture(loop_scope="session")
async def run(pool, tenant):
    """run(fn, **kwargs): calls core.fn(conn, company, **kwargs) in one tenant transaction."""
    async def _run(fn, tenant_id: str | None = None, **kwargs):
        tid = tenant_id or tenant["id"]
        async with tenant_conn(pool, explicit_tenant=tid, reason="test") as conn:
            company = await core.load_company(conn, tid)
            return await fn(conn, company, **kwargs)
    return _run


def tomorrow_at(hour: int, minute: int = 0) -> str:
    tz = ZoneInfo("Asia/Kolkata")
    day = datetime.datetime.now(tz).date() + datetime.timedelta(days=1)
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=tz).isoformat()
