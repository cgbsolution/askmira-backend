"""Orchestration for a payroll run.

This is where slice 01's computation and slice 02's attendance meet. The rule
the slice spec is explicit about, and which is enforced here rather than by
convention:

    A payroll run for a period where attendance has not been finalised must
    refuse to run rather than assume zero LOP.

Assuming zero silently overpays everyone who had unpaid absence that month, and
the error is only discovered when someone reconciles the bank file. Blocking is
recoverable; overpaying is not.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmployeeWorkLocation, EmploymentStatus
from app.models.payroll import (
    PayrollRuleSet,
    PayrollRun,
    PayrollRunStatus,
    PayrollRunType,
    Payslip,
)
from app.models.user import User
from app.services import audit
from app.services.attendance import service as attendance_service
from app.services.attendance.service import AttendanceLockError
from app.services.payroll import statutory_service
from app.services.payroll.engine import PayrollInput, PayrollRuleError, compute_payroll
from app.services.payroll.service import active_salary_on
from app.services.payroll.statutory import PtStateResolution, resolve_professional_tax_state

# Rule types resolved for every run. Professional Tax and the Labour Welfare
# Fund are state levies, so they are resolved per employee by their location.
CENTRAL_RULE_TYPES = ("pf", "esi", "tds")
STATE_RULE_TYPES = ("professional_tax", "labour_welfare_fund")


def period_bounds(year: int, month: int) -> tuple[date, date]:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


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
                resolved[f"{rule_set.rule_type}:{rule_set.jurisdiction}"] = rule_set
        elif rule_set.rule_type in CENTRAL_RULE_TYPES:
            existing = resolved.get(rule_set.rule_type)
            # Latest effective version wins when several are still open.
            if existing is None or rule_set.effective_from > existing.effective_from:
                resolved[rule_set.rule_type] = rule_set
    return resolved


async def create_run(
    session: AsyncSession,
    user: User,
    *,
    legal_entity_id: uuid.UUID,
    period_year: int,
    period_month: int,
    run_type: PayrollRunType = PayrollRunType.REGULAR,
    request: Request | None = None,
) -> PayrollRun:
    """Open a draft run for a period.

    `effective_date` is fixed here, at the period end, and never re-derived —
    that is what makes a re-run months later produce the identical result.
    """
    start, end = period_bounds(period_year, period_month)

    existing = await session.scalar(
        tenant_scoped(
            select(PayrollRun).where(
                PayrollRun.legal_entity_id == legal_entity_id,
                PayrollRun.period_year == period_year,
                PayrollRun.period_month == period_month,
                PayrollRun.run_type == run_type,
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
        run_type=run_type,
        status=PayrollRunStatus.DRAFT,
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
        changes={"period": f"{period_month}/{period_year}", "type": run_type.value},
        summary=f"Opened {run_type.value} payroll run for {period_month}/{period_year}",
        request=request,
    )
    await session.commit()
    await session.refresh(run)
    return run


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
    run = await session.scalar(
        tenant_scoped(select(PayrollRun).where(PayrollRun.id == run_id), PayrollRun, user)
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payroll run not found")
    if run.is_locked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This run has been paid and cannot be recomputed. Raise an arrears run.",
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

    employees = (
        await session.scalars(
            tenant_scoped(
                select(Employee).where(
                    Employee.legal_entity_id == run.legal_entity_id,
                    Employee.deleted_at.is_(None),
                    Employee.status.in_(
                        [
                            EmploymentStatus.ACTIVE,
                            EmploymentStatus.ON_NOTICE,
                            EmploymentStatus.ON_LEAVE,
                        ]
                    ),
                ),
                Employee,
                user,
            )
        )
    ).all()

    # State codes in scope, so Professional Tax resolves per location.
    states = {
        loc_state
        for loc_state in [
            (await _location_state(session, user, e.location_id)) for e in employees
        ]
        if loc_state
    }
    rule_sets = await _resolve_rule_sets(session, user, run.effective_date, states)

    run.status = PayrollRunStatus.PROCESSING
    await session.flush()

    # Prior cycle payslips, for variance flagging.
    previous = await _previous_net_by_employee(session, user, run)

    working_days = Decimal((end - start).days + 1)
    payslips: list[Payslip] = []
    variances: list[dict] = []
    review_flags: list[str] = []
    errors: list[str] = []

    for employee in employees:
        salary = await active_salary_on(session, user, employee.id, run.effective_date)
        if salary is None:
            errors.append(f"{employee.employee_code}: no salary assigned for {run.effective_date}")
            continue

        try:
            lop = await attendance_service.get_lop_days(
                session, user, employee.id, start, end, require_lock=True
            )
        except AttendanceLockError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        employee_rules = {
            key: value for key, value in rule_sets.items() if ":" not in key
        }

        # Professional Tax is a state levy. Where the employee's work state
        # cannot be established for the whole period — no location, or a
        # location change mid-period — payroll flags it rather than picking
        # one, because filing in the wrong state is a silent failure.
        work_state = await _professional_tax_state(session, user, employee, start, end)
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

        try:
            computation = compute_payroll(
                PayrollInput(
                    employee_salary=salary,
                    period_start=start,
                    period_end=end,
                    working_days=working_days,
                    lop_days=lop,
                    rule_sets=employee_rules,
                    pf_election=pf_election,
                    esi_contributing_at_period_start=contributing_at_start,
                    designation=employee.designation,
                    state_review=state_review,
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
            )
        )

        review_flags.extend(computation.review_flags)

        # Open the ESI contribution period on its first month, so the later
        # months of the period can apply the ceiling-crossing rule. Idempotent:
        # a recompute reads back the row it already wrote rather than
        # rewriting it against changed wages.
        if esi_rules is not None:
            await statutory_service.open_esi_coverage_period(
                session,
                user,
                employee_id=employee.id,
                on_date=start,
                esi_wage_base=computation.esi_wage_base,
                esi_rules=esi_rules,
            )

        # ── Variance flagging (FS-PAY-01) ───────────────────────────────────
        prior = previous.get(employee.id)
        if prior is not None and prior > 0:
            change = abs(computation.net_pay - prior) / prior * Decimal(100)
            if change > variance_threshold_percent:
                variances.append(
                    {
                        "employee_id": str(employee.id),
                        "employee_code": employee.employee_code,
                        "previous_net": str(prior),
                        "current_net": str(computation.net_pay),
                        "change_percent": str(change.quantize(Decimal("0.01"))),
                    }
                )

    if errors:
        run.status = PayrollRunStatus.FAILED
        run.error_log = "\n".join(errors)
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Payroll could not compute for {len(errors)} employee(s). "
                f"First: {errors[0]}"
            ),
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
    run.review_flags = review_flags
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
            "employees": len(payslips),
            "total_net": str(run.total_net),
            "variances": len(variances),
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


async def approve_run(
    session: AsyncSession,
    user: User,
    run_id: uuid.UUID,
    *,
    acknowledge_variances: bool = False,
    request: Request | None = None,
) -> PayrollRun:
    """Sign off a computed run.

    Unacknowledged variance flags block approval. A 20% swing in someone's net
    pay is either a real change HR knows about or a mistake; either way it
    should be looked at before money moves.
    """
    run = await session.scalar(
        tenant_scoped(select(PayrollRun).where(PayrollRun.id == run_id), PayrollRun, user)
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payroll run not found")
    if run.status is not PayrollRunStatus.PENDING_REVIEW:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A run in status '{run.status.value}' cannot be approved",
        )
    if run.variance_flags and not acknowledge_variances:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{len(run.variance_flags)} employee(s) have a net pay change beyond the "
                "review threshold. Review them and approve with acknowledgement."
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

    run.status = PayrollRunStatus.APPROVED
    run.approved_by_user_id = user.id
    run.approved_at = datetime.now(UTC)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE,
        entity_type="payroll_run",
        entity_id=run.id,
        actor=user,
        changes={
            "total_net": str(run.total_net),
            "variances_acknowledged": len(run.variance_flags),
        },
        summary=f"Approved payroll run for {run.period_month}/{run.period_year}",
        request=request,
    )
    await session.commit()
    await session.refresh(run)
    return run


async def _professional_tax_state(
    session: AsyncSession,
    user: User,
    employee: Employee,
    period_start: date,
    period_end: date,
) -> PtStateResolution:
    """Which state's levies apply to this employee for this period.

    An employee with no recorded transfers resolves from their current
    location. One with a transfer history is resolved from the series, which is
    what makes a mid-period move visible instead of silently applying the new
    state to the whole month.
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
        state = await _location_state(session, user, employee.location_id)
        if state is None:
            return PtStateResolution(
                state_code=None,
                needs_review=True,
                reason=(
                    "This employee has no work location, so no Professional Tax state "
                    "can be determined."
                ),
            )
        return PtStateResolution(
            state_code=state, needs_review=False, reason=f"Works in {state}."
        )

    history: list[tuple[date, str]] = []
    for posting in postings:
        state = await _location_state(session, user, posting.location_id)
        if state:
            history.append((posting.effective_from, state))

    return resolve_professional_tax_state(
        location_history=history, period_start=period_start, period_end=period_end
    )


async def _location_state(
    session: AsyncSession, user: User, location_id: uuid.UUID | None
) -> str | None:
    if location_id is None:
        return None
    from app.models.org import Location

    return await session.scalar(
        tenant_scoped(
            select(Location.state_code).where(Location.id == location_id), Location, user
        )
    )


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
            ),
            PayrollRun,
            user,
        )
    )
    if previous_run is None:
        return {}

    payslips = (
        await session.scalars(
            tenant_scoped(
                select(Payslip).where(Payslip.run_id == previous_run.id), Payslip, user
            )
        )
    ).all()
    return {p.employee_id: Decimal(str(p.net_pay)) for p in payslips}
