"""Ask Mira: the chat, each person's conversations, and the workspace's own setup."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.services import uploads
from app.services.companion import attachments
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


@router.post("/messages/files")
async def send_message_with_files(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    files: Annotated[list[UploadFile], File()],
    text: Annotated[str, Form(max_length=2000)] = "",
    conversation_id: Annotated[uuid.UUID | None, Form()] = None,
    client_ref: Annotated[
        str | None, Form(min_length=8, max_length=64, pattern=r"^[\w-]+$")
    ] = None,
) -> dict[str, Any]:
    """One turn with pictures or PDFs attached: read into words first, then answered."""
    if len(files) > attachments.MAX_FILES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Attach up to {attachments.MAX_FILES} files to one message.",
        )
    raw = [
        (file.filename or "file", await uploads.read_file(file, session, user.tenant_id))
        for file in files
    ]
    payload = companion.MessageIn(text=text, conversation_id=conversation_id, client_ref=client_ref)
    return await companion.send(session, user, payload, request=request, files=raw)


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


@router.post("/actions/{action_id}/confirm")
async def confirm_action(
    action_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """The caller pressed Confirm on an offer Ask Mira made them: carry it out, as them."""
    return await companion.confirm_offer(session, user, action_id, request=request)


@router.post("/actions/{action_id}/cancel")
async def cancel_action(
    action_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """The caller declined an offer Ask Mira made them. Nothing is done."""
    return await companion.cancel_offer(session, user, action_id, request=request)
