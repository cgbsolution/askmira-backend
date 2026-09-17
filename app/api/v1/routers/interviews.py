"""Interview scheduling, panels and feedback (slice 21).

Who sees what: HR and the requisition's hiring team see every interview for it;
a panellist sees the interviews they are on. Peer feedback stays hidden until
you submit your own, by tenant setting — there is no query parameter to turn it
off.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.interview import (
    CalendarProviderKind,
    InterviewMode,
    InterviewStageType,
    InterviewStatus,
    Recommendation,
    ResponseStatus,
)
from app.models.user import UserRole
from app.services.hiring import public_guard
from app.services.interviews import calendar_oauth, panel_slots
from app.services.interviews import service as interview_service

router = APIRouter(tags=["interviews"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
MANAGER_ROLES = (*HR_ROLES, UserRole.MANAGER)
PANEL_ROLES = (*MANAGER_ROLES, UserRole.EMPLOYEE)


# ── Contracts ───────────────────────────────────────────────────────────────


class StageCreate(BaseModel):
    requisition_id: uuid.UUID
    sequence: int = Field(ge=1, le=20)
    name: str = Field(min_length=1, max_length=128)
    stage_type: InterviewStageType
    duration_minutes: int = Field(default=60, ge=5, le=480)
    mode: InterviewMode = InterviewMode.VIDEO
    feedback_template_id: uuid.UUID | None = None
    required_competencies: list[str] = Field(default_factory=list, max_length=30)
    is_eliminating: bool = True
    feedback_required: bool = True
    preparation_notes: str | None = Field(default=None, max_length=4000)


class StageUpdate(BaseModel):
    sequence: int | None = Field(default=None, ge=1, le=20)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    stage_type: InterviewStageType | None = None
    duration_minutes: int | None = Field(default=None, ge=5, le=480)
    mode: InterviewMode | None = None
    required_competencies: list[str] | None = Field(default=None, max_length=30)
    is_eliminating: bool | None = None
    feedback_required: bool | None = None
    preparation_notes: str | None = Field(default=None, max_length=4000)


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
    preparation_notes: str | None


class ProposeRequest(BaseModel):
    stage_id: uuid.UUID
    panelist_user_ids: list[uuid.UUID] = Field(min_length=1, max_length=10)
    from_day: date
    days: int = Field(default=7, ge=1, le=30)
    timezone_name: str = Field(default="Asia/Kolkata", max_length=64)
    limit: int = Field(default=20, ge=1, le=100)


class ScheduleRequest(BaseModel):
    candidate_id: uuid.UUID
    stage_id: uuid.UUID
    #: Wall-clock in `candidate_timezone` if naive; an instant if it carries one.
    start: datetime
    candidate_timezone: str = Field(default="Asia/Kolkata", max_length=64)
    panelist_user_ids: list[uuid.UUID] = Field(min_length=1, max_length=10)
    observer_user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)
    chair_user_id: uuid.UUID | None = None
    mode: InterviewMode | None = None
    location: str | None = Field(default=None, max_length=1024)


class RescheduleRequest(BaseModel):
    start: datetime
    reason: str = Field(min_length=1, max_length=2000)
    candidate_timezone: str | None = Field(default=None, max_length=64)


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class ResponseRequest(BaseModel):
    panelist_user_id: uuid.UUID
    response: ResponseStatus


class OutcomeRequest(BaseModel):
    status: InterviewStatus
    note: str | None = Field(default=None, max_length=2000)


class FeedbackRequest(BaseModel):
    recommendation: Recommendation
    #: `{competency_code: score}` as strings — scores cross the wire as strings so
    #: nothing becomes a float on the way.
    ratings: dict[str, str] = Field(default_factory=dict, max_length=40)
    strengths: str = Field(default="", max_length=5000)
    concerns: str = Field(default="", max_length=5000)


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


class CalendarCallback(BaseModel):
    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=1, max_length=4096)


class SelfScheduleCreate(BaseModel):
    candidate_id: uuid.UUID
    stage_id: uuid.UUID
    #: `[{"start": iso, "end": iso, "panelist_user_ids": [...]}]`
    slots: list[dict[str, Any]] = Field(min_length=1, max_length=50)
    candidate_timezone: str = Field(default="Asia/Kolkata", max_length=64)


class SelfScheduleBooking(BaseModel):
    slot_start: datetime
    candidate_timezone: str | None = Field(default=None, max_length=64)


class BulkPlanRequest(BaseModel):
    window_start: datetime
    window_end: datetime
    duration_minutes: int = Field(ge=5, le=480)
    gap_minutes: int = Field(default=10, ge=0, le=120)
    candidate_ids: list[str] = Field(min_length=1, max_length=500)
    interviewers: list[dict[str, Any]] = Field(min_length=1, max_length=100)
    panel_size: int = Field(default=1, ge=1, le=6)
    timezone_name: str = Field(default="Asia/Kolkata", max_length=64)


# ═══════════════════════════════════════════════════════════════════════════
# Stages and panel people
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "/interviews/stages",
    response_model=StageRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def create_stage(
    payload: StageCreate, user: CurrentUser, session: DbSession, request: Request
) -> StageRead:
    stage = await interview_service.create_stage(
        session, user, values=payload.model_dump(), request=request
    )
    await session.commit()
    await session.refresh(stage)
    return StageRead.model_validate(stage)


@router.patch(
    "/interviews/stages/{stage_id}",
    response_model=StageRead,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def update_stage(
    stage_id: uuid.UUID,
    payload: StageUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> StageRead:
    stage = await interview_service.update_stage(
        session, user, stage_id=stage_id, changes=payload.model_dump(exclude_unset=True),
        request=request,
    )
    await session.commit()
    await session.refresh(stage)
    return StageRead.model_validate(stage)


@router.get(
    "/interviews/stages",
    response_model=list[StageRead],
    dependencies=[Depends(require_roles(*MANAGER_ROLES))],
)
async def list_stages(
    user: CurrentUser, session: DbSession, requisition_id: uuid.UUID | None = None
) -> list[StageRead]:
    rows = await interview_service.list_stages(session, user, requisition_id=requisition_id)
    return [StageRead.model_validate(r) for r in rows]


@router.get(
    "/interviews/panel-people", dependencies=[Depends(require_roles(*HR_ROLES))]
)
async def panel_people(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    return await interview_service.panel_people(session, user)


# ═══════════════════════════════════════════════════════════════════════════
# Calendar connections — your own
# ═══════════════════════════════════════════════════════════════════════════


@router.get(
    "/interviews/calendar/status", dependencies=[Depends(require_roles(*PANEL_ROLES))]
)
async def calendar_status(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    return await calendar_oauth.connection_status(session, user)


@router.post(
    "/interviews/calendar/{provider}/authorize",
    dependencies=[Depends(require_roles(*PANEL_ROLES))],
)
async def calendar_authorize(provider: CalendarProviderKind, user: CurrentUser) -> dict[str, Any]:
    """Where to send the browser to sign in. 409 when this deployment has no OAuth client."""
    return calendar_oauth.authorization_url(user, provider)


@router.post(
    "/interviews/calendar/{provider}/callback",
    dependencies=[Depends(require_roles(*PANEL_ROLES))],
)
async def calendar_callback(
    provider: CalendarProviderKind,
    payload: CalendarCallback,
    user: CurrentUser,
    session: DbSession,
) -> dict[str, Any]:
    result = await calendar_oauth.complete_connection(
        session, user, provider, code=payload.code, state=payload.state
    )
    await session.commit()
    return result


@router.post(
    "/interviews/calendar/{provider}/disconnect",
    dependencies=[Depends(require_roles(*PANEL_ROLES))],
)
async def calendar_disconnect(
    provider: CalendarProviderKind, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    result = await calendar_oauth.disconnect(session, user, provider)
    await session.commit()
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Scheduling
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/interviews/propose", dependencies=[Depends(require_roles(*HR_ROLES))])
async def propose_slots(
    payload: ProposeRequest, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Times that work for the whole panel; unread calendars named, never assumed free."""
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


@router.get("/interviews", dependencies=[Depends(require_roles(*MANAGER_ROLES))])
async def list_interviews(
    user: CurrentUser,
    session: DbSession,
    scope: str = Query(default="all", pattern="^(all|mine)$"),
    candidate_id: uuid.UUID | None = None,
    requisition_id: uuid.UUID | None = None,
    interview_status: InterviewStatus | None = Query(default=None, alias="status"),
    upcoming: bool = False,
) -> list[dict[str, Any]]:
    return await interview_service.list_interviews_for(
        session,
        user,
        scope=scope,
        candidate_id=candidate_id,
        requisition_id=requisition_id,
        interview_status=interview_status,
        upcoming_only=upcoming,
    )


@router.get("/interviews/mine", dependencies=[Depends(require_roles(*PANEL_ROLES))])
async def my_interviews(
    user: CurrentUser, session: DbSession, upcoming: bool = False
) -> list[dict[str, Any]]:
    """Interviews you are on the panel of, and nothing else."""
    return await interview_service.list_interviews_for(
        session, user, scope="mine", upcoming_only=upcoming
    )


@router.post("/interviews/bulk-plan", dependencies=[Depends(require_roles(*HR_ROLES))])
async def bulk_plan(payload: BulkPlanRequest, user: CurrentUser) -> dict[str, Any]:
    """Plan a campus or walk-in drive. Returns the grid without booking anything."""
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
    return {"slots_generated": len(slots), **plan.as_dict(payload.timezone_name)}


@router.post(
    "/interviews/reminders/run", dependencies=[Depends(require_roles(*HR_ROLES))]
)
async def run_reminders(
    user: CurrentUser,
    session: DbSession,
    window_minutes: int = Query(default=15, ge=1, le=180),
) -> dict[str, Any]:
    """Raise reminders for interviews starting soon. The scheduler calls the service directly."""
    result = await interview_service.send_due_interview_reminders(
        session, user, window_minutes=window_minutes
    )
    await session.commit()
    return result


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
        "note": "This token is shown once. Send the link to the candidate now.",
    }


@router.get(
    "/interviews/{interview_id}", dependencies=[Depends(require_roles(*PANEL_ROLES))]
)
async def get_interview(
    interview_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    return await interview_service.interview_detail(session, user, interview_id=interview_id)


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
    """Record a panel response — your own, unless HR. A decline reaches the recruiter now."""
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
    request: Request,
) -> dict[str, Any]:
    """Mark completed, or record a candidate or panel no-show."""
    result = await interview_service.record_outcome(
        session,
        user,
        interview_id=interview_id,
        outcome=payload.status,
        note=payload.note,
        request=request,
    )
    await session.commit()
    return result


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
    interview_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Peer feedback is hidden until you submit your own (tenant setting, default on)."""
    return await interview_service.visible_feedback(session, user, interview_id=interview_id)


@router.get(
    "/interviews/{interview_id}/consolidated",
    dependencies=[Depends(require_roles(*MANAGER_ROLES))],
)
async def consolidated(
    interview_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """The panel's view for HR and the hiring team, disagreement intact."""
    return await interview_service.consolidated_view(session, user, interview_id=interview_id)


@router.get(
    "/interviews/{interview_id}/advance-check",
    dependencies=[Depends(require_roles(*MANAGER_ROLES))],
)
async def advance_check(
    interview_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """What stops this candidate moving on. Enforced at the service, too."""
    return await interview_service.advance_check(session, user, interview_id=interview_id)


# ═══════════════════════════════════════════════════════════════════════════
# Public — the candidate's self-scheduling link
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/public/interviews/self-schedule/{token}")
async def view_self_schedule(token: str, request: Request, session: DbSession) -> dict[str, Any]:
    """The offered slots in the candidate's own timezone. No login; the token is the key."""
    public_guard.throttle(request, public_guard.SCHEDULE_READ)
    return await interview_service.view_self_schedule(session, token=token[:128])


@router.post("/public/interviews/self-schedule/{token}")
async def book_self_schedule(
    token: str, payload: SelfScheduleBooking, request: Request, session: DbSession
) -> dict[str, Any]:
    """A candidate books one of the offered slots, and only one of them."""
    public_guard.throttle(request, public_guard.BOOKING)
    result = await interview_service.book_self_schedule(
        session,
        token=token[:128],
        slot_start=payload.slot_start,
        candidate_timezone=payload.candidate_timezone,
    )
    await session.commit()
    return result
