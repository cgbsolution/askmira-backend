"""360 feedback requests and private feedback between colleagues.

Thin: parse, delegate, serialise. Who reads which answer — the manager line
with names, the person as an anonymised summary once enough have answered — is
decided in `app.services.performance.feedback`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, status

from app.core.deps import CurrentUser, DbSession
from app.schemas.feedback import (
    FeedbackDecline,
    FeedbackForm,
    FeedbackInboxItem,
    FeedbackRemindResult,
    FeedbackRequestCreate,
    FeedbackRequestDetail,
    FeedbackRequestSummary,
    FeedbackResponseSubmit,
    FeedbackScope,
    PrivateFeedbackCreate,
    PrivateFeedbackRead,
    PrivateFeedbackScope,
)
from app.services.performance import feedback as service

router = APIRouter(prefix="/feedback", tags=["feedback"])


# ── Requests ────────────────────────────────────────────────────────────────


@router.get("/requests", response_model=list[FeedbackRequestSummary])
async def list_requests(
    user: CurrentUser, session: DbSession, scope: FeedbackScope = "about_me"
) -> list[FeedbackRequestSummary]:
    return await service.list_requests(session, user, scope=scope)


@router.post(
    "/requests", response_model=FeedbackRequestDetail, status_code=status.HTTP_201_CREATED
)
async def create_request(
    payload: FeedbackRequestCreate, user: CurrentUser, session: DbSession, request: Request
) -> FeedbackRequestDetail:
    return await service.create_request(session, user, payload, request=request)


@router.get("/requests/{request_id}", response_model=FeedbackRequestDetail)
async def get_request(
    request_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> FeedbackRequestDetail:
    return await service.get_request(session, user, request_id)


@router.post("/requests/{request_id}/remind", response_model=FeedbackRemindResult)
async def remind(
    request_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> FeedbackRemindResult:
    return await service.remind(session, user, request_id, request=request)


@router.post("/requests/{request_id}/close", response_model=FeedbackRequestDetail)
async def close_request(
    request_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> FeedbackRequestDetail:
    return await service.close_request(session, user, request_id, request=request)


# ── Answering ───────────────────────────────────────────────────────────────


@router.get("/inbox", response_model=list[FeedbackInboxItem])
async def inbox(user: CurrentUser, session: DbSession) -> list[FeedbackInboxItem]:
    """Feedback you have been asked to give."""
    return await service.inbox(session, user)


@router.get("/responses/{reviewer_id}", response_model=FeedbackForm)
async def get_form(reviewer_id: uuid.UUID, user: CurrentUser, session: DbSession) -> FeedbackForm:
    return await service.get_form(session, user, reviewer_id)


@router.post("/responses/{reviewer_id}/submit", response_model=FeedbackForm)
async def submit(
    reviewer_id: uuid.UUID,
    payload: FeedbackResponseSubmit,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> FeedbackForm:
    return await service.submit(session, user, reviewer_id, payload, request=request)


@router.post("/responses/{reviewer_id}/decline", response_model=FeedbackForm)
async def decline(
    reviewer_id: uuid.UUID,
    payload: FeedbackDecline,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> FeedbackForm:
    return await service.decline(session, user, reviewer_id, payload, request=request)


# ── Private feedback ────────────────────────────────────────────────────────


@router.get("/private", response_model=list[PrivateFeedbackRead])
async def list_private(
    user: CurrentUser, session: DbSession, scope: PrivateFeedbackScope = "received"
) -> list[PrivateFeedbackRead]:
    return await service.list_private(session, user, scope=scope)


@router.post("/private", response_model=PrivateFeedbackRead, status_code=status.HTTP_201_CREATED)
async def give_private(
    payload: PrivateFeedbackCreate, user: CurrentUser, session: DbSession, request: Request
) -> PrivateFeedbackRead:
    return await service.give_private(session, user, payload, request=request)
