"""Request-scoped dependencies: the current user, and tenant scoping.

Everything that decides "who is calling and what may they touch" lives here, so
there is exactly one implementation of the tenant-isolation rule to audit.
"""

from __future__ import annotations

import uuid
from typing import Annotated, TypeVar

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import cookies
from app.core.security import decode_token
from app.db.session import get_session
from app.models.user import User, UserRole

bearer_scheme = HTTPBearer(auto_error=False)

CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """Resolve the caller from a Bearer token or the web app's session cookie.

    Re-loads the user on every request rather than trusting the token's claims
    wholesale, so a deactivated user or a suspended tenant loses access
    immediately instead of at token expiry.

    A cookie-authenticated request that changes state must carry the CSRF
    header (see `app.core.cookies`); a Bearer request need not, because no
    browser attaches an Authorization header by itself.
    """
    if credentials is not None:
        token = credentials.credentials
    else:
        token = request.cookies.get(cookies.ACCESS_COOKIE)
        if not token:
            raise CREDENTIALS_ERROR
        if not cookies.csrf_ok(request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This request is missing its CSRF token. Reload the page and try again.",
            )

    try:
        payload = decode_token(token, expected_type="access")
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise CREDENTIALS_ERROR from exc

    subject = payload.get("sub")
    if not subject:
        raise CREDENTIALS_ERROR

    try:
        user_id = uuid.UUID(subject)
    except ValueError as exc:
        raise CREDENTIALS_ERROR from exc

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise CREDENTIALS_ERROR

    # A token minted before a forced logout or password change is dead.
    issued_at = payload.get("iat")
    # Whole seconds: `iat` has no fraction, so a sign-in in the same second as
    # the reset would otherwise read as older than it and be refused.
    cutoff = int(user.tokens_valid_from.timestamp()) if user.tokens_valid_from else None
    if cutoff is not None and issued_at and issued_at < cutoff:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session was ended. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # The tenant's session policy: privileged-role IP allowlist, idle timeout,
    # absolute lifetime and per-device revocation. A no-op for a tenant that
    # has not saved a policy. Imported here because opsec imports this module.
    from app.services.opsec.enforcement import enforce_session_policy

    await enforce_session_policy(request, session, user, payload)

    user.has_reports = await has_direct_reports(session, user)
    return user


async def has_direct_reports(session: AsyncSession, user: User) -> bool:
    """Whether anybody in the caller's tenant reports to the caller's employee record."""
    if user.employee_id is None:
        return False
    from app.models.employee import Employee  # local: models import deps indirectly

    found = await session.scalar(
        tenant_scoped(
            select(Employee.id).where(
                Employee.manager_id == user.employee_id, Employee.deleted_at.is_(None)
            ),
            Employee,
            user,
        ).limit(1)
    )
    return found is not None


CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_session)]


def require_roles(*roles: UserRole, line_managers: bool = False):
    """Dependency factory gating an endpoint on the caller's coarse role.

    `line_managers=True` also admits anybody with a direct report, whatever
    their role: managing people is a place in the org chart, not a login role.
    Which people they may act on is still the reporting-line check in
    `app.core.permissions`.

    Fine-grained per-module permissions are checked in the service layer
    against `RoleModulePermission`; this is the cheap first gate.
    """

    async def _check(user: CurrentUser) -> User:
        if line_managers and user.has_reports:
            return user
        if user.role not in roles and user.role is not UserRole.SUPER_ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this resource",
            )
        return user

    return _check


def require_module(module: str):
    """Dependency factory refusing requests to a module the workspace has not switched on.

    The sidebar hiding a module is a convenience; this is the enforcement. A
    tenant on a plan without rewards cannot reach the rewards API by typing a URL.
    """

    async def _module_enabled(user: CurrentUser, session: DbSession) -> None:
        from app.models.tenant import Tenant  # local: models import deps indirectly

        tenant = await session.get(Tenant, user.tenant_id)
        if tenant is None or not (tenant.enabled_modules or {}).get(module):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"The {module.replace('_', ' ')} module is not enabled for this workspace.",
            )

    return _module_enabled


ModelT = TypeVar("ModelT")


def tenant_scoped(stmt: Select, model: type[ModelT], user: User) -> Select:
    """Apply the tenant filter to a query.

    The single place tenant isolation is implemented. Services call this rather
    than hand-writing the filter, so the rule cannot drift between modules and
    a missing filter is visible as a missing call.

    A super admin acting inside a tenant is still scoped to it; the only way
    to read across tenants is a deliberate query that does not use this helper.
    """
    return stmt.where(model.tenant_id == user.tenant_id)  # type: ignore[attr-defined]


async def get_tenant_id(user: CurrentUser) -> uuid.UUID:
    """The caller's tenant. Never read a tenant id from the request body."""
    return user.tenant_id


__all__ = [
    "CurrentUser",
    "DbSession",
    "get_current_user",
    "get_tenant_id",
    "has_direct_reports",
    "require_roles",
    "select",
    "tenant_scoped",
]
