"""Tenant administration (FS-ADM-01, FS-AIT-01).

Company settings, logins and roles, module toggles, the audit trail and the
background jobs. Every rule about who may change whose role lives in
`app.services.identity.users`; this router parses, gates and serialises.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.core.step_up import confirmation
from app.models.audit import AuditAction, AuditLog, RoleModulePermission
from app.models.tenant import Tenant
from app.models.user import UserRole
from app.schemas.common import Page
from app.schemas.platform_admin import (
    ActiveChange,
    BulkLoginCreate,
    BulkLoginResult,
    EmployeeLink,
    LoginDelete,
    ManagedUserCreate,
    ManagedUserCreated,
    ManagedUserRead,
    ModuleToggle,
    PasswordResetByAdmin,
    RoleChange,
    TenantSettingsUpdate,
    WelcomeEmailRead,
)
from app.services import access_admin, integrations
from app.services.identity import users as user_admin
from app.services.scheduler import service as scheduler_service

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
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


@router.patch("/tenant", response_model=TenantSettingsRead)
async def update_tenant(
    payload: TenantSettingsUpdate, user: CurrentUser, session: DbSession, request: Request
) -> TenantSettingsRead:
    tenant = await user_admin.update_tenant(session, user, payload, request=request)
    return TenantSettingsRead.model_validate(tenant)


# ── Logins and roles ────────────────────────────────────────────────────────


@router.get("/users", response_model=list[ManagedUserRead])
async def list_users(user: CurrentUser, session: DbSession) -> list[ManagedUserRead]:
    return await user_admin.list_users(session, user)


@router.post("/users", response_model=ManagedUserCreated, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: ManagedUserCreate, user: CurrentUser, session: DbSession, request: Request
) -> ManagedUserCreated:
    """Create a login. The temporary password is returned once and never again."""
    return await user_admin.create_user(session, user, payload, request=request)


@router.post("/users/bulk", response_model=BulkLoginResult)
async def create_logins(
    payload: BulkLoginCreate, user: CurrentUser, session: DbSession, request: Request
) -> BulkLoginResult:
    """Logins for many employees at once, at their work emails.

    Each one-time password is returned once and never again. Anyone who cannot
    have a login comes back with the reason.
    """
    return await user_admin.create_logins_for_employees(session, user, payload, request=request)


@router.patch("/users/{user_id}/role", response_model=ManagedUserRead)
async def change_role(
    user_id: uuid.UUID,
    payload: RoleChange,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ManagedUserRead:
    """Change somebody else's role. Never your own; never to super admin."""
    return await user_admin.set_role(
        session,
        user,
        user_id=user_id,
        role=payload.role,
        reason=payload.reason,
        request=request,
    )


@router.patch("/users/{user_id}/active", response_model=ManagedUserRead)
async def change_active(
    user_id: uuid.UUID,
    payload: ActiveChange,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ManagedUserRead:
    return await user_admin.set_active(
        session,
        user,
        user_id=user_id,
        active=payload.active,
        reason=payload.reason,
        request=request,
    )


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_login(
    user_id: uuid.UUID,
    payload: LoginDelete,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> None:
    """Delete a login for good; its email can be given a login again."""
    await user_admin.delete_login(
        session, user, user_id=user_id, reason=payload.reason, request=request
    )


@router.patch("/users/{user_id}/employee", response_model=ManagedUserRead)
async def link_employee(
    user_id: uuid.UUID,
    payload: EmployeeLink,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ManagedUserRead:
    """Link a login to its employee record (or unlink it). Self-service depends on it."""
    return await user_admin.link_employee(
        session,
        user,
        user_id=user_id,
        employee_id=payload.employee_id,
        reason=payload.reason,
        request=request,
    )


@router.post("/users/{user_id}/reset-password", response_model=ManagedUserCreated)
async def reset_password(
    user_id: uuid.UUID,
    payload: PasswordResetByAdmin,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ManagedUserCreated:
    """Issue a one-time password the person must change at sign-in. Shown once."""
    return await user_admin.issue_temporary_password(
        session, user, user_id=user_id, reason=payload.reason, request=request
    )


@router.post("/users/{user_id}/welcome-email", response_model=WelcomeEmailRead)
async def resend_welcome_email(
    user_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> WelcomeEmailRead:
    """Email a login its sign-in details again, with a fresh set-password link."""
    return await user_admin.resend_welcome(session, user, user_id=user_id, request=request)


# ── Modules ─────────────────────────────────────────────────────────────────


@router.get("/modules")
async def get_modules(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    return await user_admin.module_state(session, user)


@router.put("/modules")
async def set_modules(
    payload: ModuleToggle, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Switch modules on or off. The API refuses a switched-off module's routes."""
    return await user_admin.set_modules(session, user, modules=payload.modules, request=request)


# ── Audit trail ─────────────────────────────────────────────────────────────


@router.get("/audit-logs", response_model=Page[AuditLogRead])
async def list_audit_logs(
    user: CurrentUser,
    session: DbSession,
    entity_type: str | None = None,
    action: AuditAction | None = None,
    actor_type: str | None = Query(
        default=None, description='Filter to AI actions with "ai_companion"'
    ),
    actor_user_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    search: str | None = Query(default=None, max_length=100),
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
    if actor_user_id:
        stmt = stmt.where(AuditLog.actor_user_id == actor_user_id)
    if since:
        stmt = stmt.where(AuditLog.created_at >= since)
    if until:
        stmt = stmt.where(AuditLog.created_at < until)
    if search:
        stmt = stmt.where(AuditLog.summary.ilike(f"%{search}%"))

    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        await session.scalars(
            stmt.order_by(AuditLog.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return Page[AuditLogRead](
        items=[AuditLogRead.model_validate(row) for row in rows],
        total=int(total or 0),
        page=page,
        page_size=page_size,
    )


@router.get("/audit-logs/entity-types")
async def audit_entity_types(user: CurrentUser, session: DbSession) -> list[str]:
    """The entity types present in this workspace's trail, for the filter."""
    stmt = tenant_scoped(
        select(AuditLog.entity_type).distinct().order_by(AuditLog.entity_type), AuditLog, user
    )
    return list((await session.scalars(stmt)).all())


# ── Background jobs ─────────────────────────────────────────────────────────


@router.get("/scheduler/jobs")
async def scheduler_jobs(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """Every scheduled job, what it calls, and how it last went for this workspace."""
    return await scheduler_service.list_jobs(session, user)


@router.get("/scheduler/jobs/{job_key}/runs")
async def scheduler_runs(
    job_key: str, user: CurrentUser, session: DbSession
) -> list[dict[str, Any]]:
    return await scheduler_service.recent_runs(session, user, job_key=job_key)


@router.post(
    "/scheduler/jobs/{job_key}/run",
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def run_job_now(
    job_key: str, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Run one job now, for this workspace only. Admins only."""
    return await scheduler_service.run_job_now(session, user, job_key=job_key, request=request)


# ── HR operators: rights by area, and scope ─────────────────────────────────


class AreaRightsIn(BaseModel):
    can_view: bool = False
    can_create: bool = False
    can_edit: bool = False
    can_delete: bool = False
    can_approve: bool = False
    can_export: bool = False


class ScopeIn(BaseModel):
    department_ids: list[uuid.UUID] = []
    location_ids: list[uuid.UUID] = []


@router.get("/access")
async def access_overview(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """Areas, the sub-admin role's rights in each, and each sub-admin's own rights and scope."""
    return await access_admin.access_overview(session, user)


@router.put("/access/role/{area}")
async def set_role_rights(
    area: str, payload: AreaRightsIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, bool]:
    result = await access_admin.set_role_rights(
        session,
        user,
        area,
        payload.model_dump(),
        confirm=confirmation(session, user, "user.role_change"),
        request=request,
    )
    await session.commit()
    return result


@router.delete("/access/role/{area}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def clear_role_rights(
    area: str, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await access_admin.clear_role_rights(
        session,
        user,
        area,
        confirm=confirmation(session, user, "user.role_change"),
        request=request,
    )
    await session.commit()


@router.put("/access/users/{user_id}/areas/{area}")
async def set_user_rights(
    user_id: uuid.UUID,
    area: str,
    payload: AreaRightsIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, bool]:
    result = await access_admin.set_user_rights(
        session,
        user,
        user_id,
        area,
        payload.model_dump(),
        confirm=confirmation(session, user, "user.role_change"),
        request=request,
    )
    await session.commit()
    return result


@router.delete(
    "/access/users/{user_id}/areas/{area}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def clear_user_rights(
    user_id: uuid.UUID, area: str, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await access_admin.clear_user_rights(
        session,
        user,
        user_id,
        area,
        confirm=confirmation(session, user, "user.role_change"),
        request=request,
    )
    await session.commit()


@router.put("/access/users/{user_id}/scope")
async def set_scope(
    user_id: uuid.UUID, payload: ScopeIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Limit a sub-admin to some departments or locations; both empty removes the limit."""
    result = await access_admin.set_scope(
        session,
        user,
        user_id,
        department_ids=payload.department_ids,
        location_ids=payload.location_ids,
        confirm=confirmation(session, user, "user.role_change"),
        request=request,
    )
    await session.commit()
    return {"scope": result}


# ── API keys and webhooks ───────────────────────────────────────────────────

_ADMIN = [Depends(require_roles(UserRole.ADMIN))]


class _WebhookActive(BaseModel):
    active: bool


@router.get("/integrations/api-keys", dependencies=_ADMIN)
async def list_api_keys(user: CurrentUser, session: DbSession) -> list[dict]:
    return await integrations.list_keys(session, user)


@router.post("/integrations/api-keys", status_code=status.HTTP_201_CREATED, dependencies=_ADMIN)
async def create_api_key(
    payload: integrations.ApiKeyIn, user: CurrentUser, session: DbSession, request: Request
) -> dict:
    """The key is in this response only; it cannot be shown again."""
    return await integrations.create_key(session, user, payload, request=request)


@router.post("/integrations/api-keys/{key_id}/revoke", dependencies=_ADMIN)
async def revoke_api_key(
    key_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict:
    return await integrations.revoke_key(session, user, key_id, request=request)


@router.get("/integrations/webhooks", dependencies=_ADMIN)
async def list_webhooks(user: CurrentUser, session: DbSession) -> list[dict]:
    return await integrations.list_endpoints(session, user)


@router.post("/integrations/webhooks", status_code=status.HTTP_201_CREATED, dependencies=_ADMIN)
async def create_webhook(
    payload: integrations.EndpointIn, user: CurrentUser, session: DbSession, request: Request
) -> dict:
    """The signing secret is in this response only."""
    return await integrations.create_endpoint(session, user, payload, request=request)


@router.patch("/integrations/webhooks/{endpoint_id}", dependencies=_ADMIN)
async def set_webhook_active(
    endpoint_id: uuid.UUID,
    payload: _WebhookActive,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict:
    return await integrations.set_endpoint_active(
        session, user, endpoint_id, payload.active, request=request
    )


@router.get("/integrations/webhooks/{endpoint_id}/deliveries", dependencies=_ADMIN)
async def webhook_deliveries(
    endpoint_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[dict]:
    return await integrations.list_deliveries(session, user, endpoint_id)
