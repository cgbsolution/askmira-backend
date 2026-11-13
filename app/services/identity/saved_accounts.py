"""Accounts signed in on one browser, and switching between them.

A person who runs HR for several workspaces holds a separate login in each
(one email is one login platform-wide). Signing out and back in to move
between them is slow, so the browser keeps each account's refresh token in an
httpOnly cookie (`app.core.cookies.ACCOUNTS_COOKIE`) and the account menu
switches with one click — as an email provider's account switcher does.

Nothing about this bypasses a workspace's rules. A switch is checked exactly
as a refresh is (active login, no password change or forced sign-out since,
active workspace) and then as a request is: the target workspace's IP
allowlist, idle timeout, session lifetime and two-step rule. One account is
active per browser at a time; the portal reloads every open tab on a switch.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import cookies
from app.models.audit import AuditAction
from app.schemas.auth import AuthResponse, SavedAccountRead
from app.services import audit
from app.services import auth as auth_service
from app.services.identity import directory
from app.services.opsec.enforcement import enforce_session_policy


async def list_accounts(
    session: AsyncSession, request: Request
) -> tuple[list[SavedAccountRead], list[str]]:
    """The accounts still usable on this browser, and the saved list pruned to them.

    The session's own account is added when missing, so a browser signed in
    before the list existed lists it too.
    """
    tokens = cookies.saved_account_tokens(request)
    current_token = request.cookies.get(cookies.REFRESH_COOKIE)
    current = cookies.refresh_subject(current_token)
    if current_token and current is not None:
        tokens = cookies.merge_account(tokens, current_token)

    accounts: list[SavedAccountRead] = []
    kept: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        try:
            user, tenant = await auth_service.account_for_refresh_token(session, token)
        except HTTPException:
            continue  # ended, deactivated or its workspace switched off: forget it
        if str(user.id) in seen:
            continue
        seen.add(str(user.id))
        kept.append(token)
        brand = await directory.branding(session, tenant)
        read = await auth_service.user_read(session, user)
        accounts.append(
            SavedAccountRead(
                user_id=user.id,
                full_name=user.full_name,
                email=user.email,
                role=user.role,
                photo_url=read.photo_url,
                workspace=brand.workspace,
                workspace_name=brand.name,
                logo_url=brand.logo_url,
                current=str(user.id) == current,
            )
        )
    return accounts, kept


def saved_token(request: Request, user_id: uuid.UUID) -> str | None:
    wanted = str(user_id)
    for token in cookies.saved_account_tokens(request):
        if cookies.refresh_subject(token) == wanted:
            return token
    current = request.cookies.get(cookies.REFRESH_COOKIE)
    return current if cookies.refresh_subject(current) == wanted else None


async def switch(session: AsyncSession, request: Request, user_id: uuid.UUID) -> AuthResponse:
    """A fresh session for a saved account, after every check a request would make.

    Raises 404 when the account is not saved here, 401 when its session has
    ended (the caller then forgets it), 403 when its workspace refuses it.
    """
    token = saved_token(request, user_id)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That account is not signed in on this browser. Sign in to it instead.",
        )
    user, _tenant = await auth_service.account_for_refresh_token(session, token)
    # The workspace's own rules, as on any request: IP allowlist, idle timeout,
    # session lifetime. Two-step set-up is enforced by the portal after the switch.
    await enforce_session_policy(request, session, user, {})
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.LOGIN,
        entity_type="user",
        entity_id=user.id,
        actor=user,
        summary="Switched to this account on a browser where it was signed in",
        request=request,
    )
    await session.commit()
    await session.refresh(user)
    return await auth_service.issue_session(session, user)
