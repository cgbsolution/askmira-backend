"""People analytics the report builder cannot express as a single query.

A rate needs a denominator from another point in time (attrition is exits over
*average* headcount, not over today's), a trend needs one value per month, and
a signal needs several facts about one person put side by side. These are
computed here, from the same tables, and scoped the same way: HR sees the
company, a line manager their reporting line, anybody else only themselves.

**Signals, not predictions.** The absence and burnout signals are rules with
their evidence attached — "no leave for 140 days, 42 hours of overtime in the
last 30" — never a number out of 100. The one score here, the Bradford factor,
is arithmetic on the attendance record, shown with both of its inputs. A manager
can check every one of them against the record, and nothing here claims to
predict anything (FS-ANL-02).
"""

from __future__ import annotations

import bisect
import calendar
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.attendance import AttendanceDay, AttendanceStatus
from app.models.employee import Employee, EmploymentStatus
from app.models.exit_workflow import ExitRecord, ExitStatus, ExitType
from app.models.leave import LeaveRequest, LeaveRequestStatus, LeaveType
from app.models.org import Department
from app.models.payroll import EmployeeSalary
from app.models.user import User, UserRole
from app.services.notifications.dispatch import notify, register_event

__all__ = [
    "absence_scores",
    "absence_signals",
    "attrition",
    "bradford_factor",
    "dashboard",
    "headcount_trend",
    "leave_trend",
    "month_ends",
    "pin_report",
    "send_signal_digest",
    "unpin_report",
]

#: Exit types an employee chose. Everything else is the employer's or nobody's.
VOLUNTARY = frozenset({ExitType.RESIGNATION, ExitType.RETIREMENT})

# Signal thresholds. Rules, stated, and shown beside every signal they raise.
NO_LEAVE_DAYS = 120
OVERTIME_HOURS_30D = 30
BRIDGE_ABSENCES_90D = 3
LOP_DAYS_90D = 3
#: A Bradford factor at or above this is raised as a signal. Stated beside it.
BRADFORD_REVIEW = 200
#: Months without a pay revision before it is raised (admins only: it is pay data).
PAY_REVISION_MONTHS = 18

UNPLANNED = (AttendanceStatus.ABSENT, AttendanceStatus.LOP)
WORKED = (AttendanceStatus.PRESENT, AttendanceStatus.HALF_DAY)
BRADFORD_METHOD = (
    "Bradford factor = spells squared times days, over unplanned absence (days marked absent or "
    "loss of pay) in the last 52 weeks. Approved leave never counts. A spell is a run of "
    "absent days not broken by a day worked."
)


def _months_between(start: date, end: date) -> int:
    """Whole months from start to end."""
    months = (end.year - start.year) * 12 + (end.month - start.month)
    return months - 1 if end.day < start.day else months


def _pay_revision_signal(
    person: Employee, last_revision: date | None, today: date
) -> dict[str, Any] | None:
    """No salary change for a long time: a fact about pay, not a guess about intent."""
    revised = last_revision is not None and last_revision != person.date_of_joining
    since = last_revision if revised else person.date_of_joining
    if since is None:
        return None
    months = _months_between(since, today)
    if months < PAY_REVISION_MONTHS:
        return None
    lead = (
        f"Pay last revised effective {since:%d %b %Y}"
        if revised
        else f"No revision since joining on {since:%d %b %Y}"
    )
    return {
        "kind": "pay_revision",
        "label": "No pay revision in a long time",
        "rule": f"No salary revision for {PAY_REVISION_MONTHS}+ months.",
        "evidence": [f"{lead}, {months} months ago"],
    }


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def month_ends(today: date, months: int) -> list[date]:
    """The last day of each of the `months` months ending with today's month."""
    out: list[date] = []
    year, month = today.year, today.month
    for _ in range(months):
        out.append(_month_end(year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(out))


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator <= 0:
        return None
    return (numerator / denominator * Decimal(100)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )


async def _people(session: AsyncSession, user: User) -> list[Employee]:
    """Everybody the caller may see, including leavers (who are the point of attrition)."""
    visible = await permissions.visible_employee_ids(session, user)
    stmt = select(Employee).where(Employee.deleted_at.is_(None))
    if visible is not None:
        stmt = stmt.where(Employee.id.in_(visible or {uuid.uuid4()}))
    return list((await session.scalars(tenant_scoped(stmt, Employee, user))).all())


def _average_headcount(people: list[Employee], month_end: date) -> Decimal:
    """The mean of a month's opening and closing headcount."""
    day_before = month_end.replace(day=1) - timedelta(days=1)
    opening = sum(1 for p in people if _employed_on(p, day_before))
    closing = sum(1 for p in people if _employed_on(p, month_end))
    return Decimal(opening + closing) / Decimal(2)


def _employed_on(employee: Employee, day: date) -> bool:
    joined = employee.date_of_joining
    left = employee.date_of_exit
    if joined is None or joined > day:
        return False
    return left is None or left >= day


async def headcount_trend(
    session: AsyncSession, user: User, *, today: date, months: int = 12
) -> list[dict[str, Any]]:
    """Headcount at each month end, with the month's joiners and leavers."""
    people = await _people(session, user)
    rows = []
    for end in month_ends(today, months):
        start = end.replace(day=1)
        rows.append(
            {
                "month": end.strftime("%Y-%m"),
                "headcount": sum(1 for p in people if _employed_on(p, end)),
                "joiners": sum(
                    1 for p in people if p.date_of_joining and start <= p.date_of_joining <= end
                ),
                "leavers": sum(
                    1 for p in people if p.date_of_exit and start <= p.date_of_exit <= end
                ),
            }
        )
    return rows


async def attrition(
    session: AsyncSession,
    user: User,
    *,
    today: date,
    months: int = 12,
    by_department: bool = False,
) -> dict[str, Any]:
    """Attrition as a rate: exits over average headcount, monthly and for the window.

    Voluntary attrition (resignation, retirement) is reported apart from the
    total, because a company losing people it wanted to keep and a company
    restructuring are different conversations.
    """
    people = await _people(session, user)
    exit_types = {
        row.employee_id: row.exit_type
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(ExitRecord).where(
                        ExitRecord.employee_id.in_([p.id for p in people] or [uuid.uuid4()]),
                        ExitRecord.status != ExitStatus.CANCELLED,
                    ),
                    ExitRecord,
                    user,
                )
            )
        ).all()
    }
    ends = month_ends(today, months)
    monthly = []
    for end in ends:
        start = end.replace(day=1)
        opening = sum(1 for p in people if _employed_on(p, start - timedelta(days=1)))
        closing = sum(1 for p in people if _employed_on(p, end))
        leavers = [p for p in people if p.date_of_exit and start <= p.date_of_exit <= end]
        voluntary = [p for p in leavers if exit_types.get(p.id) in VOLUNTARY]
        average = Decimal(opening + closing) / Decimal(2)
        monthly.append(
            {
                "month": end.strftime("%Y-%m"),
                "average_headcount": str(average.quantize(Decimal("0.1"))),
                "exits": len(leavers),
                "voluntary_exits": len(voluntary),
                "rate_percent": str(_pct(Decimal(len(leavers)), average) or "0.0"),
                "voluntary_rate_percent": str(_pct(Decimal(len(voluntary)), average) or "0.0"),
            }
        )

    window_start = ends[0].replace(day=1)
    window_end = ends[-1]
    exits = [p for p in people if p.date_of_exit and window_start <= p.date_of_exit <= window_end]
    average = sum(
        (Decimal(row["average_headcount"]) for row in monthly), start=Decimal(0)
    ) / Decimal(len(monthly) or 1)
    voluntary_exits = [p for p in exits if exit_types.get(p.id) in VOLUNTARY]
    result: dict[str, Any] = {
        "from": window_start.isoformat(),
        "to": window_end.isoformat(),
        "average_headcount": str(average.quantize(Decimal("0.1"))),
        "exits": len(exits),
        "voluntary_exits": len(voluntary_exits),
        "attrition_percent": str(_pct(Decimal(len(exits)), average) or "0.0"),
        "voluntary_attrition_percent": str(
            _pct(Decimal(len(voluntary_exits)), average) or "0.0"
        ),
        "basis": (
            f"Exits from {window_start:%d %b %Y} to {window_end:%d %b %Y} over the average of "
            "each month's opening and closing headcount."
        ),
        "monthly": monthly,
    }
    if by_department:
        names = {
            row.id: row.name
            for row in (
                await session.scalars(tenant_scoped(select(Department), Department, user))
            ).all()
        }
        groups: dict[str, list[Employee]] = defaultdict(list)
        for person in people:
            groups[names.get(person.department_id, "No department")].append(person)
        departments = []
        for name, members in sorted(groups.items()):
            dept_exits = [
                p
                for p in members
                if p.date_of_exit and window_start <= p.date_of_exit <= window_end
            ]
            dept_avg = sum(
                (_average_headcount(members, end) for end in ends), start=Decimal(0)
            ) / Decimal(len(ends) or 1)
            departments.append(
                {
                    "department": name,
                    "average_headcount": str(dept_avg.quantize(Decimal("0.1"))),
                    "exits": len(dept_exits),
                    "attrition_percent": str(_pct(Decimal(len(dept_exits)), dept_avg) or "0.0"),
                }
            )
        result["by_department"] = departments
    return result


async def leave_trend(
    session: AsyncSession, user: User, *, today: date, months: int = 12
) -> dict[str, Any]:
    """Approved leave days by month and type, counted in the month the leave starts."""
    visible = await permissions.visible_employee_ids(session, user)
    ends = month_ends(today, months)
    start = ends[0].replace(day=1)
    stmt = (
        select(LeaveRequest, LeaveType.name)
        .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
        .where(
            LeaveRequest.status == LeaveRequestStatus.APPROVED,
            LeaveRequest.start_date >= start,
            LeaveRequest.start_date <= ends[-1],
        )
    )
    if visible is not None:
        stmt = stmt.where(LeaveRequest.employee_id.in_(visible or {uuid.uuid4()}))
    rows = (await session.execute(tenant_scoped(stmt, LeaveRequest, user))).all()
    types = sorted({name for _, name in rows})
    by_month: dict[str, dict[str, Decimal]] = {
        end.strftime("%Y-%m"): defaultdict(Decimal) for end in ends
    }
    for leave, type_name in rows:
        key = leave.start_date.strftime("%Y-%m")
        if key in by_month:
            by_month[key][type_name] += Decimal(str(leave.total_days))
    return {
        "leave_types": types,
        "months": [
            {"month": month, **{t: str(values.get(t, Decimal(0))) for t in types}}
            for month, values in by_month.items()
        ],
    }


@dataclass(slots=True)
class _Facts:
    last_leave_end: date | None = None
    overtime_minutes_30d: int = 0
    bridge_absences: list[str] = field(default_factory=list)
    lop_days_90d: Decimal = Decimal(0)
    spells: int = 0
    absent_days: Decimal = Decimal(0)
    last_pay_revision: date | None = None


def bradford_factor(spells: int, days: Decimal) -> Decimal:
    """Spells squared times days. Many short absences score far higher than one long one."""
    return (Decimal(spells * spells) * days).quantize(Decimal(1), rounding=ROUND_HALF_UP)


def count_spells(absent: list[date], worked: list[date]) -> int:
    """Runs of absent days (both sorted); a day worked between two absences starts a new run."""
    spells = 0
    previous: date | None = None
    for day in absent:
        if previous is None:
            spells += 1
        else:
            i = bisect.bisect_right(worked, previous)
            if i < len(worked) and worked[i] < day:
                spells += 1
        previous = day
    return spells


async def _unplanned_absence(
    session: AsyncSession, user: User, ids: list[uuid.UUID], today: date
) -> dict[uuid.UUID, tuple[int, Decimal]]:
    """Spells and days of unplanned absence per person over the last 52 weeks."""
    since = today - timedelta(days=364)
    rows = (
        await session.execute(
            tenant_scoped(
                select(
                    AttendanceDay.employee_id,
                    AttendanceDay.attendance_date,
                    AttendanceDay.payable_fraction,
                ).where(
                    AttendanceDay.employee_id.in_(ids or [uuid.uuid4()]),
                    AttendanceDay.attendance_date >= since,
                    AttendanceDay.attendance_date <= today,
                    AttendanceDay.status.in_(UNPLANNED),
                ),
                AttendanceDay,
                user,
            )
        )
    ).all()
    absent: dict[uuid.UUID, list[date]] = defaultdict(list)
    days: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    for employee_id, day, payable in rows:
        absent[employee_id].append(day)
        days[employee_id] += Decimal(1) - Decimal(str(payable or 0))
    if not absent:
        return {}

    # Worked days, only for people with absences and only from their first one.
    first = min(min(v) for v in absent.values())
    worked_rows = (
        await session.execute(
            tenant_scoped(
                select(AttendanceDay.employee_id, AttendanceDay.attendance_date).where(
                    AttendanceDay.employee_id.in_(list(absent)),
                    AttendanceDay.attendance_date >= first,
                    AttendanceDay.attendance_date <= today,
                    AttendanceDay.status.in_(WORKED),
                ),
                AttendanceDay,
                user,
            )
        )
    ).all()
    worked: dict[uuid.UUID, list[date]] = defaultdict(list)
    for employee_id, day in worked_rows:
        worked[employee_id].append(day)

    return {
        employee_id: (
            count_spells(sorted(dates), sorted(worked.get(employee_id, []))),
            days[employee_id],
        )
        for employee_id, dates in absent.items()
    }


async def absence_scores(session: AsyncSession, user: User, *, today: date) -> dict[str, Any]:
    """The Bradford factor for everyone with unplanned absence in the last 52 weeks.

    A score, so it comes with both of its inputs and the formula; it is
    arithmetic on the attendance record, not a prediction.
    """
    people = {
        p.id: p for p in await _people(session, user) if p.status != EmploymentStatus.EXITED
    }
    absence = await _unplanned_absence(session, user, list(people), today)
    rows = []
    for employee_id, (spells, days) in absence.items():
        if days <= 0:
            continue
        person = people[employee_id]
        factor = bradford_factor(spells, days)
        rows.append(
            {
                "employee_id": str(employee_id),
                "employee_code": person.employee_code,
                "employee_name": person.full_name,
                "spells": spells,
                "days": str(days.normalize()),
                "bradford_factor": int(factor),
                "at_or_above_review": factor >= BRADFORD_REVIEW,
            }
        )
    rows.sort(key=lambda r: (-r["bradford_factor"], r["employee_name"]))
    return {
        "method": BRADFORD_METHOD,
        "review_threshold": BRADFORD_REVIEW,
        "from": (today - timedelta(days=364)).isoformat(),
        "to": today.isoformat(),
        "people": rows,
    }


async def absence_signals(session: AsyncSession, user: User, *, today: date) -> list[dict]:
    """Explainable, rule-based signals per person: possible burnout, absence patterns.

    Each signal carries the rule it applied and the facts that triggered it.
    A person appears only if at least one rule fires.
    """
    people = [
        p
        for p in await _people(session, user)
        if p.status != EmploymentStatus.EXITED and p.date_of_joining
        and p.date_of_joining <= today - timedelta(days=NO_LEAVE_DAYS)
    ]
    if not people:
        return []
    ids = [p.id for p in people]
    facts: dict[uuid.UUID, _Facts] = {pid: _Facts() for pid in ids}

    leaves = (
        await session.scalars(
            tenant_scoped(
                select(LeaveRequest).where(
                    LeaveRequest.employee_id.in_(ids),
                    LeaveRequest.status == LeaveRequestStatus.APPROVED,
                    LeaveRequest.start_date <= today,
                ),
                LeaveRequest,
                user,
            )
        )
    ).all()
    for leave in leaves:
        current = facts[leave.employee_id].last_leave_end
        if current is None or leave.end_date > current:
            facts[leave.employee_id].last_leave_end = min(leave.end_date, today)

    days = (
        await session.scalars(
            tenant_scoped(
                select(AttendanceDay).where(
                    AttendanceDay.employee_id.in_(ids),
                    AttendanceDay.attendance_date >= today - timedelta(days=90),
                    AttendanceDay.attendance_date <= today,
                    or_(
                        AttendanceDay.overtime_minutes > 0,
                        AttendanceDay.status.in_([AttendanceStatus.LOP, AttendanceStatus.ABSENT]),
                        and_(
                            AttendanceDay.status == AttendanceStatus.ON_LEAVE,
                            AttendanceDay.attendance_date >= today - timedelta(days=90),
                        ),
                    ),
                ),
                AttendanceDay,
                user,
            )
        )
    ).all()
    for employee_id, (spells, absent_days) in (
        await _unplanned_absence(session, user, ids, today)
    ).items():
        facts[employee_id].spells = spells
        facts[employee_id].absent_days = absent_days

    # When pay last changed is payroll data: only an admin is shown this signal.
    show_pay = permissions.is_admin(user)
    if show_pay:
        revisions = (
            await session.execute(
                tenant_scoped(
                    select(EmployeeSalary.employee_id, EmployeeSalary.effective_from).where(
                        EmployeeSalary.employee_id.in_(ids),
                        EmployeeSalary.effective_from <= today,
                    ),
                    EmployeeSalary,
                    user,
                )
            )
        ).all()
        for employee_id, effective_from in revisions:
            current = facts[employee_id].last_pay_revision
            if current is None or effective_from > current:
                facts[employee_id].last_pay_revision = effective_from

    for day in days:
        f = facts[day.employee_id]
        if day.attendance_date >= today - timedelta(days=30):
            f.overtime_minutes_30d += int(day.overtime_minutes or 0)
        if day.status in (AttendanceStatus.LOP, AttendanceStatus.ABSENT):
            f.lop_days_90d += Decimal(1) - Decimal(str(day.payable_fraction or 0))
        # Monday or Friday off: the day that turns a weekend into a long one.
        off = (AttendanceStatus.LOP, AttendanceStatus.ABSENT, AttendanceStatus.ON_LEAVE)
        if day.status in off and day.attendance_date.isoweekday() in (1, 5):
            f.bridge_absences.append(day.attendance_date.isoformat())

    out = []
    for person in people:
        f = facts[person.id]
        signals = []
        reference = f.last_leave_end or person.date_of_joining
        days_without = (today - reference).days if reference else 0
        overtime_hours = round(f.overtime_minutes_30d / 60, 1)
        if days_without >= NO_LEAVE_DAYS and overtime_hours >= OVERTIME_HOURS_30D:
            signals.append(
                {
                    "kind": "burnout_risk",
                    "label": "Long stretch without leave, with heavy overtime",
                    "rule": (
                        f"No leave for {NO_LEAVE_DAYS}+ days and {OVERTIME_HOURS_30D}+ hours of "
                        "overtime in the last 30 days."
                    ),
                    "evidence": [
                        f"No approved leave for {days_without} days"
                        + (f" (last ended {reference:%d %b %Y})" if f.last_leave_end else ""),
                        f"{overtime_hours} hours of overtime in the last 30 days",
                    ],
                }
            )
        elif days_without >= NO_LEAVE_DAYS:
            signals.append(
                {
                    "kind": "no_leave",
                    "label": "No leave in a long time",
                    "rule": f"No approved leave for {NO_LEAVE_DAYS}+ days.",
                    "evidence": [f"No approved leave for {days_without} days"],
                }
            )
        if len(f.bridge_absences) >= BRIDGE_ABSENCES_90D:
            signals.append(
                {
                    "kind": "bridge_absences",
                    "label": "Absences next to weekends",
                    "rule": (
                        f"{BRIDGE_ABSENCES_90D}+ days off on a Monday or Friday in the last "
                        "90 days."
                    ),
                    "evidence": [f"Off on {', '.join(sorted(f.bridge_absences)[-6:])}"],
                }
            )
        if f.lop_days_90d >= LOP_DAYS_90D:
            signals.append(
                {
                    "kind": "unpaid_absence",
                    "label": "Repeated unpaid absence",
                    "rule": f"{LOP_DAYS_90D}+ days of loss of pay in the last 90 days.",
                    "evidence": [f"{f.lop_days_90d.normalize()} day(s) of loss of pay in 90 days"],
                }
            )
        factor = bradford_factor(f.spells, f.absent_days)
        if factor >= BRADFORD_REVIEW:
            signals.append(
                {
                    "kind": "frequent_absence",
                    "label": "Frequent short absences",
                    "rule": f"Bradford factor {BRADFORD_REVIEW}+. {BRADFORD_METHOD}",
                    "evidence": [
                        f"{f.spells} spell(s) of unplanned absence in 52 weeks",
                        f"{f.absent_days.normalize()} day(s) in total",
                        f"Bradford factor {f.spells}^2 x {f.absent_days.normalize()} = {factor}",
                    ],
                }
            )
        if show_pay:
            signal = _pay_revision_signal(person, f.last_pay_revision, today)
            if signal is not None:
                signals.append(signal)
        if signals:
            out.append(
                {
                    "employee_id": str(person.id),
                    "employee_code": person.employee_code,
                    "employee_name": person.full_name,
                    "signals": signals,
                }
            )
    return out


# ── The dashboard: saved reports a person pinned ────────────────────────────


async def dashboard(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """The caller's pinned reports, in order, with what each needs to render."""
    from app.models.reporting import DashboardWidget, Report

    rows = (
        await session.execute(
            tenant_scoped(
                select(DashboardWidget, Report)
                .join(Report, Report.id == DashboardWidget.report_id)
                .where(DashboardWidget.user_id == user.id)
                .order_by(DashboardWidget.position, DashboardWidget.created_at),
                DashboardWidget,
                user,
            )
        )
    ).all()
    return [
        {
            "id": str(widget.id),
            "report_id": str(report.id),
            "name": report.name,
            "description": report.description,
            "chart_type": report.chart_type.value,
            "position": widget.position,
        }
        for widget, report in rows
    ]


async def pin_report(session: AsyncSession, user: User, report_id: uuid.UUID) -> list[dict]:
    """Pin a report the caller can open. Pinning twice is a no-op."""
    from fastapi import HTTPException, status

    from app.models.reporting import DashboardWidget
    from app.services.reporting import service as report_service

    await report_service.visible_report(session, user, report_id)
    existing = await session.scalar(
        tenant_scoped(
            select(DashboardWidget.id).where(
                DashboardWidget.user_id == user.id, DashboardWidget.report_id == report_id
            ),
            DashboardWidget,
            user,
        )
    )
    if existing is None:
        count = len(await dashboard(session, user))
        if count >= 24:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A dashboard holds up to 24 reports. Unpin one first.",
            )
        session.add(
            DashboardWidget(
                tenant_id=user.tenant_id, user_id=user.id, report_id=report_id, position=count
            )
        )
        await session.commit()
    return await dashboard(session, user)


async def unpin_report(session: AsyncSession, user: User, widget_id: uuid.UUID) -> list[dict]:
    from app.models.reporting import DashboardWidget

    widget = await session.scalar(
        tenant_scoped(
            select(DashboardWidget).where(
                DashboardWidget.id == widget_id, DashboardWidget.user_id == user.id
            ),
            DashboardWidget,
            user,
        )
    )
    if widget is not None:
        await session.delete(widget)
        await session.commit()
    return await dashboard(session, user)


# ── The weekly digest: signals pushed, not only waited for ──────────────────

#: Pay is payroll data; the digest goes to sub-admins and managers too.
DIGEST_KINDS = frozenset({"burnout_risk", "no_leave", "bridge_absences", "unpaid_absence",
                          "frequent_absence"})
DIGEST_WEEKDAY = 1  # Monday

register_event(
    "analytics.signals.digest",
    "{count} people flagged in this week's people signals",
    "{summary}. Each comes with the rule it applied and the record behind it under "
    "Reports, Analytics. They are rules, not predictions: check before acting.",
)
register_event(
    "analytics.signals.team",
    "People signals in your team this week",
    "{summary}. Open Reports, Analytics for the rule and the record behind each one.",
)


def _summary(people: list[dict]) -> str:
    return "; ".join(
        f"{person['employee_name']}: {', '.join(s['label'].lower() for s in person['signals'])}"
        for person in people[:10]
    ) + (f"; and {len(people) - 10} more" if len(people) > 10 else "")


async def send_signal_digest(session: AsyncSession, actor: User, *, today: date) -> dict[str, int]:
    """Once a week, tell HR and each line manager who is flagged. Scheduler-callable.

    Sends nothing when nothing is flagged; leaves out the pay signal, which only
    an admin may see.
    """
    if today.isoweekday() != DIGEST_WEEKDAY:
        return {"skipped": 1}
    flagged = []
    for person in await absence_signals(session, actor, today=today):
        kept = [s for s in person["signals"] if s["kind"] in DIGEST_KINDS]
        if kept:
            flagged.append({**person, "signals": kept})
    if not flagged:
        return {"flagged": 0, "notified": 0}

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
    notified = 0
    if hr:
        await notify(
            session,
            tenant_id=actor.tenant_id,
            event_key="analytics.signals.digest",
            recipient_user_ids=hr,
            payload={"count": len(flagged), "summary": _summary(flagged)},
            commit=False,
        )
        notified += len(hr)

    # Each flagged person's own manager hears about their own team only.
    flagged_ids = [uuid.UUID(p["employee_id"]) for p in flagged]
    managers = {
        employee_id: manager_user
        for employee_id, manager_user in (
            await session.execute(
                tenant_scoped(
                    select(Employee.id, User.id)
                    .join(User, User.employee_id == Employee.manager_id)
                    .where(Employee.id.in_(flagged_ids), User.is_active.is_(True)),
                    Employee,
                    actor,
                )
            )
        ).all()
    }
    by_manager: dict[uuid.UUID, list[dict]] = defaultdict(list)
    for person in flagged:
        manager = managers.get(uuid.UUID(person["employee_id"]))
        if manager is not None and manager not in hr:
            by_manager[manager].append(person)
    for manager, people in by_manager.items():
        await notify(
            session,
            tenant_id=actor.tenant_id,
            event_key="analytics.signals.team",
            recipient_user_ids=[manager],
            payload={"summary": _summary(people)},
            commit=False,
        )
        notified += 1
    await session.commit()
    return {"flagged": len(flagged), "notified": notified}
