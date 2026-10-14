"""Scheduling, rescheduling, panel responses, feedback and the advance gate.

Four things this service is careful about, each because getting it wrong costs
somebody a day they cannot get back.

**A reschedule patches the existing calendar event.** Never delete-then-create,
never create-and-forget. An orphan event is an interviewer who turns up at the
old time and a candidate who joins a call with nobody in it.

**A decline reaches the recruiter immediately.** The whole point of tracking
panelist responses is that "the interviewer declined three days ago" is
actionable and "the interviewer never showed up" is not. `decline_notified_at`
records that somebody was actually told.

**Feedback blocks advancement, at the service.** A check that lives only in the
screen is not a check, because the API is reachable without it — and unrecorded
verbal feedback is how a hiring decision becomes undefendable.

**A calendar that could not be read is never treated as free.** The proposal
carries the caveat and names whose calendar is unverified.

Who sees what: HR and the requisition's hiring team see every interview for it;
a panellist sees the interviews they are on and nothing else (see
`app.services.hiring.visibility`). Whether peer feedback is hidden until you
submit your own is the tenant's setting, read here — never a query parameter a
panellist could flip.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_filter, tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.hiring import Candidate, InterviewScore, Requisition
from app.models.interview import (
    CalendarConnection,
    Interview,
    InterviewFeedback,
    InterviewMode,
    InterviewPanelist,
    InterviewStage,
    InterviewStatus,
    PanelistRole,
    Recommendation,
    ResponseStatus,
    SelfScheduleLink,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.calendar import registry as calendar_registry
from app.services.calendar.base import (
    CalendarPermanentFailure,
    CalendarProvider,
    CalendarTransientFailure,
    EventSpec,
    Transport,
)
from app.services.hiring import candidate_comms, signals, visibility
from app.services.interviews import availability
from app.services.interviews import feedback as feedback_engine
from app.services.notifications import dispatch

__all__ = [
    "DEFAULT_REMINDER_HOURS",
    "SELF_SCHEDULE_TTL_DAYS",
    "advance_blockers",
    "advance_check",
    "book_self_schedule",
    "cancel_interview",
    "consolidated_view",
    "create_self_schedule_link",
    "create_stage",
    "interview_detail",
    "list_interviews_for",
    "list_stages",
    "panel_people",
    "propose_panel_slots",
    "record_outcome",
    "record_response",
    "reschedule_interview",
    "schedule_interview",
    "send_due_interview_reminders",
    "submit_feedback",
    "update_stage",
    "view_self_schedule",
    "visible_feedback",
]

#: A self-scheduling link that never expires is a link a candidate books
#: three months late against a role that closed.
SELF_SCHEDULE_TTL_DAYS = 14

#: Hours before an interview that panellists are reminded, unless the tenant sets
#: `interview_reminder_hours` in its settings.
DEFAULT_REMINDER_HOURS: tuple[int, ...] = (24, 1)


# ═══════════════════════════════════════════════════════════════════════════
# Loading
# ═══════════════════════════════════════════════════════════════════════════


async def _interview(session: AsyncSession, user: User, interview_id: uuid.UUID) -> Interview:
    row = await session.scalar(
        tenant_scoped(select(Interview).where(Interview.id == interview_id), Interview, user)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview not found")
    return row


async def _stage(session: AsyncSession, user: User, stage_id: uuid.UUID) -> InterviewStage:
    row = await session.scalar(
        tenant_scoped(
            select(InterviewStage).where(InterviewStage.id == stage_id), InterviewStage, user
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Interview stage not found"
        )
    return row


async def _panelists(
    session: AsyncSession, user: User, interview_id: uuid.UUID
) -> list[InterviewPanelist]:
    rows = await session.scalars(
        tenant_scoped(
            select(InterviewPanelist).where(InterviewPanelist.interview_id == interview_id),
            InterviewPanelist,
            user,
        )
    )
    return list(rows.all())


async def _connection(
    session: AsyncSession, user: User, user_id: uuid.UUID
) -> CalendarConnection | None:
    return await session.scalar(
        tenant_scoped(
            select(CalendarConnection).where(
                CalendarConnection.user_id == user_id,
                CalendarConnection.revoked_at.is_(None),
            ),
            CalendarConnection,
            user,
        )
    )


async def _email_for(session: AsyncSession, user_id: uuid.UUID) -> str | None:
    person = await session.get(User, user_id)
    return person.email if person else None


async def _require_panel_members(
    session: AsyncSession, user: User, user_ids: list[uuid.UUID]
) -> None:
    """Every panellist must be an active login in this tenant.

    Ids arrive in the request body. Without this check a panel could name a user
    from another company, who would then be invited to — and able to read — this
    interview.
    """
    wanted = {uid for uid in user_ids if uid is not None}
    if not wanted:
        return
    found = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(User.id).where(User.id.in_(wanted), User.is_active.is_(True)),
                    User,
                    user,
                )
            )
        ).all()
    )
    if found != wanted:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "One or more people on this panel are not active users in this workspace. "
                "Nothing was scheduled."
            ),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Stages
# ═══════════════════════════════════════════════════════════════════════════

_STAGE_FIELDS = (
    "name",
    "sequence",
    "stage_type",
    "duration_minutes",
    "mode",
    "feedback_template_id",
    "required_competencies",
    "is_eliminating",
    "feedback_required",
    "preparation_notes",
)


async def _sequence_taken(
    session: AsyncSession, user: User, requisition_id: uuid.UUID, sequence: int,
    *, excluding: uuid.UUID | None = None,
) -> bool:
    stmt = select(InterviewStage.id).where(
        InterviewStage.requisition_id == requisition_id,
        InterviewStage.sequence == sequence,
    )
    if excluding is not None:
        stmt = stmt.where(InterviewStage.id != excluding)
    return await session.scalar(tenant_scoped(stmt, InterviewStage, user)) is not None


async def create_stage(
    session: AsyncSession, user: User, *, values: dict[str, Any], request: Request | None = None
) -> InterviewStage:
    """Add a round to a requisition's interview process."""
    requisition = await visibility.load_requisition(session, user, values["requisition_id"])
    if await _sequence_taken(session, user, requisition.id, values["sequence"]):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Round {values['sequence']} already exists on {requisition.code}.",
        )
    stage = InterviewStage(
        tenant_id=user.tenant_id,
        requisition_id=requisition.id,
        **{k: values[k] for k in _STAGE_FIELDS if k in values},
    )
    stage.required_competencies = [
        str(c).strip()[:64] for c in (values.get("required_competencies") or []) if str(c).strip()
    ]
    session.add(stage)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="interview_stage",
        entity_id=stage.id,
        summary=f"Interview round {stage.sequence} '{stage.name}' added to {requisition.code}",
        request=request,
    )
    return stage


async def update_stage(
    session: AsyncSession,
    user: User,
    *,
    stage_id: uuid.UUID,
    changes: dict[str, Any],
    request: Request | None = None,
) -> InterviewStage:
    stage = await _stage(session, user, stage_id)
    await visibility.load_requisition(session, user, stage.requisition_id)
    unknown = set(changes) - set(_STAGE_FIELDS)
    if unknown or not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Nothing to change." if not changes else f"Not editable: {sorted(unknown)}",
        )
    if "sequence" in changes and await _sequence_taken(
        session, user, stage.requisition_id, changes["sequence"], excluding=stage.id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Round {changes['sequence']} already exists on this requisition.",
        )
    for field, value in changes.items():
        if value is None and field not in ("feedback_template_id", "preparation_notes"):
            continue
        setattr(stage, field, value)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="interview_stage",
        entity_id=stage.id,
        summary=f"Interview round '{stage.name}' edited: {', '.join(sorted(changes))}",
        request=request,
    )
    return stage


async def list_stages(
    session: AsyncSession, user: User, *, requisition_id: uuid.UUID | None
) -> list[InterviewStage]:
    """The rounds for a requisition. Outside HR, only for the caller's own requisitions."""
    stmt = select(InterviewStage).order_by(InterviewStage.requisition_id, InterviewStage.sequence)
    if requisition_id is not None:
        await visibility.load_requisition(session, user, requisition_id)
        stmt = stmt.where(InterviewStage.requisition_id == requisition_id)
    elif not permissions.is_hr(user):
        stmt = stmt.where(
            InterviewStage.requisition_id.in_(visibility.team_requisition_ids(user))
        )
    rows = await session.scalars(tenant_scoped(stmt, InterviewStage, user))
    return list(rows.all())


async def panel_people(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Active logins who can sit on a panel, for the scheduling form."""
    rows = await session.scalars(
        tenant_scoped(
            select(User).where(User.is_active.is_(True)).order_by(User.full_name),
            User,
            user,
        )
    )
    return [
        {"user_id": str(u.id), "full_name": u.full_name, "email": u.email, "role": u.role.value}
        for u in rows.all()
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Availability
# ═══════════════════════════════════════════════════════════════════════════


async def propose_panel_slots(
    session: AsyncSession,
    user: User,
    *,
    stage_id: uuid.UUID,
    panelist_user_ids: list[uuid.UUID],
    from_day: date,
    days: int = 7,
    transport: Transport | None = None,
    timezone_name: str = "Asia/Kolkata",
    limit: int = 20,
) -> dict:
    """Times that work for the whole panel, with the gaps named.

    A panelist whose calendar could not be read does not veto a slot and does not
    confirm one — they are listed as unverified, and the caveat says so. Treating
    a failed read as "free" books an interview over somebody's existing meeting,
    and nobody finds out until the day.
    """
    stage = await _stage(session, user, stage_id)
    if not panelist_user_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No panelists. There is nothing to find a time for.",
        )
    await _require_panel_members(session, user, panelist_user_ids)

    working = availability.WorkingHours(timezone_name=timezone_name)
    windows = working.windows(from_day=from_day, days=days)
    if not windows:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"No working days in the {days} day(s) from {from_day}.",
        )
    range_start, range_end = windows[0][0], windows[-1][1]

    busy: list[availability.PanelistBusy] = []
    for user_id in panelist_user_ids:
        label = await _email_for(session, user_id) or str(user_id)
        connection = await _connection(session, user, user_id)
        if connection is None:
            busy.append(
                availability.PanelistBusy(
                    panelist_id=str(user_id),
                    read_failed=True,
                    failure_reason="no calendar connected",
                    label=label,
                )
            )
            continue

        try:
            token = calendar_registry.access_token_for(connection, user.tenant_id)
            adapter = calendar_registry.get_adapter(
                CalendarProvider(connection.provider.value), transport
            )
            results = await adapter.free_busy(
                [connection.account_email], start=range_start, end=range_end, token=token
            )
        except (
            CalendarTransientFailure,
            CalendarPermanentFailure,
            calendar_registry.CalendarUnavailable,
        ) as exc:
            connection.last_error = str(exc)
            busy.append(
                availability.PanelistBusy(
                    panelist_id=str(user_id), read_failed=True,
                    failure_reason=str(exc), label=label,
                )
            )
            continue

        result = results[0] if results else None
        if result is None or not result.ok:
            busy.append(
                availability.PanelistBusy(
                    panelist_id=str(user_id), read_failed=True,
                    failure_reason=(result.error if result else "no result returned"),
                    label=label,
                )
            )
            continue

        busy.append(
            availability.PanelistBusy(
                panelist_id=str(user_id),
                intervals=tuple(
                    availability.BusyInterval(start=start, end=end)
                    for start, end in result.busy
                ),
                label=label,
            )
        )

    slots, caveats = availability.propose_slots(
        panelists=busy,
        working_hours=working,
        from_day=from_day,
        days=days,
        duration_minutes=stage.duration_minutes,
        limit=limit,
    )
    await session.flush()
    return {
        "stage_id": str(stage.id),
        "duration_minutes": stage.duration_minutes,
        "timezone": timezone_name,
        "slots": [slot.as_dict(timezone_name) for slot in slots],
        "caveats": caveats,
        "all_calendars_read": not any(p.read_failed for p in busy),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Scheduling
# ═══════════════════════════════════════════════════════════════════════════


def _description(
    stage: InterviewStage, candidate: Candidate, panelist_names: list[str]
) -> str:
    """Joining instructions the candidate can actually use.

    Interviewer names included deliberately: walking into a room without knowing
    who is in it is a needless disadvantage, and the people interviewing are not
    anonymous.
    """
    parts = [
        f"<p>Interview: {stage.name}</p>",
        f"<p>Candidate: {candidate.full_name}</p>",
    ]
    if panelist_names:
        parts.append(f"<p>You will be meeting: {', '.join(panelist_names)}</p>")
    if stage.preparation_notes:
        parts.append(f"<p>{stage.preparation_notes}</p>")
    return "".join(parts)


async def _sync_event(
    session: AsyncSession,
    user: User,
    interview: Interview,
    stage: InterviewStage,
    candidate: Candidate,
    *,
    transport: Transport | None,
    organiser_user_id: uuid.UUID,
) -> list[str]:
    """Create or patch the calendar event. Returns any warnings.

    The `external_event_id` branch is the whole point: a reschedule moves the
    event, so attendees keep their responses and the join link stays valid for
    anyone already holding it.
    """
    connection = await _connection(session, user, organiser_user_id)
    if connection is None:
        return [
            "No calendar is connected for the organiser, so no invitation was sent. "
            "The interview is recorded here; everybody has to be told by hand."
        ]

    panelists = await _panelists(session, user, interview.id)
    attendees: list[str] = []
    names: list[str] = []
    for panelist in panelists:
        person = await session.get(User, panelist.user_id)
        if person is None:
            continue
        attendees.append(person.email)
        employee = (
            await session.get(Employee, person.employee_id) if person.employee_id else None
        )
        names.append(employee.display_name or person.email if employee else person.email)
    if candidate.email:
        attendees.append(candidate.email)

    spec = EventSpec(
        summary=f"{stage.name} - {candidate.full_name}",
        description=_description(stage, candidate, names),
        start=interview.scheduled_start,
        end=interview.scheduled_end,
        attendees=tuple(attendees),
        organiser=connection.account_email,
        location=interview.location_or_link or "",
        with_conference=interview.mode is InterviewMode.VIDEO,
        timezone_name=interview.candidate_timezone,
        correlation_id=str(interview.id),
    )

    try:
        token = calendar_registry.access_token_for(connection, user.tenant_id)
        adapter = calendar_registry.get_adapter(
            CalendarProvider(connection.provider.value), transport
        )
        if interview.external_event_id:
            # Patch, never delete-then-create. Attendees keep their responses and
            # a failure cannot leave the interview with no event at all.
            result = await adapter.update_event(
                interview.external_event_id, spec, token=token
            )
        else:
            result = await adapter.create_event(spec, token=token)
    except (
        CalendarTransientFailure,
        CalendarPermanentFailure,
        calendar_registry.CalendarUnavailable,
    ) as exc:
        connection.last_error = str(exc)
        return [
            f"The calendar invitation could not be sent: {exc}. The interview is "
            "recorded here and is not in anybody's calendar — tell them by hand or "
            "reconnect the calendar and reschedule."
        ]

    interview.external_event_id = result.external_event_id
    interview.calendar_provider = connection.provider
    if result.join_url:
        interview.join_url = result.join_url
        if interview.mode is InterviewMode.VIDEO:
            interview.location_or_link = result.join_url

    warnings = list(result.warnings)
    if result.rejected_attendees:
        warnings.append(
            f"The calendar would not invite: {', '.join(result.rejected_attendees)}. "
            "They will not get the invitation and will not show up."
        )
    return warnings


async def schedule_interview(
    session: AsyncSession,
    user: User,
    *,
    candidate_id: uuid.UUID,
    stage_id: uuid.UUID,
    start: datetime,
    panelist_user_ids: list[uuid.UUID],
    observer_user_ids: list[uuid.UUID] | None = None,
    chair_user_id: uuid.UUID | None = None,
    mode: InterviewMode | None = None,
    location: str | None = None,
    candidate_timezone: str = "Asia/Kolkata",
    transport: Transport | None = None,
    request: Request | None = None,
) -> Interview:
    """Book one interview and put it in everybody's calendar."""
    stage = await _stage(session, user, stage_id)
    candidate = await session.scalar(
        tenant_scoped(select(Candidate).where(Candidate.id == candidate_id), Candidate, user)
    )
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    if candidate.requisition_id != stage.requisition_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "This interview round belongs to a different requisition from the one "
                "the candidate is being considered for."
            ),
        )
    if not panelist_user_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="An interview needs at least one interviewer.",
        )
    await _require_panel_members(
        session, user, [*panelist_user_ids, *(observer_user_ids or [])]
    )

    utc_start = availability.to_utc(start, candidate_timezone)
    interview = Interview(
        tenant_id=user.tenant_id,
        candidate_id=candidate_id,
        stage_id=stage_id,
        scheduled_start=utc_start,
        scheduled_end=utc_start + timedelta(minutes=stage.duration_minutes),
        candidate_timezone=candidate_timezone,
        mode=mode or stage.mode,
        location_or_link=location,
        scheduled_by_user_id=user.id,
    )
    session.add(interview)
    await session.flush()

    for user_id in panelist_user_ids:
        session.add(
            InterviewPanelist(
                tenant_id=user.tenant_id, interview_id=interview.id, user_id=user_id,
                role=PanelistRole.CHAIR if user_id == chair_user_id else PanelistRole.INTERVIEWER,
            )
        )
    for user_id in observer_user_ids or []:
        session.add(
            InterviewPanelist(
                tenant_id=user.tenant_id, interview_id=interview.id, user_id=user_id,
                role=PanelistRole.OBSERVER,
            )
        )
    await session.flush()

    warnings = await _sync_event(
        session, user, interview, stage, candidate,
        transport=transport, organiser_user_id=user.id,
    )
    interview.calendar_warnings = warnings
    await session.flush()

    await candidate_comms.on_interview(
        session, user, event_key=candidate_comms.INTERVIEW_SCHEDULED,
        interview=interview, stage_name=stage.name, candidate=candidate,
    )
    await dispatch.notify(
        session,
        tenant_id=user.tenant_id,
        event_key="interview.scheduled",
        recipient_user_ids=list(
            dict.fromkeys([*panelist_user_ids, *(observer_user_ids or [])])
        ),
        payload={
            "candidate": candidate.full_name,
            "stage": stage.name,
            "when": utc_start.isoformat(),
        },
        commit=False,
    )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="interview",
        entity_id=interview.id,
        summary=(
            f"{stage.name} scheduled for {candidate.full_name} at "
            f"{availability.render_for(utc_start, candidate_timezone):%d %b %H:%M} "
            f"({candidate_timezone})"
            + (f"; {len(warnings)} calendar warning(s)" if warnings else "")
        ),
        request=request,
    )
    return interview


async def reschedule_interview(
    session: AsyncSession,
    user: User,
    *,
    interview_id: uuid.UUID,
    start: datetime,
    reason: str,
    candidate_timezone: str | None = None,
    transport: Transport | None = None,
    request: Request | None = None,
) -> dict:
    """Move an interview, updating the existing calendar event.

    The event id is kept and patched. Creating a second event would leave the
    first one live — an interviewer turning up at the old time and a candidate
    joining a call with nobody in it.
    """
    interview = await _interview(session, user, interview_id)
    if interview.status in (
        InterviewStatus.COMPLETED,
        InterviewStatus.CANCELLED,
        InterviewStatus.CANDIDATE_NO_SHOW,
        InterviewStatus.PANEL_NO_SHOW,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This interview is {interview.status.value}. Rescheduling one that has "
                "already happened is a new interview, not a move."
            ),
        )
    if not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "A reschedule needs a reason. Repeated reschedules are a real signal — "
                "about a panel that is too busy, or a candidate who is no longer "
                "interested — and it only exists if somebody wrote it down."
            ),
        )

    stage = await _stage(session, user, interview.stage_id)
    candidate = await session.get(Candidate, interview.candidate_id)
    previous_start = interview.scheduled_start
    previous_event_id = interview.external_event_id

    zone = candidate_timezone or interview.candidate_timezone
    utc_start = availability.to_utc(start, zone)
    interview.scheduled_start = utc_start
    interview.scheduled_end = utc_start + timedelta(minutes=stage.duration_minutes)
    interview.candidate_timezone = zone
    interview.status = InterviewStatus.RESCHEDULED
    interview.reschedule_count += 1

    # Responses reset: an acceptance was for the old time.
    for panelist in await _panelists(session, user, interview.id):
        panelist.response_status = ResponseStatus.NEEDS_ACTION
        panelist.responded_at = None
        panelist.decline_notified_at = None

    warnings = await _sync_event(
        session, user, interview, stage, candidate,
        transport=transport, organiser_user_id=user.id,
    )
    interview.calendar_warnings = warnings
    await session.flush()

    if previous_event_id and interview.external_event_id != previous_event_id:
        # Should be impossible — `_sync_event` patches when an id exists. Caught
        # rather than trusted, because the cost is an orphan event nobody
        # deletes.
        warnings.append(
            f"The calendar event id changed from {previous_event_id} to "
            f"{interview.external_event_id}. The old event may still be live — check "
            "the organiser's calendar."
        )
        interview.calendar_warnings = warnings
        await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="interview",
        entity_id=interview.id,
        summary=(
            f"Interview moved from {previous_start:%d %b %H:%M} to "
            f"{interview.scheduled_start:%d %b %H:%M} UTC (reschedule "
            f"{interview.reschedule_count}): {reason}"
        ),
        request=request,
    )
    if candidate is not None:
        await candidate_comms.on_interview(
            session, user, event_key=candidate_comms.INTERVIEW_RESCHEDULED,
            interview=interview, stage_name=stage.name, candidate=candidate,
        )
    return {
        "interview_id": str(interview.id),
        "scheduled_start": interview.scheduled_start.isoformat(),
        "reschedule_count": interview.reschedule_count,
        "external_event_id": interview.external_event_id,
        "event_was_moved_not_duplicated": interview.external_event_id == previous_event_id
        or previous_event_id is None,
        "warnings": warnings,
    }


async def cancel_interview(
    session: AsyncSession,
    user: User,
    *,
    interview_id: uuid.UUID,
    reason: str,
    transport: Transport | None = None,
) -> dict:
    interview = await _interview(session, user, interview_id)
    if not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A cancellation needs a reason.",
        )

    warnings: list[str] = []
    if interview.external_event_id and interview.calendar_provider:
        connection = await _connection(session, user, interview.scheduled_by_user_id or user.id)
        if connection is not None:
            try:
                token = calendar_registry.access_token_for(connection, user.tenant_id)
                adapter = calendar_registry.get_adapter(
                    CalendarProvider(connection.provider.value), transport
                )
                await adapter.cancel_event(interview.external_event_id, token=token)
            except (
                CalendarTransientFailure,
                CalendarPermanentFailure,
                calendar_registry.CalendarUnavailable,
            ) as exc:
                warnings.append(
                    f"The calendar event could not be cancelled: {exc}. It is still in "
                    "everybody's calendar — remove it by hand, or people will turn up."
                )

    interview.status = InterviewStatus.CANCELLED
    interview.cancelled_reason = reason
    interview.calendar_warnings = warnings
    await session.flush()
    cancelled_for = await session.scalar(
        tenant_scoped(
            select(Candidate).where(Candidate.id == interview.candidate_id), Candidate, user
        )
    )
    if cancelled_for is not None:
        cancelled_stage = await _stage(session, user, interview.stage_id)
        await candidate_comms.on_interview(
            session, user, event_key=candidate_comms.INTERVIEW_CANCELLED,
            interview=interview, stage_name=cancelled_stage.name, candidate=cancelled_for,
        )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="interview",
        entity_id=interview.id,
        summary=f"Interview cancelled: {reason}",
    )
    return {"interview_id": str(interview.id), "status": interview.status.value,
            "warnings": warnings}


_OUTCOMES = frozenset(
    {InterviewStatus.COMPLETED, InterviewStatus.CANDIDATE_NO_SHOW, InterviewStatus.PANEL_NO_SHOW}
)


async def record_outcome(
    session: AsyncSession,
    user: User,
    *,
    interview_id: uuid.UUID,
    outcome: InterviewStatus,
    note: str | None = None,
    request: Request | None = None,
) -> dict:
    """Mark an interview completed, or record which side did not show.

    Candidate and panel no-shows are separate statuses: one is a sourcing signal
    and one is an internal one, and a single NO_SHOW answers neither question.
    """
    interview = await _interview(session, user, interview_id)
    if outcome not in _OUTCOMES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "An outcome is completed, candidate no-show or panel no-show. Use cancel "
                "or reschedule for anything else."
            ),
        )
    if interview.status is InterviewStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This interview was cancelled; it has no outcome to record.",
        )
    if interview.scheduled_start > datetime.now(UTC) + timedelta(minutes=15):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This interview has not started yet, so its outcome cannot be recorded.",
        )

    previous = interview.status
    interview.status = outcome
    if outcome is InterviewStatus.COMPLETED:
        interview.completed_at = datetime.now(UTC)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="interview",
        entity_id=interview.id,
        summary=(
            f"Interview outcome recorded: {previous.value} -> {outcome.value}"
            + (f" ({note.strip()})" if note and note.strip() else "")
        ),
        request=request,
    )
    return {"interview_id": str(interview.id), "status": interview.status.value}


# ═══════════════════════════════════════════════════════════════════════════
# Reading
# ═══════════════════════════════════════════════════════════════════════════


async def _names_for(
    session: AsyncSession, user: User, ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, str]]:
    if not ids:
        return {}
    rows = await session.execute(
        tenant_scoped(
            select(User.id, User.full_name, User.email).where(User.id.in_(ids)), User, user
        )
    )
    return {row.id: (row.full_name, row.email) for row in rows}


async def list_interviews_for(
    session: AsyncSession,
    user: User,
    *,
    scope: str = "all",
    candidate_id: uuid.UUID | None = None,
    requisition_id: uuid.UUID | None = None,
    interview_status: InterviewStatus | None = None,
    upcoming_only: bool = False,
) -> list[dict[str, Any]]:
    """Interviews this caller may see.

    `mine` is the interviews the caller is on the panel of. `all` is, for HR,
    everything; for a manager, their panels plus their own requisitions; for
    anybody else, their panels — the same as `mine`.
    """
    on_my_panel = tenant_scoped(
        select(InterviewPanelist.interview_id).where(InterviewPanelist.user_id == user.id),
        InterviewPanelist,
        user,
    )
    stmt = tenant_scoped(
        select(Interview, Candidate, InterviewStage, Requisition)
        .join(Candidate, Candidate.id == Interview.candidate_id)
        .join(InterviewStage, InterviewStage.id == Interview.stage_id)
        .join(Requisition, Requisition.id == Candidate.requisition_id),
        Interview,
        user,
    )
    if scope == "mine" or (not permissions.is_hr(user) and not user.has_reports):
        stmt = stmt.where(Interview.id.in_(on_my_panel))
    elif not permissions.is_hr(user):
        stmt = stmt.where(
            or_(
                Interview.id.in_(on_my_panel),
                Candidate.requisition_id.in_(visibility.team_requisition_ids(user)),
            )
        )
    if candidate_id is not None:
        stmt = stmt.where(Interview.candidate_id == candidate_id)
    if requisition_id is not None:
        stmt = stmt.where(Candidate.requisition_id == requisition_id)
    if interview_status is not None:
        stmt = stmt.where(Interview.status == interview_status)
    if upcoming_only:
        stmt = stmt.where(Interview.scheduled_end >= datetime.now(UTC))

    rows = (await session.execute(stmt.order_by(Interview.scheduled_start).limit(500))).all()
    interview_ids = [interview.id for interview, *_ in rows]
    panel_rows: list[InterviewPanelist] = []
    if interview_ids:
        panel_rows = list(
            (
                await session.scalars(
                    tenant_scoped(
                        select(InterviewPanelist).where(
                            InterviewPanelist.interview_id.in_(interview_ids)
                        ),
                        InterviewPanelist,
                        user,
                    )
                )
            ).all()
        )
    names = await _names_for(session, user, {p.user_id for p in panel_rows})
    by_interview: dict[uuid.UUID, list[InterviewPanelist]] = {}
    for panelist in panel_rows:
        by_interview.setdefault(panelist.interview_id, []).append(panelist)

    out = []
    for interview, candidate, stage, requisition in rows:
        panel = by_interview.get(interview.id, [])
        mine = next((p for p in panel if p.user_id == user.id), None)
        oversees = permissions.is_hr(user) or visibility.is_hiring_team(user, requisition)
        out.append(
            {
                "id": str(interview.id),
                "candidate_id": str(candidate.id),
                "candidate_name": candidate.full_name,
                "requisition_id": str(requisition.id),
                "requisition_code": requisition.code,
                "requisition_title": requisition.title,
                "stage_id": str(stage.id),
                "stage_name": stage.name,
                "scheduled_start": interview.scheduled_start.isoformat(),
                "scheduled_end": interview.scheduled_end.isoformat(),
                "candidate_timezone": interview.candidate_timezone,
                "mode": interview.mode.value,
                "location_or_link": interview.location_or_link,
                "status": interview.status.value,
                "reschedule_count": interview.reschedule_count,
                "calendar_warnings": list(interview.calendar_warnings or []) if oversees else [],
                "panel": [
                    {
                        "user_id": str(p.user_id),
                        "name": names.get(p.user_id, ("", ""))[0],
                        "role": p.role.value,
                        "response": p.response_status.value,
                        "feedback_submitted": p.feedback_submitted_at is not None,
                    }
                    for p in panel
                ],
                "my_role": mine.role.value if mine else None,
                "my_response": mine.response_status.value if mine else None,
                "my_feedback_submitted": bool(mine and mine.feedback_submitted_at),
                "can_manage": permissions.is_hr(user),
                "oversees": oversees,
            }
        )
    return out


async def interview_detail(
    session: AsyncSession, user: User, *, interview_id: uuid.UUID
) -> dict[str, Any]:
    """One interview for someone on its panel, its hiring team or HR."""
    interview, mine, requisition, oversees = await visibility.load_interview(
        session, user, interview_id
    )
    stage = await _stage(session, user, interview.stage_id)
    candidate = await session.get(Candidate, interview.candidate_id)
    panel = await _panelists(session, user, interview.id)
    names = await _names_for(session, user, {p.user_id for p in panel})
    return {
        "id": str(interview.id),
        "candidate_id": str(interview.candidate_id),
        "candidate_name": candidate.full_name if candidate else "",
        "requisition_code": requisition.code if requisition else None,
        "requisition_title": requisition.title if requisition else None,
        "stage": {
            "id": str(stage.id),
            "name": stage.name,
            "stage_type": stage.stage_type.value,
            "duration_minutes": stage.duration_minutes,
            "required_competencies": list(stage.required_competencies or []),
            "feedback_required": stage.feedback_required,
            "preparation_notes": stage.preparation_notes,
        },
        "scheduled_start": interview.scheduled_start.isoformat(),
        "scheduled_end": interview.scheduled_end.isoformat(),
        "candidate_timezone": interview.candidate_timezone,
        "mode": interview.mode.value,
        "location_or_link": interview.location_or_link,
        "status": interview.status.value,
        "reschedule_count": interview.reschedule_count,
        "calendar_warnings": list(interview.calendar_warnings or []) if oversees else [],
        "panel": [
            {
                "user_id": str(p.user_id),
                "name": names.get(p.user_id, ("", ""))[0],
                "email": names.get(p.user_id, ("", ""))[1] if oversees else None,
                "role": p.role.value,
                "response": p.response_status.value,
                "feedback_submitted": p.feedback_submitted_at is not None,
            }
            for p in panel
        ],
        "my_role": mine.role.value if mine else None,
        "my_feedback_submitted": bool(mine and mine.feedback_submitted_at),
        "can_manage": permissions.is_hr(user),
        "oversees": oversees,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Panel responses
# ═══════════════════════════════════════════════════════════════════════════


async def record_response(
    session: AsyncSession,
    user: User,
    *,
    interview_id: uuid.UUID,
    panelist_user_id: uuid.UUID,
    response: ResponseStatus,
    now: datetime | None = None,
) -> dict:
    """Record a panelist's response, and tell the recruiter about a decline now.

    Immediately, not on a digest. "The interviewer declined three days ago" is
    something a recruiter can act on; "the interviewer never showed up" is not,
    and the gap between those two sentences is the entire value of this.

    Outside HR, a person records only their own response.
    """
    moment = now or datetime.now(UTC)
    if not permissions.is_hr(user) and panelist_user_id != user.id:
        raise permissions.forbidden(
            "You can only record your own response to an interview invitation."
        )
    interview = await _interview(session, user, interview_id)

    panelist = await session.scalar(
        tenant_scoped(
            select(InterviewPanelist).where(
                InterviewPanelist.interview_id == interview_id,
                InterviewPanelist.user_id == panelist_user_id,
            ),
            InterviewPanelist,
            user,
        )
    )
    if panelist is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That person is not on this interview's panel.",
        )

    panelist.response_status = response
    panelist.responded_at = moment
    await session.flush()

    notified = False
    if response is ResponseStatus.DECLINED:
        recipients = [
            uid
            for uid in (interview.scheduled_by_user_id,)
            if uid is not None
        ]
        person = await session.get(User, panelist_user_id)
        candidate = await session.get(Candidate, interview.candidate_id)
        if recipients:
            await dispatch.notify(
                session,
                tenant_id=user.tenant_id,
                event_key="interview.panelist_declined",
                recipient_user_ids=recipients,
                payload={
                    "interviewer": person.email if person else str(panelist_user_id),
                    "candidate": candidate.full_name if candidate else "",
                    "when": interview.scheduled_start.isoformat(),
                },
                commit=False,
            )
        # Recorded whether or not a channel was available. The point is that the
        # decline is not sitting unnoticed; a missing notification template must
        # not make it look unhandled forever.
        panelist.decline_notified_at = moment
        notified = True
        await session.flush()

        await audit.record(
            session,
            tenant_id=user.tenant_id,
            actor=user,
            action=AuditAction.UPDATE,
            entity_type="interview",
            entity_id=interview.id,
            summary=(
                f"{person.email if person else panelist_user_id} declined this "
                f"interview. The recruiter has been told."
            ),
        )

    return {
        "interview_id": str(interview.id),
        "response": response.value,
        "recruiter_notified": notified,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Feedback
# ═══════════════════════════════════════════════════════════════════════════


async def _panel_feedback(
    session: AsyncSession, user: User, interview: Interview
) -> tuple[list[feedback_engine.PanelFeedback], list[tuple[str, str, bool]]]:
    panelists = await _panelists(session, user, interview.id)
    rows = await session.scalars(
        tenant_scoped(
            select(InterviewFeedback).where(InterviewFeedback.interview_id == interview.id),
            InterviewFeedback,
            user,
        )
    )
    by_panelist = {str(row.panelist_id): row for row in rows.all()}

    labels: dict[str, str] = {}
    for panelist in panelists:
        person = await session.get(User, panelist.user_id)
        labels[str(panelist.id)] = person.email if person else str(panelist.user_id)

    entries: list[feedback_engine.PanelFeedback] = []
    for panelist in panelists:
        row = by_panelist.get(str(panelist.id))
        entries.append(
            feedback_engine.PanelFeedback(
                panelist_id=str(panelist.id),
                interview_id=str(interview.id),
                recommendation=(
                    feedback_engine.Recommendation(row.recommendation.value)
                    if row
                    else feedback_engine.Recommendation.NO
                ),
                ratings=dict(row.ratings) if row else {},
                strengths=row.strengths or "" if row else "",
                concerns=row.concerns or "" if row else "",
                submitted_at=row.submitted_at if row else None,
                panelist_label=labels[str(panelist.id)],
                is_chair=panelist.role is PanelistRole.CHAIR,
            )
        )

    roster = [
        (
            str(p.id),
            labels[str(p.id)],
            # Observers are not required. An observer who had to file a review
            # would either stop observing or start filing empty ones.
            p.role is not PanelistRole.OBSERVER,
        )
        for p in panelists
    ]
    return entries, roster


async def submit_feedback(
    session: AsyncSession,
    user: User,
    *,
    interview_id: uuid.UUID,
    recommendation: Recommendation,
    ratings: dict[str, str],
    strengths: str = "",
    concerns: str = "",
    now: datetime | None = None,
    request: Request | None = None,
) -> dict:
    """Record one panelist's feedback, once."""
    from decimal import Decimal, InvalidOperation

    moment = now or datetime.now(UTC)
    interview = await _interview(session, user, interview_id)
    stage = await _stage(session, user, interview.stage_id)

    panelist = await session.scalar(
        tenant_scoped(
            select(InterviewPanelist).where(
                InterviewPanelist.interview_id == interview_id,
                InterviewPanelist.user_id == user.id,
            ),
            InterviewPanelist,
            user,
        )
    )
    if panelist is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not on this interview's panel.",
        )
    if interview.status in (InterviewStatus.CANCELLED, InterviewStatus.CANDIDATE_NO_SHOW):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This interview is {interview.status.value.replace('_', ' ')}; there is "
                "nothing to give feedback on."
            ),
        )

    existing = await session.scalar(
        tenant_scoped(
            select(InterviewFeedback).where(
                InterviewFeedback.interview_id == interview_id,
                InterviewFeedback.panelist_id == panelist.id,
            ),
            InterviewFeedback,
            user,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "You have already submitted feedback for this interview. A second "
                "submission would replace the first, and the first is what you thought "
                "before you knew what anyone else said."
            ),
        )

    try:
        decimal_ratings = {code: Decimal(str(value)) for code, value in ratings.items()}
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Every rating has to be a number.",
        ) from exc
    try:
        feedback_engine.validate_feedback(
            recommendation=feedback_engine.Recommendation(recommendation.value),
            ratings=decimal_ratings,
            required_competencies=list(stage.required_competencies or []),
            concerns=concerns,
        )
    except feedback_engine.FeedbackError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    row = InterviewFeedback(
        tenant_id=user.tenant_id,
        interview_id=interview_id,
        panelist_id=panelist.id,
        recommendation=recommendation,
        ratings={code: str(value) for code, value in decimal_ratings.items()},
        strengths=strengths or None,
        concerns=concerns or None,
        submitted_at=moment,
    )
    session.add(row)
    panelist.feedback_submitted_at = moment
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="interview_feedback",
        entity_id=row.id,
        summary=f"Feedback submitted for interview {interview.id}: {recommendation.value}",
        request=request,
    )
    return {"feedback_id": str(row.id), "submitted_at": moment.isoformat()}


async def _anchoring_protection(session: AsyncSession, user: User) -> bool:
    """The tenant's switch. On unless the tenant has deliberately turned it off."""
    tenant = await session.get(Tenant, user.tenant_id)
    configured = ((tenant.settings or {}) if tenant else {}).get(
        "interview_anchoring_protection", True
    )
    return configured is not False


async def visible_feedback(
    session: AsyncSession,
    user: User,
    *,
    interview_id: uuid.UUID,
    hide_until_submitted: bool | None = None,
    viewer_is_hiring_manager: bool | None = None,
) -> dict:
    """What this viewer may read right now.

    A panelist who has not submitted sees only their own. Reading "STRONG_YES,
    brilliant" before writing your own review measurably changes what you write,
    and four opinions that anchored on the first are one opinion.

    Somebody neither on the panel nor on the hiring team gets a 404.
    `hide_until_submitted` defaults to the tenant's setting; a caller passes it
    only from configuration, never from a request.
    """
    interview, _, _, oversees = await visibility.load_interview(session, user, interview_id)
    if hide_until_submitted is None:
        hide_until_submitted = await _anchoring_protection(session, user)
    if viewer_is_hiring_manager is None:
        viewer_is_hiring_manager = oversees
    entries, roster = await _panel_feedback(session, user, interview)

    panelist = await session.scalar(
        tenant_scoped(
            select(InterviewPanelist).where(
                InterviewPanelist.interview_id == interview_id,
                InterviewPanelist.user_id == user.id,
            ),
            InterviewPanelist,
            user,
        )
    )
    viewer_id = str(panelist.id) if panelist else ""

    visible = feedback_engine.visible_feedback_for(
        viewer_panelist_id=viewer_id,
        all_feedback=[e for e in entries if e.is_submitted],
        hide_until_submitted=hide_until_submitted,
        viewer_is_hiring_manager=viewer_is_hiring_manager,
    )
    withheld = sum(1 for e in entries if e.is_submitted) - len(visible)

    return {
        "interview_id": str(interview.id),
        "feedback": [
            {
                "panelist": e.panelist_label,
                "recommendation": e.recommendation.value,
                "ratings": {k: str(v) for k, v in e.ratings.items()},
                "strengths": e.strengths,
                "concerns": e.concerns,
                "submitted_at": e.submitted_at.isoformat() if e.submitted_at else None,
                "is_mine": e.panelist_id == viewer_id,
            }
            for e in visible
        ],
        "withheld_count": withheld,
        "withheld_note": (
            "Other panelists' feedback is hidden until you submit your own. Reading it "
            "first measurably changes what you write."
            if withheld
            else ""
        ),
        "outstanding": feedback_engine.missing_feedback(panelists=roster, feedback=entries),
        "anchoring_protection": hide_until_submitted,
    }


async def consolidated_view(
    session: AsyncSession, user: User, *, interview_id: uuid.UUID
) -> dict:
    """The hiring manager's view, once feedback is in.

    AI round scores sit beside the human panel rather than being blended into it,
    and only with their contributing signals and a confidence — FS-ANL-02. A
    bare Fit Score is never returned.
    """
    interview, _, _, oversees = await visibility.load_interview(session, user, interview_id)
    if not oversees:
        raise visibility.not_found("Interview")
    entries, roster = await _panel_feedback(session, user, interview)
    consolidation = feedback_engine.consolidate(feedback=entries, panelists=roster)

    candidate = await session.get(Candidate, interview.candidate_id)
    stage = await _stage(session, user, interview.stage_id)
    score_rows = (
        await session.scalars(
            tenant_scoped(
                select(InterviewScore)
                .where(
                    InterviewScore.candidate_id == interview.candidate_id,
                    InterviewScore.is_ai_generated.is_(True),
                )
                .order_by(InterviewScore.round_number),
                InterviewScore,
                user,
            )
        )
    ).all()
    presented = [
        signals.present_score(
            {
                "id": s.id,
                "round_type": s.round_type,
                "round_number": s.round_number,
                "score": s.score,
                "max_score": s.max_score,
                "confidence": s.confidence,
                "is_ai_generated": s.is_ai_generated,
                "summary": s.summary,
                "signals": s.signals,
                "proctoring_notes": s.proctoring_notes,
            }
        )
        for s in score_rows
    ]
    return {
        "interview_id": str(interview.id),
        "stage": stage.name,
        "candidate": candidate.full_name if candidate else "",
        "ai_rounds": presented,
        "fit_score": signals.fit_score_view(
            fit_score=candidate.fit_score if candidate else None,
            presented_scores=presented,
        ),
        **consolidation.as_dict(),
    }


async def advance_blockers(
    session: AsyncSession, user: User, *, interview_id: uuid.UUID
) -> list[str]:
    """What stops this candidate moving past this stage. Empty means nothing.

    Enforced at the service rather than in the screen, because the API is
    reachable without the screen — and unrecorded verbal feedback is how a
    hiring decision becomes undefendable six months later.
    """
    interview = await _interview(session, user, interview_id)
    stage = await _stage(session, user, interview.stage_id)
    blockers: list[str] = []

    if interview.status in (InterviewStatus.SCHEDULED, InterviewStatus.RESCHEDULED):
        blockers.append(
            f"This interview is still {interview.status.value}. Mark it completed, or "
            "record a no-show, before moving the candidate on."
        )

    if stage.feedback_required and interview.status is InterviewStatus.COMPLETED:
        entries, roster = await _panel_feedback(session, user, interview)
        outstanding = feedback_engine.missing_feedback(panelists=roster, feedback=entries)
        if outstanding:
            blockers.append(
                f"Feedback is outstanding from: {', '.join(outstanding)}. Advancing on "
                "unrecorded verbal feedback is how a hiring decision becomes impossible "
                "to defend."
            )
    return blockers


async def advance_check(
    session: AsyncSession, user: User, *, interview_id: uuid.UUID
) -> dict:
    """The advance gate, for somebody allowed to oversee this interview."""
    _, _, _, oversees = await visibility.load_interview(session, user, interview_id)
    if not oversees:
        raise visibility.not_found("Interview")
    blockers = await advance_blockers(session, user, interview_id=interview_id)
    return {"can_advance": not blockers, "blockers": blockers}


# ═══════════════════════════════════════════════════════════════════════════
# Reminders
# ═══════════════════════════════════════════════════════════════════════════


async def send_due_interview_reminders(
    session: AsyncSession,
    user: User,
    *,
    now: datetime | None = None,
    window_minutes: int = 15,
) -> dict:
    """Remind panellists of interviews starting soon. Scheduler-callable.

    Run every `window_minutes`. For each configured lead time (the tenant's
    `interview_reminder_hours`, default 24 and 1), an interview whose start falls
    in `(now + lead - window, now + lead]` gets one reminder per panellist who
    has not declined. Stateless on purpose: nothing is written to the interview,
    so a run that overlaps the previous window can send a second reminder — run
    it on a fixed cadence equal to the window.

    Candidates are not reminded here: they have no login, and the notification
    engine addresses users. That is reported in the result rather than skipped
    silently.
    """
    moment = now or datetime.now(UTC)
    tenant = await session.get(Tenant, user.tenant_id)
    configured = ((tenant.settings or {}) if tenant else {}).get("interview_reminder_hours")
    leads = (
        tuple(int(h) for h in configured if isinstance(h, int | float) and 0 < h <= 168)
        if isinstance(configured, list)
        else DEFAULT_REMINDER_HOURS
    ) or DEFAULT_REMINDER_HOURS
    window = timedelta(minutes=max(1, window_minutes))

    sent = 0
    reminded_interviews: list[str] = []
    for lead in leads:
        target = moment + timedelta(hours=lead)
        due = (
            await session.execute(
                tenant_scoped(
                    select(Interview, Candidate, InterviewStage)
                    .join(Candidate, Candidate.id == Interview.candidate_id)
                    .join(InterviewStage, InterviewStage.id == Interview.stage_id)
                    .where(
                        Interview.status.in_(
                            (InterviewStatus.SCHEDULED, InterviewStatus.RESCHEDULED)
                        ),
                        Interview.scheduled_start > target - window,
                        Interview.scheduled_start <= target,
                    ),
                    Interview,
                    user,
                )
            )
        ).all()
        for interview, candidate, stage in due:
            recipients = [
                p.user_id
                for p in await _panelists(session, user, interview.id)
                if p.response_status is not ResponseStatus.DECLINED
            ]
            if not recipients:
                continue
            await dispatch.notify(
                session,
                tenant_id=user.tenant_id,
                event_key="interview.reminder",
                recipient_user_ids=recipients,
                payload={
                    "candidate": candidate.full_name,
                    "stage": stage.name,
                    "when": interview.scheduled_start.isoformat(),
                    "hours_before": lead,
                    "join": interview.location_or_link or "",
                },
                commit=False,
            )
            sent += len(recipients)
            reminded_interviews.append(str(interview.id))

    if reminded_interviews:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            actor=user,
            action=AuditAction.UPDATE,
            entity_type="interview",
            entity_id=None,
            summary=(
                f"Interview reminders raised for {len(reminded_interviews)} interview(s), "
                f"{sent} panellist notification(s)"
            ),
        )
    return {
        "as_of": moment.isoformat(),
        "lead_hours": list(leads),
        "interviews_reminded": len(reminded_interviews),
        "notifications_raised": sent,
        "candidate_reminders": (
            "Not sent. Candidates have no login and the notification engine only "
            "addresses users; the candidate portal shows their schedule instead."
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Candidate self-scheduling
# ═══════════════════════════════════════════════════════════════════════════


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_self_schedule_link(
    session: AsyncSession,
    user: User,
    *,
    candidate_id: uuid.UUID,
    stage_id: uuid.UUID,
    slots: list[dict],
    candidate_timezone: str = "Asia/Kolkata",
    now: datetime | None = None,
) -> tuple[SelfScheduleLink, str]:
    """Offer a candidate a set of recruiter-approved slots.

    This removes the back-and-forth email thread, which is where most recruiter
    scheduling time actually goes. The slots are snapshotted so the candidate is
    offered exactly what was approved, even if a calendar changes underneath.
    """
    moment = now or datetime.now(UTC)
    stage = await _stage(session, user, stage_id)
    if not slots:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "No slots to offer. A self-scheduling link with nothing in it is worse "
                "than no link — the candidate opens it and finds an empty page."
            ),
        )
    candidate = await session.scalar(
        tenant_scoped(select(Candidate).where(Candidate.id == candidate_id), Candidate, user)
    )
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    if candidate.requisition_id != stage.requisition_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This interview round belongs to a different requisition.",
        )

    cleaned: list[dict] = []
    panel_ids: list[uuid.UUID] = []
    for slot in slots:
        try:
            start = datetime.fromisoformat(str(slot["start"]))
            end = datetime.fromisoformat(str(slot.get("end") or ""))
            ids = [uuid.UUID(str(uid)) for uid in slot.get("panelist_user_ids", [])]
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Each slot needs an ISO start and end, and valid panellist ids.",
            ) from exc
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Each slot needs a start and end with a timezone, and the end after "
                    "the start. A time without a zone is an interview at the wrong hour "
                    "for somebody."
                ),
            )
        if not ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Each slot needs at least one interviewer.",
            )
        panel_ids.extend(ids)
        cleaned.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "panelist_user_ids": [str(uid) for uid in ids],
            }
        )
    await _require_panel_members(session, user, panel_ids)

    token = secrets.token_urlsafe(32)
    link = SelfScheduleLink(
        tenant_id=user.tenant_id,
        candidate_id=candidate_id,
        stage_id=stage_id,
        token_hash=_hash(token),
        offered_slots=cleaned,
        candidate_timezone=candidate_timezone,
        expires_at=moment + timedelta(days=SELF_SCHEDULE_TTL_DAYS),
    )
    session.add(link)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="self_schedule_link",
        entity_id=link.id,
        summary=f"Self-scheduling link issued for '{stage.name}' with {len(cleaned)} slot(s)",
    )
    await candidate_comms.on_self_schedule_link(
        session, user, candidate=candidate, stage_name=stage.name, token=token,
        expires_at=link.expires_at, timezone_name=candidate_timezone,
    )
    return link, token


async def view_self_schedule(
    session: AsyncSession, *, token: str, now: datetime | None = None
) -> dict:
    """What a candidate sees when they open a scheduling link.

    Not tenant-scoped: a candidate has no tenant context and the token carries
    it. Shows the slots in their own zone, the round's name and length, and the
    organisation — never who else is being interviewed or the interviewers' ids.
    """
    moment = now or datetime.now(UTC)
    link = await session.scalar(
        select(SelfScheduleLink).where(SelfScheduleLink.token_hash == _hash(token))
    )
    if link is None or link.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="This link is no longer valid."
        )
    stage = await session.scalar(
        select(InterviewStage).where(
            InterviewStage.id == link.stage_id, tenant_filter(InterviewStage, link.tenant_id)
        )
    )
    tenant = await session.get(Tenant, link.tenant_id)
    booked = None
    if link.booked_interview_id is not None:
        interview = await session.scalar(
            select(Interview).where(
                Interview.id == link.booked_interview_id,
                tenant_filter(Interview, link.tenant_id),
            )
        )
        if interview is not None:
            booked = {
                "start_utc": interview.scheduled_start.isoformat(),
                "start_local": availability.render_for(
                    interview.scheduled_start, interview.candidate_timezone
                ).isoformat(),
                "timezone": interview.candidate_timezone,
                "mode": interview.mode.value,
                "join_url": interview.join_url,
                "status": interview.status.value,
            }

    zone = link.candidate_timezone
    return {
        "organisation": tenant.name if tenant else "",
        "stage_name": stage.name if stage else "Interview",
        "duration_minutes": stage.duration_minutes if stage else None,
        "mode": stage.mode.value if stage else None,
        "preparation_notes": stage.preparation_notes if stage else None,
        "timezone": zone,
        "expires_at": link.expires_at.isoformat(),
        "expired": link.expires_at < moment,
        "booked": booked,
        "slots": [
            {
                "start": slot["start"],
                "start_local": availability.render_for(
                    datetime.fromisoformat(slot["start"]).astimezone(UTC), zone
                ).isoformat(),
                "end_local": availability.render_for(
                    datetime.fromisoformat(slot["end"]).astimezone(UTC), zone
                ).isoformat()
                if slot.get("end")
                else None,
                "in_past": datetime.fromisoformat(slot["start"]).astimezone(UTC) <= moment,
            }
            for slot in link.offered_slots
        ],
    }


async def book_self_schedule(
    session: AsyncSession,
    *,
    token: str,
    slot_start: datetime,
    candidate_timezone: str | None = None,
    transport: Transport | None = None,
    now: datetime | None = None,
) -> dict:
    """A candidate picks one of the offered slots.

    Not tenant-scoped: a candidate has no tenant context, and the token carries
    it. The chosen slot must be one that was actually offered — accepting an
    arbitrary time would let anyone with the link book any hour of any day.
    """
    moment = now or datetime.now(UTC)
    link = await session.scalar(
        select(SelfScheduleLink).where(SelfScheduleLink.token_hash == _hash(token))
    )
    if link is None or link.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="This link is no longer valid."
        )
    if link.booked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This link has already been used to book an interview. Ask the "
                "recruiter to move it rather than booking a second."
            ),
        )
    if link.expires_at < moment:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=(
                f"These slots expired on {link.expires_at.date()}. Ask the recruiter for "
                "fresh times — the interviewers' calendars have moved on."
            ),
        )

    zone = candidate_timezone or link.candidate_timezone
    chosen = availability.to_utc(slot_start, zone)
    offered = {
        datetime.fromisoformat(slot["start"]).astimezone(UTC)
        for slot in link.offered_slots
    }
    if chosen not in offered:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "That time was not one of the offered slots. Only the times the "
                "recruiter approved can be booked — otherwise this link could book any "
                "hour of any day."
            ),
        )
    if chosen <= moment:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That time has already passed. Pick one of the later slots.",
        )

    matching = next(
        slot
        for slot in link.offered_slots
        if datetime.fromisoformat(slot["start"]).astimezone(UTC) == chosen
    )

    class _Actor:
        """Tenant carried by the token, not by anything the caller sent."""

        __slots__ = ("employee_id", "id", "is_active", "role", "tenant_id")

        def __init__(self, tenant_id: uuid.UUID) -> None:
            self.tenant_id = tenant_id
            self.id = None
            self.role = None
            self.employee_id = None
            self.is_active = True

    actor = _Actor(link.tenant_id)
    interview = await schedule_interview(
        session,
        actor,
        candidate_id=link.candidate_id,
        stage_id=link.stage_id,
        start=chosen,
        panelist_user_ids=[uuid.UUID(uid) for uid in matching.get("panelist_user_ids", [])],
        candidate_timezone=zone,
        transport=transport,
    )

    link.booked_interview_id = interview.id
    link.booked_at = moment
    await session.flush()

    return {
        "interview_id": str(interview.id),
        "start_utc": interview.scheduled_start.isoformat(),
        "start_local": availability.render_for(
            interview.scheduled_start, zone
        ).isoformat(),
        "timezone": zone,
        "join_url": interview.join_url,
    }
