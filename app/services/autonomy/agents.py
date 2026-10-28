"""The agents that create proposals. Each one is code that reads the record and
proposes; none of them applies anything on its own authority.

**The probation agent** (`lifecycle.raise_confirmation`). A tenant whose
probation policy treats silence as consent has decided that a review nobody
answered ends in confirmation. Something has to carry that out, and it should
not be a silent field edit: the agent drafts the confirmation event — with the
review's own dates — and proposes submitting it. Under the default autonomy a
person approves the proposal; the event then goes through the normal lifecycle
approval like any other, and the letter is issued when it takes effect. A
proposal that is rejected leaves the draft where it is, and the agent does not
propose it again.

The agent makes no judgement. Its reasoning is the facts — how overdue, how
many reminders went unanswered, which policy applies — and its citations are
the review and the employee, so an approver can open both.

**The timesheet agent** (`timesheets.recommend_approval`). For each submitted
timesheet it lays the hours logged against the attendance recorded for the same
days. Where every day's hours are within `TIMESHEET_TOLERANCE_MINUTES` of the
time the person was in, it proposes approval, citing the timesheet and the
attendance days. Where any day logs more than attendance shows, it proposes
nothing and says which days. The proposal is decided by the timesheet's own
approver, whose approval then prices the hours exactly as a direct approval does.

**The schedule agent** (`scheduling.cover_short_shift`). Over the coming week it
reads shift coverage, the same view HR sees, and for each shift short of its
minimum headcount it looks for people who could cover: active, not on approved
leave, not already working that day (a weekly off counts as free, and the
reasoning says so), and qualified for the shift and its site. It proposes up to
the shortfall, one person per proposal. Approving one writes a one-day shift
assignment for that person, as an approved swap does; nothing changes until a
person approves, and the agent never proposes the same person twice for a day.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.attendance import AttendanceDay, shift_off_on
from app.models.autonomy import AgentProposal, ProposalStatusEnum
from app.models.cycle_config import ProbationReview
from app.models.lifecycle import LifecycleEvent, LifecycleEventType, LifecycleStatus
from app.models.timesheets import Timesheet, TimesheetEntry, TimesheetStatus
from app.models.user import User
from app.services.autonomy import service as autonomy
from app.services.autonomy.catalogue import AutonomyLevel
from app.services.lifecycle import service as lifecycle

__all__ = ["apply_shift_cover", "probation_agent", "schedule_agent", "timesheet_agent"]

ACTION_KEY = "lifecycle.raise_confirmation"

#: A confirmation in any of these states means the review is already being
#: answered, by a person or by an earlier run of this agent.
_IN_HAND = (
    LifecycleStatus.DRAFT,
    LifecycleStatus.PENDING_APPROVAL,
    LifecycleStatus.APPROVED,
    LifecycleStatus.EFFECTED,
)


async def _confirmation_in_hand(
    session: AsyncSession, user: User, employee_id: uuid.UUID, review: ProbationReview
) -> bool:
    found = await session.scalar(
        tenant_scoped(
            select(LifecycleEvent.id).where(
                LifecycleEvent.employee_id == employee_id,
                LifecycleEvent.event_type == LifecycleEventType.CONFIRMATION,
                LifecycleEvent.status.in_(_IN_HAND),
                LifecycleEvent.created_at >= review.created_at,
            ),
            LifecycleEvent,
            user,
        )
    )
    return found is not None


async def probation_agent(
    session: AsyncSession, user: User, *, today: date | None = None
) -> dict[str, Any]:
    """Propose the confirmations this tenant's silence policy calls for."""
    on = today or date.today()
    try:
        drift = await lifecycle.resolve_probation_drift(session, user, on=on)
    except HTTPException as exc:
        # No probation policy chosen: there is no silence rule to carry out,
        # and the agent does not pick one.
        return {"as_of": on.isoformat(), "skipped": str(exc.detail)}
    confirmations = [c for c in drift["cases"] if c.get("auto_outcome") == "confirm"]

    configured, _threshold, _source = await autonomy.effective_autonomy(
        session, user, action_key=ACTION_KEY
    )
    if configured is AutonomyLevel.SUGGEST_ONLY:
        # The tenant set this action to suggestions only: nothing is drafted or
        # queued, and the drift report already shows the cases.
        return {
            "as_of": on.isoformat(),
            "policy": drift["policy"],
            "proposed": 0,
            "already_in_hand": 0,
            "suggest_only": len(confirmations),
            "refused": [],
        }

    proposed = 0
    already = 0
    refused: list[str] = []
    for case in confirmations:
        review = await session.scalar(
            tenant_scoped(
                select(ProbationReview).where(ProbationReview.id == uuid.UUID(case["review_id"])),
                ProbationReview,
                user,
            )
        )
        if review is None or review.outcome is not None:
            continue
        employee_id = review.employee_id
        if await _confirmation_in_hand(session, user, employee_id, review):
            already += 1
            continue

        reason = (
            f"Probation review was due on {review.review_due_date.isoformat()} and is "
            f"{case['days_overdue']} day(s) past it with no decision after "
            f"{case['reminders_sent']} reminder(s). This organisation's probation policy "
            "confirms when a review passes in silence."
        )
        try:
            # Validation refuses before anything is written, so a refusal here
            # leaves nothing behind.
            event = await lifecycle.raise_event(
                session,
                user,
                employee_id=employee_id,
                event_type=LifecycleEventType.CONFIRMATION,
                effective_from=review.probation_end_date,
                new_values={"probation_end_date": review.probation_end_date.isoformat()},
                reason=reason,
                today=on,
                status_on_raise=LifecycleStatus.DRAFT,
            )
        except HTTPException as exc:
            refused.append(str(exc.detail))
            continue
        await autonomy.propose(
            session,
            user,
            action_key=ACTION_KEY,
            subject_type="lifecycle_event",
            subject_id=event.id,
            proposed_change={"status": LifecycleStatus.PENDING_APPROVAL.value},
            previous_state={"status": LifecycleStatus.DRAFT.value},
            reasoning=reason,
            citations=(
                f"probation_reviews:{review.id}",
                f"employees:{employee_id}",
                f"lifecycle_events:{event.id}",
            ),
            confidence=1.0,
        )
        proposed += 1

    return {
        "as_of": on.isoformat(),
        "policy": drift["policy"],
        "proposed": proposed,
        "already_in_hand": already,
        "suggest_only": 0,
        "refused": refused,
    }


TIMESHEET_ACTION = "timesheets.recommend_approval"
#: How far a day's logged hours may run past the time attendance shows.
TIMESHEET_TOLERANCE_MINUTES = 30


async def timesheet_agent(
    session: AsyncSession, user: User, *, today: date | None = None
) -> dict[str, Any]:
    """Propose approving submitted timesheets whose hours match attendance."""
    from datetime import timedelta
    from decimal import Decimal

    on = today or date.today()
    configured, _threshold, _source = await autonomy.effective_autonomy(
        session, user, action_key=TIMESHEET_ACTION
    )
    sheets = (
        await session.scalars(
            tenant_scoped(
                select(Timesheet).where(Timesheet.status == TimesheetStatus.SUBMITTED),
                Timesheet,
                user,
            )
        )
    ).all()
    if configured is AutonomyLevel.SUGGEST_ONLY:
        return {"as_of": on.isoformat(), "proposed": 0, "held": [], "suggest_only": len(sheets)}

    already = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(AgentProposal.subject_id).where(
                        AgentProposal.action_key == TIMESHEET_ACTION,
                        AgentProposal.subject_id.in_([s.id for s in sheets] or [uuid.uuid4()]),
                        AgentProposal.status.in_(
                            (
                                ProposalStatusEnum.PENDING,
                                ProposalStatusEnum.APPROVED,
                                ProposalStatusEnum.REJECTED,
                            )
                        ),
                    ),
                    AgentProposal,
                    user,
                )
            )
        ).all()
    )
    proposed = 0
    held: list[str] = []
    for sheet in sheets:
        if sheet.id in already:
            continue
        entries = (
            await session.scalars(
                tenant_scoped(
                    select(TimesheetEntry).where(TimesheetEntry.timesheet_id == sheet.id),
                    TimesheetEntry,
                    user,
                )
            )
        ).all()
        logged: dict[date, Decimal] = {}
        for entry in entries:
            logged[entry.work_date] = logged.get(entry.work_date, Decimal(0)) + Decimal(
                str(entry.hours)
            )
        if not logged:
            continue
        days = {
            d.attendance_date: d
            for d in (
                await session.scalars(
                    tenant_scoped(
                        select(AttendanceDay).where(
                            AttendanceDay.employee_id == sheet.employee_id,
                            AttendanceDay.attendance_date >= sheet.week_start,
                            AttendanceDay.attendance_date <= sheet.week_start + timedelta(days=6),
                        ),
                        AttendanceDay,
                        user,
                    )
                )
            ).all()
        }
        over = [
            f"{day:%a %d %b}: {hours} h logged, "
            f"{(days[day].worked_minutes if day in days else 0) / 60:.2f} h in attendance"
            for day, hours in sorted(logged.items())
            if hours * 60
            > (days[day].worked_minutes if day in days else 0) + TIMESHEET_TOLERANCE_MINUTES
        ]
        if over:
            held.append(f"Week of {sheet.week_start}: " + "; ".join(over))
            continue
        total = sum(logged.values(), Decimal(0))
        reason = (
            f"{total} hours logged across {len(logged)} day(s) in the week of "
            f"{sheet.week_start:%d %b %Y}. Each day is within {TIMESHEET_TOLERANCE_MINUTES} "
            "minutes of the time attendance recorded, so the hours are supported."
        )
        await autonomy.propose(
            session,
            user,
            action_key=TIMESHEET_ACTION,
            subject_type="timesheet",
            subject_id=sheet.id,
            proposed_change={"status": TimesheetStatus.APPROVED.value},
            previous_state={"status": TimesheetStatus.SUBMITTED.value},
            reasoning=reason,
            citations=(
                f"timesheets:{sheet.id}",
                f"employees:{sheet.employee_id}",
                *(f"attendance_days:{days[d].id}" for d in sorted(logged) if d in days),
            ),
            confidence=1.0,
        )
        proposed += 1
    await session.commit()
    return {"as_of": on.isoformat(), "proposed": proposed, "held": held, "suggest_only": 0}


SCHEDULE_ACTION = "scheduling.cover_short_shift"
SCHEDULE_HORIZON_DAYS = 7


async def _proposed_cover(
    session: AsyncSession, user: User
) -> tuple[dict[tuple[str, str], int], set[tuple[str, str]]]:
    """Open cover proposals: count per (shift, day), and the (person, day) pairs taken."""
    rows = (
        await session.scalars(
            tenant_scoped(
                select(AgentProposal).where(
                    AgentProposal.action_key == SCHEDULE_ACTION,
                    AgentProposal.status.in_(
                        (ProposalStatusEnum.PENDING, ProposalStatusEnum.APPROVED)
                    ),
                ),
                AgentProposal,
                user,
            )
        )
    ).all()
    per_slot: dict[tuple[str, str], int] = {}
    taken: set[tuple[str, str]] = set()
    for row in rows:
        change = row.proposed_change or {}
        day = str(change.get("date"))
        per_slot[(str(row.subject_id), day)] = per_slot.get((str(row.subject_id), day), 0) + 1
        taken.add((str(change.get("employee_id")), day))
    return per_slot, taken


async def schedule_agent(
    session: AsyncSession, user: User, *, today: date | None = None
) -> dict[str, Any]:
    """Propose cover for shifts short of their minimum headcount over the coming week."""
    from datetime import timedelta

    from app.models.attendance import Shift
    from app.models.employee import Employee, EmploymentStatus
    from app.services.attendance import qualifications, shift_swaps
    from app.services.attendance.service import ShiftResolver

    on = today or date.today()
    configured, _threshold, _source = await autonomy.effective_autonomy(
        session, user, action_key=SCHEDULE_ACTION
    )
    start, end = on + timedelta(days=1), on + timedelta(days=SCHEDULE_HORIZON_DAYS)
    report = await shift_swaps.coverage(session, user, start=start, end=end)
    short = [
        (date.fromisoformat(day["date"]), row)
        for day in report["days"]
        for row in day["shifts"]
        if row["short_by"] > 0
    ]
    if configured is AutonomyLevel.SUGGEST_ONLY or not short:
        return {
            "as_of": on.isoformat(),
            "short_slots": len(short),
            "proposed": 0,
            "uncovered": [],
            "suggest_only": len(short) if configured is AutonomyLevel.SUGGEST_ONLY else 0,
        }

    people = (
        await session.scalars(
            tenant_scoped(
                select(Employee)
                .where(
                    Employee.deleted_at.is_(None),
                    Employee.status != EmploymentStatus.EXITED,
                )
                .order_by(Employee.employee_code),
                Employee,
                user,
            )
        )
    ).all()
    resolver = await ShiftResolver.load(session, user, [p.id for p in people], start, end)
    leave = await shift_swaps.approved_leave_days(session, user, [p.id for p in people], start, end)
    shifts = {
        s.id: s
        for s in (
            await session.scalars(
                tenant_scoped(
                    select(Shift).where(Shift.id.in_({uuid.UUID(r["shift_id"]) for _, r in short})),
                    Shift,
                    user,
                )
            )
        ).all()
    }
    per_slot, taken = await _proposed_cover(session, user)
    proposed = 0
    uncovered: list[str] = []
    for day, row in short:
        shift = shifts.get(uuid.UUID(row["shift_id"]))
        if shift is None:
            continue
        needed = row["short_by"] - per_slot.get((str(shift.id), day.isoformat()), 0)
        found = 0
        for person in people:
            if found >= needed:
                break
            if (str(person.id), day.isoformat()) in taken:
                continue
            if person.date_of_joining and person.date_of_joining > day:
                continue
            if day in leave.get(person.id, set()):
                continue
            own = resolver.resolve(person.id, day).shift
            weekly_off = own is not None and shift_off_on(own, day)
            if own is not None and not weekly_off:
                continue
            try:
                await qualifications.require_qualified(
                    session, user, person.id, [shift], day, who=person.employee_code
                )
            except HTTPException:
                continue
            free = "on their weekly off" if weekly_off else "not scheduled that day"
            reason = (
                f"{shift.code} on {day:%a %d %b} has {row['available']} available against a "
                f"minimum of {row['min_headcount']}. {person.employee_code} is {free}, not on "
                "leave, and qualified for the shift and its site."
            )
            await autonomy.propose(
                session,
                user,
                action_key=SCHEDULE_ACTION,
                subject_type="shift_cover",
                subject_id=shift.id,
                proposed_change={"employee_id": str(person.id), "date": day.isoformat()},
                previous_state={"employee_id": None, "date": None},
                reasoning=reason,
                citations=(f"shifts:{shift.id}", f"employees:{person.id}"),
                confidence=1.0,
            )
            taken.add((str(person.id), day.isoformat()))
            found += 1
            proposed += 1
        if found < needed:
            uncovered.append(f"{shift.code} on {day.isoformat()}: {needed - found} short")
    await session.commit()
    return {
        "as_of": on.isoformat(),
        "short_slots": len(short),
        "proposed": proposed,
        "uncovered": uncovered,
        "suggest_only": 0,
    }


async def apply_shift_cover(
    session: AsyncSession, user: User, *, shift: Any, change: dict[str, Any]
) -> None:
    """An approved cover proposal: a one-day assignment, checked again as of now."""
    from app.models.attendance import ShiftAssignment
    from app.models.audit import AuditAction
    from app.models.employee import Employee, EmploymentStatus
    from app.services import audit
    from app.services.attendance import qualifications
    from app.services.attendance.service import local_today

    try:
        employee_id = uuid.UUID(str(change.get("employee_id")))
        day = date.fromisoformat(str(change.get("date")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="This cover proposal is malformed.") from exc
    if day < await local_today(session, user):
        raise HTTPException(status_code=409, detail="That day has passed; nothing to cover.")
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None or employee.status is EmploymentStatus.EXITED:
        raise HTTPException(status_code=409, detail="That person is no longer here.")
    if not shift.active:
        raise HTTPException(status_code=409, detail=f"Shift {shift.code} is inactive.")
    await qualifications.require_qualified(
        session, user, employee.id, [shift], day, who=employee.employee_code
    )
    row = ShiftAssignment(
        tenant_id=user.tenant_id,
        employee_id=employee.id,
        shift_id=shift.id,
        effective_from=day,
        effective_to=day,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="shift_assignment",
        entity_id=row.id,
        actor=user,
        changes={
            "employee_id": str(employee.id),
            "shift_id": str(shift.id),
            "date": day.isoformat(),
            "via": "schedule agent proposal",
        },
        summary=f"{employee.employee_code} covers {shift.code} on {day} (schedule agent)",
    )
