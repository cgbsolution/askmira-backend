"""Consultant hours: finance enters a consultant's billable hours, a second person approves."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, File, Query, Request, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import CurrentUser, DbSession
from app.services import uploads
from app.services.consultants import hours
from app.services.filings import support

router = APIRouter(prefix="/consultant-hours", tags=["consultant-hours"])


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool
    note: str | None = Field(default=None, max_length=1000)


@router.get("")
async def list_entries(
    user: CurrentUser,
    session: DbSession,
    month: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    employee_id: uuid.UUID | None = None,
    waiting_for_me: bool = False,
) -> list[dict[str, Any]]:
    return await hours.list_entries(
        session,
        user,
        month=month,
        status_filter=status_filter,
        employee_id=employee_id,
        waiting_for_me=waiting_for_me,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_entry(
    payload: hours.HoursIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await hours.create_entry(session, user, payload, request=request)


@router.get("/consultants")
async def list_consultants(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    return await hours.list_consultants(session, user)


@router.get("/{entry_id}")
async def get_entry(entry_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict[str, Any]:
    return await hours.get_entry(session, user, entry_id)


@router.put("/{entry_id}")
async def update_entry(
    entry_id: uuid.UUID,
    payload: hours.HoursIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await hours.update_entry(session, user, entry_id, payload, request=request)


@router.post("/{entry_id}/attachments", status_code=status.HTTP_201_CREATED)
async def add_attachment(
    entry_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    data = await uploads.read_file(file, session, user.tenant_id)
    return await hours.add_attachment(
        session, user, entry_id, name=file.filename or "", data=data, request=request
    )


@router.delete("/{entry_id}/attachments/{attachment_id}")
async def remove_attachment(
    entry_id: uuid.UUID,
    attachment_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await hours.remove_attachment(session, user, entry_id, attachment_id, request=request)


@router.get("/{entry_id}/attachments/{attachment_id}")
async def download_attachment(
    entry_id: uuid.UUID, attachment_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> Response:
    name, media_type, content = await hours.attachment_file(session, user, entry_id, attachment_id)
    return Response(
        content=content, media_type=media_type, headers=support.attachment_headers(name)
    )


@router.post("/{entry_id}/submit")
async def submit_entry(
    entry_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await hours.submit_entry(session, user, entry_id, request=request)


@router.post("/{entry_id}/decide")
async def decide_entry(
    entry_id: uuid.UUID,
    payload: DecisionIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await hours.decide_entry(
        session, user, entry_id, approve=payload.approve, note=payload.note, request=request
    )


@router.post("/{entry_id}/reopen")
async def reopen_entry(
    entry_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await hours.reopen_entry(session, user, entry_id, request=request)
