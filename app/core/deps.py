"""Request-scoped dependencies: the current user, and tenant scoping.

Everything that decides "who is calling and what may they touch" lives here, so
there is exactly one implementation of the tenant-isolation rule to audit.
"""

from __future__ import annotations

import uuid
from typing import Annotated, TypeVar

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

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
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """Resolve the caller from the Bearer token.

    Re-loads the user on every request rather than trusting the token's claims
    wholesale, so a deactivated user or a suspended tenant loses access
    immediately instead of at token expiry.
    """
    if credentials is None:
        raise CREDENTIALS_ERROR

    try:
        payload = decode_token(credentials.credentials, expected_type="access")
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
    if user.tokens_valid_from and issued_at and issued_at < user.tokens_valid_from.timestamp():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session was ended. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_session)]


def require_roles(*roles: UserRole):
    """Dependency factory gating an endpoint on the caller's coarse role.

    Fine-grained per-module permissions are checked in the service layer
    against `RoleModulePermission`; this is the cheap first gate.
    """

    async def _check(user: CurrentUser) -> User:
        if user.role not in roles and user.role is not UserRole.PLATFORM_ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this resource",
            )
        return user

    return _check


ModelT = TypeVar("ModelT")


def tenant_scoped(stmt: Select, model: type[ModelT], user: User) -> Select:
    """Apply the tenant filter to a query.

    The single place tenant isolation is implemented. Services call this rather
    than hand-writing the filter, so the rule cannot drift between modules and
    a missing filter is visible as a missing call.

    A platform admin acting inside a tenant is still scoped to it; the only way
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
    "require_roles",
    "select",
    "tenant_scoped",
]
