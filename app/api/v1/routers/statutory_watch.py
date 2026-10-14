"""Statutory change notices: published by the platform, acted on by each workspace."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, status

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.services import statutory_watch

router = APIRouter(
    prefix="/statutory-notices",
    tags=["payroll"],
    dependencies=[Depends(require_roles(UserRole.ADMIN, UserRole.SUB_ADMIN))],
)


@router.get("")
async def list_notices(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Changes in the law that touch this workspace, and whether its rule sets cover them."""
    return await statutory_watch.notices_for(session, user)


@router.get("/coverage")
async def coverage(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Which rule types have an approved version in force today, per jurisdiction."""
    return await statutory_watch.coverage(session, user)


@router.post("", status_code=status.HTTP_201_CREATED)
async def publish(
    payload: statutory_watch.NoticeIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Platform only: publish a change in the law to every workspace it affects."""
    return await statutory_watch.publish(session, user, payload, request=request)


@router.post("/{notice_id}/withdraw")
async def withdraw(
    notice_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await statutory_watch.withdraw(session, user, notice_id, request=request)


@router.post("/{notice_id}/draft", status_code=status.HTTP_201_CREATED)
async def draft(
    notice_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """A new unapproved rule-set version from the notice; a second admin approves it."""
    return await statutory_watch.draft_from_notice(session, user, notice_id, request=request)
