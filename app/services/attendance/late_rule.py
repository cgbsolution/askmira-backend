"""The late-mark rule: every Nth late mark in a month is a half day.

The workspace sets N (2, 3, 4…; never the first late mark). In each calendar
month a person's late marks are counted in date order, and the Nth, the 2Nth
and so on turn a present day into a half day — the 3rd, 6th and 9th late day
with N = 3. The count starts again on the 1st.

The rule runs over the stored days of a month after they are computed, so it
always reflects the month as it now stands: a correction that removes an
earlier late mark moves the half day to the next one, and a half day the rule
no longer calls for goes back to present. A day HR or an approver corrected
counts as a late mark if it still shows one, but is never turned into a half
day by the rule. Locked (paid) days are counted and never changed.

The rule applies from the month it was switched on or last changed
(`late_marks_rule_from`); earlier months stay as they were.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.attendance import AttendanceDay, AttendancePeriodLock, AttendanceStatus
from app.models.user import User

__all__ = ["apply_late_rule", "month_starts"]

#: Between the rule's note and the day's own, so the rule's can be taken off again.
_SEP = ". Without the late-mark rule: "
_ORDINAL_SUFFIX = {1: "st", 2: "nd", 3: "rd"}


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else _ORDINAL_SUFFIX.get(n % 10, "th")
    return f"{n}{suffix}"


def _note(count: int, every: int, on: date) -> str:
    return (
        f"Half day: late for the {_ordinal(count)} time in {on:%B} "
        f"(every {_ordinal(every)} late mark in a month is a half day)"
    )


def month_starts(start: date, end: date) -> list[date]:
    months: list[date] = []
    cursor = start.replace(day=1)
    while cursor <= end:
        months.append(cursor)
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    return months


def _month_end(first: date) -> date:
    return (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)


async def _locked(session: AsyncSession, user: User, start: date, end: date) -> set[date]:
    locks = (
        await session.scalars(
            tenant_scoped(
                select(AttendancePeriodLock).where(
                    AttendancePeriodLock.unlocked_at.is_(None),
                    AttendancePeriodLock.period_start <= end,
                    AttendancePeriodLock.period_end >= start,
                ),
                AttendancePeriodLock,
                user,
            )
        )
    ).all()
    days: set[date] = set()
    for lock in locks:
        cursor = max(lock.period_start, start)
        while cursor <= min(lock.period_end, end):
            days.add(cursor)
            cursor += timedelta(days=1)
    return days


def _convert(day: AttendanceDay, count: int, every: int) -> None:
    day.status = AttendanceStatus.HALF_DAY
    day.payable_fraction = Decimal("0.5")
    day.late_mark_half_day = True
    note = _note(count, every, day.attendance_date)
    day.computation_note = f"{note}{_SEP}{day.computation_note}" if day.computation_note else note


def _revert(day: AttendanceDay) -> None:
    day.status = AttendanceStatus.PRESENT
    day.payable_fraction = Decimal("1")
    day.late_mark_half_day = False
    note = day.computation_note or ""
    if note.startswith("Half day: late for the"):
        note = note.split(_SEP, 1)[1] if _SEP in note else ""
    day.computation_note = note or None


async def apply_late_rule(
    session: AsyncSession,
    user: User,
    *,
    employee_ids: list[uuid.UUID] | None,
    start: date,
    end: date,
    every: int | None,
    rule_from: date | None,
) -> int:
    """Apply the rule to the whole of each month touching start..end. Returns days changed.

    `every` None (the rule off) changes nothing here; switching the rule off
    undoes the current month through `undo_late_rule`.
    """
    if not every:
        return 0
    await session.flush()
    changed = 0
    for first in month_starts(start, end):
        if rule_from is not None and first < rule_from.replace(day=1):
            continue
        last = _month_end(first)
        stmt = select(AttendanceDay).where(
            AttendanceDay.attendance_date >= first, AttendanceDay.attendance_date <= last
        )
        if employee_ids is not None:
            if not employee_ids:
                continue
            stmt = stmt.where(AttendanceDay.employee_id.in_(employee_ids))
        stmt = stmt.order_by(AttendanceDay.employee_id, AttendanceDay.attendance_date)
        days = (await session.scalars(tenant_scoped(stmt, AttendanceDay, user))).all()
        locked = await _locked(session, user, first, last)

        counts: dict[uuid.UUID, int] = {}
        for day in days:
            late = day.late_by_minutes > 0 and (
                day.late_mark_half_day
                or day.status in (AttendanceStatus.PRESENT, AttendanceStatus.HALF_DAY)
            )
            count = counts.get(day.employee_id, 0) + (1 if late else 0)
            counts[day.employee_id] = count
            if day.attendance_date in locked:
                continue
            due = (
                late
                and count % every == 0
                and not day.is_regularised
                and (day.late_mark_half_day or day.status is AttendanceStatus.PRESENT)
            )
            if due and not day.late_mark_half_day:
                _convert(day, count, every)
                changed += 1
            elif not due and day.late_mark_half_day:
                _revert(day)
                changed += 1
    return changed


async def undo_late_rule(session: AsyncSession, user: User, *, start: date, end: date) -> int:
    """Put back every unlocked day in start..end the rule had made a half day."""
    await session.flush()
    days = (
        await session.scalars(
            tenant_scoped(
                select(AttendanceDay).where(
                    AttendanceDay.attendance_date >= start,
                    AttendanceDay.attendance_date <= end,
                    AttendanceDay.late_mark_half_day.is_(True),
                ),
                AttendanceDay,
                user,
            )
        )
    ).all()
    locked = await _locked(session, user, start, end)
    changed = 0
    for day in days:
        if day.attendance_date not in locked:
            _revert(day)
            changed += 1
    return changed
