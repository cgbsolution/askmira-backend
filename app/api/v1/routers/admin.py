"""Tenant administration: module toggles, RBAC, audit trail (FS-ADM-01, FS-AIT-01)."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.audit import AuditAction, AuditLog, RoleModulePermission
from app.models.tenant import Tenant
from app.models.user import UserRole
from app.schemas.common import Page

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_roles(UserRole.TENANT_ADMIN, UserRole.HR_ADMIN))],
)


class TenantSettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    plan: str
    data_region: str
    enabled_modules: dict
    settings: dict


class PermissionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    module: str
    can_view: bool
    can_create: bool
    can_edit: bool
    can_delete: bool
    can_approve: bool
    can_export: bool
    field_restrictions: dict


class AuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor_user_id: uuid.UUID | None
    actor_type: str
    action: AuditAction
    entity_type: str
    entity_id: uuid.UUID | None
    changes: dict
    summary: str | None
    ai_context: dict | None
    created_at: datetime


@router.get("/tenant", response_model=TenantSettingsRead)
async def get_tenant(user: CurrentUser, session: DbSession) -> TenantSettingsRead:
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    return TenantSettingsRead.model_validate(tenant)


@router.get("/permissions", response_model=list[PermissionRead])
async def list_permissions(user: CurrentUser, session: DbSession) -> list[PermissionRead]:
    stmt = tenant_scoped(
        select(RoleModulePermission).order_by(
            RoleModulePermission.role, RoleModulePermission.module
        ),
        RoleModulePermission,
        user,
    )
    rows = (await session.scalars(stmt)).all()
    return [PermissionRead.model_validate(row) for row in rows]


@router.get("/audit-logs", response_model=Page[AuditLogRead])
async def list_audit_logs(
    user: CurrentUser,
    session: DbSession,
    entity_type: str | None = None,
    action: AuditAction | None = None,
    actor_type: str | None = Query(
        default=None, description='Filter to AI actions with "ai_companion"'
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> Page[AuditLogRead]:
    """The immutable audit trail (FRD 6, FS-AIT-01).

    Read-only by construction: there is no update or delete endpoint here, and
    none should be added.
    """
    stmt = tenant_scoped(select(AuditLog), AuditLog, user)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if actor_type:
        stmt = stmt.where(AuditLog.actor_type == actor_type)

    rows = (
        await session.scalars(
            stmt.order_by(AuditLog.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return Page[AuditLogRead](
        items=[AuditLogRead.model_validate(row) for row in rows],
        total=len(rows),
        page=page,
        page_size=page_size,
    )


# TODO(V1): PATCH /permissions — with the rule from FS-ADM-01 that a user can
# never elevate their own role, and every change audit-logged.
# TODO(V2): GET /audit-logs/export — the exportable, immutable log procurement
# asks for.
