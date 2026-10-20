"""Ask Mira: the chat, each person's conversations, and the workspace's own setup."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, status

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.services.companion import config as companion_config
from app.services.companion import service as companion

router = APIRouter(prefix="/companion", tags=["companion"])

ADMIN_ONLY = [Depends(require_roles(UserRole.ADMIN))]


@router.get("/status")
async def companion_status(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """Whether the caller can chat now, why not, and who the workspace's Ask Mira is."""
    return await companion.status_for(session, user)


@router.get("/conversations")
async def list_conversations(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    return await companion.list_conversations(session, user)


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    return await companion.get_conversation(session, user, conversation_id)


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_conversation(
    conversation_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await companion.delete_conversation(session, user, conversation_id, request=request)


@router.post("/messages")
async def send_message(
    payload: companion.MessageIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """One turn: the caller's message in, Ask Mira's reply and what it looked up out."""
    return await companion.send(session, user, payload, request=request)


@router.get("/settings", dependencies=ADMIN_ONLY)
async def get_settings(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    return await companion.settings_view(session, user)


@router.put("/settings", dependencies=ADMIN_ONLY)
async def put_settings(
    payload: companion_config.CompanionConfig,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    await companion_config.set_config(session, user, payload, request=request)
    return await companion.settings_view(session, user)
