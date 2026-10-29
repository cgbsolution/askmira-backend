"""A financial year's salary, as payroll pays it: approved months, then the rest projected.

The employee's own tax projection, the regime comparison and the snapshots taken
when a regime changes all need the year's salary. Reading it off the salary in
force on 1 April and multiplying by twelve is wrong for anybody whose year is not
twelve identical months: a mid-year joiner gets a whole year (or, if they had no
salary on 1 April, none at all), a revision in August re-prices April, and
Professional Tax — deductible under the old regime — is left out. The result is a
projection that disagrees with the TDS on the employee's own payslip.

So this works the year out the way a payroll run does (`run.py`,
`tds.compute_monthly_tds`):

- **months already paid** come from approved payslips — what was actually paid,
  after loss of pay, at the salary of the day;
- **months still to come** are projected at the salary in force in each of them,
  prorated for the days employed in a joining or leaving month — and so are
  earlier months no payslip covers, for a year that began on another payroll. A
  month worked before the first salary record is counted at that record, and a
  note says so (`months_from_structure`, which payroll runs use too);
- **Professional Tax** is the salary master's fixed amount when it has one, else
  what the payslips deducted, then the latest month's amount (or the state's
  slab, before any payslip exists) for the months to come;
- **one-off pay** that payroll paid (bonus, incentives, perquisites) is counted
  from the payslips' input lines, except the kinds the tax projection's own
  income sources already report (arrears, in-service encashment), which would
  otherwise be counted twice.
"""

from __future__ import annotations

import calendar
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.employee import Employee
from app.models.exit_workflow import ExitRecord, ExitStatus
from app.models.payroll import (
    STATUTORY_OVERRIDE_CODES,
    ComponentType,
    EmployeeSalary,
    PayrollRuleSet,
    PayrollRun,
    PayrollRunStatus,
    PayrollRunType,
    Payslip,
)
from app.models.user import User
from app.services.payroll import statutory_service
from app.services.payroll.engine import PayrollRuleError, professional_tax_slab

__all__ = ["StructureMonths", "YearPay", "month_span", "months_from_structure", "year_pay"]

ZERO = Decimal(0)
_CLOSED = (PayrollRunStatus.APPROVED, PayrollRunStatus.PAID)
_HRA_CODES = frozenset({"HRA"})
_EMPLOYER_NPS_CODES = frozenset({"NPS_ER", "EMPLOYER_NPS"})
#: Input providers whose income the tax projection's sources already report for
#: the whole year, paid or not (`services/tax/sources.py`).
REPORTED_BY_TAX_SOURCES = frozenset({"arrears", "leave_encashment"})


@dataclass(frozen=True, slots=True)
class YearPay:
    """The year's salary figures the income tax projection reads."""

    #: Recurring taxable salary: approved months as paid, the rest projected.
    taxable_salary: Decimal = ZERO
    basic_plus_da: Decimal = ZERO
    hra: Decimal = ZERO
    employer_nps: Decimal = ZERO
    professional_tax: Decimal = ZERO
    #: Taxable one-off pay payroll has already paid this year.
    one_off_taxable: Decimal = ZERO
    months_paid: int = 0
    months_projected: int = 0
    notes: list[str] = field(default_factory=list)


def _d(value: object) -> Decimal:
    return Decimal(str(value or 0))


async def _last_day(session: AsyncSession, user: User, employee: Employee) -> date | None:
    """The day employment ends: the recorded exit, or an agreed last working day."""
    if employee.date_of_exit:
        return employee.date_of_exit
    return await session.scalar(
        tenant_scoped(
            select(ExitRecord.approved_last_working_day)
            .where(
                ExitRecord.employee_id == employee.id,
                ExitRecord.status != ExitStatus.CANCELLED,
                ExitRecord.approved_last_working_day.is_not(None),
            )
            .order_by(ExitRecord.created_at.desc()),
            ExitRecord,
            user,
        )
    )


async def _pt_rule(
    session: AsyncSession, user: User, state: str, on_date: date
) -> PayrollRuleSet | None:
    return await session.scalar(
        tenant_scoped(
            select(PayrollRuleSet)
            .where(
                PayrollRuleSet.rule_type == "professional_tax",
                PayrollRuleSet.jurisdiction == state,
                PayrollRuleSet.approved_at.is_not(None),
                PayrollRuleSet.effective_from <= on_date,
                or_(PayrollRuleSet.effective_to.is_(None), PayrollRuleSet.effective_to >= on_date),
            )
            .order_by(PayrollRuleSet.effective_from.desc()),
            PayrollRuleSet,
            user,
        )
    )


async def year_pay(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    fy_start: date,
    fy_end: date,
) -> YearPay:
    """The year's salary for one employee: paid months from payslips, the rest projected."""
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        return YearPay()

    totals = {
        "taxable": ZERO,
        "basic": ZERO,
        "hra": ZERO,
        "nps": ZERO,
        "pt": ZERO,
        "one_off": ZERO,
    }
    notes: list[str] = []

    # ── Months already paid ─────────────────────────────────────────────────
    rows = (
        await session.execute(
            tenant_scoped(
                select(Payslip, PayrollRun)
                .join(PayrollRun, Payslip.run_id == PayrollRun.id)
                .where(
                    Payslip.employee_id == employee_id,
                    PayrollRun.status.in_(_CLOSED),
                    PayrollRun.effective_date >= fy_start,
                    PayrollRun.effective_date <= fy_end,
                ),
                Payslip,
                user,
            )
        )
    ).all()
    paid_months: set[tuple[int, int]] = set()
    latest_pt: tuple[tuple[int, int], Decimal] | None = None
    for payslip, run in rows:
        totals["pt"] += _d(payslip.professional_tax)
        for line in payslip.input_lines or []:
            if line.get("skipped") or line.get("provider") in REPORTED_BY_TAX_SOURCES:
                continue
            totals["one_off"] += _d(line.get("taxable_amount"))
        if payslip.is_supplementary:
            continue
        period = (run.period_year, run.period_month)
        paid_months.add(period)
        projection = (payslip.tds_working or {}).get("projection") or {}
        totals["taxable"] += _d(projection.get("this_month_taxable_pay", payslip.gross_earnings))
        totals["basic"] += _d(payslip.pf_wage_base)
        for line in payslip.line_items or []:
            code = str(line.get("code", ""))
            if code in _HRA_CODES:
                totals["hra"] += _d(line.get("amount"))
            elif code in _EMPLOYER_NPS_CODES:
                totals["nps"] += _d(line.get("amount"))
        if latest_pt is None or period > latest_pt[0]:
            latest_pt = (period, _d(payslip.professional_tax))

    # ── Months payroll ran without paying them ──────────────────────────────
    # From the company's first approved AskMira month to its latest, a month
    # with no payslip was not paid (no salary then, or not employed): it is
    # not income. Months before that were the previous payroll's.
    first_here, last_here = await _regular_months(session, user, employee, fy_start, fy_end)
    not_paid: set[tuple[int, int]] = set()
    if first_here is not None and last_here is not None:
        current = first_here
        while current <= last_here:
            if current not in paid_months:
                not_paid.add(current)
            current = (current[0] + 1, 1) if current[1] == 12 else (current[0], current[1] + 1)

    # ── Months still to come (and any earlier month no payslip covers) ──────
    rest = await months_from_structure(
        session,
        user,
        employee,
        fy_start=fy_start,
        fy_end=fy_end,
        skip=paid_months | not_paid,
        assume_by=(
            date(*first_here, calendar.monthrange(*first_here)[1])
            if first_here is not None
            else None
        ),
        latest_pt=latest_pt[1] if latest_pt is not None else None,
    )
    notes.extend(rest.notes)

    return YearPay(
        taxable_salary=totals["taxable"] + rest.taxable,
        basic_plus_da=totals["basic"] + rest.basic_plus_da,
        hra=totals["hra"] + rest.hra,
        employer_nps=totals["nps"] + rest.employer_nps,
        professional_tax=totals["pt"] + rest.professional_tax,
        one_off_taxable=totals["one_off"],
        months_paid=len(paid_months),
        months_projected=len(rest.months),
        notes=notes,
    )


async def _regular_months(
    session: AsyncSession, user: User, employee: Employee, fy_start: date, fy_end: date
) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """The first and latest month of the year the employee's company ran approved payroll."""
    if employee.legal_entity_id is None:
        return None, None
    key = PayrollRun.period_year * 100 + PayrollRun.period_month
    first, last = (
        await session.execute(
            tenant_scoped(
                select(func.min(key), func.max(key)).where(
                    PayrollRun.legal_entity_id == employee.legal_entity_id,
                    PayrollRun.run_type == PayrollRunType.REGULAR,
                    PayrollRun.status.in_(_CLOSED),
                    PayrollRun.effective_date >= fy_start,
                    PayrollRun.effective_date <= fy_end,
                ),
                PayrollRun,
                user,
            )
        )
    ).one()
    if first is None or last is None:
        return None, None
    return (first // 100, first % 100), (last // 100, last % 100)


@dataclass(frozen=True, slots=True)
class StructureMonths:
    """Months worked out from the salary structure, not from payslips."""

    taxable: Decimal = ZERO
    basic_plus_da: Decimal = ZERO
    hra: Decimal = ZERO
    employer_nps: Decimal = ZERO
    professional_tax: Decimal = ZERO
    #: `(year, month)` of every month counted.
    months: tuple[tuple[int, int], ...] = ()
    #: Months worked with no salary record in force, counted at the first later one.
    assumed: tuple[tuple[int, int], ...] = ()
    notes: tuple[str, ...] = ()


async def months_from_structure(
    session: AsyncSession,
    user: User,
    employee: Employee,
    *,
    fy_start: date,
    fy_end: date,
    skip: set[tuple[int, int]] | frozenset[tuple[int, int]] = frozenset(),
    before: tuple[int, int] | None = None,
    assume_by: date | None = None,
    latest_pt: Decimal | None = None,
) -> StructureMonths:
    """The year's months no payslip covers, at the salary in force in each.

    Prorated for the days employed in a joining or leaving month. A month the
    person worked with no salary record yet (the salary master was loaded from a
    later revision) is counted at the first later record and listed in
    `assumed`: leaving it out would project too little income and deduct too
    little tax, which the employer answers for.

    `assume_by` limits that to a later record starting by that day: the end of
    the company's first AskMira month. A salary loaded when the company moved
    to AskMira stands in for the months its old payroll paid; one that starts
    after that month is a first salary (a consultant put on payroll, say), and
    the months before it were not salary at all.

    `skip` is the months a payslip already covers; `before` stops at a month,
    for a payroll run that wants only the months earlier than its own.
    Professional Tax is the salary master's fixed amount when it has one (as
    payroll deducts it), else `latest_pt`, else the state's slab.
    """
    salaries = (
        await session.scalars(
            tenant_scoped(
                select(EmployeeSalary)
                .where(EmployeeSalary.employee_id == employee.id)
                .order_by(EmployeeSalary.effective_from),
                EmployeeSalary,
                user,
            )
        )
    ).all()
    joined = employee.date_of_joining
    leaves = await _last_day(session, user, employee)
    totals = {"taxable": ZERO, "basic": ZERO, "hra": ZERO, "nps": ZERO, "pt": ZERO}
    counted: list[tuple[int, int]] = []
    assumed: list[tuple[int, int]] = []
    assumed_from: date | None = None
    notes: list[str] = []
    state: str | None = None
    state_known = False

    year, month = fy_start.year, fy_start.month
    while date(year, month, 1) <= fy_end:
        days = calendar.monthrange(year, month)[1]
        month_start, month_end = date(year, month, 1), date(year, month, days)
        current = (year, month)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        if before is not None and current >= before:
            break
        if current in skip:
            continue
        first = max(month_start, joined) if joined else month_start
        last = min(month_end, leaves) if leaves else month_end
        if first > last:
            continue
        salary = next(
            (
                s
                for s in reversed(salaries)
                if s.effective_from <= last and (s.effective_to is None or s.effective_to >= first)
            ),
            None,
        )
        if salary is None:
            salary = next((s for s in salaries if s.effective_from > last), None)
            if salary is None or (assume_by is not None and salary.effective_from > assume_by):
                continue
            assumed.append(current)
            assumed_from = assumed_from or salary.effective_from
        share = Decimal((last - first).days + 1) / Decimal(days)
        month_taxable = month_gross = ZERO
        fixed_pt: Decimal | None = None
        for component in salary.components:
            if STATUTORY_OVERRIDE_CODES.get(component.component_code) == "pt":
                fixed_pt = _d(component.monthly_amount)
                continue
            amount = _d(component.monthly_amount) * share
            if component.component_type is ComponentType.EARNING:
                month_gross += amount
                if component.taxable:
                    month_taxable += amount
            if component.counts_toward_pf:
                totals["basic"] += amount
            if component.component_code in _HRA_CODES:
                totals["hra"] += amount
            elif component.component_code in _EMPLOYER_NPS_CODES:
                totals["nps"] += amount
        totals["taxable"] += month_taxable
        counted.append(current)

        if fixed_pt is not None:
            totals["pt"] += fixed_pt
            continue
        if latest_pt is not None:
            totals["pt"] += latest_pt
            continue
        # No payslip yet this year: the state's slab on the month's pay.
        if not state_known:
            resolution = await statutory_service.work_state_for_period(
                session, user, employee, month_start, month_end
            )
            state, state_known = resolution.state_code, True
        if state is None:
            continue
        rule = await _pt_rule(session, user, state, month_end)
        if rule is None:
            continue
        try:
            totals["pt"] += professional_tax_slab(
                rule,
                month_gross,
                month=month_start.month,
                gender=employee.gender.value if employee.gender else None,
            )
        except PayrollRuleError as exc:
            notes.append(f"Professional Tax is not projected: {exc}")
            state = None

    if assumed and assumed_from is not None:
        notes.append(
            f"No salary is recorded for {month_span(assumed)}: counted at the salary from "
            f"{assumed_from:%d %b %Y}. Add the earlier salary in the salary master if it "
            "was different."
        )
    return StructureMonths(
        taxable=totals["taxable"],
        basic_plus_da=totals["basic"],
        hra=totals["hra"],
        employer_nps=totals["nps"],
        professional_tax=totals["pt"],
        months=tuple(counted),
        assumed=tuple(assumed),
        notes=tuple(notes),
    )


def month_span(months: list[tuple[int, int]] | tuple[tuple[int, int], ...]) -> str:
    """'Apr-Aug 2026', or 'Sep 2026' for one month. Months in order."""
    first, last = date(*months[0], 1), date(*months[-1], 1)
    if first == last:
        return f"{first:%b %Y}"
    if first.year == last.year:
        return f"{first:%b}-{last:%b %Y}"
    return f"{first:%b %Y}-{last:%b %Y}"
