"""Interview scheduling, panels and feedback (slice 21)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.crypto import encrypt_secret
from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.interview import (
    CalendarConnection,
    CalendarProviderKind,
    Interview,
    InterviewMode,
    InterviewStage,
    InterviewStageType,
    InterviewStatus,
    Recommendation,
    ResponseStatus,
)
from app.models.user import UserRole
from app.services.interviews import panel_slots
from app.services.interviews import service as interview_service

router = APIRouter(tags=["interviews"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)
PANEL_ROLES = (
    UserRole.TENANT_ADMIN,
    UserRole.HR_ADMIN,
    UserRole.HR_OPS,
    UserRole.MANAGER,
    UserRole.EMPLOYEE,
)


# ── Contracts ───────────────────────────────────────────────────────────────


class StageCreate(BaseModel):
    requisition_id: uuid.UUID
    sequence: int = Field(ge=1, le=20)
    name: str = Field(min_length=1, max_length=128)
    stage_type: InterviewStageType
    duration_minutes: int = Field(default=60, ge=5, le=480)
    mode: InterviewMode = InterviewMode.VIDEO
    feedback_template_id: uuid.UUID | None = None
    required_competencies: list[str] = Field(default_factory=list)
    is_eliminating: bool = True
    feedback_required: bool = True
    preparation_notes: str | None = None


class StageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    requisition_id: uuid.UUID
    sequence: int
    name: str
    stage_type: InterviewStageType
    duration_minutes: int
    mode: InterviewMode
    required_competencies: list
    is_eliminating: bool
    feedback_required: bool


class ProposeRequest(BaseModel):
    stage_id: uuid.UUID
    panelist_user_ids: list[uuid.UUID] = Field(min_length=1)
    from_day: date
    days: int = Field(default=7, ge=1, le=30)
    timezone_name: str = "Asia/Kolkata"
    limit: int = Field(default=20, ge=1, le=100)


class ScheduleRequest(BaseModel):
    candidate_id: uuid.UUID
    stage_id: uuid.UUID
    #: Wall-clock in `candidate_timezone` if naive; an instant if it carries one.
    start: datetime
    candidate_timezone: str = "Asia/Kolkata"
    panelist_user_ids: list[uuid.UUID] = Field(min_length=1)
    observer_user_ids: list[uuid.UUID] = Field(default_factory=list)
    chair_user_id: uuid.UUID | None = None
    mode: InterviewMode | None = None
    location: str | None = Field(default=None, max_length=1024)


class RescheduleRequest(BaseModel):
    start: datetime
    reason: str = Field(min_length=1, max_length=2000)
    candidate_timezone: str | None = None


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class ResponseRequest(BaseModel):
    panelist_user_id: uuid.UUID
    response: ResponseStatus


class OutcomeRequest(BaseModel):
    status: InterviewStatus


class FeedbackRequest(BaseModel):
    recommendation: Recommendation
    #: `{competency_code: score}` as strings — money and scores cross the wire as
    #: strings so nothing becomes a float on the way.
    ratings: dict[str, str] = Field(default_factory=dict)
    strengths: str = ""
    concerns: str = ""


class InterviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    candidate_id: uuid.UUID
    stage_id: uuid.UUID
    scheduled_start: datetime
    scheduled_end: datetime
    candidate_timezone: str
    mode: InterviewMode
    location_or_link: str | None
    status: InterviewStatus
    reschedule_count: int
    external_event_id: str | None
    join_url: str | None
    calendar_warnings: list


class CalendarConnect(BaseModel):
    provider: CalendarProviderKind
    account_email: str = Field(min_length=3, max_length=320)
    access_token: str = Field(min_length=1)
    refresh_token: str | None = None
    expires_at: datetime | None = None
    scopes: list[str] = Field(default_factory=list)


class SelfScheduleCreate(BaseModel):
    candidate_id: uuid.UUID
    stage_id: uuid.UUID
    #: `[{"start": iso, "end": iso, "panelist_user_ids": [...]}]`
    slots: list[dict[str, Any]] = Field(min_length=1)
    candidate_timezone: str = "Asia/Kolkata"


class SelfScheduleBooking(BaseModel):
    slot_start: datetime
    candidate_timezone: str | None = None


class BulkPlanRequest(BaseModel):
    window_start: datetime
    window_end: datetime
    duration_minutes: int = Field(ge=5, le=480)
    gap_minutes: int = Field(default=10, ge=0, le=120)
    candidate_ids: list[str] = Field(min_length=1)
    interviewers: list[dict[str, Any]] = Field(min_length=1)
    panel_size: int = Field(default=1, ge=1, le=6)
    timezone_name: str = "Asia/Kolkata"


# ═══════════════════════════════════════════════════════════════════════════
# Stages
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "/interviews/stages",
    response_model=StageRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def create_stage(
    payload: StageCreate, user: CurrentUser, session: DbSession
) -> StageRead:
    stage = InterviewStage(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(stage)
    await session.commit()
    await session.refresh(stage)
    return StageRead.model_validate(stage)


@router.get(
    "/interviews/stages",
    response_model=list[StageRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_stages(
    user: CurrentUser, session: DbSession, requisition_id: uuid.UUID | None = None
) -> list[StageRead]:
    stmt = select(InterviewStage).order_by(InterviewStage.sequence)
    if requisition_id is not None:
        stmt = stmt.where(InterviewStage.requisition_id == requisition_id)
    rows = await session.scalars(tenant_scoped(stmt, InterviewStage, user))
    return [StageRead.model_validate(r) for r in rows.all()]


# ═══════════════════════════════════════════════════════════════════════════
# Calendar connections
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "/interviews/calendar/connect", dependencies=[Depends(require_roles(*PANEL_ROLES))]
)
async def connect_calendar(
    payload: CalendarConnect, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Store one interviewer's own OAuth tokens.

    Per interviewer rather than per tenant: reading free/busy needs that person's
    consent, and a service account impersonating the whole company is both a
    harder sell and a much larger blast radius.
    """
    existing = await session.scalar(
        tenant_scoped(
            select(CalendarConnection).where(
                CalendarConnection.user_id == user.id,
                CalendarConnection.provider == payload.provider,
            ),
            CalendarConnection,
            user,
        )
    )
    connection = existing or CalendarConnection(
        tenant_id=user.tenant_id, user_id=user.id, provider=payload.provider,
        account_email=payload.account_email,
    )
    connection.account_email = payload.account_email
    connection.access_token_encrypted = encrypt_secret(payload.access_token, user.tenant_id)
    if payload.refresh_token:
        connection.refresh_token_encrypted = encrypt_secret(
            payload.refresh_token, user.tenant_id
        )
    connection.expires_at = payload.expires_at
    connection.scopes = payload.scopes
    connection.revoked_at = None
    connection.last_error = None
    session.add(connection)
    await session.commit()
    return {"connected": True, "provider": payload.provider.value}


@router.post(
    "/interviews/calendar/disconnect", dependencies=[Depends(require_roles(*PANEL_ROLES))]
)
async def disconnect_calendar(
    provider: CalendarProviderKind, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    connection = await session.scalar(
        tenant_scoped(
            select(CalendarConnection).where(
                CalendarConnection.user_id == user.id,
                CalendarConnection.provider == provider,
            ),
            CalendarConnection,
            user,
        )
    )
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not connected.")
    connection.revoked_at = datetime.now(tz=None).astimezone()
    connection.access_token_encrypted = None
    connection.refresh_token_encrypted = None
    await session.commit()
    return {"disconnected": True}


# ═══════════════════════════════════════════════════════════════════════════
# Scheduling
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/interviews/propose", dependencies=[Depends(require_roles(*HR_ROLES))])
async def propose_slots(
    payload: ProposeRequest, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Times that work for the whole panel.

    A panelist whose calendar could not be read is named as unverified rather
    than assumed free — that assumption books an interview over an existing
    meeting, discovered on the day.
    """
    result = await interview_service.propose_panel_slots(
        session,
        user,
        stage_id=payload.stage_id,
        panelist_user_ids=payload.panelist_user_ids,
        from_day=payload.from_day,
        days=payload.days,
        timezone_name=payload.timezone_name,
        limit=payload.limit,
    )
    await session.commit()
    return result


@router.post(
    "/interviews",
    response_model=InterviewRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def schedule(
    payload: ScheduleRequest, user: CurrentUser, session: DbSession, request: Request
) -> InterviewRead:
    interview = await interview_service.schedule_interview(
        session,
        user,
        candidate_id=payload.candidate_id,
        stage_id=payload.stage_id,
        start=payload.start,
        panelist_user_ids=payload.panelist_user_ids,
        observer_user_ids=payload.observer_user_ids,
        chair_user_id=payload.chair_user_id,
        mode=payload.mode,
        location=payload.location,
        candidate_timezone=payload.candidate_timezone,
        request=request,
    )
    await session.commit()
    await session.refresh(interview)
    return InterviewRead.model_validate(interview)


@router.patch(
    "/interviews/{interview_id}/reschedule",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def reschedule(
    interview_id: uuid.UUID,
    payload: RescheduleRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Move an interview. The calendar event is patched, never duplicated."""
    result = await interview_service.reschedule_interview(
        session,
        user,
        interview_id=interview_id,
        start=payload.start,
        reason=payload.reason,
        candidate_timezone=payload.candidate_timezone,
        request=request,
    )
    await session.commit()
    return result


@router.post(
    "/interviews/{interview_id}/cancel", dependencies=[Depends(require_roles(*HR_ROLES))]
)
async def cancel(
    interview_id: uuid.UUID,
    payload: ReasonRequest,
    user: CurrentUser,
    session: DbSession,
) -> dict[str, Any]:
    result = await interview_service.cancel_interview(
        session, user, interview_id=interview_id, reason=payload.reason
    )
    await session.commit()
    return result


@router.post(
    "/interviews/{interview_id}/response",
    dependencies=[Depends(require_roles(*PANEL_ROLES))],
)
async def record_response(
    interview_id: uuid.UUID,
    payload: ResponseRequest,
    user: CurrentUser,
    session: DbSession,
) -> dict[str, Any]:
    """Record a panel response. A decline reaches the recruiter immediately."""
    result = await interview_service.record_response(
        session,
        user,
        interview_id=interview_id,
        panelist_user_id=payload.panelist_user_id,
        response=payload.response,
    )
    await session.commit()
    return result


@router.patch(
    "/interviews/{interview_id}/outcome", dependencies=[Depends(require_roles(*HR_ROLES))]
)
async def set_outcome(
    interview_id: uuid.UUID,
    payload: OutcomeRequest,
    user: CurrentUser,
    session: DbSession,
) -> dict[str, Any]:
    """Mark completed, or record a no-show.

    Candidate and panel no-shows are separate statuses: one is a sourcing signal
    and one is an internal one, and a single NO_SHOW answers neither question.
    """
    interview = await session.scalar(
        tenant_scoped(select(Interview).where(Interview.id == interview_id), Interview, user)
    )
    if interview is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    interview.status = payload.status
    if payload.status is InterviewStatus.COMPLETED:
        interview.completed_at = datetime.now(tz=None).astimezone()
    await session.commit()
    return {"interview_id": str(interview.id), "status": interview.status.value}


@router.get(
    "/interviews", response_model=list[InterviewRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_interviews(
    user: CurrentUser, session: DbSession, candidate_id: uuid.UUID | None = None
) -> list[InterviewRead]:
    stmt = select(Interview).order_by(Interview.scheduled_start)
    if candidate_id is not None:
        stmt = stmt.where(Interview.candidate_id == candidate_id)
    rows = await session.scalars(tenant_scoped(stmt, Interview, user))
    return [InterviewRead.model_validate(r) for r in rows.all()]


# ═══════════════════════════════════════════════════════════════════════════
# Feedback
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "/interviews/{interview_id}/feedback",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*PANEL_ROLES))],
)
async def submit_feedback(
    interview_id: uuid.UUID,
    payload: FeedbackRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    result = await interview_service.submit_feedback(
        session,
        user,
        interview_id=interview_id,
        recommendation=payload.recommendation,
        ratings=payload.ratings,
        strengths=payload.strengths,
        concerns=payload.concerns,
        request=request,
    )
    await session.commit()
    return result


@router.get(
    "/interviews/{interview_id}/feedback",
    dependencies=[Depends(require_roles(*PANEL_ROLES))],
)
async def read_feedback(
    interview_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    hide_until_submitted: bool = True,
) -> dict[str, Any]:
    """Peer feedback is hidden until you submit your own.

    `hide_until_submitted` is the tenant's switch. The default is on: four
    opinions that anchored on the first are one opinion.
    """
    return await interview_service.visible_feedback(
        session,
        user,
        interview_id=interview_id,
        hide_until_submitted=hide_until_submitted,
        viewer_is_hiring_manager=user.role
        in (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.MANAGER),
    )


@router.get(
    "/interviews/{interview_id}/consolidated",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def consolidated(
    interview_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """The panel's view for the hiring manager, disagreement intact."""
    return await interview_service.consolidated_view(session, user, interview_id=interview_id)


@router.get(
    "/interviews/{interview_id}/advance-check",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def advance_check(
    interview_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """What stops this candidate moving on. Enforced at the service.

    A check that lives only in the screen is not a check — the API is reachable
    without it.
    """
    blockers = await interview_service.advance_blockers(
        session, user, interview_id=interview_id
    )
    return {"can_advance": not blockers, "blockers": blockers}


# ═══════════════════════════════════════════════════════════════════════════
# Self-scheduling and bulk
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "/interviews/self-schedule",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def create_self_schedule(
    payload: SelfScheduleCreate, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Offer a candidate a set of approved slots to pick from."""
    link, token = await interview_service.create_self_schedule_link(
        session,
        user,
        candidate_id=payload.candidate_id,
        stage_id=payload.stage_id,
        slots=payload.slots,
        candidate_timezone=payload.candidate_timezone,
    )
    await session.commit()
    return {
        "token": token,
        "expires_at": link.expires_at.isoformat(),
        "slot_count": len(payload.slots),
        "note": "This token is shown once. Send it to the candidate now.",
    }


@router.post("/public/interviews/self-schedule/{token}")
async def book_self_schedule(
    token: str, payload: SelfScheduleBooking, session: DbSession
) -> dict[str, Any]:
    """A candidate books one of the offered slots.

    Unauthenticated by design — a candidate has no account. The tenant comes
    from the token, and only a slot that was actually offered can be booked.
    """
    result = await interview_service.book_self_schedule(
        session,
        token=token,
        slot_start=payload.slot_start,
        candidate_timezone=payload.candidate_timezone,
    )
    await session.commit()
    return result


@router.post("/interviews/bulk-plan", dependencies=[Depends(require_roles(*HR_ROLES))])
async def bulk_plan(
    payload: BulkPlanRequest, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Plan a campus or walk-in drive.

    Returns the grid without booking anything: a drive plan is reviewed before
    it is committed, and dozens of calendar invitations are not something to
    send on a first attempt.
    """
    slots = panel_slots.generate_slots(
        window_start=payload.window_start,
        window_end=payload.window_end,
        duration_minutes=payload.duration_minutes,
        gap_minutes=payload.gap_minutes,
    )
    plan = panel_slots.allocate(
        slots=slots,
        candidate_ids=payload.candidate_ids,
        interviewers=[
            panel_slots.InterviewerCapacity(
                interviewer_id=str(row["interviewer_id"]),
                label=str(row.get("label", "")),
                max_interviews=int(row.get("max_interviews", 6)),
            )
            for row in payload.interviewers
        ],
        panel_size=payload.panel_size,
    )
    return {
        "slots_generated": len(slots),
        **plan.as_dict(payload.timezone_name),
    }
