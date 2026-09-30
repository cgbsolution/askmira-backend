"""1:1 meetings between a manager and a report.

Thin: parse, delegate, serialise. Who may see a meeting — its two participants
and nobody else — is decided in `app.services.performance.one_on_ones`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request, status

from app.core.deps import CurrentUser, DbSession
from app.models.one_on_one import OneOnOneStatus
from app.schemas.one_on_one import (
    OneOnOneCancel,
    OneOnOneComplete,
    OneOnOneCreate,
    OneOnOneDetail,
    OneOnOneItemCreate,
    OneOnOneItemUpdate,
    OneOnOneNoteWrite,
    OneOnOnePartner,
    OneOnOneRead,
    OneOnOneUpdate,
    ReportOneOnOneSummary,
)
from app.services.performance import one_on_ones as service

router = APIRouter(prefix="/one-on-ones", tags=["one-on-ones"])


@router.get("", response_model=list[OneOnOneRead])
async def list_meetings(
    user: CurrentUser,
    session: DbSession,
    meeting_status: OneOnOneStatus | None = Query(default=None, alias="status"),
    with_employee_id: uuid.UUID | None = None,
) -> list[OneOnOneRead]:
    """Your own 1:1s, from either side."""
    return await service.list_meetings(
        session, user, meeting_status=meeting_status, with_employee_id=with_employee_id
    )


@router.get("/partners", response_model=list[OneOnOnePartner])
async def partners(user: CurrentUser, session: DbSession) -> list[OneOnOnePartner]:
    """Who you can schedule a 1:1 with: your manager and your direct reports."""
    return await service.partners(session, user)


@router.get("/reports", response_model=list[ReportOneOnOneSummary])
async def reports(user: CurrentUser, session: DbSession) -> list[ReportOneOnOneSummary]:
    """Your direct reports and where their 1:1s stand."""
    return await service.reports_summary(session, user)


@router.post("", response_model=OneOnOneDetail, status_code=status.HTTP_201_CREATED)
async def schedule(
    payload: OneOnOneCreate, user: CurrentUser, session: DbSession, request: Request
) -> OneOnOneDetail:
    return await service.schedule(session, user, payload, request=request)


@router.get("/{meeting_id}", response_model=OneOnOneDetail)
async def detail(meeting_id: uuid.UUID, user: CurrentUser, session: DbSession) -> OneOnOneDetail:
    return await service.get_detail(session, user, meeting_id)


@router.patch("/{meeting_id}", response_model=OneOnOneDetail)
async def reschedule(
    meeting_id: uuid.UUID,
    payload: OneOnOneUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> OneOnOneDetail:
    return await service.reschedule(session, user, meeting_id, payload, request=request)


@router.post("/{meeting_id}/items", response_model=OneOnOneDetail, status_code=201)
async def add_item(
    meeting_id: uuid.UUID,
    payload: OneOnOneItemCreate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> OneOnOneDetail:
    return await service.add_item(session, user, meeting_id, payload, request=request)


@router.patch("/{meeting_id}/items/{item_id}", response_model=OneOnOneDetail)
async def update_item(
    meeting_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: OneOnOneItemUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> OneOnOneDetail:
    return await service.update_item(session, user, meeting_id, item_id, payload, request=request)


@router.delete(
    "/{meeting_id}/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_item(
    meeting_id: uuid.UUID,
    item_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> None:
    await service.delete_item(session, user, meeting_id, item_id, request=request)


@router.put("/{meeting_id}/my-note", response_model=OneOnOneDetail)
async def write_note(
    meeting_id: uuid.UUID,
    payload: OneOnOneNoteWrite,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> OneOnOneDetail:
    """Your private note. Returned to you and to nobody else."""
    return await service.write_note(session, user, meeting_id, payload, request=request)


@router.post("/{meeting_id}/complete", response_model=OneOnOneDetail)
async def complete(
    meeting_id: uuid.UUID,
    payload: OneOnOneComplete,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> OneOnOneDetail:
    return await service.complete(session, user, meeting_id, payload, request=request)


@router.post("/{meeting_id}/cancel", response_model=OneOnOneDetail)
async def cancel(
    meeting_id: uuid.UUID,
    payload: OneOnOneCancel,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> OneOnOneDetail:
    return await service.cancel(session, user, meeting_id, payload, request=request)
