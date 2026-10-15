"""Shift swaps, and how well each shift is covered.

A swap is two people trading their shifts on one day. The requester names a
colleague and a date; the colleague accepts; the requester's line manager, or
HR, approves. Approval writes two one-day shift assignments — the schedule each
person's attendance is judged against that day — in the same transaction as the
decision and its audit row. A swap is refused when either person has no shift
that day (a week off cannot be traded here), when both work the same shift, or
when the day has already begun or its attendance is locked.

Coverage counts, for each day and shift, who is scheduled to work it and who of
them is on approved leave, against the shift's minimum headcount when one is
set. It reads the same shift resolution attendance uses, so the two never
disagree about who works when.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.attendance import AttendancePeriodLock, Shift, ShiftAssignment
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import LeaveRequest, LeaveRequestStatus
from app.models.shift_swaps import ShiftSwapRequest, ShiftSwapStatus
from app.models.user import User
from app.services import audit
from app.services.attendance import qualifications
from app.services.attendance.service import (
    ShiftResolver,
    _approver_user_ids,
    _employee_name,
)
from app.services.notifications.dispatch import notify, register_event

__all__ = [
    "COVERAGE_MAX_DAYS",
    "approved_leave_days",
    "cancel_swap",
    "coverage",
    "decide_swap",
    "list_swaps",
    "pending_for_approver",
    "request_swap",
    "respond_to_swap",
]

COVERAGE_MAX_DAYS = 31

register_event(
    "attendance.shift_swap.asked",
    "{requester_name} asked to swap shifts with you",
    "{requester_name} would like to swap shifts with you on {swap_date}: you would work "
    "{requester_shift} and they would work {partner_shift}. {reason}",
)
register_event(
    "attendance.shift_swap.to_approve",
    "A shift swap needs your approval",
    "{requester_name} and {partner_name} have agreed to swap shifts on {swap_date}.",
)
register_event(
    "attendance.shift_swap.outcome",
    "Shift swap {outcome}",
    "The shift swap on {swap_date} was {outcome}.{note_suffix}",
)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


async def _swap(session: AsyncSession, user: User, swap_id: uuid.UUID) -> ShiftSwapRequest:
    row = await session.scalar(
        tenant_scoped(
            select(ShiftSwapRequest).where(ShiftSwapRequest.id == swap_id),
            ShiftSwapRequest,
            user,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Swap not found")
    return row


async def _locked(session: AsyncSession, user: User, on: date) -> bool:
    found = await session.scalar(
        tenant_scoped(
            select(AttendancePeriodLock.id).where(
                AttendancePeriodLock.period_start <= on,
                AttendancePeriodLock.period_end >= on,
                AttendancePeriodLock.unlocked_at.is_(None),
            ),
            AttendancePeriodLock,
            user,
        )
    )
    return found is not None


async def _shift_on(
    session: AsyncSession, user: User, employee_id: uuid.UUID, on: date
) -> Shift | None:
    """The shift they work that day; None on a week off, rostered or weekly."""
    resolver = await ShiftResolver.load(session, user, [employee_id], on, on)
    shift = resolver.resolve(employee_id, on).shift
    if shift is None or on.isoweekday() in (shift.weekly_off_days or []):
        return None
    return shift


async def _user_ids_for(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> list[uuid.UUID]:
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(User.id).where(
                        User.employee_id == employee_id, User.is_active.is_(True)
                    ),
                    User,
                    user,
                )
            )
        ).all()
    )


def _read(row: ShiftSwapRequest, names: dict[uuid.UUID, str], shifts: dict[uuid.UUID, Shift],
          *, viewer: User) -> dict[str, Any]:
    def shift(shift_id: uuid.UUID) -> dict[str, Any]:
        s = shifts.get(shift_id)
        return {
            "id": str(shift_id),
            "name": s.name if s else "A shift",
            "start_time": s.start_time.isoformat() if s else None,
            "end_time": s.end_time.isoformat() if s else None,
        }

    return {
        "id": str(row.id),
        "requester_employee_id": str(row.requester_employee_id),
        "requester_name": names.get(row.requester_employee_id, "A colleague"),
        "partner_employee_id": str(row.partner_employee_id),
        "partner_name": names.get(row.partner_employee_id, "A colleague"),
        "swap_date": row.swap_date.isoformat(),
        "requester_shift": shift(row.requester_shift_id),
        "partner_shift": shift(row.partner_shift_id),
        "reason": row.reason,
        "status": row.status.value,
        "note": row.note,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "you_are": (
            "requester" if viewer.employee_id == row.requester_employee_id
            else "partner" if viewer.employee_id == row.partner_employee_id
            else "approver"
        ),
    }


async def _reads(
    session: AsyncSession, user: User, rows: list[ShiftSwapRequest]
) -> list[dict[str, Any]]:
    people = {r.requester_employee_id for r in rows} | {r.partner_employee_id for r in rows}
    shift_ids = {r.requester_shift_id for r in rows} | {r.partner_shift_id for r in rows}
    names: dict[uuid.UUID, str] = {}
    if people:
        names = {
            e.id: e.full_name
            for e in (
                await session.scalars(
                    tenant_scoped(select(Employee).where(Employee.id.in_(people)), Employee, user)
                )
            ).all()
        }
    shifts: dict[uuid.UUID, Shift] = {}
    if shift_ids:
        shifts = {
            s.id: s
            for s in (
                await session.scalars(
                    tenant_scoped(select(Shift).where(Shift.id.in_(shift_ids)), Shift, user)
                )
            ).all()
        }
    return [_read(r, names, shifts, viewer=user) for r in rows]


# ── Asking, answering, deciding ─────────────────────────────────────────────


async def request_swap(
    session: AsyncSession,
    user: User,
    *,
    partner_employee_id: uuid.UUID,
    swap_date: date,
    reason: str,
    today: date | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Ask a colleague to trade shifts on one day. The requester is always the caller."""
    permissions.own_employee_id(user)
    me = user.employee_id
    assert me is not None
    if partner_employee_id == me:
        raise _unprocessable("Choose a colleague to swap with, not yourself.")
    if swap_date <= (today or date.today()):
        raise _unprocessable("A swap is for a day that has not started yet.")
    partner = await session.scalar(
        tenant_scoped(
            select(Employee).where(
                Employee.id == partner_employee_id,
                Employee.deleted_at.is_(None),
                Employee.status != EmploymentStatus.EXITED,
            ),
            Employee,
            user,
        )
    )
    if partner is None:
        raise _unprocessable("That colleague is not in the directory.")
    if await _locked(session, user, swap_date):
        raise _conflict("Attendance for that day is locked.")

    mine = await _shift_on(session, user, me, swap_date)
    theirs = await _shift_on(session, user, partner.id, swap_date)
    if mine is None or theirs is None:
        raise _unprocessable(
            "Both of you need a shift that day to swap. A week off cannot be traded here; "
            "ask HR to change the roster."
        )
    if mine.id == theirs.id:
        raise _unprocessable(f"You both work {mine.name} that day; there is nothing to swap.")
    open_already = await session.scalar(
        tenant_scoped(
            select(ShiftSwapRequest.id).where(
                ShiftSwapRequest.swap_date == swap_date,
                ShiftSwapRequest.status.in_(
                    [ShiftSwapStatus.AWAITING_PARTNER, ShiftSwapStatus.AWAITING_APPROVAL]
                ),
                or_(
                    ShiftSwapRequest.requester_employee_id.in_([me, partner.id]),
                    ShiftSwapRequest.partner_employee_id.in_([me, partner.id]),
                ),
            ),
            ShiftSwapRequest,
            user,
        )
    )
    if open_already is not None:
        raise _conflict("One of you already has a swap waiting for that day.")

    row = ShiftSwapRequest(
        tenant_id=user.tenant_id,
        requester_employee_id=me,
        partner_employee_id=partner.id,
        swap_date=swap_date,
        requester_shift_id=mine.id,
        partner_shift_id=theirs.id,
        reason=reason.strip(),
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="shift_swap",
        entity_id=row.id,
        actor=user,
        changes={
            "partner_employee_id": str(partner.id),
            "swap_date": swap_date.isoformat(),
            "requester_shift": mine.code,
            "partner_shift": theirs.code,
        },
        summary=f"Asked to swap {mine.code} for {theirs.code} on {swap_date}",
        request=request,
    )
    recipients = await _user_ids_for(session, user, partner.id)
    if recipients:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key="attendance.shift_swap.asked",
            recipient_user_ids=recipients,
            payload={
                "requester_name": await _employee_name(session, user, me),
                "swap_date": swap_date.strftime("%d %b %Y"),
                "requester_shift": mine.name,
                "partner_shift": theirs.name,
                "reason": row.reason,
            },
            commit=False,
        )
    await session.commit()
    return (await _reads(session, user, [row]))[0]


async def respond_to_swap(
    session: AsyncSession,
    user: User,
    swap_id: uuid.UUID,
    *,
    accept: bool,
    request: Request | None = None,
) -> dict[str, Any]:
    """The named colleague agrees or declines."""
    row = await _swap(session, user, swap_id)
    if user.employee_id is None or row.partner_employee_id != user.employee_id:
        raise permissions.forbidden("Only the colleague asked can answer this swap.")
    if row.status is not ShiftSwapStatus.AWAITING_PARTNER:
        raise _conflict("This swap has already been answered.")
    row.status = ShiftSwapStatus.AWAITING_APPROVAL if accept else ShiftSwapStatus.DECLINED
    row.partner_responded_at = datetime.now(UTC)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="shift_swap",
        entity_id=row.id,
        actor=user,
        changes={"status": row.status.value},
        summary=f"{'Agreed to' if accept else 'Declined'} the shift swap on {row.swap_date}",
        request=request,
    )
    if accept:
        approvers = [
            uid
            for uid in await _approver_user_ids(session, user, row.requester_employee_id)
            if uid != user.id
        ]
        if approvers:
            await notify(
                session,
                tenant_id=user.tenant_id,
                event_key="attendance.shift_swap.to_approve",
                recipient_user_ids=approvers,
                payload={
                    "requester_name": await _employee_name(
                        session, user, row.requester_employee_id
                    ),
                    "partner_name": await _employee_name(session, user, row.partner_employee_id),
                    "swap_date": row.swap_date.strftime("%d %b %Y"),
                },
                commit=False,
            )
    else:
        await _tell_requester(session, user, row, "declined by your colleague", None)
    await session.commit()
    return (await _reads(session, user, [row]))[0]


async def _tell_requester(
    session: AsyncSession, user: User, row: ShiftSwapRequest, outcome: str, note: str | None
) -> None:
    recipients = await _user_ids_for(session, user, row.requester_employee_id)
    if outcome == "approved":
        recipients += await _user_ids_for(session, user, row.partner_employee_id)
    if recipients:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key="attendance.shift_swap.outcome",
            recipient_user_ids=recipients,
            payload={
                "swap_date": row.swap_date.strftime("%d %b %Y"),
                "outcome": outcome,
                "note_suffix": f" {note}" if note else "",
            },
            commit=False,
        )


async def decide_swap(
    session: AsyncSession,
    user: User,
    swap_id: uuid.UUID,
    *,
    approve: bool,
    note: str | None = None,
    today: date | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """The requester's line manager, or HR, approves or rejects an agreed swap.

    Approval re-reads both schedules: if either person's shift for the day has
    changed since they agreed, the swap they agreed to no longer exists.
    """
    row = await _swap(session, user, swap_id)
    if user.employee_id in (row.requester_employee_id, row.partner_employee_id):
        raise permissions.forbidden("You cannot decide a swap you are part of.")
    if not (
        permissions.is_hr(user)
        or await permissions.is_line_manager_of(session, user, row.requester_employee_id)
    ):
        raise permissions.forbidden("Only their manager or HR can decide this swap.")
    if row.status is not ShiftSwapStatus.AWAITING_APPROVAL:
        raise _conflict("This swap is not waiting for a decision.")

    if approve:
        if row.swap_date <= (today or date.today()):
            raise _conflict("The day has started; the swap can no longer be applied.")
        if await _locked(session, user, row.swap_date):
            raise _conflict("Attendance for that day is locked.")
        mine = await _shift_on(session, user, row.requester_employee_id, row.swap_date)
        theirs = await _shift_on(session, user, row.partner_employee_id, row.swap_date)
        if (mine and mine.id) != row.requester_shift_id or (
            theirs and theirs.id
        ) != row.partner_shift_id:
            raise _conflict(
                "One of their shifts for that day has changed since they agreed. Ask them "
                "to raise the swap again."
            )
        await qualifications.require_qualified(
            session, user, row.requester_employee_id, [theirs], row.swap_date,
            who=await _employee_name(session, user, row.requester_employee_id),
        )
        await qualifications.require_qualified(
            session, user, row.partner_employee_id, [mine], row.swap_date,
            who=await _employee_name(session, user, row.partner_employee_id),
        )
        for employee_id, shift_id in (
            (row.requester_employee_id, row.partner_shift_id),
            (row.partner_employee_id, row.requester_shift_id),
        ):
            session.add(
                ShiftAssignment(
                    tenant_id=user.tenant_id,
                    employee_id=employee_id,
                    shift_id=shift_id,
                    effective_from=row.swap_date,
                    effective_to=row.swap_date,
                )
            )
    row.status = ShiftSwapStatus.APPROVED if approve else ShiftSwapStatus.REJECTED
    row.decided_by_user_id = user.id
    row.decided_at = datetime.now(UTC)
    row.note = (note or "").strip() or None
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="shift_swap",
        entity_id=row.id,
        actor=user,
        changes={"status": row.status.value, "note": row.note},
        summary=f"{'Approved' if approve else 'Rejected'} the shift swap on {row.swap_date}",
        request=request,
    )
    await _tell_requester(session, user, row, "approved" if approve else "rejected", row.note)
    await session.commit()
    return (await _reads(session, user, [row]))[0]


async def cancel_swap(
    session: AsyncSession, user: User, swap_id: uuid.UUID, *, request: Request | None = None
) -> dict[str, Any]:
    """The requester withdraws a swap that has not been decided."""
    row = await _swap(session, user, swap_id)
    if user.employee_id is None or row.requester_employee_id != user.employee_id:
        raise permissions.forbidden("Only the person who asked can withdraw a swap.")
    if row.status not in (ShiftSwapStatus.AWAITING_PARTNER, ShiftSwapStatus.AWAITING_APPROVAL):
        raise _conflict("Only a swap still waiting can be withdrawn.")
    row.status = ShiftSwapStatus.CANCELLED
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="shift_swap",
        entity_id=row.id,
        actor=user,
        changes={"status": row.status.value},
        summary=f"Withdrew the shift swap on {row.swap_date}",
        request=request,
    )
    await session.commit()
    return (await _reads(session, user, [row]))[0]


async def list_swaps(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Swaps the caller asked for or was asked into, newest first."""
    permissions.own_employee_id(user)
    me = user.employee_id
    rows = (
        await session.scalars(
            tenant_scoped(
                select(ShiftSwapRequest)
                .where(
                    or_(
                        ShiftSwapRequest.requester_employee_id == me,
                        ShiftSwapRequest.partner_employee_id == me,
                    )
                )
                .order_by(ShiftSwapRequest.swap_date.desc())
                .limit(100),
                ShiftSwapRequest,
                user,
            )
        )
    ).all()
    return await _reads(session, user, list(rows))


async def pending_for_approver(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Agreed swaps this caller may decide: their reporting line's, or all for HR."""
    stmt = tenant_scoped(
        select(ShiftSwapRequest).where(
            ShiftSwapRequest.status == ShiftSwapStatus.AWAITING_APPROVAL
        ),
        ShiftSwapRequest,
        user,
    )
    if not permissions.is_hr(user):
        line = await permissions.reporting_line(session, user)
        if not line:
            return []
        stmt = stmt.where(ShiftSwapRequest.requester_employee_id.in_(line))
    if user.employee_id is not None:
        stmt = stmt.where(
            ShiftSwapRequest.requester_employee_id != user.employee_id,
            ShiftSwapRequest.partner_employee_id != user.employee_id,
        )
    rows = (await session.scalars(stmt.order_by(ShiftSwapRequest.swap_date))).all()
    return await _reads(session, user, list(rows))


# ── Coverage ────────────────────────────────────────────────────────────────


async def approved_leave_days(
    session: AsyncSession,
    user: User,
    people: list[uuid.UUID],
    start: date,
    end: date,
) -> dict[uuid.UUID, set[date]]:
    """Each person's days of approved full-day leave in the range."""
    out: dict[uuid.UUID, set[date]] = {}
    if not people:
        return out
    leaves = (
        await session.scalars(
            tenant_scoped(
                select(LeaveRequest).where(
                    LeaveRequest.employee_id.in_(people),
                    LeaveRequest.status == LeaveRequestStatus.APPROVED,
                    LeaveRequest.start_date <= end,
                    LeaveRequest.end_date >= start,
                    LeaveRequest.is_half_day.is_(False),
                ),
                LeaveRequest,
                user,
            )
        )
    ).all()
    for leave in leaves:
        cursor = max(leave.start_date, start)
        while cursor <= min(leave.end_date, end):
            out.setdefault(leave.employee_id, set()).add(cursor)
            cursor += timedelta(days=1)
    return out


async def coverage(
    session: AsyncSession,
    user: User,
    *,
    start: date,
    end: date,
    location_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    work_area: str | None = None,
) -> dict[str, Any]:
    """Per day and shift: who is scheduled, who of them is on leave, and the gap.

    `location_id` narrows to people based at a site; `work_area` to the shifts
    of one line or area. Each shift says which site and line it belongs to.

    HR sees everyone; a manager sees their reporting line. A shift with a
    minimum headcount is flagged on any working day where fewer than that are
    available.
    """
    if end < start:
        raise _unprocessable("The end date is before the start date.")
    if (end - start).days + 1 > COVERAGE_MAX_DAYS:
        raise _unprocessable(f"Coverage covers at most {COVERAGE_MAX_DAYS} days at a time.")
    stmt = tenant_scoped(
        select(Employee.id).where(
            Employee.deleted_at.is_(None),
            Employee.status != EmploymentStatus.EXITED,
            or_(Employee.date_of_joining.is_(None), Employee.date_of_joining <= end),
        ),
        Employee,
        user,
    )
    if location_id:
        stmt = stmt.where(Employee.location_id == location_id)
    if department_id:
        stmt = stmt.where(Employee.department_id == department_id)
    if not permissions.is_hr(user):
        line = await permissions.reporting_line(session, user)
        if not line:
            raise permissions.forbidden("Coverage is for HR and managers.")
        stmt = stmt.where(Employee.id.in_(line))
    people = list((await session.scalars(stmt)).all())

    on_leave: dict[date, set[uuid.UUID]] = {}
    for employee_id, leave_days in (
        await approved_leave_days(session, user, people, start, end)
    ).items():
        for day in leave_days:
            on_leave.setdefault(day, set()).add(employee_id)

    resolver = await ShiftResolver.load(session, user, people, start, end)
    shifts: dict[uuid.UUID, Shift] = {}
    days: list[dict[str, Any]] = []
    cursor = start
    while cursor <= end:
        counts: dict[uuid.UUID, dict[str, int]] = {}
        for employee_id in people:
            shift = resolver.resolve(employee_id, cursor).shift
            if shift is None or cursor.isoweekday() in (shift.weekly_off_days or []):
                continue
            if work_area and (shift.work_area or "").lower() != work_area.strip().lower():
                continue
            shifts[shift.id] = shift
            entry = counts.setdefault(shift.id, {"scheduled": 0, "on_leave": 0})
            entry["scheduled"] += 1
            if employee_id in on_leave.get(cursor, set()):
                entry["on_leave"] += 1
        rows = []
        for shift_id, entry in counts.items():
            shift = shifts[shift_id]
            available = entry["scheduled"] - entry["on_leave"]
            rows.append(
                {
                    "shift_id": str(shift_id),
                    "scheduled": entry["scheduled"],
                    "on_leave": entry["on_leave"],
                    "available": available,
                    "min_headcount": shift.min_headcount,
                    "short_by": (
                        max(0, shift.min_headcount - available) if shift.min_headcount else 0
                    ),
                }
            )
        days.append({"date": cursor.isoformat(), "shifts": rows})
        cursor += timedelta(days=1)

    from app.models.org import Location

    site_names = (
        dict(
            (
                await session.execute(
                    tenant_scoped(
                        select(Location.id, Location.name).where(
                            Location.id.in_({s.location_id for s in shifts.values()})
                        ),
                        Location,
                        user,
                    )
                )
            ).all()
        )
        if any(s.location_id for s in shifts.values())
        else {}
    )
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "people": len(people),
        "shifts": [
            {
                "id": str(s.id),
                "code": s.code,
                "name": s.name,
                "start_time": s.start_time.isoformat(),
                "end_time": s.end_time.isoformat(),
                "min_headcount": s.min_headcount,
                "location_id": str(s.location_id) if s.location_id else None,
                "location_name": site_names.get(s.location_id) if s.location_id else None,
                "work_area": s.work_area,
            }
            for s in sorted(
                shifts.values(), key=lambda s: (s.work_area or "", s.start_time, s.code)
            )
        ],
        "days": days,
        "note": (
            "Half-day leave is counted as available. Shifts with no minimum headcount are "
            "counted, not judged."
        ),
    }
