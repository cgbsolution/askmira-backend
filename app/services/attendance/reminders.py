"""Nudges for attendance work that payroll waits on.

Two things hold a payroll up without anybody noticing: corrections nobody has
decided, and a month nobody has locked. Once a day this tells the people who
can act — each approver about the corrections waiting on them for more than
two days, and HR when last month is still open after the 3rd — and stops
saying so as soon as the work is done.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.attendance import (
    AttendancePeriodLock,
    AttendanceRegularisation,
    RegularisationStatus,
)
from app.models.user import User, UserRole
from app.services.notifications.dispatch import notify, register_event

__all__ = ["PENDING_AFTER", "UNLOCKED_AFTER_DAY", "send_attendance_reminders"]

#: A correction waiting longer than this is worth a reminder.
PENDING_AFTER = timedelta(hours=48)
#: From this day of the month, last month still being open is worth a reminder.
UNLOCKED_AFTER_DAY = 3

register_event(
    "attendance.regularisation.reminder",
    "{count} attendance correction(s) waiting on you",
    "{count} attendance correction(s) from your team have waited more than two days, the "
    "oldest from {oldest}. Payroll uses the corrected days, so decide them under Time, "
    "Attendance review.",
)
register_event(
    "attendance.period.unlocked",
    "Attendance for {month} is not locked yet",
    "Attendance for {month} is still open. Payroll cannot run for it until it is locked "
    "under Time, Attendance review.{pending_line}",
)


def _previous_month(today: date) -> tuple[date, date]:
    first = today.replace(day=1)
    end = first - timedelta(days=1)
    return end.replace(day=1), end


async def send_attendance_reminders(
    session: AsyncSession, actor: User, *, today: date, now: datetime | None = None
) -> dict[str, int]:
    """Scheduler-callable. Returns how many people were reminded of what."""
    from app.services.attendance.service import _approver_user_ids

    moment = now or datetime.now(UTC)
    pending = (
        await session.scalars(
            tenant_scoped(
                select(AttendanceRegularisation).where(
                    AttendanceRegularisation.status == RegularisationStatus.PENDING,
                    AttendanceRegularisation.created_at <= moment - PENDING_AFTER,
                ),
                AttendanceRegularisation,
                actor,
            )
        )
    ).all()

    per_approver: dict[uuid.UUID, list[AttendanceRegularisation]] = defaultdict(list)
    for row in pending:
        recipients = (
            [row.approver_user_id]
            if row.approver_user_id is not None
            else await _approver_user_ids(session, actor, row.employee_id)
        )
        for recipient in recipients:
            per_approver[recipient].append(row)
    for recipient, rows in per_approver.items():
        oldest = min(r.attendance_date for r in rows)
        await notify(
            session,
            tenant_id=actor.tenant_id,
            event_key="attendance.regularisation.reminder",
            recipient_user_ids=[recipient],
            payload={"count": len(rows), "oldest": f"{oldest:%d %b %Y}"},
            commit=False,
        )

    hr_told = 0
    if today.day >= UNLOCKED_AFTER_DAY:
        start, end = _previous_month(today)
        locked = await session.scalar(
            tenant_scoped(
                select(AttendancePeriodLock.id).where(
                    AttendancePeriodLock.period_start <= start,
                    AttendancePeriodLock.period_end >= end,
                    AttendancePeriodLock.unlocked_at.is_(None),
                ),
                AttendancePeriodLock,
                actor,
            )
        )
        if locked is None:
            hr = list(
                (
                    await session.scalars(
                        tenant_scoped(
                            select(User.id).where(
                                User.role.in_([UserRole.ADMIN, UserRole.SUB_ADMIN]),
                                User.is_active.is_(True),
                            ),
                            User,
                            actor,
                        )
                    )
                ).all()
            )
            open_in_month = sum(1 for r in pending if start <= r.attendance_date <= end)
            if hr:
                await notify(
                    session,
                    tenant_id=actor.tenant_id,
                    event_key="attendance.period.unlocked",
                    recipient_user_ids=hr,
                    payload={
                        "month": f"{start:%B %Y}",
                        "pending_line": (
                            f" {open_in_month} correction(s) for that month are still undecided."
                            if open_in_month
                            else ""
                        ),
                    },
                    commit=False,
                )
                hr_told = len(hr)
    await session.commit()
    return {"approvers_reminded": len(per_approver), "hr_reminded_unlocked": hr_told}
