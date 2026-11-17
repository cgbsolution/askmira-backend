"""Consultant payout runs: approved consultant weeks become one payout line per consultant.

HR picks a company (or every company), a cut-off and a pay date. The run takes
every approved consultant week ending on or before the cut-off that is not in
another run, was approved on or after the workspace's payout release date and
was never paid through payroll. Older weeks are added one by one
(`include_week`).

Each line:
- gross = each day's hours x the rate in force that day (the consultant's rate
  for that project, else their general rate);
- GST = gross x their GST rate, where they charge GST (not part of the TDS base);
- TDS on the gross under their section (194J by default), from the
  `tds_non_salary` rule set in force on the run's pay date, with the
  financial year's thresholds and catch-up: the year so far is this
  consultant's lines in approved or paid runs paid that year, plus any opening
  figure HR entered;
- TDS is never more than gross + GST, so net pay is never below nil: what is
  left over is carried to the consultant's next payout in the same financial
  year (`tds_due` = this line's TDS plus any carried in; `tds_amount` = what is
  deducted; `tds_carried_out` = what is still owed);
- net payable = gross + GST - TDS deducted.

A line with a problem HR must fix (no rate for a day worked, no bank account,
GST without a GSTIN, a pay date before one of theirs already approved this
year) is held back — excluded, keeping its weeks — until the data
is fixed and the run recalculated. A line HR excludes by hand releases its
weeks for a later run. Only a draft changes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import hr_scope, permissions
from app.core.deps import tenant_scoped
from app.core.step_up import Confirm
from app.models.audit import AuditAction
from app.models.consultant_payouts import (
    ConsultantPayoutLine,
    ConsultantPayoutRun,
    ConsultantPayoutWeek,
    ConsultantTdsOpening,
    PayoutRunStatus,
)
from app.models.contractors import ContractorKind, TdsSection
from app.models.employee import Employee, EmploymentType
from app.models.org import LegalEntity
from app.models.payroll import PayrollRuleSet
from app.models.payroll_inputs import PayrollAdjustment
from app.models.tenant import Tenant
from app.models.timesheets import Timesheet, TimesheetStatus
from app.models.user import User
from app.services import audit, tds_non_salary, timesheets
from app.services.consultants import (
    adjustment_never_paid,
    is_consultant,
    not_paid_through_payroll,
)
from app.services.payroll.run import _require_scope_covers

__all__ = [
    "approve_run",
    "consultant_cost_by_month",
    "create_run",
    "delete_run",
    "eligible_weeks",
    "exclude_line",
    "get_run",
    "include_week",
    "list_runs",
    "mark_paid",
    "my_payouts",
    "recalculate",
    "tds_carried_in",
]

DEFAULT_GST_RATE = Decimal("18")
DEFAULT_SECTION = TdsSection.S194J_PROFESSIONAL
DEFAULT_PAYEE_KIND = ContractorKind.INDIVIDUAL

NO_BANK_ACCOUNT = "No bank account"
NO_GSTIN = "GST applies but no GSTIN"

_CENT = Decimal("0.01")
_HUNDRED = Decimal(100)
_HR_ONLY = "Consultant payouts are run by HR."


def _money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _text(value: Any) -> str:
    return str(_money(value))


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


_TAKEN_RACE = "Another payout run took some of these weeks. Try again."


# ── Which weeks ─────────────────────────────────────────────────────────────


async def _release_instant(session: AsyncSession, user: User) -> datetime | None:
    """The start of the payout release day, where the workspace is."""
    from app.services.attendance.service import tenant_timezone

    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or tenant.consultant_payouts_from is None:
        return None
    zone = await tenant_timezone(session, user)
    return datetime.combine(tenant.consultant_payouts_from, time.min, tzinfo=zone)


def _held_in_a_run():
    return (
        select(ConsultantPayoutWeek.id)
        .where(ConsultantPayoutWeek.timesheet_id == Timesheet.id)
        .exists()
    )


async def eligible_weeks(
    session: AsyncSession,
    user: User,
    *,
    legal_entity_id: uuid.UUID | None,
    cut_off: date,
) -> list[Timesheet]:
    """Approved consultant weeks a new run takes: ending by the cut-off, in no run,
    approved since payouts were released, never paid through payroll."""
    permissions.require_hr(user, detail=_HR_ONLY)
    stmt = (
        select(Timesheet)
        .join(Employee, Employee.id == Timesheet.employee_id)
        .where(
            Employee.employment_type == EmploymentType.CONSULTANT,
            Timesheet.status == TimesheetStatus.APPROVED,
            Timesheet.week_start <= cut_off - timedelta(days=6),
            not_paid_through_payroll(),
            ~_held_in_a_run(),
        )
        .order_by(Timesheet.employee_id, Timesheet.week_start)
    )
    release = await _release_instant(session, user)
    if release is not None:
        stmt = stmt.where(Timesheet.decided_at >= release)
    if legal_entity_id is not None:
        stmt = stmt.where(Employee.legal_entity_id == legal_entity_id)
    return list((await session.scalars(tenant_scoped(stmt, Timesheet, user))).all())


# ── Pricing ─────────────────────────────────────────────────────────────────


@dataclass
class _PricedWeek:
    hours: Decimal = Decimal(0)
    gross: Decimal = Decimal(0)
    breakdown: list[dict[str, Any]] = field(default_factory=list)
    missing: set[date] = field(default_factory=set)


async def _price_week(session: AsyncSession, user: User, sheet: Timesheet) -> _PricedWeek:
    """The week by day and project, each at the rate in force that day."""
    grouped: dict[tuple[date, uuid.UUID | None], Decimal] = {}
    for entry in await timesheets._entries(session, user, sheet):
        key = (entry.work_date, entry.project_id)
        grouped[key] = grouped.get(key, Decimal(0)) + Decimal(str(entry.hours))
    priced = _PricedWeek()
    for (day, project_id), hours in grouped.items():
        rate = await timesheets._rate_on(session, user, sheet.employee_id, day, project_id)
        amount = _money(hours * rate) if rate is not None else Decimal("0.00")
        if rate is None:
            priced.missing.add(day)
        priced.hours += hours
        priced.gross += amount
        priced.breakdown.append(
            {
                "work_date": day.isoformat(),
                "project_id": str(project_id) if project_id else None,
                "hours": _text(hours),
                "rate": _text(rate) if rate is not None else None,
                "amount": _text(amount),
            }
        )
    priced.gross = _money(priced.gross)
    return priced


async def _year_to_date(
    session: AsyncSession,
    user: User,
    run: ConsultantPayoutRun,
    employee_id: uuid.UUID,
    section: TdsSection,
) -> tds_non_salary.YearToDate:
    """Paid so far this financial year under the section: lines in other approved or
    paid runs with a pay date in the year, plus HR's opening figure. Drafts never count."""
    start, end = tds_non_salary.fy_bounds(run.pay_date)
    rows = (
        await session.execute(
            tenant_scoped(
                select(ConsultantPayoutLine.gross, ConsultantPayoutLine.tds_base)
                .join(ConsultantPayoutRun, ConsultantPayoutRun.id == ConsultantPayoutLine.run_id)
                .where(
                    ConsultantPayoutLine.employee_id == employee_id,
                    ConsultantPayoutLine.section == section,
                    ConsultantPayoutLine.excluded.is_(False),
                    ConsultantPayoutLine.run_id != run.id,
                    ConsultantPayoutRun.status.in_(
                        [PayoutRunStatus.APPROVED, PayoutRunStatus.PAID]
                    ),
                    ConsultantPayoutRun.pay_date >= start,
                    ConsultantPayoutRun.pay_date <= end,
                ),
                ConsultantPayoutLine,
                user,
            )
        )
    ).all()
    total = sum((Decimal(str(gross)) for gross, _ in rows), Decimal(0))
    taxed = sum((Decimal(str(base)) for _, base in rows), Decimal(0))
    opening = await session.scalar(
        tenant_scoped(
            select(ConsultantTdsOpening).where(
                ConsultantTdsOpening.employee_id == employee_id,
                ConsultantTdsOpening.fy_start == start,
                ConsultantTdsOpening.section == section,
            ),
            ConsultantTdsOpening,
            user,
        )
    )
    if opening is not None:
        total += Decimal(str(opening.fees_paid))
        taxed += Decimal(str(opening.taxed_base))
    return tds_non_salary.YearToDate(total=total, taxed_base=taxed)


async def tds_carried_in(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    *,
    section: TdsSection,
    before_run_id: uuid.UUID,
    pay_date: date,
) -> Decimal:
    """TDS an earlier payout could not take, owed now: what the consultant's latest
    line before this one (approved or paid, same section, same financial year) carried out.

    Each line's carry already includes what it brought in, so only the latest counts.
    Lines paid the same day are ordered by when their run was approved, then by id.
    A carry never crosses into a new financial year.
    """
    start, _ = tds_non_salary.fy_bounds(pay_date)
    carried = await session.scalar(
        tenant_scoped(
            select(ConsultantPayoutLine.tds_carried_out)
            .join(ConsultantPayoutRun, ConsultantPayoutRun.id == ConsultantPayoutLine.run_id)
            .where(
                ConsultantPayoutLine.employee_id == employee_id,
                ConsultantPayoutLine.section == section,
                ConsultantPayoutLine.excluded.is_(False),
                ConsultantPayoutLine.run_id != before_run_id,
                ConsultantPayoutRun.status.in_([PayoutRunStatus.APPROVED, PayoutRunStatus.PAID]),
                ConsultantPayoutRun.pay_date >= start,
                ConsultantPayoutRun.pay_date <= pay_date,
            )
            .order_by(
                ConsultantPayoutRun.pay_date.desc(),
                ConsultantPayoutRun.approved_at.desc().nulls_last(),
                ConsultantPayoutLine.id.desc(),
            )
            .limit(1),
            ConsultantPayoutLine,
            user,
        )
    )
    return _money(carried or 0)


async def _latest_approved_pay_date(
    session: AsyncSession, user: User, run: ConsultantPayoutRun, employee_id: uuid.UUID
) -> date | None:
    """The latest pay date this financial year of the consultant's lines in other
    approved or paid runs.

    A run may not be dated before it: the year so far and the TDS carried would then
    disagree, and a carry already taken could be taken again.
    """
    start, end = tds_non_salary.fy_bounds(run.pay_date)
    return await session.scalar(
        tenant_scoped(
            select(func.max(ConsultantPayoutRun.pay_date))
            .select_from(ConsultantPayoutLine)
            .join(ConsultantPayoutRun, ConsultantPayoutRun.id == ConsultantPayoutLine.run_id)
            .where(
                ConsultantPayoutLine.employee_id == employee_id,
                ConsultantPayoutLine.excluded.is_(False),
                ConsultantPayoutLine.run_id != run.id,
                ConsultantPayoutRun.status.in_([PayoutRunStatus.APPROVED, PayoutRunStatus.PAID]),
                ConsultantPayoutRun.pay_date >= start,
                ConsultantPayoutRun.pay_date <= end,
            ),
            ConsultantPayoutLine,
            user,
        )
    )


def _percent(rate: Decimal) -> str:
    return format(rate.normalize(), "f")


def _tds_note(
    result: tds_non_salary.TdsResult,
    gross: Decimal,
    payee: tds_non_salary.Payee,
    *,
    carried_in: Decimal = Decimal(0),
    carried_out: Decimal = Decimal(0),
) -> str | None:
    """The line's TDS in words: below the threshold, caught up, no PAN, carried.

    Worded for payouts rather than invoices; the amounts are `compute`'s own.
    """
    parts: list[str] = []
    if result.base == 0:
        if result.note:
            parts.append(result.note)
    else:
        if result.base > gross:
            parts.append(
                "The year's total crossed the threshold, so fees paid earlier this year "
                "without TDS are taxed now."
            )
        if not payee.pan:
            parts.append(f"No PAN: TDS at {_percent(result.rate)}%.")
    if carried_out > 0:
        parts.append(f"₹{carried_out} of TDS is carried to the next payout.")
    if carried_in > 0:
        parts.append(f"Includes ₹{carried_in} TDS carried from an earlier payout.")
    return " ".join(parts) or None


async def _employee(session: AsyncSession, user: User, employee_id: uuid.UUID) -> Employee | None:
    return await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )


async def _has_bank_account(session: AsyncSession, user: User, employee_id: uuid.UUID) -> bool:
    """The account a bank file would pay: the same one disbursement picks."""
    from app.services.disbursement.service import _primary_account

    account, number = await _primary_account(session, user, employee_id)
    return account is not None and bool(number)


async def _line_weeks(
    session: AsyncSession, user: User, line_id: uuid.UUID
) -> list[ConsultantPayoutWeek]:
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(ConsultantPayoutWeek).where(ConsultantPayoutWeek.line_id == line_id),
                    ConsultantPayoutWeek,
                    user,
                )
            )
        ).all()
    )


@dataclass
class _LineFigures:
    """A line priced from scratch, not yet written to it."""

    weeks: list[tuple[ConsultantPayoutWeek, _PricedWeek]]
    hours: Decimal
    gross: Decimal
    gst_rate: Decimal
    gst_amount: Decimal
    section: TdsSection
    tds_base: Decimal
    tds_rate: Decimal
    tds_due: Decimal
    tds_amount: Decimal
    tds_carried_out: Decimal
    net_payable: Decimal
    tds_note: str | None
    rule_set_id: uuid.UUID | None
    problems: list[str]

    #: The figures approval checks are still what is stored on the line.
    COMPARED = (
        "gross",
        "gst_amount",
        "tds_base",
        "tds_rate",
        "tds_due",
        "tds_amount",
        "tds_carried_out",
        "net_payable",
    )

    def matches(self, line: ConsultantPayoutLine) -> bool:
        return (
            all(
                Decimal(str(getattr(self, name))) == Decimal(str(getattr(line, name)))
                for name in self.COMPARED
            )
            and list(self.problems) == list(line.problems or [])
            and bool(self.problems) == line.excluded
        )


async def _price_line(
    session: AsyncSession,
    user: User,
    run: ConsultantPayoutRun,
    line: ConsultantPayoutLine,
    rule_set: PayrollRuleSet,
) -> _LineFigures:
    """Price a line from scratch from its weeks and the consultant's record as it is now.

    Reads only: nothing on the line or its weeks changes.
    """
    employee = await _employee(session, user, line.employee_id)
    if employee is None:
        raise _not_found("Consultant not found")
    hours = gross = Decimal(0)
    missing: set[date] = set()
    weeks: list[tuple[ConsultantPayoutWeek, _PricedWeek]] = []
    for week in await _line_weeks(session, user, line.id):
        sheet = await session.scalar(
            tenant_scoped(
                select(Timesheet).where(Timesheet.id == week.timesheet_id), Timesheet, user
            )
        )
        priced = await _price_week(session, user, sheet)
        weeks.append((week, priced))
        hours += priced.hours
        gross += priced.gross
        missing |= priced.missing
    gross = _money(gross)

    problems = [f"No hourly rate on {day.isoformat()}" for day in sorted(missing)]
    if not await _has_bank_account(session, user, employee.id):
        problems.append(NO_BANK_ACCOUNT)
    if employee.gst_applicable and not (employee.gstin or "").strip():
        problems.append(NO_GSTIN)
    later = await _latest_approved_pay_date(session, user, run, employee.id)
    if later is not None and later > run.pay_date:
        problems.append(
            f"Paid on {later.isoformat()} in an approved run; this run's pay date is earlier. "
            "Change the pay date."
        )

    gst_rate = (
        Decimal(str(employee.gst_rate if employee.gst_rate is not None else DEFAULT_GST_RATE))
        if employee.gst_applicable
        else Decimal(0)
    )
    gst = _money(gross * gst_rate / _HUNDRED)
    section = employee.tds_section or DEFAULT_SECTION
    payee = tds_non_salary.Payee(
        pan=(employee.pan or "").strip() or None,
        kind=employee.payee_kind or DEFAULT_PAYEE_KIND,
        section=section,
    )
    ytd = await _year_to_date(session, user, run, employee.id, section)
    result = tds_non_salary.compute(rule_set, payee, gross, ytd)
    carried_in = await tds_carried_in(
        session, user, employee.id, section=section, before_run_id=run.id, pay_date=run.pay_date
    )
    # TDS is never more than this payout, in whole rupees (the paise are paid);
    # the rest is taken from the next one.
    tds_due = _money(result.tds) + carried_in
    payable = gross + gst
    tds = _money(min(tds_due, payable.to_integral_value(rounding=ROUND_FLOOR)))
    carried_out = tds_due - tds

    return _LineFigures(
        weeks=weeks,
        hours=_money(hours),
        gross=gross,
        gst_rate=gst_rate,
        gst_amount=gst,
        section=section,
        tds_base=_money(result.base),
        tds_rate=result.rate,
        tds_due=tds_due,
        tds_amount=tds,
        tds_carried_out=carried_out,
        net_payable=_money(payable - tds),
        tds_note=_tds_note(result, gross, payee, carried_in=carried_in, carried_out=carried_out),
        rule_set_id=result.rule_set_id,
        problems=problems,
    )


async def _reprice(
    session: AsyncSession,
    user: User,
    run: ConsultantPayoutRun,
    line: ConsultantPayoutLine,
    rule_set: PayrollRuleSet,
) -> None:
    """Price a line from scratch and write the figures to it and its weeks."""
    figures = await _price_line(session, user, run, line, rule_set)
    for week, priced in figures.weeks:
        week.hours, week.gross, week.breakdown = priced.hours, priced.gross, priced.breakdown
    line.hours = figures.hours
    line.gross = figures.gross
    line.gst_rate = figures.gst_rate
    line.gst_amount = figures.gst_amount
    line.section = figures.section
    line.tds_base = figures.tds_base
    line.tds_rate = figures.tds_rate
    line.tds_due = figures.tds_due
    line.tds_amount = figures.tds_amount
    line.tds_carried_out = figures.tds_carried_out
    line.net_payable = figures.net_payable
    line.tds_note = figures.tds_note
    line.rule_set_id = figures.rule_set_id
    line.problems = figures.problems
    line.excluded = bool(figures.problems)


# ── Reading ─────────────────────────────────────────────────────────────────


def _totals(lines: list[ConsultantPayoutLine]) -> dict[str, Any]:
    paid = [line for line in lines if not line.excluded]

    def total(attr: str) -> str:
        return _text(sum((Decimal(str(getattr(line, attr))) for line in paid), Decimal(0)))

    return {
        "lines_included": len(paid),
        "lines_held": sum(1 for line in lines if line.excluded and not line.excluded_reason),
        "lines_excluded": sum(1 for line in lines if line.excluded_reason),
        "hours": total("hours"),
        "gross": total("gross"),
        "gst_amount": total("gst_amount"),
        "tds_due": total("tds_due"),
        "tds_amount": total("tds_amount"),
        "tds_carried_out": total("tds_carried_out"),
        "net_payable": total("net_payable"),
    }


def _run_read(run: ConsultantPayoutRun, lines: list[ConsultantPayoutLine]) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "legal_entity_id": str(run.legal_entity_id) if run.legal_entity_id else None,
        "cut_off": run.cut_off.isoformat(),
        "pay_date": run.pay_date.isoformat(),
        "status": run.status.value,
        "created_by_user_id": str(run.created_by_user_id) if run.created_by_user_id else None,
        "approved_by_user_id": str(run.approved_by_user_id) if run.approved_by_user_id else None,
        "approved_at": run.approved_at.isoformat() if run.approved_at else None,
        "paid_on": run.paid_on.isoformat() if run.paid_on else None,
        "payment_reference": run.payment_reference,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "totals": _totals(lines),
    }


def _line_read(
    line: ConsultantPayoutLine,
    employee: Employee | None,
    weeks: list[tuple[ConsultantPayoutWeek, Timesheet]],
) -> dict[str, Any]:
    return {
        "id": str(line.id),
        "employee_id": str(line.employee_id),
        "employee_code": employee.employee_code if employee else None,
        "employee_name": employee.full_name if employee else None,
        "hours": _text(line.hours),
        "gross": _text(line.gross),
        "gst_rate": _text(line.gst_rate),
        "gst_amount": _text(line.gst_amount),
        "section": line.section.value,
        "tds_base": _text(line.tds_base),
        "tds_rate": str(Decimal(str(line.tds_rate)).quantize(Decimal("0.001"))),
        "tds_due": _text(line.tds_due),
        "tds_amount": _text(line.tds_amount),
        "tds_carried_out": _text(line.tds_carried_out),
        "net_payable": _text(line.net_payable),
        "tds_note": line.tds_note,
        "rule_set_id": str(line.rule_set_id) if line.rule_set_id else None,
        "problems": list(line.problems or []),
        "excluded": line.excluded,
        "excluded_reason": line.excluded_reason,
        "challan_id": str(line.challan_id) if line.challan_id else None,
        "weeks": [
            {
                "id": str(week.id),
                "timesheet_id": str(week.timesheet_id),
                "week_start": sheet.week_start.isoformat(),
                "hours": _text(week.hours),
                "gross": _text(week.gross),
                "breakdown": list(week.breakdown or []),
            }
            for week, sheet in sorted(weeks, key=lambda pair: pair[1].week_start)
        ],
    }


async def _run(
    session: AsyncSession, user: User, run_id: uuid.UUID, *, lock: bool = False
) -> ConsultantPayoutRun:
    stmt = tenant_scoped(
        select(ConsultantPayoutRun).where(ConsultantPayoutRun.id == run_id),
        ConsultantPayoutRun,
        user,
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    run = await session.scalar(stmt)
    if run is None:
        raise _not_found("Payout run not found")
    return run


async def _draft(session: AsyncSession, user: User, run_id: uuid.UUID) -> ConsultantPayoutRun:
    """The run, locked, if it is still a draft."""
    permissions.require_hr(user, detail=_HR_ONLY)
    run = await _run(session, user, run_id, lock=True)
    if run.status is not PayoutRunStatus.DRAFT:
        raise _conflict("Only a draft payout run can be changed.")
    return run


async def _lines(
    session: AsyncSession, user: User, run_ids: list[uuid.UUID], *, everyone: bool = False
) -> list[ConsultantPayoutLine]:
    """The runs' lines; `everyone` reads past the caller's HR scope (approval checks)."""
    if not run_ids:
        return []
    stmt = tenant_scoped(
        select(ConsultantPayoutLine)
        .where(ConsultantPayoutLine.run_id.in_(run_ids))
        .order_by(ConsultantPayoutLine.created_at, ConsultantPayoutLine.id),
        ConsultantPayoutLine,
        user,
    )
    if everyone:
        stmt = stmt.execution_options(hr_scope_bypass=True)
    return list((await session.scalars(stmt)).all())


async def get_run(session: AsyncSession, user: User, run_id: uuid.UUID) -> dict[str, Any]:
    """A run with its lines, each with its weeks priced by day and project."""
    permissions.require_hr(user, detail=_HR_ONLY)
    run = await _run(session, user, run_id)
    lines = await _lines(session, user, [run.id])
    employees: dict[uuid.UUID, Employee] = {}
    if lines:
        people = await session.scalars(
            tenant_scoped(
                select(Employee).where(Employee.id.in_({line.employee_id for line in lines})),
                Employee,
                user,
            )
        )
        employees = {person.id: person for person in people.all()}
    weeks: dict[uuid.UUID, list[tuple[ConsultantPayoutWeek, Timesheet]]] = {}
    rows = (
        await session.execute(
            tenant_scoped(
                select(ConsultantPayoutWeek, Timesheet)
                .join(Timesheet, Timesheet.id == ConsultantPayoutWeek.timesheet_id)
                .where(ConsultantPayoutWeek.run_id == run.id),
                ConsultantPayoutWeek,
                user,
            )
        )
    ).all()
    for week, sheet in rows:
        weeks.setdefault(week.line_id, []).append((week, sheet))
    lines.sort(
        key=lambda line: (
            employees[line.employee_id].employee_code if line.employee_id in employees else ""
        )
    )
    return {
        **_run_read(run, lines),
        "lines": [
            _line_read(line, employees.get(line.employee_id), weeks.get(line.id, []))
            for line in lines
        ],
    }


async def list_runs(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Every run, newest pay date first, with its totals."""
    permissions.require_hr(user, detail=_HR_ONLY)
    runs = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(ConsultantPayoutRun)
                    .order_by(
                        ConsultantPayoutRun.pay_date.desc(), ConsultantPayoutRun.created_at.desc()
                    )
                    .limit(200),
                    ConsultantPayoutRun,
                    user,
                )
            )
        ).all()
    )
    by_run: dict[uuid.UUID, list[ConsultantPayoutLine]] = {}
    for line in await _lines(session, user, [run.id for run in runs]):
        by_run.setdefault(line.run_id, []).append(line)
    return [_run_read(run, by_run.get(run.id, [])) for run in runs]


# ── Changing a draft ────────────────────────────────────────────────────────


async def _new_line(
    session: AsyncSession, user: User, run: ConsultantPayoutRun, employee_id: uuid.UUID
) -> ConsultantPayoutLine:
    line = ConsultantPayoutLine(
        tenant_id=user.tenant_id,
        run_id=run.id,
        employee_id=employee_id,
        section=DEFAULT_SECTION,
    )
    session.add(line)
    await session.flush()
    return line


def _hold(
    session: AsyncSession,
    user: User,
    run: ConsultantPayoutRun,
    line: ConsultantPayoutLine,
    sheet: Timesheet,
) -> None:
    """Put the week on the line. The unique timesheet_id refuses a week held elsewhere."""
    session.add(
        ConsultantPayoutWeek(
            tenant_id=user.tenant_id, run_id=run.id, line_id=line.id, timesheet_id=sheet.id
        )
    )


async def create_run(
    session: AsyncSession,
    user: User,
    *,
    legal_entity_id: uuid.UUID | None,
    cut_off: date,
    pay_date: date,
    request: Request | None = None,
) -> dict[str, Any]:
    """A draft run of every eligible week, one line per consultant."""
    permissions.require_hr(user, detail=_HR_ONLY)
    if legal_entity_id is not None:
        entity = await session.scalar(
            tenant_scoped(
                select(LegalEntity.id).where(LegalEntity.id == legal_entity_id), LegalEntity, user
            )
        )
        if entity is None:
            raise _not_found("Company not found")
    rule_set = await tds_non_salary.rules_on(session, user, pay_date)
    weeks = await eligible_weeks(session, user, legal_entity_id=legal_entity_id, cut_off=cut_off)
    by_person: dict[uuid.UUID, list[Timesheet]] = {}
    for sheet in weeks:
        by_person.setdefault(sheet.employee_id, []).append(sheet)
    run = ConsultantPayoutRun(
        tenant_id=user.tenant_id,
        legal_entity_id=legal_entity_id,
        cut_off=cut_off,
        pay_date=pay_date,
        status=PayoutRunStatus.DRAFT,
        created_by_user_id=user.id,
        computed_by_user_id=user.id,
    )
    session.add(run)
    try:
        await session.flush()
        for employee_id, sheets in by_person.items():
            line = await _new_line(session, user, run, employee_id)
            for sheet in sheets:
                _hold(session, user, run, line, sheet)
            await session.flush()
            await _reprice(session, user, run, line, rule_set)
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.CREATE,
            entity_type="consultant_payout_run",
            entity_id=run.id,
            actor=user,
            changes={
                "legal_entity_id": str(legal_entity_id) if legal_entity_id else None,
                "cut_off": cut_off.isoformat(),
                "pay_date": pay_date.isoformat(),
                "consultants": len(by_person),
                "weeks": len(weeks),
            },
            summary=(
                f"Drafted a consultant payout run to {cut_off:%d %b %Y}, paying "
                f"{pay_date:%d %b %Y}: {len(by_person)} consultant(s)"
            ),
            request=request,
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise _conflict(_TAKEN_RACE) from exc
    return await get_run(session, user, run.id)


async def include_week(
    session: AsyncSession,
    user: User,
    run_id: uuid.UUID,
    timesheet_id: uuid.UUID,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """Add one approved, unpaid week a run did not take on its own (one approved
    before payouts were released)."""
    run = await _draft(session, user, run_id)
    run.computed_by_user_id = user.id
    sheet = await timesheets._locked_sheet(session, user, timesheet_id)
    if sheet is None:
        raise _not_found("Timesheet not found")
    employee = await _employee(session, user, sheet.employee_id)
    if employee is None or not is_consultant(employee):
        raise _conflict("Only a consultant's week is paid in a consultant payout.")
    if sheet.status is not TimesheetStatus.APPROVED:
        raise _conflict("Only an approved week can be paid.")
    if sheet.payroll_adjustment_id is not None:
        adjustment = await session.scalar(
            tenant_scoped(
                select(PayrollAdjustment).where(
                    PayrollAdjustment.id == sheet.payroll_adjustment_id
                ),
                PayrollAdjustment,
                user,
            )
        )
        if adjustment is None or not adjustment_never_paid(adjustment):
            raise _conflict(
                "This week was paid through payroll, or is still waiting there. Cancel it "
                "under Payroll → Adjustments first if it should go into a consultant payout."
            )
    if await timesheets._in_payout_run(session, user, sheet):
        raise _conflict("This week is already in a consultant payout.")
    if run.legal_entity_id is not None and employee.legal_entity_id != run.legal_entity_id:
        raise _conflict("This consultant is paid by another company than this run's.")
    if sheet.week_start + timedelta(days=6) > run.cut_off:
        raise _conflict("This week ends after the run's cut-off.")
    line = await session.scalar(
        tenant_scoped(
            select(ConsultantPayoutLine).where(
                ConsultantPayoutLine.run_id == run.id,
                ConsultantPayoutLine.employee_id == employee.id,
            ),
            ConsultantPayoutLine,
            user,
        )
    )
    if line is not None and line.excluded_reason:
        raise _conflict("This consultant was taken out of this run. Pay the week in another run.")
    rule_set = await tds_non_salary.rules_on(session, user, run.pay_date)
    try:
        if line is None:
            line = await _new_line(session, user, run, employee.id)
        _hold(session, user, run, line, sheet)
        await session.flush()
        await _reprice(session, user, run, line, rule_set)
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="consultant_payout_run",
            entity_id=run.id,
            actor=user,
            changes={"included_timesheet_id": str(sheet.id), "employee_id": str(employee.id)},
            summary=(
                f"Added {employee.full_name}'s week of {sheet.week_start:%d %b %Y} to a "
                "consultant payout run"
            ),
            request=request,
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise _conflict(_TAKEN_RACE) from exc
    return await get_run(session, user, run.id)


async def recalculate(
    session: AsyncSession, user: User, run_id: uuid.UUID, *, request: Request | None = None
) -> dict[str, Any]:
    """Price every line again from its weeks and the records as they are now.

    A line whose problems are fixed comes back; a line HR took out stays out.
    """
    run = await _draft(session, user, run_id)
    run.computed_by_user_id = user.id
    rule_set = await tds_non_salary.rules_on(session, user, run.pay_date)
    lines = [line for line in await _lines(session, user, [run.id]) if not line.excluded_reason]
    for line in lines:
        await _reprice(session, user, run, line, rule_set)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="consultant_payout_run",
        entity_id=run.id,
        actor=user,
        changes={"recalculated_lines": len(lines)},
        summary="Recalculated a consultant payout run",
        request=request,
    )
    await session.commit()
    return await get_run(session, user, run.id)


async def exclude_line(
    session: AsyncSession,
    user: User,
    run_id: uuid.UUID,
    line_id: uuid.UUID,
    *,
    reason: str,
    request: Request | None = None,
) -> dict[str, Any]:
    """Take a consultant out of this run; their weeks are free for a later one."""
    clean = (reason or "").strip()
    if not clean:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Say why this consultant is left out of the run.",
        )
    run = await _draft(session, user, run_id)
    run.computed_by_user_id = user.id
    line = await session.scalar(
        tenant_scoped(
            select(ConsultantPayoutLine).where(
                ConsultantPayoutLine.id == line_id, ConsultantPayoutLine.run_id == run.id
            ),
            ConsultantPayoutLine,
            user,
        )
    )
    if line is None:
        raise _not_found("Payout line not found")
    if line.excluded_reason:
        raise _conflict("This consultant is already left out of the run.")
    released = len(await _line_weeks(session, user, line.id))
    await session.execute(
        delete(ConsultantPayoutWeek).where(
            ConsultantPayoutWeek.line_id == line.id,
            ConsultantPayoutWeek.tenant_id == user.tenant_id,
        )
    )
    line.excluded = True
    line.excluded_reason = clean
    line.hours = line.gross = line.gst_amount = Decimal(0)
    line.tds_base = line.tds_rate = line.tds_amount = line.net_payable = Decimal(0)
    line.tds_due = line.tds_carried_out = Decimal(0)
    line.tds_note = None
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="consultant_payout_run",
        entity_id=run.id,
        actor=user,
        changes={
            "excluded_line_id": str(line.id),
            "employee_id": str(line.employee_id),
            "reason": clean,
            "weeks_released": released,
        },
        summary=f"Left a consultant out of a payout run: {clean}",
        request=request,
    )
    await session.commit()
    return await get_run(session, user, run.id)


async def delete_run(
    session: AsyncSession, user: User, run_id: uuid.UUID, *, request: Request | None = None
) -> None:
    """Delete a draft; every week in it is free for a later run."""
    run = await _draft(session, user, run_id)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="consultant_payout_run",
        entity_id=run.id,
        actor=user,
        changes={"cut_off": run.cut_off.isoformat(), "pay_date": run.pay_date.isoformat()},
        summary=f"Deleted the draft consultant payout run paying {run.pay_date:%d %b %Y}",
        request=request,
    )
    # Its lines and week rows go with it (ON DELETE CASCADE).
    await session.execute(
        delete(ConsultantPayoutRun).where(
            ConsultantPayoutRun.id == run.id, ConsultantPayoutRun.tenant_id == user.tenant_id
        )
    )
    await session.commit()


# ── Approve, pay, and the consultant's own view ─────────────────────────────


_STALE = (
    "The figures have changed since this run was priced (another run was approved, a rate or "
    "opening changed, or a record was updated). Recalculate the run, check it, then approve."
)


async def _require_figures_current(
    session: AsyncSession,
    user: User,
    run: ConsultantPayoutRun,
    lines: list[ConsultantPayoutLine],
) -> None:
    """Refuse approval when pricing the run again would give other figures.

    Another run approved since (its fees count to the year so far, its carry is
    used), a changed rate, opening or consultant record: HR recalculates and
    checks before anyone signs. Pricing here reads only; nothing is written.
    """
    rule_set = await tds_non_salary.rules_on(session, user, run.pay_date)
    with session.no_autoflush:
        for line in lines:
            if line.excluded_reason:
                continue
            figures = await _price_line(session, user, run, line, rule_set)
            if not figures.matches(line):
                raise _conflict(_STALE)


async def approve_run(
    session: AsyncSession,
    user: User,
    run_id: uuid.UUID,
    *,
    confirm: Confirm,
    request: Request | None = None,
) -> dict[str, Any]:
    """Lock a draft run for payment. From here its figures are never priced again.

    Like the regular payroll run, whoever prepared it cannot approve it, and
    approving needs fresh proof of identity. A line held back for a problem must
    be fixed and recalculated, or left out by hand, first; and if the figures
    would come out differently priced now, the run is recalculated first.
    """
    run = await _draft(session, user, run_id)
    if run.computed_by_user_id is not None and run.computed_by_user_id == user.id:
        raise permissions.forbidden(
            "You prepared these figures, so somebody else has to approve the run. Payouts are "
            "maker-checker: the person who prepares the numbers does not sign them off."
        )
    # Approving locks everybody on the run, so the approver's department or location
    # scope must cover them all, and the checks below must see them all.
    if hr_scope.current_scope() is not None:
        on_run = await session.scalars(
            tenant_scoped(
                select(ConsultantPayoutLine.employee_id).where(
                    ConsultantPayoutLine.run_id == run.id
                ),
                ConsultantPayoutLine,
                user,
            ).execution_options(hr_scope_bypass=True)
        )
        _require_scope_covers(set(on_run.all()), doing="approve")
    lines = await _lines(session, user, [run.id], everyone=True)
    held = [line for line in lines if line.excluded and not line.excluded_reason]
    if held:
        raise _conflict(
            f"{len(held)} consultant(s) are held back for a problem. Fix it and recalculate, "
            "or leave them out of the run, before approving."
        )
    if not any(not line.excluded for line in lines):
        raise _conflict("There is nobody to pay in this run.")
    await _require_figures_current(session, user, run, lines)
    await confirm()
    run.status = PayoutRunStatus.APPROVED
    run.approved_by_user_id = user.id
    run.approved_at = datetime.now(UTC)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE,
        entity_type="consultant_payout_run",
        entity_id=run.id,
        actor=user,
        changes={"step": "approve", **_totals(lines)},
        summary=f"Approved the consultant payout run paying {run.pay_date:%d %b %Y}",
        request=request,
    )
    await session.commit()
    return await get_run(session, user, run.id)


async def mark_paid(
    session: AsyncSession,
    user: User,
    run_id: uuid.UUID,
    *,
    paid_on: date,
    reference: str,
    request: Request | None = None,
) -> dict[str, Any]:
    """Record that an approved run was paid, with the bank reference."""
    permissions.require_hr(user, detail=_HR_ONLY)
    clean = (reference or "").strip()
    if not clean:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Enter the payment reference from the bank.",
        )
    run = await _run(session, user, run_id, lock=True)
    if run.status is not PayoutRunStatus.APPROVED:
        raise _conflict("Only an approved payout run can be marked paid.")
    run.status = PayoutRunStatus.PAID
    run.paid_on = paid_on
    run.payment_reference = clean
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="consultant_payout_run",
        entity_id=run.id,
        actor=user,
        changes={"step": "paid", "paid_on": paid_on.isoformat(), "payment_reference": clean},
        summary=f"Marked the consultant payout run paying {run.pay_date:%d %b %Y} as paid",
        request=request,
    )
    await session.commit()
    return await get_run(session, user, run.id)


async def my_payouts(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """The caller's own payouts from approved and paid runs, newest pay date first."""
    if user.employee_id is None:
        return []
    rows = (
        await session.execute(
            tenant_scoped(
                select(ConsultantPayoutLine, ConsultantPayoutRun)
                .join(ConsultantPayoutRun, ConsultantPayoutRun.id == ConsultantPayoutLine.run_id)
                .where(
                    ConsultantPayoutLine.employee_id == user.employee_id,
                    ConsultantPayoutLine.excluded.is_(False),
                    ConsultantPayoutRun.status.in_(
                        [PayoutRunStatus.APPROVED, PayoutRunStatus.PAID]
                    ),
                )
                .order_by(ConsultantPayoutRun.pay_date.desc(), ConsultantPayoutLine.id),
                ConsultantPayoutLine,
                user,
            )
        )
    ).all()
    return [
        {
            "line_id": str(line.id),
            "run_id": str(run.id),
            "pay_date": run.pay_date.isoformat(),
            "status": run.status.value,
            "paid_on": run.paid_on.isoformat() if run.paid_on else None,
            "hours": _text(line.hours),
            "gross": _text(line.gross),
            "gst_amount": _text(line.gst_amount),
            "section": line.section.value,
            "tds_due": _text(line.tds_due),
            "tds_amount": _text(line.tds_amount),
            "tds_carried_out": _text(line.tds_carried_out),
            "net_payable": _text(line.net_payable),
        }
        for line, run in rows
    ]


async def consultant_cost_by_month(
    session: AsyncSession, user: User, *, start: date, end: date
) -> dict[tuple[uuid.UUID, int, int], Decimal]:
    """What each consultant cost, by the month the work was done: (employee, year, month).

    Taken from the priced days of weeks in approved or paid payout runs, left-out
    lines excluded: the fee before GST, which a company claims back. A week
    worked on 28 Sep to 4 Oct and paid on 15 Oct costs September for the days
    in September and October for the rest; nothing lands in the month paid.
    """
    rows = await session.execute(
        tenant_scoped(
            select(ConsultantPayoutLine.employee_id, ConsultantPayoutWeek.breakdown)
            .join(ConsultantPayoutLine, ConsultantPayoutLine.id == ConsultantPayoutWeek.line_id)
            .join(ConsultantPayoutRun, ConsultantPayoutRun.id == ConsultantPayoutWeek.run_id)
            .join(Timesheet, Timesheet.id == ConsultantPayoutWeek.timesheet_id)
            .where(
                ConsultantPayoutLine.excluded.is_(False),
                ConsultantPayoutRun.status.in_([PayoutRunStatus.APPROVED, PayoutRunStatus.PAID]),
                Timesheet.week_start >= start - timedelta(days=6),
                Timesheet.week_start <= end,
            ),
            ConsultantPayoutWeek,
            user,
        )
    )
    costs: dict[tuple[uuid.UUID, int, int], Decimal] = {}
    for employee_id, breakdown in rows:
        for day in breakdown or []:
            worked = date.fromisoformat(day["work_date"])
            if not start <= worked <= end:
                continue
            key = (employee_id, worked.year, worked.month)
            costs[key] = costs.get(key, Decimal(0)) + _money(day["amount"])
    return costs
