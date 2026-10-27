from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from libs.tenancy import set_target_tenant
from services.config.auth import CurrentUser
from services.config.deps import assert_tenant_access, get_current_user, is_platform_scoped, require_role

from .. import agent_kb as agent_kb_service
from ..schemas import AgentDocumentsUpdate

router = APIRouter(prefix="/agents/{agent_id}", tags=["agent_documents"])


@router.get("/documents")
async def list_agent_documents(agent_id: str, current_user: CurrentUser = Depends(get_current_user)):
    await _authorize_agent(agent_id, current_user)
    return await agent_kb_service.list_for_agent(agent_id)


@router.put("/documents")
async def replace_agent_documents(
    agent_id: str,
    body: AgentDocumentsUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await _authorize_agent(agent_id, current_user)
    try:
        return await agent_kb_service.replace_documents(
            agent_id, body.document_ids, user_id=current_user.id, user_email=current_user.email,
        )
    except (LookupError, asyncpg.DataError):
        # Foreign and nonexistent documents get the same response.
        raise HTTPException(status_code=404, detail="document not found")


@router.api_route("/knowledge-bases", methods=["GET", "POST"], include_in_schema=False)
@router.api_route("/knowledge-bases/{kb_id}", methods=["PATCH", "DELETE"], include_in_schema=False)
async def knowledge_base_links_gone(agent_id: str, kb_id: str | None = None):
    raise HTTPException(status_code=410, detail="agents now use documents: GET or PUT /agents/{agent_id}/documents")


async def _authorize_agent(agent_id: str, current_user: CurrentUser) -> None:
    """The link routes are keyed on agent_id rather than on a row of their own — agent_documents
    carries no tenant column — so the check resolves the *agent's* tenant. A foreign agent gets
    the same 404 as a missing one."""
    not_found = HTTPException(status_code=404, detail=f"agent {agent_id!r} not found")
    platform_scoped = is_platform_scoped(current_user)
    tenant_id = await agent_kb_service.get_agent_tenant_id(agent_id, platform_scoped=platform_scoped)
    if tenant_id is None:
        raise not_found
    try:
        await assert_tenant_access(tenant_id, current_user)
    except HTTPException:
        raise not_found
    set_target_tenant(tenant_id)
