"""Sales-team endpoints: site visits, leads and callback requests, for console users of the tenant."""

from __future__ import annotations

import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from libs.tenancy import tenant_conn
from services.config.auth import CurrentUser
from services.config.deps import bind_path_tenant, require_path_tenant_access, require_role

from .. import core, db

tenant_scoped_router = APIRouter(
    prefix="/tenants/{tenant_id}/realestate", tags=["realestate"],
    dependencies=[Depends(bind_path_tenant), Depends(require_path_tenant_access)],
)

_STAFF = require_role("superadmin", "admin", "supervisor", "agent")


async def _scoped_tenant(conn) -> str:
    """The path segment may be a slug or a UUID; tenant_conn has already resolved it."""
    return await conn.fetchval("SELECT current_setting('app.tenant_id')")


@tenant_scoped_router.get("/projects")
async def list_projects(tenant_id: str, current_user: CurrentUser = Depends(_STAFF)):
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        co = await core.load_company(conn, await _scoped_tenant(conn))
        return await core.list_projects(conn, co, "en")


@tenant_scoped_router.get("/site-visits")
async def list_site_visits(
    tenant_id: str,
    day_from: datetime.date | None = Query(default=None, alias="from"),
    day_to: datetime.date | None = Query(default=None, alias="to"),
    status: Literal["booked", "cancelled", "completed", "no_show"] | None = None,
    current_user: CurrentUser = Depends(_STAFF),
):
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        co = await core.load_company(conn, await _scoped_tenant(conn))
        first = day_from or co.today
        last = day_to or first + datetime.timedelta(days=co.settings["booking_window_days"])
        rows = await conn.fetch(
            "SELECT v.reference, v.visitor_name, v.visitor_phone, v.starts_at, v.ends_at, v.status, v.notes, "
            "       v.language, v.created_at, p.code AS project_code, p.name AS project "
            "FROM re_site_visits v JOIN re_projects p ON p.id = v.project_id "
            "WHERE v.tenant_id = $1 AND v.starts_at >= $2 AND v.starts_at < $3 AND ($4::text IS NULL OR v.status = $4) "
            "ORDER BY v.starts_at",
            co.tenant_id, datetime.datetime.combine(first, datetime.time(0), tzinfo=co.tz),
            datetime.datetime.combine(last + datetime.timedelta(days=1), datetime.time(0), tzinfo=co.tz), status)
        return [dict(r) for r in rows]


class VisitStatus(BaseModel):
    status: Literal["completed", "no_show", "cancelled"]


@tenant_scoped_router.patch("/site-visits/{reference}")
async def update_site_visit(tenant_id: str, reference: str, body: VisitStatus,
                            current_user: CurrentUser = Depends(_STAFF)):
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        done = await conn.fetchval(
            "UPDATE re_site_visits SET status = $2, cancelled_at = CASE WHEN $2 = 'cancelled' THEN now() ELSE cancelled_at END "
            "WHERE tenant_id = $1 AND reference = $3 RETURNING reference",
            await _scoped_tenant(conn), body.status, reference)
    if done is None:
        raise HTTPException(status_code=404, detail="visit not found")
    return {"reference": done, "status": body.status}


@tenant_scoped_router.get("/leads")
async def list_leads(tenant_id: str, status: Literal["new", "contacted", "qualified", "closed"] | None = None,
                     limit: int = Query(default=100, ge=1, le=500), current_user: CurrentUser = Depends(_STAFF)):
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            "SELECT phone, name, project_code, property_type, budget, timeline, notes, status, source, created_at, "
            "       updated_at FROM re_leads WHERE tenant_id = $1 AND ($2::text IS NULL OR status = $2) "
            "ORDER BY updated_at DESC LIMIT $3", await _scoped_tenant(conn), status, limit)
        return [dict(r) for r in rows]


@tenant_scoped_router.get("/callbacks")
async def list_callbacks(tenant_id: str, status: Literal["pending", "done", "cancelled"] | None = "pending",
                         limit: int = Query(default=100, ge=1, le=500), current_user: CurrentUser = Depends(_STAFF)):
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        rows = await conn.fetch(
            "SELECT reference, phone, name, preferred_time, topic, status, language, created_at, done_at "
            "FROM re_callbacks WHERE tenant_id = $1 AND ($2::text IS NULL OR status = $2) "
            "ORDER BY created_at DESC LIMIT $3", await _scoped_tenant(conn), status, limit)
        return [dict(r) for r in rows]


class CallbackStatus(BaseModel):
    status: Literal["done", "cancelled"]


@tenant_scoped_router.patch("/callbacks/{reference}")
async def update_callback(tenant_id: str, reference: str, body: CallbackStatus,
                          current_user: CurrentUser = Depends(_STAFF)):
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        done = await conn.fetchval(
            "UPDATE re_callbacks SET status = $2, done_at = CASE WHEN $2 = 'done' THEN now() ELSE done_at END "
            "WHERE tenant_id = $1 AND reference = $3 RETURNING reference",
            await _scoped_tenant(conn), body.status, reference)
    if done is None:
        raise HTTPException(status_code=404, detail="callback not found")
    return {"reference": done, "status": body.status}
