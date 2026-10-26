"""The agent's real-estate tools, called by toolexec as ordinary custom APIs.

The tenant comes from the bearer key (toolauth). `caller_phone` is toolexec's `caller_id`
param source, i.e. the number on the call, never a value the model chose.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from libs.tenancy import tenant_conn

from .. import core, db
from ..toolauth import require_tool_tenant

router = APIRouter(prefix="/tool/v1", tags=["realestate-tools"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")
    language: str | None = "en"
    caller_phone: str | None = None


class AvailabilityBody(_Body):
    project: str | None = None
    date: str | None = None
    part_of_day: str | None = None


class PrepareVisitBody(_Body):
    project: str = ""
    slot_start: str = ""
    visitor_name: str = ""
    visitor_phone: str | None = None


class BookVisitBody(_Body):
    project_code: str = ""
    slot_start: str = ""
    visitor_name: str = ""
    visitor_phone: str = ""
    notes: str | None = None


class FindBody(_Body):
    reference: str | None = None
    visitor_phone: str | None = None


class CancelBody(_Body):
    reference: str = ""
    visitor_phone: str | None = None


class LeadBody(_Body):
    name: str | None = None
    project: str | None = None
    property_type: str | None = None
    budget: str | None = None
    timeline: str | None = None
    notes: str | None = None


class CallbackBody(_Body):
    name: str | None = None
    phone: str | None = None
    preferred_time: str | None = None
    topic: str | None = None


async def _run(tenant_id: str, work: Callable[[Any, core.Company], Awaitable[dict]]) -> dict:
    pool = await db.get_pool()
    async with tenant_conn(pool, explicit_tenant=tenant_id, reason="realestate-tool-call") as conn:
        company = await core.load_company(conn, tenant_id)
        result = await work(conn, company)
    # Sentences that toolexec speaks through a template also go out in template-sized parts.
    for key in ("confirm_text", "spoken"):
        if key in result:
            core.with_parts(result, key)
    return result


@router.post("/projects")
async def projects(body: _Body, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    async def work(conn, co):
        return {"ok": True, "company": co.settings["company_name"],
                "projects": await core.list_projects(conn, co, body.language or "en")}
    return await _run(tenant_id, work)


@router.post("/visits/availability")
async def availability(body: AvailabilityBody, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    return await _run(tenant_id, lambda conn, co: core.availability(
        conn, co, project=body.project, date=body.date, part_of_day=body.part_of_day, language=body.language or "en"))


@router.post("/visits/prepare")
async def prepare_visit(body: PrepareVisitBody, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    return await _run(tenant_id, lambda conn, co: core.prepare_visit(
        conn, co, project=body.project, slot_start=body.slot_start, visitor_name=body.visitor_name,
        visitor_phone=body.visitor_phone, caller_phone=body.caller_phone, language=body.language or "en"))


@router.post("/visits")
async def book_visit(body: BookVisitBody, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    return await _run(tenant_id, lambda conn, co: core.book_visit(
        conn, co, project_code=body.project_code, slot_start=body.slot_start, visitor_name=body.visitor_name,
        visitor_phone=body.visitor_phone, caller_phone=body.caller_phone, notes=body.notes,
        language=body.language or "en"))


@router.post("/visits/find")
async def find_visits(body: FindBody, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    return await _run(tenant_id, lambda conn, co: core.find_visits(
        conn, co, reference=body.reference, visitor_phone=body.visitor_phone, caller_phone=body.caller_phone,
        language=body.language or "en"))


@router.post("/visits/cancel/prepare")
async def prepare_cancel(body: CancelBody, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    return await _run(tenant_id, lambda conn, co: core.prepare_cancel(
        conn, co, reference=body.reference, visitor_phone=body.visitor_phone, caller_phone=body.caller_phone,
        language=body.language or "en"))


@router.post("/visits/cancel")
async def cancel_visit(body: CancelBody, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    return await _run(tenant_id, lambda conn, co: core.cancel_visit(
        conn, co, reference=body.reference, visitor_phone=body.visitor_phone, caller_phone=body.caller_phone,
        language=body.language or "en"))


@router.post("/leads")
async def save_lead(body: LeadBody, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    return await _run(tenant_id, lambda conn, co: core.save_lead(
        conn, co, caller_phone=body.caller_phone, name=body.name, project=body.project,
        property_type=body.property_type, budget=body.budget, timeline=body.timeline, notes=body.notes))


@router.post("/callbacks")
async def request_callback(body: CallbackBody, tenant_id: str = Depends(require_tool_tenant)) -> dict:
    return await _run(tenant_id, lambda conn, co: core.request_callback(
        conn, co, caller_phone=body.caller_phone, phone=body.phone, name=body.name,
        preferred_time=body.preferred_time, topic=body.topic, language=body.language or "en"))
