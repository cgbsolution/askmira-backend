"""Consultant payout runs: approved hours entries become one payout line per consultant.

HR picks a company (or every company), a cut-off and a pay date. The run takes
every approved hours entry ending on or before the cut-off that is not in
another run. HR moves the cut-off to take more.

Each line:
- gross = the hours of each of the consultant's entries at the rate in force:
  an entry given day by day at each day's rate (the consultant's rate for the
  entry's project, else their general rate); an entry given as a total at the
  rate on its last day, held back if the rate changed during the period; a
  fixed-monthly consultant's entries instead at their monthly fee, each calendar
  day worth the fee in force that day over the days in its month (hours not priced);
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

A line with a problem HR must fix (no rate for a day worked, no monthly fee for
a day of a fixed-monthly period, a rate change inside a total-only period, no
bank account, GST without a GSTIN, a pay date before one of theirs already
approved this year) is held back — excluded, keeping its entries — until the
data is fixed and the run recalculated. A line HR excludes by hand releases its
entries for a later run. Only a draft changes.
"""

from __future__ import annotations

import calendar
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
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
from app.models.consultant_hours import (
    ConsultantHoursAttachment,
    ConsultantHoursEntry,
    HoursEntryStatus,
)
from app.models.consultant_payouts import (
    ConsultantPayoutEntry,
    ConsultantPayoutLine,
    ConsultantPayoutRun,
    ConsultantTdsOpening,
    PayoutRunStatus,
)
from app.models.contractors import ContractorKind, TdsSection
from app.models.employee import ConsultantPayBasis, Employee, EmploymentType
from app.models.org import LegalEntity
from app.models.payroll import PayrollRuleSet
from app.models.user import User
from app.services import audit, tds_non_salary, timesheets
from app.services.consultants import fees
from app.services.consultants.hours import period_label
from app.services.payroll.run import _require_scope_covers

__all__ = [
    "approve_run",
    "consultant_cost_by_month",
    "create_run",
    "delete_run",
    "eligible_entries",
    "exclude_line",
    "get_run",
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
NO_LONGER_APPROVED = (
    "These hours are no longer approved. Leave this line out or recalculate after they're "
    "approved again."
)

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


_TAKEN_RACE = "Another payout run took some of these hours. Try again."


# ── Which entries ───────────────────────────────────────────────────────────


def _held_in_a_run():
    return (
        select(ConsultantPayoutEntry.id)
        .where(ConsultantPayoutEntry.entry_id == ConsultantHoursEntry.id)
        .exists()
    )


async def eligible_entries(
    session: AsyncSession,
    user: User,
    *,
    legal_entity_id: uuid.UUID | None,
    cut_off: date,
) -> list[ConsultantHoursEntry]:
    """Approved consultant hours a new run takes: ending by the cut-off and in no run."""
    permissions.require_hr(user, detail=_HR_ONLY)
    stmt = (
        select(ConsultantHoursEntry)
        .join(Employee, Employee.id == ConsultantHoursEntry.employee_id)
        .where(
            Employee.employment_type == EmploymentType.CONSULTANT,
            ConsultantHoursEntry.status == HoursEntryStatus.APPROVED,
            ConsultantHoursEntry.period_end <= cut_off,
            ~_held_in_a_run(),
        )
        .order_by(ConsultantHoursEntry.employee_id, ConsultantHoursEntry.period_start)
    )
    if legal_entity_id is not None:
        stmt = stmt.where(Employee.legal_entity_id == legal_entity_id)
    # Lock the entries the run takes, as reopening does: an entry reopened meanwhile is
    # re-read once that commits, and is no longer APPROVED, so it is not taken.
    stmt = stmt.with_for_update(of=ConsultantHoursEntry)
    return list((await session.scalars(tenant_scoped(stmt, ConsultantHoursEntry, user))).all())


# ── Pricing ─────────────────────────────────────────────────────────────────


def by_the_day(entry: ConsultantHoursEntry) -> bool:
    """Whether the hours were given day by day. A grid of day statuses with no hours
    (Week-offs marked, the total typed) is a total-only entry."""
    return any(Decimal(str(day.get("hours") or 0)) > 0 for day in entry.days or [])


def rate_changed(period: str) -> str:
    """Why a total-only entry is held: the rate moved inside its period."""
    return f"The hourly rate changed during {period}. Enter the hours day by day."


@dataclass
class _PricedEntry:
    hours: Decimal = Decimal(0)
    gross: Decimal = Decimal(0)
    breakdown: list[dict[str, Any]] = field(default_factory=list)
    #: Days with hours but no rate (an entry given day by day).
    missing: set[date] = field(default_factory=set)
    #: What holds the whole entry back (an entry given as a total).
    problems: list[str] = field(default_factory=list)
    #: A fixed-monthly entry's month parts, unrounded, in breakdown order.
    exact: list[Decimal] = field(default_factory=list)


def no_monthly_fee(period: str) -> str:
    """Why a fixed-monthly entry is held: a day in its period has no fee in force."""
    return f"No monthly fee for {period}"


def _entry_hours(entry: ConsultantHoursEntry) -> Decimal:
    """The billable hours on the entry: the days' hours when given day by day, else the total."""
    if by_the_day(entry):
        return sum((Decimal(str(day.get("hours") or 0)) for day in entry.days), Decimal(0))
    return Decimal(str(entry.total_billable_hours))


def _price_fixed_entry(
    entry: ConsultantHoursEntry, history: list[tuple[date, Decimal]]
) -> _PricedEntry:
    """A fixed-monthly consultant's entry: each calendar day of its period is worth the
    fee in force that day divided by the days in its month. Each month part is kept
    exact (one division) in `exact`; `_round_fixed_months` rounds each month once
    across the whole line, so a full month is the fee exactly however it was entered.

    The hours are kept for the record and never priced. A day with no fee holds
    the whole entry back: every month part is then 0 and lists no fees, only the
    days without one (`missing_days`).
    """
    priced = _PricedEntry(hours=_entry_hours(entry))
    period = period_label(entry.period_start, entry.period_end)
    # (year, month) -> fee -> days at that fee, in the order the fees came; None = no fee.
    months: dict[tuple[int, int], dict[Decimal | None, int]] = {}
    for offset in range((entry.period_end - entry.period_start).days + 1):
        day = entry.period_start + timedelta(days=offset)
        fee = fees.in_force(history, day)
        at_fee = months.setdefault((day.year, day.month), {})
        at_fee[fee] = at_fee.get(fee, 0) + 1
    missing_fee = any(None in at_fee for at_fee in months.values())
    if missing_fee:
        priced.problems.append(no_monthly_fee(period))
    for (year, month), at_fee in months.items():
        in_month = calendar.monthrange(year, month)[1]
        priced_days = {fee: days for fee, days in at_fee.items() if fee is not None}
        exact = (
            Decimal(0)
            if missing_fee
            else sum((fee * days for fee, days in priced_days.items()), Decimal(0)) / in_month
        )
        shown = (
            []
            if missing_fee
            else [{"fee": _text(fee), "days": days} for fee, days in priced_days.items()]
        )
        priced.exact.append(exact)
        priced.breakdown.append(
            {
                "month": f"{year:04d}-{month:02d}",
                "days": sum(at_fee.values()),
                "days_in_month": in_month,
                "fee": shown[0]["fee"] if len(shown) == 1 else None,
                "fees": shown,
                "missing_days": at_fee.get(None, 0),
                "amount": _text(_money(exact)),
            }
        )
    priced.gross = _money(sum((_money(x) for x in priced.exact), Decimal(0)))
    return priced


def _round_fixed_months(priced_entries: list[_PricedEntry]) -> None:
    """Round each month once across a line's fixed-monthly entries.

    The month is worth its exact share summed over all the line's entries, rounded to
    the paisa once, so a month entered as several entries still totals the fee
    exactly. For display each entry's month part is its own share rounded, and the
    last entry (by period) holding that month takes the remainder paisa, so the
    parts add up to the month. Entries arrive in period order.
    """
    months: dict[str, list[tuple[_PricedEntry, int]]] = {}
    for priced in priced_entries:
        for index, item in enumerate(priced.breakdown):
            if "month" in item:
                months.setdefault(item["month"], []).append((priced, index))
    for parts in months.values():
        total = _money(sum((p.exact[i] for p, i in parts), Decimal(0)))
        given = Decimal(0)
        for position, (priced, index) in enumerate(parts):
            share = total - given if position == len(parts) - 1 else _money(priced.exact[index])
            given += share
            priced.breakdown[index]["amount"] = _text(share)
    for priced in priced_entries:
        if priced.exact:
            priced.gross = _money(
                sum(
                    (Decimal(item["amount"]) for item in priced.breakdown if "month" in item),
                    Decimal(0),
                )
            )


async def _price_entry(
    session: AsyncSession,
    user: User,
    entry: ConsultantHoursEntry,
    fee_history: list[tuple[date, Decimal]] | None = None,
) -> _PricedEntry:
    """The entry at the rate in force: day by day when the days were given, else
    the total at the rate on the last day, provided that rate held all period.

    A fixed-monthly consultant's entry (`fee_history` given) is priced from their
    monthly fee instead (`_price_fixed_entry`).
    """
    if fee_history is not None:
        return _price_fixed_entry(entry, fee_history)
    priced = _PricedEntry()
    if by_the_day(entry):
        for day in entry.days:
            hours = Decimal(str(day["hours"]))
            if hours <= 0:
                continue
            worked = date.fromisoformat(day["date"])
            rate = await timesheets._rate_on(
                session, user, entry.employee_id, worked, entry.project_id
            )
            amount = _money(hours * rate) if rate is not None else Decimal("0.00")
            if rate is None:
                priced.missing.add(worked)
            priced.hours += hours
            priced.gross += amount
            priced.breakdown.append(
                {
                    "date": worked.isoformat(),
                    "hours": _text(hours),
                    "rate": _text(rate) if rate is not None else None,
                    "amount": _text(amount),
                }
            )
        priced.gross = _money(priced.gross)
        return priced

    hours = Decimal(str(entry.total_billable_hours))
    period = period_label(entry.period_start, entry.period_end)
    rate = await timesheets._rate_on(
        session, user, entry.employee_id, entry.period_end, entry.project_id
    )
    if rate is None:
        priced.problems.append(f"No hourly rate for {period}")
    else:
        for offset in range((entry.period_end - entry.period_start).days):
            day = entry.period_start + timedelta(days=offset)
            on_day = await timesheets._rate_on(
                session, user, entry.employee_id, day, entry.project_id
            )
            if on_day is None:
                priced.problems.append(f"No hourly rate for {period}")
                break
            if on_day != rate:
                priced.problems.append(rate_changed(period))
                break
    # A total is never priced at a rate that did not hold for the whole period.
    priced_rate = rate if not priced.problems else None
    amount = _money(hours * priced_rate) if priced_rate is not None else Decimal("0.00")
    priced.hours = hours
    priced.gross = amount
    priced.breakdown.append(
        {
            "period_start": entry.period_start.isoformat(),
            "period_end": entry.period_end.isoformat(),
            "hours": _text(hours),
            "rate": _text(priced_rate) if priced_rate is not None else None,
            "amount": _text(amount),
        }
    )
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


async def _hours_entries(
    session: AsyncSession, user: User, entry_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ConsultantHoursEntry]:
    if not entry_ids:
        return {}
    found = await session.scalars(
        tenant_scoped(
            select(ConsultantHoursEntry).where(ConsultantHoursEntry.id.in_(entry_ids)),
            ConsultantHoursEntry,
            user,
        ).execution_options(populate_existing=True)
    )
    return {entry.id: entry for entry in found.all()}


async def _line_entries(
    session: AsyncSession, user: User, line_id: uuid.UUID
) -> list[ConsultantPayoutEntry]:
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(ConsultantPayoutEntry).where(ConsultantPayoutEntry.line_id == line_id),
                    ConsultantPayoutEntry,
                    user,
                )
            )
        ).all()
    )


@dataclass
class _LineFigures:
    """A line priced from scratch, not yet written to it."""

    entries: list[tuple[ConsultantPayoutEntry, _PricedEntry]]
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
    """Price a line from scratch from its entries and the consultant's record as it is now.

    Reads only: nothing on the line or its entries changes.
    """
    employee = await _employee(session, user, line.employee_id)
    if employee is None:
        raise _not_found("Consultant not found")
    hours = gross = Decimal(0)
    missing: set[date] = set()
    held: list[str] = []
    entries: list[tuple[ConsultantPayoutEntry, _PricedEntry]] = []
    rows = await _line_entries(session, user, line.id)
    sources = await _hours_entries(session, user, [row.entry_id for row in rows])
    # The basis in force is the consultant's current one: a change is refused while
    # any of their hours are unpaid, so no unpaid entry straddles one.
    fee_history = (
        await fees.fee_history(session, user, employee.id)
        if employee.pay_basis is ConsultantPayBasis.FIXED_MONTHLY
        else None
    )
    for row in sorted(rows, key=lambda r: sources[r.entry_id].period_start):
        priced = await _price_entry(session, user, sources[row.entry_id], fee_history)
        entries.append((row, priced))
    if fee_history is not None:
        _round_fixed_months([priced for _, priced in entries])
    for _, priced in entries:
        hours += priced.hours
        gross += priced.gross
        missing |= priced.missing
        held.extend(priced.problems)
    gross = _money(gross)

    problems = [f"No hourly rate on {day.isoformat()}" for day in sorted(missing)]
    problems.extend(held)
    if any(source.status is not HoursEntryStatus.APPROVED for source in sources.values()):
        problems.append(NO_LONGER_APPROVED)
    if not await _has_bank_account(session, user, employee.id):
        problems.append(NO_BANK_ACCOUNT)
    if employee.gst_applicable and not (employee.gstin or "").strip():
        problems.append(NO_GSTIN)
    later = await _latest_approved_pay_date(session, user, run, employee.id)
    if later is not None and later > run.pay_date:
        problems.append(
            f"Paid on {later.isoformat()} in an approved run; this run's pay date is earlier. "
            "Delete this draft and start one with a later pay date."
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
        entries=entries,
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
    """Price a line from scratch and write the figures to it and its entries."""
    figures = await _price_line(session, user, run, line, rule_set)
    for row, priced in figures.entries:
        row.hours, row.gross, row.breakdown = priced.hours, priced.gross, priced.breakdown
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
    entries: list[tuple[ConsultantPayoutEntry, ConsultantHoursEntry]],
    files: dict[uuid.UUID, list[ConsultantHoursAttachment]],
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
        "entries": [
            {
                "id": str(row.id),
                "entry_id": str(row.entry_id),
                "period_start": source.period_start.isoformat(),
                "period_end": source.period_end.isoformat(),
                "project_id": str(source.project_id) if source.project_id else None,
                "hours": _text(row.hours),
                "gross": _text(row.gross),
                "breakdown": list(row.breakdown or []),
                "attachments": [
                    {"id": str(file.id), "file_name": file.file_name}
                    for file in files.get(row.entry_id, [])
                ],
            }
            for row, source in sorted(entries, key=lambda pair: pair[1].period_start)
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
    """A run with its lines, each with its hours entries priced and their files."""
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
    entries: dict[uuid.UUID, list[tuple[ConsultantPayoutEntry, ConsultantHoursEntry]]] = {}
    rows = (
        await session.execute(
            tenant_scoped(
                select(ConsultantPayoutEntry, ConsultantHoursEntry)
                .join(
                    ConsultantHoursEntry,
                    ConsultantHoursEntry.id == ConsultantPayoutEntry.entry_id,
                )
                .where(ConsultantPayoutEntry.run_id == run.id),
                ConsultantPayoutEntry,
                user,
            )
        )
    ).all()
    for row, source in rows:
        entries.setdefault(row.line_id, []).append((row, source))
    files: dict[uuid.UUID, list[ConsultantHoursAttachment]] = {}
    if rows:
        attached = await session.scalars(
            tenant_scoped(
                select(ConsultantHoursAttachment)
                .where(ConsultantHoursAttachment.entry_id.in_([row.entry_id for row, _ in rows]))
                .order_by(ConsultantHoursAttachment.uploaded_at),
                ConsultantHoursAttachment,
                user,
            )
        )
        for file in attached.all():
            files.setdefault(file.entry_id, []).append(file)
    lines.sort(
        key=lambda line: (
            employees[line.employee_id].employee_code if line.employee_id in employees else ""
        )
    )
    return {
        **_run_read(run, lines),
        "lines": [
            _line_read(line, employees.get(line.employee_id), entries.get(line.id, []), files)
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
    entry: ConsultantHoursEntry,
) -> None:
    """Put the entry on the line. The unique entry_id refuses an entry held elsewhere."""
    session.add(
        ConsultantPayoutEntry(
            tenant_id=user.tenant_id, run_id=run.id, line_id=line.id, entry_id=entry.id
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
    """A draft run of every eligible hours entry, one line per consultant."""
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
    taken = await eligible_entries(session, user, legal_entity_id=legal_entity_id, cut_off=cut_off)
    by_person: dict[uuid.UUID, list[ConsultantHoursEntry]] = {}
    for entry in taken:
        by_person.setdefault(entry.employee_id, []).append(entry)
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
        for employee_id, held in by_person.items():
            line = await _new_line(session, user, run, employee_id)
            for entry in held:
                _hold(session, user, run, line, entry)
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
                "hours_entries": len(taken),
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


async def recalculate(
    session: AsyncSession, user: User, run_id: uuid.UUID, *, request: Request | None = None
) -> dict[str, Any]:
    """Price every line again from its entries and the records as they are now.

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
    """Take a consultant out of this run; their hours entries are free for a later one."""
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
    released = len(await _line_entries(session, user, line.id))
    await session.execute(
        delete(ConsultantPayoutEntry).where(
            ConsultantPayoutEntry.line_id == line.id,
            ConsultantPayoutEntry.tenant_id == user.tenant_id,
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
            "hours_entries_released": released,
        },
        summary=f"Left a consultant out of a payout run: {clean}",
        request=request,
    )
    await session.commit()
    return await get_run(session, user, run.id)


async def delete_run(
    session: AsyncSession, user: User, run_id: uuid.UUID, *, request: Request | None = None
) -> None:
    """Delete a draft; every hours entry in it is free for a later run."""
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
    # Its lines and entry rows go with it (ON DELETE CASCADE).
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
    # A file still on its way to the bank would pay everyone a second time.
    from app.models.disbursement import DisbursementBatch
    from app.services.disbursement.service import UNSENT_STANDING

    unsent = await session.scalar(
        tenant_scoped(
            select(DisbursementBatch.id)
            .where(
                DisbursementBatch.consultant_payout_run_id == run.id,
                DisbursementBatch.status.in_(UNSENT_STANDING),
            )
            .limit(1),
            DisbursementBatch,
            user,
        )
    )
    if unsent is not None:
        raise _conflict(
            "This run has a bank file that hasn't been sent. Release it to the bank first, "
            "or regenerate it."
        )
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


def _split_by_working_days(
    entry: ConsultantHoursEntry, amount: Decimal
) -> list[tuple[date, Decimal]]:
    """A total-only entry's fee spread over the months it covers.

    By working days: days not marked Week-off (a day with no status counts as worked),
    or by calendar days when no statuses were given. Each month gets its share, and
    the last month what is left, so the shares add up to the fee exactly.
    """
    span = [
        entry.period_start + timedelta(days=offset)
        for offset in range((entry.period_end - entry.period_start).days + 1)
    ]
    week_off = {
        date.fromisoformat(day["date"])
        for day in entry.days or []
        if day.get("status") == "week_off"
    }
    worked = [day for day in span if day not in week_off] or span
    months: dict[tuple[int, int], int] = {}
    for day in worked:
        months[(day.year, day.month)] = months.get((day.year, day.month), 0) + 1
    shares: list[tuple[date, Decimal]] = []
    left = amount
    keys = sorted(months)
    for index, (year, month) in enumerate(keys):
        share = (
            left if index == len(keys) - 1 else _money(amount * months[(year, month)] / len(worked))
        )
        left -= share
        shares.append((date(year, month, 1), share))
    return shares


async def consultant_cost_by_month(
    session: AsyncSession, user: User, *, start: date, end: date
) -> dict[tuple[uuid.UUID, int, int], Decimal]:
    """What each consultant cost, by the month the work was done: (employee, year, month).

    Taken from the priced hours entries in approved or paid payout runs, left-out
    lines excluded: the fee before GST, which a company claims back. Day-by-day
    hours cost the month of each day; a total-only entry crossing a month end is
    split by working days (`_split_by_working_days`); a fixed monthly fee is already
    split by month. Nothing lands in the month paid.
    """
    rows = await session.execute(
        tenant_scoped(
            select(
                ConsultantPayoutLine.employee_id,
                ConsultantPayoutEntry.breakdown,
                ConsultantHoursEntry,
            )
            .join(ConsultantPayoutLine, ConsultantPayoutLine.id == ConsultantPayoutEntry.line_id)
            .join(ConsultantPayoutRun, ConsultantPayoutRun.id == ConsultantPayoutEntry.run_id)
            .join(ConsultantHoursEntry, ConsultantHoursEntry.id == ConsultantPayoutEntry.entry_id)
            .where(
                ConsultantPayoutLine.excluded.is_(False),
                ConsultantPayoutRun.status.in_([PayoutRunStatus.APPROVED, PayoutRunStatus.PAID]),
                ConsultantHoursEntry.period_end >= start,
                ConsultantHoursEntry.period_start <= end,
            ),
            ConsultantPayoutEntry,
            user,
        )
    )
    costs: dict[tuple[uuid.UUID, int, int], Decimal] = {}
    first, last = (start.year, start.month), (end.year, end.month)
    for employee_id, breakdown, entry in rows:
        for item in breakdown or []:
            if "date" in item:
                worked = date.fromisoformat(item["date"])
                shares = [(worked, _money(item["amount"]))] if start <= worked <= end else []
            elif "month" in item:
                # A fixed monthly fee, already split by the month the days fell in.
                month = date.fromisoformat(f"{item['month']}-01")
                shares = (
                    [(month, _money(item["amount"]))]
                    if first <= (month.year, month.month) <= last
                    else []
                )
            else:
                shares = [
                    (month, share)
                    for month, share in _split_by_working_days(entry, _money(item["amount"]))
                    if first <= (month.year, month.month) <= last
                ]
            for worked, amount in shares:
                key = (employee_id, worked.year, worked.month)
                costs[key] = costs.get(key, Decimal(0)) + amount
    return costs
