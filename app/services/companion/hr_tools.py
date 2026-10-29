"""Ask Mira's HR and founder questions: approvals, payroll figures, joiners and exits,
the dates coming up, and the HR helpdesk.

Each answer is read here, as the person asking, through the same rules the
module's own pages use, and goes to the screen as cards. Nothing here is given
to a model.

- What is waiting on me: the approvals inbox (approvals.service.inbox), which
  already shows a person only what they may decide, never their own request.
- Payroll figures: worked out from the payslips of the month's runs, so a
  sub-admin narrowed to some departments sees only their people's pay (hr_scope
  narrows every employee-keyed query). Needs the right to view payroll.
- Joiners, exits, notice and attrition; birthdays, work anniversaries, probation
  ends and last working days: from the employee records. Admins and sub-admins
  with the right to view people.
- HR helpdesk tickets: the person's own (helpdesk.service.list_my_tickets), or
  the queue they work (list_queue refuses anybody who is not an agent).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.employee import Employee, EmploymentStatus
from app.models.org import Department, LegalEntity
from app.models.payroll import (
    EmployeeSalary,
    PayrollRun,
    PayrollRunStatus,
    PayrollRunType,
    Payslip,
)
from app.models.user import User
from app.services.companion.people_tools import may_view
from app.services.documents.indian_numbers import format_inr

__all__ = [
    "APPROVAL_KINDS",
    "DATE_KINDS",
    "KINDS",
    "approvals",
    "hr_tickets",
    "payroll_summary",
    "people_changes",
    "people_dates",
]

#: What `kind` narrows the approvals inbox to, by the inbox's source keys.
APPROVAL_KINDS: dict[str, frozenset[str]] = {
    "leave": frozenset({"leave", "leave_comp_off"}),
    "attendance": frozenset({"attendance_regularisation", "overtime", "shift_swap", "timesheet"}),
    "expense": frozenset(
        {
            "expense_claim",
            "travel_request",
            "expense_claim_manager",
            "travel_request_manager",
            "reimbursement_claim",
        }
    ),
    "payroll": frozenset({"payroll_adjustment", "loan", "settlement", "payroll_run", "tax_proof"}),
    "hiring": frozenset({"requisition", "offer"}),
    "exit": frozenset({"resignation", "clearance_task", "settlement"}),
    "people": frozenset(
        {"lifecycle_event", "profile_change", "document_verification", "asset_request"}
    ),
}

#: What `kind` picks among the dates coming up.
DATE_KINDS = ("birthdays", "anniversaries", "probation", "last_working_day")

#: Every value `kind` may take, for the model's schema.
KINDS = ("none", *APPROVAL_KINDS, *DATE_KINDS)

_EMPLOYED = (EmploymentStatus.ACTIVE, EmploymentStatus.ON_NOTICE, EmploymentStatus.ON_LEAVE)

_RUN_TYPE = {
    PayrollRunType.REGULAR: "Regular",
    PayrollRunType.OFF_CYCLE: "Off-cycle",
    PayrollRunType.ARREAR: "Arrears",
    PayrollRunType.FULL_AND_FINAL: "Full & final",
    PayrollRunType.BONUS: "Bonus",
}

_RUN_STATUS = {
    PayrollRunStatus.DRAFT: "Draft",
    PayrollRunStatus.PROCESSING: "Processing",
    PayrollRunStatus.PENDING_REVIEW: "Waiting for approval",
    PayrollRunStatus.APPROVED: "Approved",
    PayrollRunStatus.PAID: "Paid",
    PayrollRunStatus.FAILED: "Failed",
    PayrollRunStatus.CANCELLED: "Cancelled",
}


# ── Cards ────────────────────────────────────────────────────────────────────


def _notice(text: str, tone: str = "info") -> dict[str, Any]:
    return {"type": "notice", "tone": tone, "text": text}


def _record(
    title: str,
    *,
    heading: str = "",
    figures: list[tuple[str, str]] | None = None,
    columns: list[str] | None = None,
    rows: list[list[Any]] | None = None,
    note: str = "",
    open_href: str = "",
    open_label: str = "",
) -> dict[str, Any]:
    card: dict[str, Any] = {
        "type": "record",
        "title": title,
        "subtitle": "",
        "heading": heading,
        "figures": [{"label": label, "value": value} for label, value in figures or []],
        "note": note,
    }
    if columns and rows:
        card["table"] = {"columns": columns, "rows": rows}
    if open_href:
        card["open"] = open_href
        card["open_label"] = open_label or "Open the page"
    return card


def _table(
    title: str, columns: list[str], rows: list[list[Any]], *, caption: str, open_href: str = ""
) -> dict[str, Any]:
    card: dict[str, Any] = {
        "type": "table",
        "title": title,
        "caption": caption,
        "columns": columns,
        "rows": rows,
    }
    if open_href:
        card["open"] = open_href
    return card


def _day(value: date | None) -> str:
    return f"{value:%d %b %Y}" if value else "—"


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0))


def _parse(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def _span(inputs: dict[str, Any], *, default: tuple[date, date]) -> tuple[date, date]:
    """The range asked about, at most a year; `default` when none was named."""
    start, end = _parse(inputs.get("from")), _parse(inputs.get("to"))
    if start is None and end is None:
        return default
    start = start or end
    end = end or start
    assert start is not None and end is not None
    if end < start:
        start, end = end, start
    return start, min(end, start + timedelta(days=366))


def _month_of(day: date) -> tuple[date, date]:
    first = day.replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    return first, following - timedelta(days=1)


def _span_text(start: date, end: date) -> str:
    return _day(start) if start == end else f"{_day(start)} to {_day(end)}"


async def _departments(
    session: AsyncSession, user: User, ids: set[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    rows = await session.execute(
        tenant_scoped(
            select(Department.id, Department.name).where(Department.id.in_(wanted)),
            Department,
            user,
        )
    )
    return {row.id: row.name for row in rows}


# ── What is waiting on me ────────────────────────────────────────────────────


async def approvals(
    session: AsyncSession, user: User, inputs: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """What is waiting on this person's decision, across modules."""
    from app.services.approvals.service import inbox

    kind = str(inputs.get("kind") or "none")
    keys = APPROVAL_KINDS.get(kind)
    found = await inbox(session, user)
    items = [item for item in found.items if keys is None or item.source in keys]
    used = {"kind": kind} if keys else {}
    label = {
        "leave": "leave",
        "attendance": "attendance and timesheet",
        "expense": "expense",
        "payroll": "payroll",
        "hiring": "hiring",
        "exit": "exit",
        "people": "people",
    }.get(kind, "")
    if not items:
        what = f"No {label} approvals are" if label else "Nothing is"
        return [_notice(f"{what} waiting on you right now.")], used

    counts: dict[str, int] = defaultdict(int)
    for item in items:
        counts[item.source_label] += 1
    rows = [
        [item.title, item.subject_name or "—", f"{item.submitted_at:%d %b}"] for item in items[:25]
    ]
    more = f" · first 25 of {len(items)} shown" if len(items) > 25 else ""
    card = _record(
        "Waiting on you",
        heading=f"{len(items)} item(s) to decide" + (f" · {label}" if label else ""),
        figures=[(name, str(count)) for name, count in sorted(counts.items())],
        columns=["What", "Who", "Submitted"],
        rows=rows,
        note=f"Oldest first{more}. Each is decided on its own page.",
        open_href="/approvals",
        open_label="Open Approvals",
    )
    # Which request each row is, for Ask Mira to name when it offers to decide one.
    card["refs"] = [f"{item.source}:{item.id}" for item in items[:25]]
    return [card], used


# ── Payroll figures ──────────────────────────────────────────────────────────


def _months(start: date, end: date) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month) and len(out) < 24:
        out.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


async def payroll_summary(
    session: AsyncSession, user: User, inputs: dict[str, Any], *, today: date
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """A month's payroll: runs, people paid, gross, deductions, net, cost and statutory dues."""
    if not await may_view(session, user, "payroll"):
        return [
            _notice(
                "Payroll figures are for admins and sub-admins who may view payroll.", "warning"
            )
        ], {}

    named = _parse(inputs.get("from")) or _parse(inputs.get("to"))
    if named is not None:
        start, end = _span(inputs, default=_month_of(today))
        months = _months(start, end)
    else:
        # Nothing named: the latest month that has a run, else this month.
        latest = (
            await session.execute(
                tenant_scoped(
                    select(PayrollRun.period_year, PayrollRun.period_month)
                    .where(PayrollRun.status != PayrollRunStatus.CANCELLED)
                    .order_by(PayrollRun.period_year.desc(), PayrollRun.period_month.desc())
                    .limit(1),
                    PayrollRun,
                    user,
                )
            )
        ).first()
        months = [(latest[0], latest[1])] if latest else [(today.year, today.month)]

    period = or_(
        *[and_(PayrollRun.period_year == y, PayrollRun.period_month == m) for y, m in months]
    )
    runs = (
        await session.scalars(
            tenant_scoped(
                select(PayrollRun)
                .where(period, PayrollRun.status != PayrollRunStatus.CANCELLED)
                .order_by(PayrollRun.period_year, PayrollRun.period_month, PayrollRun.created_at),
                PayrollRun,
                user,
            )
        )
    ).all()
    used = {
        "from": date(months[0][0], months[0][1], 1).isoformat(),
        "to": _month_of(date(months[-1][0], months[-1][1], 1))[1].isoformat(),
    }
    first = date(months[0][0], months[0][1], 1)
    last = date(months[-1][0], months[-1][1], 1)
    span = f"{first:%B %Y}" if first == last else f"{first:%b %Y} to {last:%b %Y}"
    if not runs:
        return [
            _notice(
                f"No payroll has been run for {span} yet. Start one under Payroll → Run payroll."
            )
        ], used

    run_ids = [run.id for run in runs]
    # Totals from the payslips, so a narrowed sub-admin sees only their people.
    by_run: dict[uuid.UUID, dict[str, Any]] = {}
    for row in await session.execute(
        tenant_scoped(
            select(
                Payslip.run_id,
                func.count(func.distinct(Payslip.employee_id)),
                func.coalesce(func.sum(Payslip.gross_earnings), 0),
                func.coalesce(func.sum(Payslip.total_deductions), 0),
                func.coalesce(func.sum(Payslip.net_pay), 0),
                func.coalesce(func.sum(Payslip.employer_cost), 0),
                func.coalesce(func.sum(Payslip.pf_employee), 0),
                func.coalesce(func.sum(Payslip.pf_employer), 0),
                func.coalesce(func.sum(Payslip.esi_employee), 0),
                func.coalesce(func.sum(Payslip.esi_employer), 0),
                func.coalesce(func.sum(Payslip.professional_tax), 0),
                func.coalesce(func.sum(Payslip.tds), 0),
                func.coalesce(func.sum(Payslip.lop_days), 0),
            )
            .where(Payslip.run_id.in_(run_ids))
            .group_by(Payslip.run_id),
            Payslip,
            user,
        )
    ):
        by_run[row[0]] = {
            "people": int(row[1]),
            "gross": _money(row[2]),
            "deductions": _money(row[3]),
            "net": _money(row[4]),
            "cost": _money(row[5]),
            "pf": _money(row[6]) + _money(row[7]),
            "esi": _money(row[8]) + _money(row[9]),
            "pt": _money(row[10]),
            "tds": _money(row[11]),
            "lop": _money(row[12]),
        }
    entities = {
        row.id: row.name
        for row in await session.execute(
            tenant_scoped(
                select(LegalEntity.id, LegalEntity.name).where(
                    LegalEntity.id.in_({run.legal_entity_id for run in runs})
                ),
                LegalEntity,
                user,
            )
        )
    }

    zero = {k: Decimal(0) for k in ("gross", "deductions", "net", "cost", "pf", "esi", "pt", "tds")}
    total = dict(zero)
    rows: list[list[Any]] = []
    for run in runs:
        figures = by_run.get(run.id, {**zero, "people": 0})
        for key in zero:
            total[key] += figures[key]
        rows.append(
            [
                f"{date(run.period_year, run.period_month, 1):%b %Y}",
                *([entities.get(run.legal_entity_id, "—")] if len(entities) > 1 else []),
                _RUN_TYPE.get(run.run_type, run.run_type.value),
                _RUN_STATUS.get(run.status, run.status.value),
                figures["people"],
                format_inr(figures["net"]),
            ]
        )
    paid = await session.scalar(
        tenant_scoped(
            select(func.count(func.distinct(Payslip.employee_id))).where(
                Payslip.run_id.in_(run_ids)
            ),
            Payslip,
            user,
        )
    )
    people_paid = int(paid or 0)
    waiting = [run for run in runs if run.status is not PayrollRunStatus.PAID]
    notes = [
        "Gross, deductions, net and cost to company are added up from the payslips of these "
        "runs (cancelled runs left out)."
    ]
    if waiting:
        notes.append(
            f"{len(waiting)} run(s) not paid yet: "
            + ", ".join(_RUN_STATUS.get(r.status, r.status.value).lower() for r in waiting)
            + "."
        )
    return [
        _record(
            "Payroll",
            heading=span,
            figures=[
                ("People paid", str(people_paid)),
                ("Gross pay", format_inr(total["gross"])),
                ("Deductions", format_inr(total["deductions"])),
                ("Net pay", format_inr(total["net"])),
                ("Cost to company", format_inr(total["cost"])),
                ("PF (employee + employer)", format_inr(total["pf"])),
                ("ESI (employee + employer)", format_inr(total["esi"])),
                ("Professional tax", format_inr(total["pt"])),
                ("TDS", format_inr(total["tds"])),
            ],
            columns=[
                "Month",
                *(["Entity"] if len(entities) > 1 else []),
                "Run",
                "Status",
                "People",
                "Net pay",
            ],
            rows=rows,
            note=" ".join(notes),
            open_href="/payroll",
            open_label="Open Payroll",
        )
    ], used


async def salary_totals(
    session: AsyncSession, user: User, *, today: date
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """What the salaries in force today add up to, company-wide and by department."""
    if not await may_view(session, user, "payroll"):
        return [
            _notice("Salary figures are for admins and sub-admins who may view payroll.", "warning")
        ], {}
    in_force = and_(
        EmployeeSalary.effective_from <= today,
        or_(EmployeeSalary.effective_to.is_(None), EmployeeSalary.effective_to >= today),
    )
    rows = (
        await session.execute(
            tenant_scoped(
                tenant_scoped(
                    select(Employee.id, Employee.department_id, EmployeeSalary.ctc_annual)
                    .join(Employee, Employee.id == EmployeeSalary.employee_id)
                    .where(
                        in_force,
                        Employee.deleted_at.is_(None),
                        Employee.status.in_(_EMPLOYED),
                    ),
                    EmployeeSalary,
                    user,
                ),
                Employee,
                user,
            )
        )
    ).all()
    if not rows:
        return [
            _notice(
                "No salary is in force today for anyone employed. Set them up in the Salary master."
            )
        ], {}
    # One salary per person: the first in force (overlaps are refused when saving).
    per_person: dict[uuid.UUID, tuple[uuid.UUID | None, Decimal]] = {}
    for row in rows:
        per_person.setdefault(row[0], (row[1], _money(row[2])))
    names = await _departments(session, user, {d for d, _ in per_person.values()})
    by_dept: dict[str, list[Decimal]] = defaultdict(list)
    for dept, ctc in per_person.values():
        by_dept[names.get(dept, "No department") if dept else "No department"].append(ctc)
    annual = sum((c for _, c in per_person.values()), Decimal(0))
    count = len(per_person)
    table = [
        [
            name,
            len(values),
            format_inr(sum(values, Decimal(0)) / 12),
            format_inr(sum(values, Decimal(0))),
            format_inr(sum(values, Decimal(0)) / len(values)),
        ]
        for name, values in sorted(by_dept.items(), key=lambda kv: -sum(kv[1], Decimal(0)))
    ]
    return [
        _record(
            "Salary bill",
            heading=f"Salaries in force on {_day(today)}",
            figures=[
                ("People with a salary", str(count)),
                ("Monthly CTC", format_inr(annual / 12)),
                ("Annual CTC", format_inr(annual)),
                ("Average annual CTC", format_inr(annual / count)),
            ],
            columns=["Department", "People", "Monthly CTC", "Annual CTC", "Average annual CTC"],
            rows=table,
            note="From the Salary master: each employed person's salary in force today. "
            "What was actually paid in a month is under payroll.",
            open_href="/payroll/salaries",
            open_label="Open Salary master",
        )
    ], {}


# ── Joiners, exits and attrition ─────────────────────────────────────────────


async def people_changes(
    session: AsyncSession, user: User, inputs: dict[str, Any], *, today: date
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Who joined and who left between two dates, who is on notice, and the attrition."""
    if not await may_view(session, user, "people"):
        return [_notice("Joiners and exits are for admins and sub-admins.", "warning")], {}
    start, end = _span(inputs, default=_month_of(today))
    used = {"from": start.isoformat(), "to": end.isoformat()}

    people = (
        await session.scalars(
            tenant_scoped(
                select(Employee).where(
                    Employee.deleted_at.is_(None),
                    or_(
                        Employee.date_of_joining.between(start, end),
                        Employee.date_of_exit.between(start, end),
                        Employee.status == EmploymentStatus.ON_NOTICE,
                    ),
                ),
                Employee,
                user,
            )
        )
    ).all()
    names = await _departments(session, user, {p.department_id for p in people})

    def dept(p: Employee) -> str:
        return names.get(p.department_id, "—") if p.department_id else "—"

    joiners = sorted(
        (p for p in people if p.date_of_joining and start <= p.date_of_joining <= end),
        key=lambda p: (p.date_of_joining, p.full_name),
    )
    leavers = sorted(
        (p for p in people if p.date_of_exit and start <= p.date_of_exit <= end),
        key=lambda p: (p.date_of_exit, p.full_name),
    )
    notice = sorted(
        (p for p in people if p.status is EmploymentStatus.ON_NOTICE),
        key=lambda p: (p.date_of_exit or date.max, p.full_name),
    )

    async def employed_on(day: date) -> int:
        return int(
            await session.scalar(
                tenant_scoped(
                    select(func.count(Employee.id)).where(
                        Employee.deleted_at.is_(None),
                        Employee.date_of_joining.is_not(None),
                        Employee.date_of_joining <= day,
                        or_(Employee.date_of_exit.is_(None), Employee.date_of_exit >= day),
                        Employee.status != EmploymentStatus.PRE_JOINING,
                    ),
                    Employee,
                    user,
                )
            )
            or 0
        )

    at_start, at_end = await employed_on(start), await employed_on(min(end, today))
    average = Decimal(at_start + at_end) / 2
    gone = [p for p in leavers if p.date_of_exit and p.date_of_exit <= today]
    attrition = (
        f"{(Decimal(len(gone)) / average * 100).quantize(Decimal('0.1'))}%" if average else "—"
    )

    cards: list[dict[str, Any]] = [
        _record(
            "Joiners and exits",
            heading=_span_text(start, end),
            figures=[
                ("Joined", str(sum(1 for p in joiners if p.date_of_joining <= today))),
                ("Joining later", str(sum(1 for p in joiners if p.date_of_joining > today))),
                ("Left", str(len(gone))),
                ("Leaving later", str(len(leavers) - len(gone))),
                ("On notice now", str(len(notice))),
                ("Headcount at start", str(at_start)),
                ("Attrition", attrition),
            ],
            note="Attrition = people who left in the period ÷ the average of the headcount at "
            "its start and end (or today, if it has not ended).",
            open_href="/people",
            open_label="Open Employees",
        )
    ]
    if joiners:
        cards.append(
            _table(
                "Joiners",
                ["Name", "Designation", "Department", "Date of joining"],
                [
                    [
                        p.full_name,
                        p.designation or "—",
                        dept(p),
                        _day(p.date_of_joining),
                    ]
                    for p in joiners[:50]
                ],
                caption=f"{len(joiners)} joining {_span_text(start, end)}",
            )
        )
    if leavers:
        cards.append(
            _table(
                "Exits",
                ["Name", "Designation", "Department", "Last working day"],
                [
                    [
                        p.full_name,
                        p.designation or "—",
                        dept(p),
                        _day(p.date_of_exit),
                    ]
                    for p in leavers[:50]
                ],
                caption=f"{len(leavers)} leaving {_span_text(start, end)}",
                open_href="/people/exits",
            )
        )
    if notice:
        cards.append(
            _table(
                "On notice",
                ["Name", "Department", "Last working day"],
                [[p.full_name, dept(p), _day(p.date_of_exit)] for p in notice[:50]],
                caption=f"{len(notice)} serving notice today",
                open_href="/people/exits",
            )
        )
    return cards, used


# ── Dates coming up ──────────────────────────────────────────────────────────


def _next_on(month: int, day: int, start: date, end: date) -> date | None:
    """The first date in [start, end] falling on this day of the year."""
    for year in range(start.year, end.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:  # 29 February in a common year: the 28th.
            candidate = date(year, 2, 28)
        if start <= candidate <= end:
            return candidate
    return None


async def people_dates(
    session: AsyncSession, user: User, inputs: dict[str, Any], *, today: date
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Birthdays, work anniversaries, probation ends and last working days in a range."""
    if not await may_view(session, user, "people"):
        return [_notice("These dates are for admins and sub-admins.", "warning")], {}
    start, end = _span(inputs, default=(today, today + timedelta(days=29)))
    kind = str(inputs.get("kind") or "none")
    kinds = (kind,) if kind in DATE_KINDS else DATE_KINDS
    used = {"from": start.isoformat(), "to": end.isoformat(), "kind": kind}

    people = (
        await session.scalars(
            tenant_scoped(
                select(Employee).where(
                    Employee.deleted_at.is_(None),
                    Employee.status.in_((*_EMPLOYED, EmploymentStatus.PRE_JOINING)),
                ),
                Employee,
                user,
            )
        )
    ).all()
    names = await _departments(session, user, {p.department_id for p in people})

    def dept(p: Employee) -> str:
        return names.get(p.department_id, "—") if p.department_id else "—"

    span = _span_text(start, end)
    cards: list[dict[str, Any]] = []
    if "birthdays" in kinds:
        found = []
        for p in people:
            if p.status is EmploymentStatus.PRE_JOINING or not p.date_of_birth:
                continue
            on = _next_on(p.date_of_birth.month, p.date_of_birth.day, start, end)
            if on:
                found.append((on, p))
        found.sort(key=lambda t: (t[0], t[1].full_name))
        cards.append(
            _table(
                "Birthdays",
                ["Name", "Department", "Birthday"],
                [[p.full_name, dept(p), f"{on:%a %d %b}"] for on, p in found[:60]],
                caption=f"{len(found)} birthday(s) {span}",
            )
            if found
            else _notice(f"No birthdays {span}.")
        )
    if "anniversaries" in kinds:
        found = []
        for p in people:
            joined = p.date_of_joining
            if p.status is EmploymentStatus.PRE_JOINING or not joined:
                continue
            on = _next_on(joined.month, joined.day, start, end)
            if on and on.year > joined.year:
                found.append((on, p, on.year - joined.year))
        found.sort(key=lambda t: (t[0], t[1].full_name))
        cards.append(
            _table(
                "Work anniversaries",
                ["Name", "Department", "Date", "Years"],
                [[p.full_name, dept(p), f"{on:%a %d %b}", years] for on, p, years in found[:60]],
                caption=f"{len(found)} work anniversary(ies) {span}",
            )
            if found
            else _notice(f"No work anniversaries {span}.")
        )
    if "probation" in kinds:
        found = sorted(
            (
                p
                for p in people
                if p.probation_end_date
                and start <= p.probation_end_date <= end
                and p.status is not EmploymentStatus.PRE_JOINING
            ),
            key=lambda p: (p.probation_end_date, p.full_name),
        )
        cards.append(
            _table(
                "Probation ending",
                ["Name", "Department", "Joined", "Probation ends"],
                [
                    [
                        p.full_name,
                        dept(p),
                        _day(p.date_of_joining),
                        _day(p.probation_end_date),
                    ]
                    for p in found[:60]
                ],
                caption=f"{len(found)} probation(s) ending {span}",
            )
            if found
            else _notice(f"No probation periods end {span}.")
        )
    if "last_working_day" in kinds:
        found = sorted(
            (p for p in people if p.date_of_exit and start <= p.date_of_exit <= end),
            key=lambda p: (p.date_of_exit, p.full_name),
        )
        cards.append(
            _table(
                "Last working days",
                ["Name", "Department", "Last working day"],
                [[p.full_name, dept(p), _day(p.date_of_exit)] for p in found[:60]],
                caption=f"{len(found)} last working day(s) {span}",
                open_href="/people/exits",
            )
            if found
            else _notice(f"Nobody's last working day falls {span}.")
        )
    return cards, used


# ── HR helpdesk ──────────────────────────────────────────────────────────────

_TICKET_STATUS = {
    "open": "active",
    "new": "open",
    "in_progress": "in_progress",
    "hold": "waiting_on_employee",
    "closed": "done",
}

_TICKET_LABEL = {
    "open": "Open",
    "in_progress": "In progress",
    "waiting_on_employee": "Waiting on employee",
    "resolved": "Resolved",
    "closed": "Closed",
    "cancelled": "Cancelled",
}


async def hr_tickets(
    session: AsyncSession, user: User, inputs: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """HR helpdesk tickets: the asker's own, or the queue they work."""
    from app.services.helpdesk import service as helpdesk

    scope = str(inputs.get("scope") or "mine")
    if scope not in ("mine", "assigned", "queue", "company"):
        scope = "mine"
    status = _TICKET_STATUS.get(str(inputs.get("status") or "none"))
    query = str(inputs.get("query") or "").strip()[:100]
    used: dict[str, Any] = {"scope": scope}
    if status:
        used["status"] = inputs.get("status")
    if scope == "mine":
        tickets = await helpdesk.list_my_tickets(session, user, status_filter=status)
        title, href = "Your HR tickets", "/helpdesk"
    else:
        try:
            tickets = await helpdesk.list_queue(
                session,
                user,
                status_filter=status,
                assignee={"assigned": "me", "queue": "unassigned"}.get(scope),
                search=query or None,
                limit=50,
            )
        except HTTPException:
            return [
                _notice(
                    "The HR ticket queue is for the people who work it. Your own tickets are "
                    "under Helpdesk → My tickets.",
                    "warning",
                )
            ], {}
        if query:
            used["query"] = query
        title = {
            "assigned": "HR tickets assigned to you",
            "queue": "Unassigned HR tickets",
            "company": "HR ticket queue",
        }[scope]
        href = "/helpdesk/queue"
    if not tickets:
        return [_notice("No HR helpdesk tickets match that.")], used
    now_open = sum(
        1 for t in tickets if t.status.value in ("open", "in_progress", "waiting_on_employee")
    )
    overdue = sum(1 for t in tickets if t.sla_state == "overdue")
    rows = [
        [
            t.ticket_number,
            t.subject,
            *([t.employee_name] if scope != "mine" else []),
            _TICKET_LABEL.get(t.status.value, t.status.value),
            *([t.assignee_name or "Unassigned"] if scope != "mine" else []),
        ]
        for t in tickets[:25]
    ]
    columns = [
        "Ticket",
        "Subject",
        *(["Raised by"] if scope != "mine" else []),
        "Status",
        *(["With"] if scope != "mine" else []),
    ]
    return [
        _record(
            title,
            figures=[
                ("Shown", str(len(tickets))),
                ("Still open", str(now_open)),
                ("Past due", str(overdue)),
            ],
            columns=columns,
            rows=rows,
            note="Most recent first." if scope == "mine" else "Most urgent first.",
            open_href=href,
            open_label="Open Helpdesk",
        )
    ], used
