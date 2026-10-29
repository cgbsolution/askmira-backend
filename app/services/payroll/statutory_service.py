"""Persistence for the statutory heads that are not monthly payslip lines.

Three things live here:

- **Statutory bonus**, which is annual and computed from a full year of
  attendance, so it gets its own record rather than a payslip line.
- **ESI coverage**, recorded when a contribution period opens so that the
  mid-period ceiling-crossing rule can be applied in month five without having
  to reconstruct what was true in month one.
- **Statutory registers**, assembled from records that already exist. Nothing
  is recomputed here — a register that recomputes could disagree with the
  payslip it is supposed to summarise.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmployeeWorkLocation
from app.models.org import Location
from app.models.payroll import (
    BonusComputation,
    BonusStatus,
    EmployeePfElection,
    EsiCoveragePeriod,
    PayrollRuleSet,
    PayrollRun,
    PayrollRunStatus,
    Payslip,
)
from app.models.user import User
from app.services import audit
from app.services.payroll import companies
from app.services.payroll import registers as register_formats
from app.services.payroll.statutory import (
    PfElection,
    PtStateResolution,
    StatutoryError,
    compute_bonus,
    esi_contribution_period,
    resolve_professional_tax_state,
)

__all__ = [
    "active_pf_election",
    "approve_bonus",
    "build_register",
    "compute_bonus_for_year",
    "disburse_bonus",
    "esi_coverage_for",
    "location_state",
    "open_esi_coverage_period",
    "work_state_for_period",
]


def _unprocessable(exc: Exception) -> HTTPException:
    """Statutory errors name the missing configuration; keep the message.

    Replacing "The bonus configuration is missing 'minimum_percentage'" with a
    generic 422 leaves an admin with no way to find out what to fix.
    """
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


async def location_state(
    session: AsyncSession, user: User, location_id: uuid.UUID | None
) -> str | None:
    """The state code of a work location."""
    if location_id is None:
        return None
    return await session.scalar(
        tenant_scoped(select(Location.state_code).where(Location.id == location_id), Location, user)
    )


async def work_state_for_period(
    session: AsyncSession,
    user: User,
    employee: Employee,
    period_start: date,
    period_end: date,
) -> PtStateResolution:
    """Which state's levies (Professional Tax, LWF) apply to an employee for a period.

    An employee with no recorded transfers resolves from their current location.
    One with a transfer history is resolved from the series, which is what makes
    a mid-period move visible instead of silently applying the new state to the
    whole month — and what prices a past month by the state they worked in then.
    """
    postings = (
        await session.scalars(
            tenant_scoped(
                select(EmployeeWorkLocation)
                .where(
                    EmployeeWorkLocation.employee_id == employee.id,
                    EmployeeWorkLocation.effective_from <= period_end,
                )
                .order_by(EmployeeWorkLocation.effective_from),
                EmployeeWorkLocation,
                user,
            )
        )
    ).all()

    if not postings:
        state = await location_state(session, user, employee.location_id)
        if state is None:
            return PtStateResolution(
                state_code=None,
                needs_review=True,
                reason=(
                    "This employee has no work location, so no Professional Tax state "
                    "can be determined."
                ),
            )
        return PtStateResolution(state_code=state, needs_review=False, reason=f"Works in {state}.")

    history: list[tuple[date, str]] = []
    for posting in postings:
        state = await location_state(session, user, posting.location_id)
        if state:
            history.append((posting.effective_from, state))

    return resolve_professional_tax_state(
        location_history=history, period_start=period_start, period_end=period_end
    )


async def _rule_set_on(
    session: AsyncSession,
    user: User,
    *,
    rule_type: str,
    jurisdiction: str,
    on_date: date,
) -> PayrollRuleSet:
    """The approved rule version in force on a date — never the latest one."""
    rule_set = await session.scalar(
        tenant_scoped(
            select(PayrollRuleSet)
            .where(
                PayrollRuleSet.rule_type == rule_type,
                PayrollRuleSet.jurisdiction == jurisdiction,
                PayrollRuleSet.effective_from <= on_date,
                or_(
                    PayrollRuleSet.effective_to.is_(None),
                    PayrollRuleSet.effective_to >= on_date,
                ),
            )
            .order_by(PayrollRuleSet.effective_from.desc()),
            PayrollRuleSet,
            user,
        )
    )
    if rule_set is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"No {rule_type} rule set for {jurisdiction} is in force on {on_date}. "
                "The Compliance SME supplies and approves it before it can be used."
            ),
        )
    if not rule_set.is_approved:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"The {rule_type} rule set for {jurisdiction} effective "
                f"{rule_set.effective_from} has not been approved by a Compliance SME."
            ),
        )
    return rule_set


# ── The PF election ─────────────────────────────────────────────────────────


async def active_pf_election(
    session: AsyncSession, user: User, employee_id: uuid.UUID, on_date: date
) -> PfElection | None:
    """The employee's PF election in force on a date, or none."""
    row = await session.scalar(
        tenant_scoped(
            select(EmployeePfElection)
            .where(
                EmployeePfElection.employee_id == employee_id,
                EmployeePfElection.effective_from <= on_date,
                or_(
                    EmployeePfElection.effective_to.is_(None),
                    EmployeePfElection.effective_to >= on_date,
                ),
            )
            .order_by(EmployeePfElection.effective_from.desc()),
            EmployeePfElection,
            user,
        )
    )
    if row is None:
        return None
    return PfElection(
        on_actual_wage=row.pf_on_actual_wage,
        vpf_percent=Decimal(str(row.vpf_percentage)) if row.vpf_percentage is not None else None,
        vpf_amount=(
            Decimal(str(row.vpf_fixed_amount)) if row.vpf_fixed_amount is not None else None
        ),
    )


# ── ESI coverage ────────────────────────────────────────────────────────────


async def open_esi_coverage_period(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    on_date: date,
    esi_wage_base: Decimal,
    esi_rules: PayrollRuleSet,
) -> EsiCoveragePeriod | None:
    """Record whether an employee was contributing when the period opened.

    Called for the first month of each contribution period. It is idempotent:
    a recompute of that month must not change what was recorded, because the
    later months of the period have already been decided against it.

    Returns None when the rule set does not define contribution periods, in
    which case the engine falls back to deciding month by month.
    """
    if "contribution_periods" not in esi_rules.rules:
        return None

    try:
        period = esi_contribution_period(on_date.month, esi_rules.rules)
    except StatutoryError as exc:
        raise _unprocessable(exc) from exc

    if on_date.month != period.start_month:
        return None

    label = f"{on_date.year}-{period.label or f'{period.start_month}-{period.end_month}'}"
    existing = await session.scalar(
        tenant_scoped(
            select(EsiCoveragePeriod).where(
                EsiCoveragePeriod.employee_id == employee_id,
                EsiCoveragePeriod.period_label == label,
            ),
            EsiCoveragePeriod,
            user,
        )
    )
    if existing is not None:
        return existing

    threshold = Decimal(str(esi_rules.rules.get("wage_threshold", 0)))
    coverage = EsiCoveragePeriod(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        period_label=label,
        period_start_month=period.start_month,
        period_end_month=period.end_month,
        was_contributing_at_start=esi_wage_base <= threshold,
        opening_wage=esi_wage_base,
    )
    session.add(coverage)
    await session.flush()
    return coverage


async def esi_coverage_for(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    on_date: date,
    esi_rules: PayrollRuleSet | None,
) -> tuple[bool | None, EsiCoveragePeriod | None]:
    """Coverage state at the start of the period containing `on_date`.

    `(None, None)` means no record — the engine then decides from this month's
    wages, which is correct at a period boundary and is the only honest answer
    before coverage tracking has begun.
    """
    if esi_rules is None or "contribution_periods" not in esi_rules.rules:
        return None, None

    try:
        period = esi_contribution_period(on_date.month, esi_rules.rules)
    except StatutoryError:
        return None, None

    # A period that wraps the calendar year belongs to the year it started in.
    year = on_date.year
    if period.start_month > period.end_month and on_date.month <= period.end_month:
        year -= 1

    label = f"{year}-{period.label or f'{period.start_month}-{period.end_month}'}"
    coverage = await session.scalar(
        tenant_scoped(
            select(EsiCoveragePeriod).where(
                EsiCoveragePeriod.employee_id == employee_id,
                EsiCoveragePeriod.period_label == label,
            ),
            EsiCoveragePeriod,
            user,
        )
    )
    if coverage is None:
        return None, None
    return coverage.was_contributing_at_start, coverage


# ── Statutory bonus ─────────────────────────────────────────────────────────


def financial_year_bounds(financial_year: str) -> tuple[date, date]:
    """`"2025-2026"` to its April-March bounds.

    The financial year start month is April in India and is not configurable
    here; a tenant on a different accounting year supplies its own bounds when
    that becomes a requirement.
    """
    try:
        start_year, end_year = (int(part) for part in financial_year.split("-"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"'{financial_year}' is not a financial year. Use the form 2025-2026.",
        ) from exc
    if end_year != start_year + 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"'{financial_year}' does not span consecutive years.",
        )
    return date(start_year, 4, 1), date(end_year, 3, 31)


async def compute_bonus_for_year(
    session: AsyncSession,
    user: User,
    *,
    financial_year: str,
    legal_entity_id: uuid.UUID,
    jurisdiction: str = "IN",
    percentage: Decimal | None = None,
    request: Request | None = None,
) -> list[BonusComputation]:
    """Compute and record statutory bonus for a financial year.

    Wages and days worked come from the year's payslips, so an employee with
    loss of pay gets a proportionally smaller bonus without this function
    knowing anything about attendance.

    Recomputing overwrites DRAFT records and refuses to touch PAID ones — a
    bonus already paid is a fact, and a later configuration change does not
    retroactively alter what was handed over.
    """
    year_start, year_end = financial_year_bounds(financial_year)
    rule_set = await _rule_set_on(
        session, user, rule_type="bonus", jurisdiction=jurisdiction, on_date=year_end
    )
    config = rule_set.rules

    # The company's people on the last day of the year, which is not always
    # the company on their record today (a move made after the year ended).
    in_company = await companies.members_on(session, user, legal_entity_id, year_end)
    employees = (
        await session.scalars(
            tenant_scoped(
                select(Employee).where(
                    Employee.id.in_(in_company),
                    Employee.deleted_at.is_(None),
                ),
                Employee,
                user,
            )
        )
    ).all()

    existing_rows = (
        await session.scalars(
            tenant_scoped(
                select(BonusComputation).where(BonusComputation.financial_year == financial_year),
                BonusComputation,
                user,
            )
        )
    ).all()
    existing = {row.employee_id: row for row in existing_rows}

    results: list[BonusComputation] = []
    for employee in employees:
        prior = existing.get(employee.id)
        if prior is not None and prior.status is BonusStatus.PAID:
            results.append(prior)
            continue

        wages, paid_days, total_days = await _bonus_inputs(
            session,
            user,
            employee.id,
            year_start,
            year_end,
            wage_components=config.get("wage_components"),
        )
        if total_days <= 0:
            continue

        try:
            result = compute_bonus(
                config=config,
                monthly_wages=wages,
                days_worked=paid_days,
                total_working_days=total_days,
                percentage=percentage,
            )
        except StatutoryError as exc:
            raise _unprocessable(exc) from exc

        row = prior or BonusComputation(
            tenant_id=user.tenant_id,
            employee_id=employee.id,
            financial_year=financial_year,
        )
        row.rule_set_id = rule_set.id
        row.is_eligible = result.is_eligible
        row.ineligibility_reason = result.ineligibility_reason
        row.eligible_wage = result.eligible_wage
        row.days_worked = result.days_worked
        row.total_working_days = total_days
        row.percentage = result.percentage
        row.amount = result.amount
        row.working = result.as_working()
        row.status = BonusStatus.DRAFT
        if prior is None:
            session.add(row)
        results.append(row)

    await session.flush()

    eligible = [row for row in results if row.is_eligible]
    total = sum((Decimal(str(row.amount)) for row in eligible), start=Decimal(0))
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="bonus_computation",
        entity_id=legal_entity_id,
        summary=(
            f"Computed statutory bonus for {financial_year}: {len(eligible)} eligible of "
            f"{len(results)} employees, {total} total"
        ),
        changes={
            "financial_year": financial_year,
            "rule_set_id": str(rule_set.id),
            "eligible": len(eligible),
            "total_amount": str(total),
        },
        request=request,
    )
    return results


async def _bonus_inputs(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    year_start: date,
    year_end: date,
    *,
    wage_components: list[str] | None = None,
) -> tuple[Decimal, Decimal, Decimal]:
    """Monthly wage, days paid and the year's days, for a bonus year.

    The wage is the average monthly rate across the year's payslips rather
    than the current salary: someone whose pay rose in January should not have
    the whole year computed at the higher figure. It is the rate, not what a
    short month paid — a joining month of ten days does not make somebody's
    salary a third of what it is, nor make them eligible.

    Only approved months count, from regular payslips: a supplementary payslip
    (an off-cycle top-up, an arrear) pays no structure and no days, and adding
    it would count its month twice.

    The bonus is prorated over the financial year's days, not over the days the
    payslips happen to cover: a mid-year joiner earns part of a year's bonus,
    and a year computed before it ends earns the part accrued so far.

    Which components make up that wage is configuration. The Act speaks of
    "salary or wage", which is not the same thing as gross — a tenant whose SME
    has said BASIC and DA gets BASIC and DA. Without a list, gross is used and
    the calculation ceiling does most of the work in any case.
    """
    rows = (
        await session.execute(
            tenant_scoped(
                select(
                    Payslip.gross_earnings,
                    Payslip.paid_days,
                    Payslip.working_days,
                    Payslip.line_items,
                )
                .join(PayrollRun, Payslip.run_id == PayrollRun.id)
                .where(
                    Payslip.employee_id == employee_id,
                    Payslip.is_supplementary.is_(False),
                    PayrollRun.status.in_([PayrollRunStatus.APPROVED, PayrollRunStatus.PAID]),
                    PayrollRun.effective_date >= year_start,
                    PayrollRun.effective_date <= year_end,
                ),
                Payslip,
                user,
            )
        )
    ).all()

    if not rows:
        return Decimal(0), Decimal(0), Decimal(0)

    rates: list[Decimal] = []
    for gross, paid, working, line_items in rows:
        wage = _bonus_wage(gross, line_items, wage_components)
        paid_days, working_days = Decimal(str(paid)), Decimal(str(working))
        if paid_days > 0 and working_days > 0:
            rates.append(wage * working_days / paid_days)
    paid_days = sum((Decimal(str(row[1])) for row in rows), start=Decimal(0))
    year_days = Decimal((year_end - year_start).days + 1)
    monthly = sum(rates, start=Decimal(0)) / Decimal(len(rates)) if rates else Decimal(0)

    return monthly, paid_days, year_days


def _bonus_wage(
    gross: object, line_items: list | None, wage_components: list[str] | None
) -> Decimal:
    if not wage_components:
        return Decimal(str(gross))

    wanted = {code.upper() for code in wage_components}
    return sum(
        (
            Decimal(str(line.get("amount", 0)))
            for line in (line_items or [])
            if str(line.get("code", "")).upper() in wanted
        ),
        start=Decimal(0),
    )


# ── Disbursement ────────────────────────────────────────────────────────────


async def approve_bonus(
    session: AsyncSession,
    user: User,
    *,
    financial_year: str,
    request: Request | None = None,
) -> list[BonusComputation]:
    """Sign off a year's bonus, freezing it against recomputation.

    Approval is separate from payment because the figure is usually agreed
    weeks before the money moves.
    """
    rows = await _bonus_rows(session, user, financial_year)
    draft = [row for row in rows if row.status is BonusStatus.DRAFT]
    if not draft:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"There is no draft bonus computation for {financial_year} to approve.",
        )

    now = datetime.now(UTC)
    for row in draft:
        row.status = BonusStatus.APPROVED
        row.approved_by_user_id = user.id
        row.approved_at = now
    await session.flush()

    total = sum((Decimal(str(row.amount)) for row in draft if row.is_eligible), start=Decimal(0))
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE,
        entity_type="bonus_computation",
        summary=(
            f"Approved statutory bonus for {financial_year}: {total} across {len(draft)} employees"
        ),
        changes={"financial_year": financial_year, "total_amount": str(total)},
        request=request,
    )
    return draft


async def disburse_bonus(
    session: AsyncSession,
    user: User,
    *,
    financial_year: str,
    paid_on: date,
    run_id: uuid.UUID | None = None,
    request: Request | None = None,
) -> list[BonusComputation]:
    """Pay an approved bonus, on its own or inside a payroll run.

    `run_id` is optional and usually absent: bonus is typically its own
    disbursement ahead of a festival, not a line inside a monthly run.

    With a `run_id`:

    - a regular run **not yet approved** has the bonus *scheduled* into it: the
      rows stay APPROVED with `paid_in_run_id` set, the run's statutory bonus
      input puts them on the payslips, and approving the run marks them PAID.
      `paid_on` is then the run's payment date, not the one supplied here.
    - a run **already approved or paid** records the bonus as paid alongside it,
      so the two reconcile.

    Only approved rows are paid, and a paid row is never recomputed — which is
    what makes the Form C "amount actually paid" column trustworthy. A row
    scheduled into one run is never paid separately as well.
    """
    from app.models.payroll import PayrollRunStatus, PayrollRunType

    rows = await _bonus_rows(session, user, financial_year)
    approved = [
        row
        for row in rows
        if row.status is BonusStatus.APPROVED
        and row.is_eligible
        and (row.paid_in_run_id is None or row.paid_in_run_id == run_id)
    ]
    if not approved:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"No approved bonus for {financial_year} is awaiting payment. Approve "
                "the computation first."
            ),
        )

    if run_id is not None:
        run = await session.scalar(
            tenant_scoped(select(PayrollRun).where(PayrollRun.id == run_id), PayrollRun, user)
        )
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Payroll run not found"
            )
        open_statuses = (
            PayrollRunStatus.DRAFT,
            PayrollRunStatus.PENDING_REVIEW,
            PayrollRunStatus.FAILED,
        )
        if run.status in open_statuses:
            if run.run_type not in (PayrollRunType.REGULAR, PayrollRunType.BONUS):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Statutory bonus rides a regular or a bonus payroll run only.",
                )
            for row in approved:
                row.paid_in_run_id = run.id
            await session.flush()
            total = sum((Decimal(str(row.amount)) for row in approved), start=Decimal(0))
            await audit.record(
                session,
                tenant_id=user.tenant_id,
                actor=user,
                action=AuditAction.UPDATE,
                entity_type="bonus_computation",
                summary=(
                    f"Scheduled statutory bonus for {financial_year} ({total} to "
                    f"{len(approved)} employees) into the {run.period_month}/"
                    f"{run.period_year} payroll run"
                ),
                changes={
                    "financial_year": financial_year,
                    "scheduled_in_run_id": str(run.id),
                    "total_amount": str(total),
                },
                request=request,
            )
            return approved
        if run.status not in (PayrollRunStatus.APPROVED, PayrollRunStatus.PAID):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A payroll run in status '{run.status.value}' cannot carry a bonus.",
            )

    for row in approved:
        row.status = BonusStatus.PAID
        row.paid_on = paid_on
        row.paid_in_run_id = run_id
    await session.flush()

    total = sum((Decimal(str(row.amount)) for row in approved), start=Decimal(0))
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="bonus_computation",
        summary=(
            f"Disbursed statutory bonus for {financial_year}: {total} to "
            f"{len(approved)} employees on {paid_on}"
        ),
        changes={
            "financial_year": financial_year,
            "paid_on": str(paid_on),
            "paid_in_run_id": str(run_id) if run_id else None,
            "total_amount": str(total),
        },
        request=request,
    )
    return approved


async def _bonus_rows(
    session: AsyncSession, user: User, financial_year: str
) -> list[BonusComputation]:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(BonusComputation).where(BonusComputation.financial_year == financial_year),
                BonusComputation,
                user,
            )
        )
    ).all()
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No bonus has been computed for {financial_year}.",
        )
    return list(rows)


# ── Registers ───────────────────────────────────────────────────────────────


async def build_register(
    session: AsyncSession,
    user: User,
    *,
    code: str,
    run_id: uuid.UUID | None = None,
    financial_year: str | None = None,
) -> register_formats.RenderedRegister:
    """Assemble one register from records that already exist.

    Monthly registers take a run; the annual bonus register takes a financial
    year.
    """
    spec = register_formats.REGISTERS.get(code)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"'{code}' is not a register this system produces. Available: "
                f"{', '.join(sorted(register_formats.REGISTERS))}."
            ),
        )

    if code == "BONUS_FORM_C":
        if financial_year is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The bonus register covers a financial year; supply one.",
            )
        return await _bonus_register(session, user, spec, financial_year)

    if run_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The {spec.title} covers one payroll run; supply a run id.",
        )
    return await _run_register(session, user, spec, run_id)


async def _bonus_register(
    session: AsyncSession,
    user: User,
    spec: register_formats.RegisterSpec,
    financial_year: str,
) -> register_formats.RenderedRegister:
    rows_raw = (
        await session.execute(
            tenant_scoped(
                select(BonusComputation, Employee)
                .join(Employee, BonusComputation.employee_id == Employee.id)
                .where(BonusComputation.financial_year == financial_year)
                .order_by(Employee.employee_code),
                BonusComputation,
                user,
            )
        )
    ).all()

    rows = []
    for computation, employee in rows_raw:
        annual_wage = Decimal(str(computation.eligible_wage)) * Decimal(12)
        amount = Decimal(str(computation.amount))
        rows.append(
            {
                "employee_name": employee.full_name,
                "guardian_name": None,
                # Age is on the employee record where it is known; where it is
                # not, the column stays blank rather than asserting a fact.
                "completed_fifteen": _completed_fifteen(employee, financial_year),
                "designation": employee.designation,
                "days_worked": Decimal(str(computation.days_worked)),
                "annual_wage": annual_wage,
                "bonus_payable": amount,
                "customary_bonus": Decimal(0),
                "advance_bonus": Decimal(0),
                "misconduct_loss": Decimal(0),
                "net_payable": amount,
                "amount_paid": amount if computation.status is BonusStatus.PAID else Decimal(0),
                "paid_on": computation.paid_on,
                "signature": None,
                "_eligible": computation.is_eligible,
            }
        )

    return register_formats.render_register(
        spec,
        rows,
        heading_lines=[f"Accounting year: {financial_year}"],
        # An ineligible employee has no bonus due, so no row in a register of
        # bonus due — but they are listed as excluded rather than vanishing.
        row_filter=lambda row: bool(row["_eligible"]),
    )


def _completed_fifteen(employee: Employee, financial_year: str) -> bool | None:
    if employee.date_of_birth is None:
        return None
    year_start, _ = financial_year_bounds(financial_year)
    years = (year_start - employee.date_of_birth).days / 365.25
    return years >= 15


async def _run_register(
    session: AsyncSession,
    user: User,
    spec: register_formats.RegisterSpec,
    run_id: uuid.UUID,
) -> register_formats.RenderedRegister:
    run = await session.scalar(
        tenant_scoped(select(PayrollRun).where(PayrollRun.id == run_id), PayrollRun, user)
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payroll run not found")

    payslips = (
        await session.execute(
            tenant_scoped(
                select(Payslip, Employee)
                .join(Employee, Payslip.employee_id == Employee.id)
                .where(Payslip.run_id == run_id)
                .order_by(Employee.employee_code),
                Payslip,
                user,
            )
        )
    ).all()

    period = f"{run.period_month:02d}/{run.period_year}"
    states = {
        employee.id: await _employee_state(session, user, employee.location_id)
        for _, employee in payslips
    }
    rows = [
        row
        for row in (
            _register_row(spec.code, payslip, employee, period, states[employee.id])
            for payslip, employee in payslips
        )
        if row is not None
    ]

    heading_lines = [f"Period: {period}", f"Payroll run: {run.id}"]
    return register_formats.render_register(spec, rows, heading_lines=heading_lines)


def _register_row(
    code: str, payslip: Payslip, employee: Employee, period: str, state: str | None
) -> dict | None:
    """One register row from one payslip.

    Rows are read off the payslip, never recomputed — a register that
    recomputes can disagree with the payslip it summarises, and the payslip is
    what the employee was actually paid.
    """
    if code == "LWF_REGISTER":
        employee_share = Decimal(str(payslip.lwf_employee))
        employer_share = Decimal(str(payslip.lwf_employer))
        if not employee_share and not employer_share:
            # Not a deduction month for this state, so nothing to register.
            return None
        return {
            "employee_name": employee.full_name,
            "designation": employee.designation,
            "wages": Decimal(str(payslip.gross_earnings)),
            "employee_contribution": employee_share,
            "employer_contribution": employer_share,
            "total_contribution": employee_share + employer_share,
            "period": period,
        }

    if code == "PF_REGISTER":
        return {
            "uan": employee.uan,
            "employee_name": employee.full_name,
            "pf_wage_base": Decimal(str(payslip.pf_wage_base)),
            "employee_contribution": Decimal(str(payslip.pf_employee)),
            "voluntary_contribution": Decimal(str(payslip.vpf)),
            "employer_contribution": Decimal(str(payslip.pf_employer)),
            "period": period,
        }

    if code == "ESI_REGISTER":
        if not Decimal(str(payslip.esi_employee)) and not Decimal(str(payslip.esi_employer)):
            return None
        return {
            "esi_number": employee.esi_number,
            "employee_name": employee.full_name,
            "paid_days": Decimal(str(payslip.paid_days)),
            "esi_wage_base": Decimal(str(payslip.esi_wage_base)),
            "employee_contribution": Decimal(str(payslip.esi_employee)),
            "employer_contribution": Decimal(str(payslip.esi_employer)),
            "contribution_period": period,
        }

    if code == "PT_REGISTER":
        tax = Decimal(str(payslip.professional_tax))
        if not tax:
            return None
        return {
            "employee_name": employee.full_name,
            "state": state,
            "gross_earnings": Decimal(str(payslip.gross_earnings)),
            "tax_deducted": tax,
            "period": period,
        }

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"'{code}' is not a per-run register.",
    )


async def _employee_state(
    session: AsyncSession, user: User, location_id: uuid.UUID | None
) -> str | None:
    if location_id is None:
        return None
    return await session.scalar(
        tenant_scoped(select(Location.state_code).where(Location.id == location_id), Location, user)
    )


async def record_register_export(
    session: AsyncSession,
    user: User,
    rendered: register_formats.RenderedRegister,
    *,
    run_id: uuid.UUID | None,
    financial_year: str | None,
    request: Request | None = None,
) -> None:
    """Audit a statutory register leaving as a file: names and wages, in bulk.

    Like every other bulk download, the audit row is written before the file
    is returned — "who took a copy of the wage register" must be answerable.
    """
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.EXPORT,
        entity_type="statutory_register",
        actor=user,
        changes={
            "register": rendered.spec.code,
            "rows": rendered.row_count,
            "run_id": str(run_id) if run_id else None,
            "financial_year": financial_year,
            "format": "csv",
        },
        summary=f"{rendered.spec.title} downloaded as CSV ({rendered.row_count} rows)",
        request=request,
    )
    await session.commit()
