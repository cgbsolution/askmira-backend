"""The platform console (super admin only): workspaces and their admins."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.services import platform

router = APIRouter(prefix="/platform", tags=["platform"])

# `require_roles` lets a super admin through any gate; naming a role nobody else
# holds alongside it would add nothing. The service refuses everyone else too.
SUPER_ONLY = [Depends(require_roles(UserRole.SUPER_ADMIN))]


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool
    reason: str = Field(min_length=3, max_length=500)


@router.get("/workspaces", dependencies=SUPER_ONLY)
async def list_workspaces(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    return await platform.list_workspaces(session, user)


@router.get("/workspaces/{tenant_id}", dependencies=SUPER_ONLY)
async def get_workspace(
    tenant_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """One company's full profile, its admins and its size."""
    return await platform.workspace_detail(session, user, tenant_id)


@router.patch("/workspaces/{tenant_id}", dependencies=SUPER_ONLY)
async def update_workspace(
    tenant_id: uuid.UUID,
    payload: platform.WorkspaceUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await platform.update(session, user, tenant_id, payload, request=request)


@router.post("/workspaces", status_code=status.HTTP_201_CREATED, dependencies=SUPER_ONLY)
async def create_workspace(
    payload: platform.WorkspaceIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await platform.create(session, user, payload, request=request)


@router.post(
    "/workspaces/{tenant_id}/admins",
    status_code=status.HTTP_201_CREATED,
    dependencies=SUPER_ONLY,
)
async def add_admin(
    tenant_id: uuid.UUID,
    payload: platform.AdminIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await platform.add_admin(session, user, tenant_id, payload, request=request)


@router.post("/workspaces/{tenant_id}/admins/{admin_id}/reset-password", dependencies=SUPER_ONLY)
async def reset_admin_password(
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await platform.reset_admin_password(session, user, tenant_id, admin_id, request=request)


@router.post("/workspaces/{tenant_id}/status", dependencies=SUPER_ONLY)
async def set_status(
    tenant_id: uuid.UUID,
    payload: StatusIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await platform.set_active(
        session, user, tenant_id, active=payload.active, reason=payload.reason, request=request
    )


@router.post("/workspaces/{tenant_id}/logo", dependencies=SUPER_ONLY)
async def upload_logo(
    tenant_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    from app.services.branding import LOGO_MAX_BYTES

    data = await file.read(LOGO_MAX_BYTES + 1)
    return await platform.upload_logo(session, user, tenant_id, data, request=request)


@router.delete(
    "/workspaces/{tenant_id}/logo",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=SUPER_ONLY,
)
async def delete_logo(
    tenant_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await platform.remove_logo(session, user, tenant_id, request=request)
