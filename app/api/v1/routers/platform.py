"""The platform console (super admin only): workspaces and their admins."""

from __future__ import annotations

import os
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Request, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.services import platform, tenant_purge
from app.services.products import platform as tools

router = APIRouter(prefix="/platform", tags=["platform"])

# `require_roles` lets a super admin through any gate; naming a role nobody else
# holds alongside it would add nothing. The service refuses everyone else too.
SUPER_ONLY = [Depends(require_roles(UserRole.SUPER_ADMIN))]


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool
    reason: str = Field(min_length=3, max_length=500)


class DeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The workspace address, typed out: a delete is never one stray click.
    confirm: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=3, max_length=500)


@router.get("/workspaces", dependencies=SUPER_ONLY)
async def list_workspaces(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    return await platform.list_workspaces(session, user)


@router.get("/workspaces/deleted", dependencies=SUPER_ONLY)
async def list_deleted_workspaces(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Deleted workspaces, with the day each one's data is purged."""
    return await tenant_purge.deleted_workspaces(session, user)


@router.get(
    "/workspaces/deleted/{tenant_id}/export", dependencies=SUPER_ONLY, response_class=FileResponse
)
async def export_deleted_workspace(
    tenant_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> FileResponse:
    """Everything a deleted workspace holds, as a zip, to hand back before the purge."""
    path, filename = await tenant_purge.export_workspace(session, user, tenant_id, request=request)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(os.remove, path),
    )


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


@router.delete("/workspaces/{tenant_id}", dependencies=SUPER_ONLY)
async def delete_workspace(
    tenant_id: uuid.UUID,
    payload: DeleteIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Close a workspace for good. Its records are kept, out of reach."""
    return await platform.delete(
        session, user, tenant_id, confirm=payload.confirm, reason=payload.reason, request=request
    )


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


@router.patch("/workspaces/{tenant_id}/admins/{admin_id}", dependencies=SUPER_ONLY)
async def change_admin_email(
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID,
    payload: platform.AdminEmailIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Correct an admin's login email; the login is issued afresh to the new one."""
    return await platform.change_admin_email(
        session, user, tenant_id, admin_id, payload, request=request
    )


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
    from app.services.uploads import READ_CEILING

    data = await file.read(READ_CEILING)
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


# ── Our other products: the platform's key per product (Platform → Tools) ─────


class ToolKeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The key to keep; leave it out and the platform makes a strong one, shown once.
    secret: str | None = Field(default=None, max_length=512)


@router.get("/tools", dependencies=SUPER_ONLY)
async def list_tools(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    return await tools.tools(session, user)


@router.put("/tools/{product}/key", dependencies=SUPER_ONLY)
async def set_tool_key(
    product: str, body: ToolKeyIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await tools.set_key(session, user, product, secret=body.secret, request=request)


@router.delete("/tools/{product}/key", dependencies=SUPER_ONLY)
async def remove_tool_key(
    product: str, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await tools.remove_key(session, user, product, request=request)


@router.get("/connections", dependencies=SUPER_ONLY)
async def list_connections(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Every workspace's standing in every tool, for the console's overview grid."""
    return await tools.connections(session, user)


@router.post("/tools/{product}/check", dependencies=SUPER_ONLY)
async def check_tool(product: str, user: CurrentUser, session: DbSession) -> dict[str, Any]:
    return await tools.check(session, user, product)


# ── A workspace in our other products ────────────────────────────────────────


class ProvisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Attach this organisation the product already has, instead of creating one.
    organization_ref: str | None = Field(default=None, max_length=120)
    #: Create a new organisation even though the admin already has one there.
    create_new: bool = False


class OrganizationSearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str = Field(min_length=2, max_length=120)


@router.get("/workspaces/{tenant_id}/products", dependencies=SUPER_ONLY)
async def list_products(
    tenant_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[dict[str, Any]]:
    return await tools.workspace_tools(session, user, tenant_id)


@router.post("/workspaces/{tenant_id}/products/{product}", dependencies=SUPER_ONLY)
async def link_product(
    tenant_id: uuid.UUID,
    product: str,
    user: CurrentUser,
    session: DbSession,
    request: Request,
    body: ProvisionIn | None = None,
) -> dict[str, Any]:
    """Create (or attach) the company's organisation in the product and connect it."""
    body = body or ProvisionIn()
    return await tools.provision(
        session,
        user,
        tenant_id,
        product,
        organization_ref=body.organization_ref,
        create_new=body.create_new,
        request=request,
    )


@router.post("/workspaces/{tenant_id}/products/{product}/organizations", dependencies=SUPER_ONLY)
async def search_product_organizations(
    tenant_id: uuid.UUID,
    product: str,
    body: OrganizationSearchIn,
    user: CurrentUser,
    session: DbSession,
) -> list[dict[str, Any]]:
    return await tools.organizations(session, user, tenant_id, product, q=body.q)


@router.delete("/workspaces/{tenant_id}/products/{product}", dependencies=SUPER_ONLY)
async def unlink_product(
    tenant_id: uuid.UUID, product: str, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await tools.disconnect(session, user, tenant_id, product, request=request)


@router.post("/workspaces/{tenant_id}/products/{product}/sync", dependencies=SUPER_ONLY)
async def sync_product(
    tenant_id: uuid.UUID, product: str, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await tools.sync_now(session, user, tenant_id, product, request=request)
