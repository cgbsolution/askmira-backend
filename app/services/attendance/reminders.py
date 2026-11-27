"""Nudges for attendance work that payroll waits on.

Three things hold a payroll up without anybody noticing: days nobody has
explained, corrections nobody has decided, and a month nobody has locked. Once
a day this tells the people who can act — each person about their own absent
and missed-punch days (the "absent report" older HRMS send), each approver
about the corrections waiting on them for more than two days, and HR when last
month is still open after the 3rd — and stops saying so as soon as the work is
done.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import tenant_scoped
from app.models.attendance import (
    AttendanceDay,
    AttendancePeriodLock,
    AttendanceRegularisation,
    AttendanceStatus,
    RegularisationStatus,
)
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import LeaveRequest, LeaveRequestStatus
from app.models.notifications import Channel, DigestMode, Notification
from app.models.user import User, UserRole
from app.services.notifications.dispatch import notify, register_event

__all__ = [
    "ABSENT_REPORT_REPEAT",
    "PENDING_AFTER",
    "UNLOCKED_AFTER_DAY",
    "open_absent_days",
    "send_absent_reports",
    "send_attendance_reminders",
]

#: A correction waiting longer than this is worth a reminder.
PENDING_AFTER = timedelta(hours=48)
#: From this day of the month, last month still being open is worth a reminder.
UNLOCKED_AFTER_DAY = 3
#: With no new day to report, the same open days are repeated this often.
ABSENT_REPORT_REPEAT = timedelta(days=7)

_ABSENT_LABELS: dict[AttendanceStatus, str] = {
    AttendanceStatus.NEEDS_REGULARISATION: "Missed punch",
    AttendanceStatus.ABSENT: "Full day absent",
}

register_event(
    "attendance.absent_report",
    "Reminder: absent report {employee_name} ({report_date})",
    "Dear {employee_name},\n\n"
    "Your attendance record shows you absent on the days below. Please check your "
    "attendance in AskMiraa (Time, Attendance) and regularise each day with a missed punch, "
    "attendance correction, on-duty or leave request, so payroll can run on time.\n\n"
    "{absent_days}\n\n"
    "For a missed punch, ask only for the punch you missed.\n\n"
    "Please ignore this email if you have already applied and are waiting for approval.\n\n"
    "Open your attendance: {link}",
    preferences={
        Channel.EMAIL: DigestMode.IMMEDIATE,
        Channel.IN_APP: DigestMode.IMMEDIATE,
        Channel.PUSH: DigestMode.IMMEDIATE,
    },
)
register_event(
    "attendance.regularisation.reminder",
    "{count} attendance correction(s) waiting on you",
    "{count} attendance correction(s) from your team have waited more than two days, the "
    "oldest from {oldest}. Payroll uses the corrected days, so decide them under Time, "
    "Attendance review.",
    preferences={Channel.EMAIL: DigestMode.IMMEDIATE, Channel.IN_APP: DigestMode.IMMEDIATE},
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


# ── Absent report: each person's own unexplained days ───────────────────────


async def _report_window(session: AsyncSession, actor: User, today: date) -> tuple[date, date]:
    """From the start of the open payroll month(s) to yesterday.

    Last month is included while it is not locked: it can still be corrected
    and payroll has not run on it. Today is not over, so it is never reported.
    """
    end = today - timedelta(days=1)
    start = today.replace(day=1)
    prev_start, prev_end = _previous_month(today)
    prev_locked = await session.scalar(
        tenant_scoped(
            select(AttendancePeriodLock.id).where(
                AttendancePeriodLock.period_start <= prev_start,
                AttendancePeriodLock.period_end >= prev_end,
                AttendancePeriodLock.unlocked_at.is_(None),
            ),
            AttendancePeriodLock,
            actor,
        )
    )
    if prev_locked is None:
        start = prev_start
    return start, end


async def open_absent_days(
    session: AsyncSession, actor: User, *, start: date, end: date
) -> dict[uuid.UUID, list[AttendanceDay]]:
    """Absent and missed-punch days nobody has explained yet, per employee.

    A day drops out once it is regularised, once a correction or on-duty /
    work-from-home request is raised for it (waiting or approved), once leave
    covering it is asked for or granted, or once it falls in a locked period.
    Only people still employed (active, on notice or on leave) are reported.
    """
    if end < start:
        return {}
    rows = (
        (
            await session.execute(
                tenant_scoped(
                    select(AttendanceDay)
                    .join(Employee, Employee.id == AttendanceDay.employee_id)
                    .where(
                        AttendanceDay.attendance_date >= start,
                        AttendanceDay.attendance_date <= end,
                        AttendanceDay.status.in_(list(_ABSENT_LABELS)),
                        AttendanceDay.is_regularised.is_(False),
                        Employee.status.in_(
                            [
                                EmploymentStatus.ACTIVE,
                                EmploymentStatus.ON_NOTICE,
                                EmploymentStatus.ON_LEAVE,
                            ]
                        ),
                        Employee.deleted_at.is_(None),
                    )
                    .order_by(AttendanceDay.employee_id, AttendanceDay.attendance_date),
                    AttendanceDay,
                    actor,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return {}
    employee_ids = {r.employee_id for r in rows}

    asked = {
        (employee_id, day)
        for employee_id, day in (
            await session.execute(
                tenant_scoped(
                    select(
                        AttendanceRegularisation.employee_id,
                        AttendanceRegularisation.attendance_date,
                    ).where(
                        AttendanceRegularisation.employee_id.in_(employee_ids),
                        AttendanceRegularisation.attendance_date >= start,
                        AttendanceRegularisation.attendance_date <= end,
                        AttendanceRegularisation.status.in_(
                            [RegularisationStatus.PENDING, RegularisationStatus.APPROVED]
                        ),
                    ),
                    AttendanceRegularisation,
                    actor,
                )
            )
        ).all()
    }
    leave = (
        await session.execute(
            tenant_scoped(
                select(
                    LeaveRequest.employee_id, LeaveRequest.start_date, LeaveRequest.end_date
                ).where(
                    LeaveRequest.employee_id.in_(employee_ids),
                    LeaveRequest.start_date <= end,
                    LeaveRequest.end_date >= start,
                    LeaveRequest.status.in_(
                        [LeaveRequestStatus.PENDING, LeaveRequestStatus.APPROVED]
                    ),
                ),
                LeaveRequest,
                actor,
            )
        )
    ).all()
    locks = (
        await session.execute(
            tenant_scoped(
                select(AttendancePeriodLock.period_start, AttendancePeriodLock.period_end).where(
                    AttendancePeriodLock.period_start <= end,
                    AttendancePeriodLock.period_end >= start,
                    AttendancePeriodLock.unlocked_at.is_(None),
                ),
                AttendancePeriodLock,
                actor,
            )
        )
    ).all()

    open_days: dict[uuid.UUID, list[AttendanceDay]] = defaultdict(list)
    for row in rows:
        day = row.attendance_date
        if (row.employee_id, day) in asked:
            continue
        if any(e == row.employee_id and s <= day <= f for e, s, f in leave):
            continue
        if any(s <= day <= f for s, f in locks):
            continue
        open_days[row.employee_id].append(row)
    return dict(open_days)


async def _last_report(
    session: AsyncSession, actor: User, recipient: uuid.UUID
) -> Notification | None:
    return (
        await session.scalars(
            tenant_scoped(
                select(Notification)
                .where(
                    Notification.event_key == "attendance.absent_report",
                    Notification.recipient_user_id == recipient,
                )
                .order_by(Notification.created_at.desc())
                .limit(1),
                Notification,
                actor,
            )
        )
    ).first()


def _attendance_link() -> str:
    base = settings.WEB_APP_URL or (settings.CORS_ORIGINS[0] if settings.CORS_ORIGINS else "")
    return f"{base.rstrip('/')}/time/attendance"


async def send_absent_reports(
    session: AsyncSession, actor: User, *, today: date, now: datetime | None = None
) -> dict[str, int]:
    """Scheduler-callable: email each person the days they still have to explain.

    Like the "Absent report" reminder of older HRMS: one email per person
    listing every absent and missed-punch day of the open month(s) that has no
    correction, on-duty, work-from-home or leave request against it. A person
    hears again as soon as a new day joins the list, and otherwise once a week
    while days stay open; nothing is sent once every day is explained.

    Switched off for the workspace by `absent_report_emails` in the attendance
    settings.
    """
    from app.services.attendance.setup import attendance_settings

    if not (await attendance_settings(session, actor)).absent_report_emails:
        return {"people_reminded": 0, "people_with_open_days": 0}

    moment = now or datetime.now(UTC)
    start, end = await _report_window(session, actor, today)
    per_employee = await open_absent_days(session, actor, start=start, end=end)
    if not per_employee:
        return {"people_reminded": 0, "people_with_open_days": 0}

    logins = (
        await session.execute(
            tenant_scoped(
                select(User.id, User.employee_id).where(
                    User.employee_id.in_(list(per_employee)), User.is_active.is_(True)
                ),
                User,
                actor,
            )
        )
    ).all()
    login_of: dict[uuid.UUID, uuid.UUID] = {}
    for user_id, employee_id in logins:
        login_of.setdefault(employee_id, user_id)
    names = {
        e.id: e.full_name
        for e in (
            await session.scalars(
                tenant_scoped(
                    select(Employee).where(Employee.id.in_(list(per_employee))), Employee, actor
                )
            )
        ).all()
    }

    link = _attendance_link()
    reminded = 0
    for employee_id, days in per_employee.items():
        recipient = login_of.get(employee_id)
        if recipient is None:
            continue
        latest = max(d.attendance_date for d in days)
        last = await _last_report(session, actor, recipient)
        if last is not None:
            told_up_to = (last.payload or {}).get("latest_day")
            nothing_new = told_up_to is not None and latest.isoformat() <= told_up_to
            if nothing_new and last.created_at > moment - ABSENT_REPORT_REPEAT:
                continue
        await notify(
            session,
            tenant_id=actor.tenant_id,
            event_key="attendance.absent_report",
            recipient_user_ids=[recipient],
            payload={
                "employee_name": names.get(employee_id, ""),
                "report_date": f"{today:%d-%b-%Y}",
                "absent_days": "\n".join(
                    f"- {d.attendance_date:%d-%b-%Y} ({_ABSENT_LABELS[d.status]})" for d in days
                ),
                "count": len(days),
                "latest_day": latest.isoformat(),
                "link": link,
            },
            commit=False,
            deliver=False,
        )
        reminded += 1
    await session.commit()
    return {"people_reminded": reminded, "people_with_open_days": len(per_employee)}
