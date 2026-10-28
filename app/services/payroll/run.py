"""Orchestration for a payroll run.

This is where slice 01's computation and slice 02's attendance meet. The rule
the slice spec is explicit about, and which is enforced here rather than by
convention:

    A payroll run for a period where attendance has not been finalised must
    refuse to run rather than assume zero LOP.

Assuming zero silently overpays everyone who had unpaid absence that month, and
the error is only discovered when someone reconciles the bank file. Blocking is
recoverable; overpaying is not.

The lifecycle, and who may move it:

    DRAFT ─compute─▶ PENDING_REVIEW ─approve─▶ APPROVED ─mark paid─▶ PAID
                  ▲        │                       │
                  └─recompute                  publish (payslips to employees)

- **Compute** refuses an unlocked attendance period and any unapproved rule set.
  It may be repeated until approval; every repeat replaces the payslips and
  clears variance acknowledgements, because the numbers they acknowledged moved.
- **Approve** needs every variance flag acknowledged with a note, no review
  flags, and a different person from whoever computed it (maker-checker). It is
  also where input sources — loans, reimbursements, arrears — are marked as
  consumed, so nothing is paid by two runs.
- **An approved run is immutable.** It is never recomputed; a correction is an
  arrears revision paid in a later run.

**Off-cycle, bonus and arrear runs** are supplementary. They open only once the
period's regular run is approved, pay the people they name from the input
sources they were opened for, and never pay the structure twice: somebody the
regular run already paid gets a supplementary payslip — input lines only, PF
untouched, Professional Tax and ESI topped up, TDS on the one-off pay alone
(see `engine.SupplementaryContext`). Somebody the regular run missed — a joiner
keyed in late — gets a full payslip, exactly as the regular run would have
computed it.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import HTTPException, Request, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.core.step_up import Confirm
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.exit_workflow import ExitRecord, ExitStatus
from app.models.org import LegalEntity
from app.models.payroll import (
    PayrollRuleSet,
    PayrollRun,
    PayrollRunStatus,
    PayrollRunType,
    Payslip,
)
from app.models.tax import TaxRegimeElection, WindowType
from app.models.user import User
from app.services import audit
from app.services.attendance import service as attendance_service
from app.services.attendance.service import AttendanceLockError
from app.services.notifications.dispatch import notify
from app.services.payroll import salary_days as salary_days_rules
from app.services.payroll import statutory_service
from app.services.payroll.engine import (
    PayrollInput,
    PayrollRuleError,
    SupplementaryContext,
    compute_payroll,
)
from app.services.payroll.inputs import (
    PROVIDER_KEYS,
    InputConsumedElsewhereError,
    RunScope,
    collect_inputs,
    consume_inputs,
)
from app.services.payroll.service import active_salary_on
from app.services.payroll.tds import (
    PriorPay,
    TdsContext,
    financial_year_for,
    fy_start_month,
    months_remaining,
    without_income_paid_by_payroll,
)
from app.services.tax import service as tax_service
from app.services.tax.computation import TaxRuleError
from app.services.tax.sources import PAID_THROUGH_PAYROLL

# Rule types resolved for every run. Professional Tax and the Labour Welfare
# Fund are state levies, so they are resolved per employee by their location.
CENTRAL_RULE_TYPES = ("pf", "esi", "tds", "wage_deductions")
STATE_RULE_TYPES = ("professional_tax", "labour_welfare_fund")

# A run in one of these states has been signed off and is history.
CLOSED_STATUSES = (PayrollRunStatus.APPROVED, PayrollRunStatus.PAID)
# Statuses a compute may start from.
COMPUTABLE_STATUSES = (
    PayrollRunStatus.DRAFT,
    PayrollRunStatus.PENDING_REVIEW,
    PayrollRunStatus.FAILED,
)

_ZERO = Decimal(0)
_INPUT_CASH_KINDS = ("earning", "reimbursement")

#: Run types that pay on top of the month's regular run.
SUPPLEMENTARY_RUN_TYPES = (
    PayrollRunType.OFF_CYCLE,
    PayrollRunType.BONUS,
    PayrollRunType.ARREAR,
)
#: What each supplementary run draws on unless the person opening it says otherwise.
DEFAULT_SOURCES: dict[PayrollRunType, tuple[str, ...]] = {
    PayrollRunType.OFF_CYCLE: ("payroll_adjustments", "overtime"),
    PayrollRunType.BONUS: ("statutory_bonus", "payroll_adjustments"),
    PayrollRunType.ARREAR: ("arrears",),
}


def period_bounds(year: int, month: int) -> tuple[date, date]:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def period_label(year: int, month: int) -> str:
    return f"{calendar.month_name[month]} {year}"


def employment_window(
    *,
    period_start: date,
    period_end: date,
    joined_on: date | None,
    left_on: date | None,
) -> tuple[date, date] | None:
    """The days of the period the person was employed, or `None` for none.

    Attendance writes no rows before someone joins or after they leave, so
    loss of pay alone would pay a 15th-of-the-month joiner for the whole month.
    The window is what makes those days unpaid.
    """
    first = max(period_start, joined_on) if joined_on else period_start
    last = min(period_end, left_on) if left_on else period_end
    if first > last:
        return None
    return first, last


def days_outside(period_start: date, period_end: date, window: tuple[date, date]) -> Decimal:
    """Calendar days of the period before the window opens and after it closes."""
    first, last = window
    return Decimal((first - period_start).days + (period_end - last).days)


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payroll run not found")


async def get_run(session: AsyncSession, user: User, run_id: uuid.UUID) -> PayrollRun:
    """A run in the caller's tenant, or a 404 — never a 403 that confirms it exists."""
    run = await session.scalar(
        tenant_scoped(select(PayrollRun).where(PayrollRun.id == run_id), PayrollRun, user)
    )
    if run is None:
        raise _not_found()
    return run


async def _resolve_rule_sets(
    session: AsyncSession, user: User, effective_date: date, jurisdictions: set[str]
) -> dict[str, PayrollRuleSet]:
    """Rule versions active on the run's effective date, never on today.

    Keyed `rule_type` for central rules and `professional_tax:<state>` for
    state ones, so a run covering several states resolves each correctly.
    """
    stmt = tenant_scoped(
        select(PayrollRuleSet).where(
            PayrollRuleSet.effective_from <= effective_date,
            or_(
                PayrollRuleSet.effective_to.is_(None),
                PayrollRuleSet.effective_to >= effective_date,
            ),
        ),
        PayrollRuleSet,
        user,
    )
    resolved: dict[str, PayrollRuleSet] = {}
    for rule_set in (await session.scalars(stmt)).all():
        if rule_set.rule_type in STATE_RULE_TYPES:
            if rule_set.jurisdiction in jurisdictions:
                key = f"{rule_set.rule_type}:{rule_set.jurisdiction}"
                existing = resolved.get(key)
                if existing is None or rule_set.effective_from > existing.effective_from:
                    resolved[key] = rule_set
        elif rule_set.rule_type in CENTRAL_RULE_TYPES:
            existing = resolved.get(rule_set.rule_type)
            # Latest effective version wins when several are still open.
            if existing is None or rule_set.effective_from > existing.effective_from:
                resolved[rule_set.rule_type] = rule_set
    return resolved


# ── Create ──────────────────────────────────────────────────────────────────


async def create_run(
    session: AsyncSession,
    user: User,
    *,
    legal_entity_id: uuid.UUID,
    period_year: int,
    period_month: int,
    run_type: PayrollRunType = PayrollRunType.REGULAR,
    payment_date: date | None = None,
    employee_ids: list[uuid.UUID] | None = None,
    included_sources: list[str] | None = None,
    reason: str | None = None,
    request: Request | None = None,
) -> PayrollRun:
    """Open a draft run for a period.

    `effective_date` is fixed here, at the period end, and never re-derived —
    that is what makes a re-run months later produce the identical result.

    A supplementary run (off-cycle, bonus, arrear) needs the people it pays, a
    reason, and an approved regular run for the period to sit on top of.
    """
    if run_type is PayrollRunType.FULL_AND_FINAL:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Full and final is settled from the exit (People → Exits → settlement), "
                "which pays salary to the last day, encashment, gratuity and recoveries "
                "together. It is not a payroll run."
            ),
        )

    entity = await session.scalar(
        tenant_scoped(
            select(LegalEntity).where(LegalEntity.id == legal_entity_id), LegalEntity, user
        )
    )
    if entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Legal entity not found")

    start, end = period_bounds(period_year, period_month)
    if payment_date is not None and payment_date < start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"The payment date {payment_date} is before the period it pays for.",
        )

    scope: list[str] = []
    sources: list[str] | None = None
    clean_reason = (reason or "").strip() or None
    if run_type in SUPPLEMENTARY_RUN_TYPES:
        label = run_type.value.replace("_", "-")
        if not employee_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Choose the employees this {label} run pays.",
            )
        if clean_reason is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Say why this {label} run is needed. It goes on the run and the audit.",
            )
        sources = list(dict.fromkeys(included_sources or DEFAULT_SOURCES[run_type]))
        unknown = [key for key in sources if key not in PROVIDER_KEYS]
        if unknown or not sources:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Unknown input source(s): {', '.join(unknown) or 'none chosen'}. "
                    f"Choose from: {', '.join(PROVIDER_KEYS)}."
                ),
            )
        regular = await session.scalar(
            tenant_scoped(
                select(PayrollRun).where(
                    PayrollRun.legal_entity_id == legal_entity_id,
                    PayrollRun.period_year == period_year,
                    PayrollRun.period_month == period_month,
                    PayrollRun.run_type == PayrollRunType.REGULAR,
                    PayrollRun.status != PayrollRunStatus.CANCELLED,
                ),
                PayrollRun,
                user,
            )
        )
        if regular is None or regular.status not in CLOSED_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"The regular run for {period_month}/{period_year} is not approved yet. "
                    f"A {label} run pays on top of an approved regular run; anything due "
                    "before then is paid by the regular run itself."
                ),
            )
        found = set(
            (
                await session.scalars(
                    tenant_scoped(
                        select(Employee.id).where(
                            Employee.id.in_(employee_ids),
                            Employee.legal_entity_id == legal_entity_id,
                            Employee.deleted_at.is_(None),
                        ),
                        Employee,
                        user,
                    )
                )
            ).all()
        )
        missing = [str(e) for e in employee_ids if e not in found]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"{len(missing)} of the chosen employees are not on {entity.name}'s "
                    "payroll. A run pays one legal entity's employees."
                ),
            )
        scope = [str(e) for e in dict.fromkeys(employee_ids)]
    else:
        existing = await session.scalar(
            tenant_scoped(
                select(PayrollRun).where(
                    PayrollRun.legal_entity_id == legal_entity_id,
                    PayrollRun.period_year == period_year,
                    PayrollRun.period_month == period_month,
                    PayrollRun.run_type == run_type,
                    PayrollRun.status != PayrollRunStatus.CANCELLED,
                ),
                PayrollRun,
                user,
            )
        )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"A {run_type.value} run already exists for {period_month}/{period_year}. "
                    "A correction is a new off-cycle or arrears run, not a re-run."
                ),
            )

    run = PayrollRun(
        tenant_id=user.tenant_id,
        legal_entity_id=legal_entity_id,
        period_year=period_year,
        period_month=period_month,
        effective_date=end,
        payment_date=payment_date,
        run_type=run_type,
        status=PayrollRunStatus.DRAFT,
        employee_scope=scope,
        included_sources=sources,
        reason=clean_reason,
    )
    session.add(run)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="payroll_run",
        entity_id=run.id,
        actor=user,
        changes={
            "period": f"{period_month}/{period_year}",
            "type": run_type.value,
            "legal_entity": entity.name,
            "payment_date": payment_date.isoformat() if payment_date else None,
            "employees": len(scope) or "all",
            "sources": sources or "all",
            "reason": clean_reason,
        },
        summary=f"Opened {run_type.value} payroll run for {period_month}/{period_year}",
        request=request,
    )
    await session.commit()
    await session.refresh(run)
    return run


async def list_runs(
    session: AsyncSession,
    user: User,
    *,
    year: int | None = None,
    run_status: PayrollRunStatus | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[PayrollRun], int]:
    stmt = tenant_scoped(select(PayrollRun), PayrollRun, user)
    count_stmt = tenant_scoped(select(func.count(PayrollRun.id)), PayrollRun, user)
    if year:
        stmt = stmt.where(PayrollRun.period_year == year)
        count_stmt = count_stmt.where(PayrollRun.period_year == year)
    if run_status is not None:
        stmt = stmt.where(PayrollRun.status == run_status)
        count_stmt = count_stmt.where(PayrollRun.status == run_status)

    total = int(await session.scalar(count_stmt) or 0)
    rows = (
        await session.scalars(
            stmt.order_by(
                PayrollRun.period_year.desc(),
                PayrollRun.period_month.desc(),
                PayrollRun.created_at.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return list(rows), total


# ── Compute ─────────────────────────────────────────────────────────────────


async def compute_run(
    session: AsyncSession,
    user: User,
    run_id: uuid.UUID,
    *,
    variance_threshold_percent: Decimal = Decimal(20),
    request: Request | None = None,
) -> PayrollRun:
    """Compute every payslip in a run.

    Refuses when attendance for the period is not locked. See the module
    docstring for why that is a refusal rather than a warning.
    """
    run = await get_run(session, user, run_id)
    if run.status in CLOSED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This run is {run.status.value} and cannot be recomputed. An approved run "
                "is history; a correction is an arrears revision paid in a later run."
            ),
        )
    if run.status not in COMPUTABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A run in status '{run.status.value}' cannot be computed.",
        )

    start, end = period_bounds(run.period_year, run.period_month)

    # ── The attendance gate ─────────────────────────────────────────────────
    lock = await attendance_service.active_lock_covering(session, user, start, end)
    if lock is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Attendance for {start} to {end} has not been finalised. Lock the "
                "attendance period before running payroll — running without it would "
                "assume zero loss of pay and overpay anyone who had unpaid absence."
            ),
        )

    # Everyone employed for any part of the period. Someone whose exit was
    # processed before this run is computed is still owed the days they worked
    # in it; `employment_window` below pays exactly those days.
    employees = (
        await session.scalars(
            tenant_scoped(
                select(Employee)
                .where(
                    Employee.legal_entity_id == run.legal_entity_id,
                    Employee.deleted_at.is_(None),
                    or_(
                        Employee.status.in_(
                            [
                                EmploymentStatus.ACTIVE,
                                EmploymentStatus.ON_NOTICE,
                                EmploymentStatus.ON_LEAVE,
                            ]
                        ),
                        and_(
                            Employee.status == EmploymentStatus.EXITED,
                            Employee.date_of_exit >= start,
                        ),
                    ),
                    *(
                        (Employee.id.in_([uuid.UUID(e) for e in run.employee_scope]),)
                        if run.employee_scope
                        else ()
                    ),
                )
                .order_by(Employee.employee_code),
                Employee,
                user,
            )
        )
    ).all()

    # State codes in scope, so Professional Tax resolves per location.
    states = {
        loc_state
        for loc_state in [
            (await statutory_service.location_state(session, user, e.location_id))
            for e in employees
        ]
        if loc_state
    }
    rule_sets = await _resolve_rule_sets(session, user, run.effective_date, states)

    run.status = PayrollRunStatus.PROCESSING
    await session.flush()

    is_regular = run.run_type is PayrollRunType.REGULAR
    # Prior cycle payslips, for variance flagging. A supplementary run pays a
    # top-up, which no previous month's net pay is a fair comparison for.
    previous = await _previous_net_by_employee(session, user, run) if is_regular else {}

    # ── Income tax: the rule set, the financial year, what was paid already ─
    run_flags: list[str] = []
    tds_rules = rule_sets.get("tds")
    tds_usable = False
    start_month = fy_start_month({})
    if tds_rules is None:
        run_flags.append(
            f"No income tax (TDS) rule set is in force on {run.effective_date}, so TDS was "
            "not computed for anyone on this run. The Compliance SME supplies and approves "
            "one; then recompute."
        )
    elif not tds_rules.is_approved:
        run_flags.append(
            f"The income tax (TDS) rule set effective {tds_rules.effective_from} has not "
            "been approved by a Compliance SME, so TDS was not computed for anyone on this "
            "run. Approve it under Payroll → Rule sets; then recompute."
        )
    else:
        try:
            start_month = fy_start_month(tds_rules.rules)
            tds_usable = True
        except TaxRuleError as exc:
            run_flags.append(f"TDS was not computed: {exc}")

    fy_label, fy_start, fy_end = financial_year_for(end, start_month)
    employee_ids = frozenset(e.id for e in employees)
    prior_pay, prior_lines = await _prior_financial_year(
        session, user, run, employee_ids=employee_ids, fy_start=fy_start, fy_end=fy_end
    )

    month_paid = (
        {}
        if is_regular
        else await _month_paid_so_far(session, user, run, frozenset(e.id for e in employees))
    )

    collected = await collect_inputs(
        session,
        user,
        RunScope(
            run=run,
            period_start=start,
            period_end=end,
            period=f"{run.period_year}-{run.period_month:02d}",
            employee_ids=employee_ids,
            fy_start=fy_start,
            fy_end=fy_end,
            prior_input_lines=prior_lines,
        ),
        sources=(frozenset(run.included_sources) if run.included_sources is not None else None),
    )

    proof_window = (
        await tax_service.window_for(session, user, fy_label, WindowType.PROOF)
        if tds_usable
        else None
    )
    # Before the proof window closes, TDS follows what employees declared;
    # after it, only what was proved and accepted. That is what spikes March for
    # anyone who declared and never proved, and it is correct.
    use_declared = proof_window is None or end <= proof_window.closes_at
    declaration_basis = (
        "Declared amounts are used: the proof window has not closed."
        if use_declared
        else f"Only proved and accepted amounts are used: the proof window closed on "
        f"{proof_window.closes_at}."  # type: ignore[union-attr]
    )

    working_days = Decimal((end - start).days + 1)
    # How this company divides a month's salary into days (calendar, fixed or
    # scheduled working days). The work week per person comes from the same
    # resolver attendance uses, so a five-day, six-day or alternate-Saturday
    # week gives each person their own divisor.
    entity = await session.scalar(
        tenant_scoped(
            select(LegalEntity).where(LegalEntity.id == run.legal_entity_id), LegalEntity, user
        )
    )
    days_basis = entity.salary_days_basis if entity is not None else salary_days_rules.CALENDAR
    fixed_days = entity.salary_fixed_days if entity is not None else None
    shift_plans = (
        await attendance_service.ShiftResolver.load(
            session, user, [e.id for e in employees], start, end
        )
        if days_basis == salary_days_rules.WORKING_DAYS
        else None
    )
    days_notices: list[str] = []
    if entity is not None and days_basis == salary_days_rules.FIXED and fixed_days:
        days_notices.append(
            f"{entity.name} divides a month's salary by {fixed_days} days, whatever the month's "
            "length. A day's loss of pay is the monthly amount divided by that."
        )
    elif entity is not None and days_basis == salary_days_rules.WORKING_DAYS:
        days_notices.append(
            f"{entity.name} divides a month's salary by each person's scheduled working days "
            "— the month less their weekly offs (five-day, six-day or alternate Saturdays). "
            "Holidays are paid working days."
        )
    payslips: list[Payslip] = []
    variances: list[dict] = []
    review_flags: list[str] = []
    errors: list[str] = []
    employment_notices: list[str] = []
    # People named on a supplementary run who turned out to have nothing due.
    unpaid_notices: list[str] = []
    leaving_on = await _agreed_last_days(session, user, employee_ids)

    for employee in employees:
        # Days before joining and after leaving are unpaid. The exit date is
        # the recorded one, or — for someone still serving notice — the last
        # working day their exit agreed.
        left_on = employee.date_of_exit or leaving_on.get(employee.id)
        window = employment_window(
            period_start=start,
            period_end=end,
            joined_on=employee.date_of_joining,
            left_on=left_on,
        )
        if window is None:
            employment_notices.append(
                f"{employee.employee_code} is not paid by this run: "
                + (
                    f"they join on {employee.date_of_joining:%d %b %Y}, after the period."
                    if employee.date_of_joining and employee.date_of_joining > end
                    else f"their last working day was {left_on:%d %b %Y}, before the period."
                )
            )
            continue
        outside = days_outside(start, end, window)
        if outside:
            paid_window = (window[1] - window[0]).days + 1
            if employee.date_of_joining and employee.date_of_joining > start:
                employment_notices.append(
                    f"{employee.employee_code} joined on {employee.date_of_joining:%d %b %Y}: "
                    f"paid for {paid_window} of {working_days} day(s)."
                )
            if left_on and left_on < end:
                employment_notices.append(
                    f"{employee.employee_code}'s last working day is {left_on:%d %b %Y}: paid "
                    f"for {paid_window} of {working_days} day(s) in this run. Their full and "
                    "final settlement must not pay these days again."
                )

        supplementary: SupplementaryContext | None = None
        if not is_regular:
            paid = month_paid.get(employee.id)
            if paid is not None and paid["has_full"]:
                if not collected.lines.get(employee.id):
                    unpaid_notices.append(
                        f"{employee.employee_code}: nothing is due from this run's sources, "
                        "so no payslip."
                    )
                    continue
                supplementary = SupplementaryContext(
                    regular_gross=paid["gross"],
                    pt_already_deducted=paid["pt"],
                    esi_covered=paid["esi_covered"],
                )

        salary = await active_salary_on(session, user, employee.id, run.effective_date)
        if salary is None and left_on is not None and left_on < end:
            # A leaver's salary is the one in force on their last day.
            salary = await active_salary_on(session, user, employee.id, left_on)
        if salary is None:
            errors.append(f"{employee.employee_code}: no salary assigned for {run.effective_date}")
            continue

        try:
            # Loss of pay inside the employed days only: an absence recorded
            # outside them is already unpaid as a day not employed.
            lop = await attendance_service.get_lop_days(
                session, user, employee.id, window[0], window[1], require_lock=True
            )
        except AttendanceLockError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        divisor = salary_days_rules.salary_days(
            days_basis,
            period_start=start,
            period_end=end,
            window=window,
            lop_days=lop,
            fixed_days=fixed_days,
            is_off=(
                (
                    lambda day, emp=employee.id, plans=shift_plans: plans.resolve(emp, day).is_off(
                        day
                    )
                )
                if shift_plans is not None
                else None
            ),
        )
        if divisor.note:
            days_notices.append(f"{employee.employee_code}: {divisor.note}")

        employee_rules = {key: value for key, value in rule_sets.items() if ":" not in key}

        # Professional Tax is a state levy. Where the employee's work state
        # cannot be established for the whole period — no location, or a
        # location change mid-period — payroll flags it rather than picking
        # one, because filing in the wrong state is a silent failure.
        work_state = await statutory_service.work_state_for_period(
            session, user, employee, start, end
        )
        state_review: str | None = None
        if work_state.needs_review:
            # Both state levies are affected, not just Professional Tax — LWF
            # is resolved by the same state. Saying so on the flag keeps the
            # message honest about what was left undeducted.
            state_review = (
                f"{employee.employee_code}: {work_state.reason} Professional Tax and "
                "Labour Welfare Fund are left undeducted for this period until the "
                "work state is confirmed."
            )
        elif work_state.state_code and f"professional_tax:{work_state.state_code}" in rule_sets:
            employee_rules["professional_tax"] = rule_sets[
                f"professional_tax:{work_state.state_code}"
            ]

        if work_state.state_code and f"labour_welfare_fund:{work_state.state_code}" in rule_sets:
            employee_rules["labour_welfare_fund"] = rule_sets[
                f"labour_welfare_fund:{work_state.state_code}"
            ]

        pf_election = await statutory_service.active_pf_election(
            session, user, employee.id, run.effective_date
        )

        # ESI coverage at the start of the contribution period. Recorded here
        # for the opening month, read for every later month in it — the
        # ceiling-crossing rule cannot be decided from this month alone.
        esi_rules = employee_rules.get("esi")
        contributing_at_start, _ = await statutory_service.esi_coverage_for(
            session, user, employee_id=employee.id, on_date=start, esi_rules=esi_rules
        )

        tds_context: TdsContext | None = None
        if tds_usable and tds_rules is not None:
            try:
                base = await tax_service.build_projection_input(
                    session,
                    user,
                    employee.id,
                    fy_label,
                    use_declared_when_unproven=use_declared,
                    # The run supplies the salary figures from this computation.
                    salary_from_payroll=False,
                )
                tds_context = TdsContext(
                    rules=tds_rules.rules,
                    rule_set_id=str(tds_rules.id),
                    financial_year=fy_label,
                    months_remaining=months_remaining(run.period_month, tds_rules.rules),
                    base=without_income_paid_by_payroll(base, PAID_THROUGH_PAYROLL),
                    prior=prior_pay.get(employee.id, PriorPay()),
                    declaration_basis=declaration_basis,
                )
            except TaxRuleError as exc:
                errors.append(f"{employee.employee_code}: {exc}")
                continue

        try:
            computation = compute_payroll(
                PayrollInput(
                    employee_salary=salary,
                    period_start=start,
                    period_end=end,
                    working_days=divisor.working_days,
                    lop_days=divisor.lop_days,
                    not_employed_days=divisor.not_employed_days,
                    rule_sets=employee_rules,
                    pf_election=pf_election,
                    esi_contributing_at_period_start=contributing_at_start,
                    designation=employee.designation,
                    gender=employee.gender.value if employee.gender else None,
                    state_review=state_review,
                    inputs=tuple(collected.lines.get(employee.id, [])),
                    loan_recoveries=(
                        ()
                        if supplementary is not None
                        else tuple(collected.loans.get(employee.id, []))
                    ),
                    tds=tds_context,
                    supplementary=supplementary,
                )
            )
        except PayrollRuleError as exc:
            errors.append(f"{employee.employee_code}: {exc}")
            continue

        payslips.append(
            Payslip(
                tenant_id=user.tenant_id,
                run_id=run.id,
                employee_id=employee.id,
                employee_salary_id=salary.id,
                working_days=computation.working_days,
                paid_days=computation.paid_days,
                lop_days=computation.lop_days,
                not_employed_days=computation.not_employed_days,
                gross_earnings=computation.gross_earnings,
                total_deductions=computation.total_deductions,
                net_pay=computation.net_pay,
                employer_cost=computation.employer_cost,
                pf_wage_base=computation.pf_wage_base,
                esi_wage_base=computation.esi_wage_base,
                pf_employee=computation.pf_employee,
                pf_employer=computation.pf_employer,
                esi_employee=computation.esi_employee,
                esi_employer=computation.esi_employer,
                professional_tax=computation.professional_tax,
                tds=computation.tds,
                lwf_employee=computation.lwf_employee,
                lwf_employer=computation.lwf_employer,
                vpf=computation.vpf,
                review_flags=computation.review_flags,
                line_items=[
                    {
                        "code": line.code,
                        "name": line.name,
                        "type": line.component_type.value,
                        "amount": str(line.amount),
                        "basis": line.basis,
                        "rule_set_id": line.rule_set_id,
                    }
                    for line in computation.lines
                ],
                input_lines=[line.as_dict() for line in computation.input_lines],
                tds_working=computation.tds_working,
                is_supplementary=computation.supplementary,
                is_published=False,
            )
        )

        review_flags.extend(computation.review_flags)

        # Open the ESI contribution period on its first month, so the later
        # months of the period can apply the ceiling-crossing rule. Idempotent:
        # a recompute reads back the row it already wrote rather than
        # rewriting it against changed wages.
        # Decided on the structure's wages: overtime never decides coverage.
        if esi_rules is not None and not computation.supplementary:
            await statutory_service.open_esi_coverage_period(
                session,
                user,
                employee_id=employee.id,
                on_date=start,
                esi_wage_base=computation.esi_eligibility_base,
                esi_rules=esi_rules,
            )

        # ── Variance flagging (FS-PAY-01) ───────────────────────────────────
        # One flag per person, for a net-pay swing, deductions past the legal
        # ceiling, or both; one acknowledgement with a note covers it.
        prior = previous.get(employee.id)
        change: Decimal | None = None
        if prior is not None and prior > 0:
            moved = abs(computation.net_pay - prior) / prior * Decimal(100)
            if moved > variance_threshold_percent:
                change = moved
        if change is not None or computation.over_deduction_ceiling is not None:
            variances.append(
                {
                    "employee_id": str(employee.id),
                    "employee_code": employee.employee_code,
                    "employee_name": employee.full_name,
                    "previous_net": str(prior) if change is not None else None,
                    "current_net": str(computation.net_pay),
                    "change_percent": (
                        str(change.quantize(Decimal("0.01"))) if change is not None else None
                    ),
                    # A joining or leaving month swings net pay for a
                    # reason the reviewer should see beside the number.
                    "not_employed_days": str(computation.not_employed_days),
                    "deduction_ceiling": computation.over_deduction_ceiling,
                    "acknowledged": False,
                    "acknowledged_by_user_id": None,
                    "acknowledged_at": None,
                    "note": None,
                }
            )

    run.computed_by_user_id = user.id

    if not is_regular and not payslips and not errors:
        run.status = PayrollRunStatus.DRAFT
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Nothing is due for the chosen employees from this run's sources. Approve the "
                "adjustments or overtime it should pay, then compute again."
            ),
        )

    if errors:
        run.status = PayrollRunStatus.FAILED
        run.error_log = "\n".join(errors)
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="payroll_run",
            entity_id=run.id,
            actor=user,
            changes={"step": "compute", "outcome": "failed", "errors": len(errors)},
            summary=(
                f"Payroll for {run.period_month}/{run.period_year} could not compute for "
                f"{len(errors)} employee(s)"
            ),
            request=request,
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(f"Payroll could not compute for {len(errors)} employee(s). First: {errors[0]}"),
        )

    # Replace any prior computation for this run — recomputation is allowed
    # until the run is approved.
    #
    # Queried rather than read off `run.payslips`: that relationship is lazily
    # loaded, and touching it here is a synchronous load inside an async
    # function, which raises MissingGreenlet. Making it eager instead would
    # pull every payslip in every run whenever a run is listed.
    prior_payslips = (
        await session.scalars(
            tenant_scoped(select(Payslip).where(Payslip.run_id == run.id), Payslip, user)
        )
    ).all()
    for existing in prior_payslips:
        await session.delete(existing)
    await session.flush()
    for payslip in payslips:
        session.add(payslip)

    run.employee_count = len(payslips)
    run.total_gross = sum((p.gross_earnings for p in payslips), start=Decimal(0))
    run.total_deductions = sum((p.total_deductions for p in payslips), start=Decimal(0))
    run.total_net = sum((p.net_pay for p in payslips), start=Decimal(0))
    run.total_employer_cost = sum((p.employer_cost for p in payslips), start=Decimal(0))
    run.variance_flags = variances
    # Run-level flags go last, so the first flag stays the first employee's.
    run.review_flags = review_flags + run_flags
    run.input_notices = (
        [{"kind": "source", **report.as_dict()} for report in collected.reports]
        + [{"kind": "notice", **notice} for notice in collected.notices]
        + [
            {
                "kind": "notice",
                "key": "employment",
                "label": "Joiners and leavers",
                "message": message,
            }
            for message in employment_notices
        ]
        + [
            {
                "kind": "notice",
                "key": "salary_days",
                "label": "Salary days",
                "message": message,
            }
            for message in days_notices
        ]
        + [
            {
                "kind": "notice",
                "key": "nothing_due",
                "label": "Nothing due",
                "message": message,
            }
            for message in unpaid_notices
        ]
    )
    run.rule_set_snapshot = {key: str(value.id) for key, value in rule_sets.items()}
    run.processed_at = datetime.now(UTC)
    run.status = PayrollRunStatus.PENDING_REVIEW
    run.error_log = None

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="payroll_run",
        entity_id=run.id,
        actor=user,
        changes={
            "step": "compute",
            "employees": len(payslips),
            "total_net": str(run.total_net),
            "variances": len(variances),
            "review_flags": len(run.review_flags),
            "input_items": sum(len(p.input_lines) for p in payslips),
        },
        summary=(
            f"Computed payroll for {run.period_month}/{run.period_year}: "
            f"{len(payslips)} employees, {len(variances)} variance flag(s)"
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(run)
    return run


# ── Variances ───────────────────────────────────────────────────────────────


async def acknowledge_variance(
    session: AsyncSession,
    user: User,
    run_id: uuid.UUID,
    employee_id: uuid.UUID,
    *,
    note: str,
    request: Request | None = None,
) -> PayrollRun:
    """Record that a flagged swing in someone's net pay was looked at, and why it is right."""
    run = await get_run(session, user, run_id)
    if run.status is not PayrollRunStatus.PENDING_REVIEW:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Variances are acknowledged before approval, not on a {run.status.value} run.",
        )
    note = note.strip()
    if not note:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Say why this payslip is correct.",
        )

    flags = [dict(flag) for flag in (run.variance_flags or [])]
    target = next((f for f in flags if f.get("employee_id") == str(employee_id)), None)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This run has no variance flag for that employee.",
        )

    now = datetime.now(UTC)
    target.update(
        {
            "acknowledged": True,
            "acknowledged_by_user_id": str(user.id),
            "acknowledged_at": now.isoformat(),
            "note": note,
        }
    )
    # A new list, so the JSONB column is seen as changed.
    run.variance_flags = flags

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="payroll_run",
        entity_id=run.id,
        actor=user,
        changes={
            "step": "acknowledge_variance",
            "employee_code": target.get("employee_code"),
            "previous_net": target.get("previous_net"),
            "current_net": target.get("current_net"),
            "note": note,
        },
        summary=(
            f"Acknowledged a {target.get('change_percent')}% net pay change for "
            f"{target.get('employee_code')}"
            if target.get("change_percent") is not None
            else f"Acknowledged deductions above the legal ceiling for "
            f"{target.get('employee_code')}"
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(run)
    return run


# ── Approve ─────────────────────────────────────────────────────────────────


async def approve_run(
    session: AsyncSession,
    user: User,
    run_id: uuid.UUID,
    *,
    acknowledge_variances: bool = False,
    confirm: Confirm | None = None,
    request: Request | None = None,
) -> PayrollRun:
    """Sign off a computed run.

    Every variance flag must have been acknowledged individually, with a note
    (`acknowledge_variance`). A 20% swing in someone's net pay is either a real
    change HR knows about or a mistake; either way somebody says which before
    money moves. `acknowledge_variances` is kept for existing callers and does
    not approve past an unacknowledged flag.

    The person who computed the run cannot approve it.
    """
    run = await get_run(session, user, run_id)
    if run.status is not PayrollRunStatus.PENDING_REVIEW:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A run in status '{run.status.value}' cannot be approved",
        )

    unacknowledged = [flag for flag in (run.variance_flags or []) if not flag.get("acknowledged")]
    if unacknowledged:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{len(unacknowledged)} employee(s) have a net pay change beyond the review "
                "threshold that nobody has acknowledged. Acknowledge each one with a note "
                "before approving."
            ),
        )
    if run.review_flags:
        # Deliberately not acknowledgeable. A variance is a number to check; a
        # review flag is a number payroll could not determine — an employee
        # whose Professional Tax state is unresolved has no correct amount to
        # acknowledge. Fix the underlying record and recompute.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{len(run.review_flags)} statutory question(s) on this run could not be "
                f"determined by payroll. First: {run.review_flags[0]} "
                "Resolve them and recompute — these cannot be acknowledged away."
            ),
        )
    if run.computed_by_user_id is not None and run.computed_by_user_id == user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You computed this run, so somebody else has to approve it. Payroll is "
                "maker-checker: the person who prepares the numbers does not sign them off."
            ),
        )
    if confirm is not None:
        await confirm()

    payslips = (
        await session.scalars(
            tenant_scoped(select(Payslip).where(Payslip.run_id == run.id), Payslip, user)
        )
    ).all()

    run_id_value, period = run.id, f"{run.period_month}/{run.period_year}"
    try:
        consumed = 0
        for payslip in payslips:
            consumed += await consume_inputs(session, user, run, list(payslip.input_lines or []))
    except InputConsumedElsewhereError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    # The regime election a run used is now locked for the year: a later switch
    # is still allowed, but it recomputes the whole year (slice 07).
    financial_years = {
        (payslip.tds_working or {}).get("financial_year")
        for payslip in payslips
        if payslip.tds_working
    }
    now = datetime.now(UTC)
    if financial_years:
        elections = (
            await session.scalars(
                tenant_scoped(
                    select(TaxRegimeElection).where(
                        TaxRegimeElection.employee_id.in_([p.employee_id for p in payslips]),
                        TaxRegimeElection.financial_year.in_(financial_years),
                        TaxRegimeElection.locked_at.is_(None),
                    ),
                    TaxRegimeElection,
                    user,
                )
            )
        ).all()
        for election in elections:
            election.locked_at = now

    run.status = PayrollRunStatus.APPROVED
    run.approved_by_user_id = user.id
    run.approved_at = now

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE,
        entity_type="payroll_run",
        entity_id=run_id_value,
        actor=user,
        changes={
            "step": "approve",
            "total_net": str(run.total_net),
            "variances_acknowledged": len(run.variance_flags or []),
            "input_items_consumed": consumed,
        },
        summary=f"Approved payroll run for {period}",
        request=request,
    )
    await session.commit()
    await session.refresh(run)
    return run


# ── Cancel ──────────────────────────────────────────────────────────────────

#: Statuses a run can be discarded from: nothing has been signed off yet.
CANCELLABLE_STATUSES = (
    PayrollRunStatus.DRAFT,
    PayrollRunStatus.FAILED,
    PayrollRunStatus.PENDING_REVIEW,
)


async def cancel_run(
    session: AsyncSession,
    user: User,
    run_id: uuid.UUID,
    *,
    reason: str,
    request: Request | None = None,
) -> PayrollRun:
    """Discard a run opened in error — the wrong month, the wrong entity.

    Only before approval: nothing has been consumed, paid or filed from it yet.
    Its payslips are removed and the row stays, cancelled, with who and why; a
    regular month it held can then be opened again.
    """
    run = await get_run(session, user, run_id)
    if run.status not in CANCELLABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A {run.status.value} run cannot be cancelled. An approved run is history; "
                "a correction is an arrears or off-cycle run."
                if run.status in CLOSED_STATUSES
                else f"A run in status '{run.status.value}' cannot be cancelled."
            ),
        )
    clean = reason.strip()
    if not clean:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Say why this run is being cancelled. It goes on the run and the audit.",
        )

    payslips = (
        await session.scalars(
            tenant_scoped(select(Payslip).where(Payslip.run_id == run.id), Payslip, user)
        )
    ).all()
    for payslip in payslips:
        await session.delete(payslip)

    previous_status = run.status.value
    run.status = PayrollRunStatus.CANCELLED
    run.cancelled_at = datetime.now(UTC)
    run.cancelled_by_user_id = user.id
    run.cancel_reason = clean
    run.employee_count = 0
    run.total_gross = run.total_deductions = run.total_net = run.total_employer_cost = _ZERO
    run.variance_flags = []
    run.review_flags = []

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="payroll_run",
        entity_id=run.id,
        actor=user,
        changes={
            "step": "cancel",
            "from_status": previous_status,
            "payslips_removed": len(payslips),
            "reason": clean,
        },
        summary=(
            f"Cancelled the {run.run_type.value} payroll run for "
            f"{run.period_month}/{run.period_year}: {clean}"
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(run)
    return run


# ── Publish and pay ─────────────────────────────────────────────────────────


async def publish_run(
    session: AsyncSession,
    user: User,
    run_id: uuid.UUID,
    *,
    request: Request | None = None,
) -> PayrollRun:
    """Release an approved run's payslips to the employees, and tell them."""
    run = await get_run(session, user, run_id)
    if run.status not in CLOSED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Payslips are published from an approved run only.",
        )
    if run.published_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This run's payslips were already published on {run.published_at:%d %b %Y}.",
        )

    payslips = (
        await session.scalars(
            tenant_scoped(select(Payslip).where(Payslip.run_id == run.id), Payslip, user)
        )
    ).all()
    now = datetime.now(UTC)
    for payslip in payslips:
        payslip.is_published = True
        payslip.published_at = now
    run.published_at = now
    run.published_by_user_id = user.id

    employee_ids = [p.employee_id for p in payslips]
    recipients = (
        list(
            (
                await session.scalars(
                    tenant_scoped(
                        select(User.id).where(
                            User.employee_id.in_(employee_ids), User.is_active.is_(True)
                        ),
                        User,
                        user,
                    )
                )
            ).all()
        )
        if employee_ids
        else []
    )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="payroll_run",
        entity_id=run.id,
        actor=user,
        changes={"step": "publish", "payslips": len(payslips), "recipients": len(recipients)},
        summary=(f"Published {len(payslips)} payslip(s) for {run.period_month}/{run.period_year}"),
        request=request,
    )
    if recipients:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key="payroll.payslip.published",
            recipient_user_ids=recipients,
            payload={"period": period_label(run.period_year, run.period_month)},
            commit=False,
        )
    await session.commit()
    await session.refresh(run)
    return run


async def mark_paid(
    session: AsyncSession,
    user: User,
    run_id: uuid.UUID,
    *,
    payment_date: date | None = None,
    request: Request | None = None,
) -> PayrollRun:
    """Record that the money moved. A paid run is locked for good."""
    run = await get_run(session, user, run_id)
    if run.status is not PayrollRunStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only an approved run can be marked paid; this one is {run.status.value}.",
        )
    now = datetime.now(UTC)
    if payment_date is not None:
        start, _ = period_bounds(run.period_year, run.period_month)
        if payment_date < start:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"The payment date {payment_date} is before the period it pays for.",
            )
        run.payment_date = payment_date
    elif run.payment_date is None:
        # The workspace's date, not the server's: at 01:00 IST the UTC date is
        # still yesterday.
        run.payment_date = await attendance_service.local_today(session, user)

    run.status = PayrollRunStatus.PAID
    run.paid_at = now
    run.paid_by_user_id = user.id

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="payroll_run",
        entity_id=run.id,
        actor=user,
        changes={
            "step": "paid",
            "payment_date": run.payment_date.isoformat() if run.payment_date else None,
            "total_net": str(run.total_net),
        },
        summary=f"Marked payroll for {run.period_month}/{run.period_year} paid",
        request=request,
    )
    await session.commit()
    await session.refresh(run)
    return run


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _prior_financial_year(
    session: AsyncSession,
    user: User,
    run: PayrollRun,
    *,
    employee_ids: frozenset[uuid.UUID],
    fy_start: date,
    fy_end: date,
) -> tuple[dict[uuid.UUID, PriorPay], dict[uuid.UUID, list[dict]]]:
    """What approved runs earlier in this financial year paid, per employee.

    Earlier *months* only for recurring pay: this month's salary is projected
    from this computation, so an approved run for the same month is not counted
    twice. One-off income and TDS count from every approved run in the year.
    """
    if not employee_ids:
        return {}, {}

    rows = (
        await session.execute(
            tenant_scoped(
                select(Payslip, PayrollRun)
                .join(PayrollRun, Payslip.run_id == PayrollRun.id)
                .where(
                    Payslip.employee_id.in_(employee_ids),
                    PayrollRun.id != run.id,
                    PayrollRun.status.in_(CLOSED_STATUSES),
                    PayrollRun.effective_date >= fy_start,
                    PayrollRun.effective_date <= fy_end,
                ),
                Payslip,
                user,
            )
        )
    ).all()

    totals: dict[uuid.UUID, dict[str, Decimal | int]] = {}
    lines: dict[uuid.UUID, list[dict]] = {}
    for payslip, prior_run in rows:
        bucket = totals.setdefault(
            payslip.employee_id,
            {
                "recurring_taxable": _ZERO,
                "basic_plus_da": _ZERO,
                "hra": _ZERO,
                "employer_nps": _ZERO,
                "professional_tax": _ZERO,
                "one_off_taxable": _ZERO,
                "tds_deducted": _ZERO,
                "months_on_record": 0,
            },
        )
        bucket["tds_deducted"] += Decimal(str(payslip.tds))
        for line in payslip.input_lines or []:
            if line.get("skipped"):
                continue
            bucket["one_off_taxable"] += Decimal(str(line.get("taxable_amount", "0")))
        lines.setdefault(payslip.employee_id, []).extend(payslip.input_lines or [])

        is_earlier_month = (prior_run.period_year, prior_run.period_month) < (
            run.period_year,
            run.period_month,
        )
        if not is_earlier_month:
            continue
        working = payslip.tds_working or {}
        projection = working.get("projection") or {}
        if "this_month_taxable_pay" in projection:
            bucket["recurring_taxable"] += Decimal(projection["this_month_taxable_pay"])
        else:
            # A payslip from before TDS was wired in: its structure earnings are
            # the best record of taxable pay that month.
            bucket["recurring_taxable"] += Decimal(str(payslip.gross_earnings))
        bucket["basic_plus_da"] += Decimal(str(payslip.pf_wage_base))
        bucket["professional_tax"] += Decimal(str(payslip.professional_tax))
        for line in payslip.line_items or []:
            code = str(line.get("code", ""))
            if code == "HRA":
                bucket["hra"] += Decimal(str(line.get("amount", "0")))
            elif code in ("NPS_ER", "EMPLOYER_NPS"):
                bucket["employer_nps"] += Decimal(str(line.get("amount", "0")))
        bucket["months_on_record"] += 1

    prior = {
        employee_id: PriorPay(
            recurring_taxable=Decimal(values["recurring_taxable"]),
            basic_plus_da=Decimal(values["basic_plus_da"]),
            hra=Decimal(values["hra"]),
            employer_nps=Decimal(values["employer_nps"]),
            professional_tax=Decimal(values["professional_tax"]),
            one_off_taxable=Decimal(values["one_off_taxable"]),
            tds_deducted=Decimal(values["tds_deducted"]),
            months_on_record=int(values["months_on_record"]),
        )
        for employee_id, values in totals.items()
    }
    return prior, lines


async def _agreed_last_days(
    session: AsyncSession, user: User, employee_ids: frozenset[uuid.UUID]
) -> dict[uuid.UUID, date]:
    """The agreed last working day of everyone with an open exit.

    `Employee.date_of_exit` is written only when access is revoked, which is
    usually after the last payroll that should pay them; until then the exit
    record's approved last day is the date employment ends.
    """
    if not employee_ids:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(
                select(ExitRecord.employee_id, ExitRecord.approved_last_working_day)
                .where(
                    ExitRecord.employee_id.in_(employee_ids),
                    ExitRecord.status != ExitStatus.CANCELLED,
                    ExitRecord.approved_last_working_day.is_not(None),
                )
                .order_by(ExitRecord.created_at),
                ExitRecord,
                user,
            )
        )
    ).all()
    # The latest exit wins, should there ever be more than one open.
    return {employee_id: last_day for employee_id, last_day in rows}


async def _month_paid_so_far(
    session: AsyncSession,
    user: User,
    run: PayrollRun,
    employee_ids: frozenset[uuid.UUID],
) -> dict[uuid.UUID, dict]:
    """What approved runs already paid each employee for this run's month.

    A supplementary payslip tops the month up: it needs the month's gross so
    far (for the Professional Tax slab), the PT already taken, and whether ESI
    was charged — and whether anybody paid the structure at all.
    """
    if not employee_ids:
        return {}
    rows = (
        (
            await session.execute(
                tenant_scoped(
                    select(Payslip)
                    .join(PayrollRun, Payslip.run_id == PayrollRun.id)
                    .where(
                        PayrollRun.legal_entity_id == run.legal_entity_id,
                        PayrollRun.period_year == run.period_year,
                        PayrollRun.period_month == run.period_month,
                        PayrollRun.status.in_(CLOSED_STATUSES),
                        PayrollRun.id != run.id,
                        Payslip.employee_id.in_(employee_ids),
                    ),
                    Payslip,
                    user,
                )
            )
        )
        .scalars()
        .all()
    )
    paid: dict[uuid.UUID, dict] = {}
    for payslip in rows:
        bucket = paid.setdefault(
            payslip.employee_id,
            {"gross": _ZERO, "pt": _ZERO, "esi_covered": False, "has_full": False},
        )
        wage_lines = sum(
            (
                Decimal(str(line.get("amount", "0")))
                for line in (payslip.input_lines or [])
                if line.get("counts_as_wages") and not line.get("skipped")
            ),
            start=_ZERO,
        )
        bucket["gross"] += Decimal(str(payslip.gross_earnings)) + wage_lines
        bucket["pt"] += Decimal(str(payslip.professional_tax))
        bucket["esi_covered"] = bucket["esi_covered"] or Decimal(str(payslip.esi_employee)) > 0
        bucket["has_full"] = bucket["has_full"] or not payslip.is_supplementary
    return paid


async def _previous_net_by_employee(
    session: AsyncSession, user: User, run: PayrollRun
) -> dict[uuid.UUID, Decimal]:
    """Net pay from the prior regular cycle, for variance comparison."""
    if run.period_month == 1:
        year, month = run.period_year - 1, 12
    else:
        year, month = run.period_year, run.period_month - 1
    previous_run = await session.scalar(
        tenant_scoped(
            select(PayrollRun).where(
                PayrollRun.legal_entity_id == run.legal_entity_id,
                PayrollRun.period_year == year,
                PayrollRun.period_month == month,
                PayrollRun.run_type == PayrollRunType.REGULAR,
                PayrollRun.status != PayrollRunStatus.CANCELLED,
            ),
            PayrollRun,
            user,
        )
    )
    if previous_run is None:
        return {}

    payslips = (
        await session.scalars(
            tenant_scoped(select(Payslip).where(Payslip.run_id == previous_run.id), Payslip, user)
        )
    ).all()
    return {p.employee_id: Decimal(str(p.net_pay)) for p in payslips}


def other_earnings_of(payslip: Payslip) -> Decimal:
    """Cash from input lines: what sits between gross earnings and net pay."""
    return sum(
        (
            Decimal(str(line.get("amount", "0")))
            for line in (payslip.input_lines or [])
            if line.get("kind") in _INPUT_CASH_KINDS and not line.get("skipped")
        ),
        start=_ZERO,
    ).quantize(Decimal("0.01"))
