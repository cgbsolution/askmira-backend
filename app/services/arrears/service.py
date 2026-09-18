"""Salary revision and arrear batch persistence.

Nothing in this module writes to a `PayrollRun` or a `Payslip`. That is the
whole design: arrears are new rows, and the closed period keeps saying what it
always said.

The guardrails are here rather than in a router because they are correctness
conditions, not input validation:

- A revision effective before the employee joined is nonsense, not a typo.
- A revision into a period whose attendance is not locked would recompute
  against loss-of-pay days that are still moving.
- A batch above the tenant's threshold needs a second look before it is paid.
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
from app.models.arrears import (
    ArrearBatch,
    ArrearLine,
    ArrearStatus,
    RevisionType,
    SalaryRevision,
)
from app.models.attendance import AttendancePeriodLock
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.payroll import (
    EmployeeSalary,
    PayrollRuleSet,
    PayrollRun,
    PayrollRunStatus,
    Payslip,
)
from app.models.user import User
from app.services import audit
from app.services.arrears import engine

__all__ = [
    "approve_batch",
    "preview_arrears",
    "revise_salary",
]

ZERO = Decimal(0)

# Above this, a batch needs a second approval beyond the ordinary one. A
# tenant setting; the default is deliberately low enough to be noticed.
DEFAULT_ADDITIONAL_APPROVAL_THRESHOLD = Decimal(500_000)


def _period(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


async def _historical_rule_sets(
    session: AsyncSession, user: User, effective_date: date, jurisdictions: set[str]
) -> dict[str, PayrollRuleSet]:
    """The rule versions in force on a past date.

    Resolved against the closed run's own `effective_date`, never against
    today. This is the single most important query in the slice: recomputing
    March against a ceiling that changed in July produces a March figure that
    could never have been deducted in March.
    """
    rows = (
        await session.scalars(
            tenant_scoped(
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
        )
    ).all()

    resolved: dict[str, PayrollRuleSet] = {}
    for rule_set in rows:
        # State levies only apply where the employee actually worked.
        if (
            rule_set.rule_type in ("professional_tax", "labour_welfare_fund")
            and rule_set.jurisdiction not in jurisdictions
        ):
            continue
        existing = resolved.get(rule_set.rule_type)
        if existing is None or rule_set.effective_from > existing.effective_from:
            resolved[rule_set.rule_type] = rule_set
    return resolved


async def _state_of(
    session: AsyncSession, user: User, location_id: uuid.UUID | None
) -> str | None:
    """The employee's work state, for resolving state levies."""
    if location_id is None:
        return None
    from app.models.org import Location

    return await session.scalar(
        tenant_scoped(
            select(Location.state_code).where(Location.id == location_id), Location, user
        )
    )


async def _closed_runs(
    session: AsyncSession,
    user: User,
    *,
    legal_entity_id: uuid.UUID,
    from_date: date,
    to_date: date,
) -> list[PayrollRun]:
    """Approved or paid runs whose period the revision reaches back into."""
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(PayrollRun)
                    .where(
                        PayrollRun.legal_entity_id == legal_entity_id,
                        PayrollRun.status.in_(
                            [PayrollRunStatus.APPROVED, PayrollRunStatus.PAID]
                        ),
                        PayrollRun.effective_date >= from_date,
                        PayrollRun.effective_date <= to_date,
                    )
                    .order_by(PayrollRun.period_year, PayrollRun.period_month),
                    PayrollRun,
                    user,
                )
            )
        ).all()
    )


async def _guard_revision(
    session: AsyncSession,
    user: User,
    employee: Employee,
    effective_from: date,
) -> None:
    """The three refusals, checked before anything is computed."""
    if employee.date_of_joining and effective_from < employee.date_of_joining:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"The revision is effective {effective_from} but "
                f"{employee.employee_code} joined on {employee.date_of_joining}. A "
                "salary cannot apply before someone was employed."
            ),
        )

    if effective_from > date.today():
        # A forward-dated revision is fine and generates no arrears; it is
        # allowed, and this is only reached for the back-dated path.
        return

    # Every month from the effective date to now needs locked attendance, or
    # the recomputation runs against loss-of-pay days that are still moving.
    cursor = date(effective_from.year, effective_from.month, 1)
    today = date.today()
    while cursor <= today:
        last = calendar.monthrange(cursor.year, cursor.month)[1]
        month_end = date(cursor.year, cursor.month, last)
        lock = await session.scalar(
            tenant_scoped(
                select(AttendancePeriodLock).where(
                    AttendancePeriodLock.unlocked_at.is_(None),
                    AttendancePeriodLock.period_start <= cursor,
                    AttendancePeriodLock.period_end >= month_end,
                ),
                AttendancePeriodLock,
                user,
            )
        )
        if lock is None and month_end < today:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Attendance for {cursor.strftime('%m/%Y')} is not locked. An arrear "
                    "recomputed against loss-of-pay days that are still moving would be "
                    "wrong the moment someone regularises a punch."
                ),
            )
        cursor = date(cursor.year + (cursor.month // 12), (cursor.month % 12) + 1, 1)


# ═══════════════════════════════════════════════════════════════════════════
# Preview
# ═══════════════════════════════════════════════════════════════════════════


async def preview_arrears(
    session: AsyncSession,
    user: User,
    *,
    legal_entity_id: uuid.UUID,
    employee_salaries: dict[uuid.UUID, EmployeeSalary],
    effective_from: date,
) -> tuple[dict[str, list[engine.PeriodArrear]], engine.ArrearSummary]:
    """Compute the arrears without writing anything.

    A payroll lead approving a liability needs to see the total before it
    exists, not after. Nothing here touches the database beyond reading.
    """
    per_employee: dict[str, list[engine.PeriodArrear]] = {}
    runs = await _closed_runs(
        session,
        user,
        legal_entity_id=legal_entity_id,
        from_date=effective_from,
        to_date=date.today(),
    )

    for employee_id, salary in employee_salaries.items():
        employee = await session.scalar(
            tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
        )
        if employee is None:
            continue

        state = await _state_of(session, user, employee.location_id)

        arrears: list[engine.PeriodArrear] = []
        for run in runs:
            payslip = await session.scalar(
                tenant_scoped(
                    select(Payslip).where(
                        Payslip.run_id == run.id, Payslip.employee_id == employee_id
                    ),
                    Payslip,
                    user,
                )
            )
            if payslip is None:
                continue

            # The rules that were in force then, resolved by the run's own
            # effective date and the employee's own state — not a fixed list of
            # states, which would apply Karnataka's Professional Tax to someone
            # in Maharashtra.
            rule_sets = await _historical_rule_sets(
                session, user, run.effective_date, {state} if state else set()
            )
            if not rule_sets:
                continue

            start = date(run.period_year, run.period_month, 1)
            last = calendar.monthrange(run.period_year, run.period_month)[1]
            try:
                arrears.append(
                    engine.recompute_period(
                        period=_period(run.period_year, run.period_month),
                        paid_line_items=payslip.line_items or [],
                        new_salary=salary,
                        working_days=Decimal(str(payslip.working_days)),
                        lop_days=Decimal(str(payslip.lop_days)),
                        period_start=start,
                        period_end=date(run.period_year, run.period_month, last),
                        historical_rule_sets=rule_sets,
                        designation=employee.designation,
                    )
                )
            except (engine.ArrearError, ValueError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"{employee.employee_code}, "
                        f"{_period(run.period_year, run.period_month)}: {exc}"
                    ),
                ) from exc

        if arrears:
            per_employee[str(employee_id)] = arrears

    return per_employee, engine.summarise(per_employee)


# ═══════════════════════════════════════════════════════════════════════════
# Commit
# ═══════════════════════════════════════════════════════════════════════════


async def revise_salary(
    session: AsyncSession,
    user: User,
    *,
    legal_entity_id: uuid.UUID,
    revisions: list[dict],
    effective_from: date,
    revision_type: RevisionType,
    reason: str | None = None,
    threshold: Decimal | None = None,
    request: Request | None = None,
) -> ArrearBatch:
    """Apply salary revisions and compute the arrears they generate.

    `revisions` is `[{"employee_id", "new_salary_id", "previous_salary_id"}]`.
    An increment cycle passes hundreds and gets **one** batch back — a payroll
    lead asked to approve three hundred separate batches will approve them
    without reading any.
    """
    if not revisions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No revisions were supplied."
        )

    salaries: dict[uuid.UUID, EmployeeSalary] = {}
    employees: dict[uuid.UUID, Employee] = {}

    for item in revisions:
        employee_id = uuid.UUID(str(item["employee_id"]))
        employee = await session.scalar(
            tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
        )
        if employee is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Employee {employee_id} not found",
            )
        await _guard_revision(session, user, employee, effective_from)

        salary = await session.scalar(
            tenant_scoped(
                select(EmployeeSalary).where(
                    EmployeeSalary.id == uuid.UUID(str(item["new_salary_id"]))
                ),
                EmployeeSalary,
                user,
            )
        )
        if salary is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Salary {item['new_salary_id']} not found",
            )
        salaries[employee_id] = salary
        employees[employee_id] = employee

    per_employee, summary = await preview_arrears(
        session,
        user,
        legal_entity_id=legal_entity_id,
        employee_salaries=salaries,
        effective_from=effective_from,
    )

    periods = sorted(
        {arrear.period for arrears in per_employee.values() for arrear in arrears}
    )
    limit = threshold or DEFAULT_ADDITIONAL_APPROVAL_THRESHOLD

    batch = ArrearBatch(
        tenant_id=user.tenant_id,
        legal_entity_id=legal_entity_id,
        from_period=periods[0] if periods else _period(effective_from.year, effective_from.month),
        to_period=periods[-1] if periods else _period(effective_from.year, effective_from.month),
        status=ArrearStatus.PENDING_APPROVAL,
        employee_count=summary.employee_count,
        total_earnings_arrear=summary.earnings,
        total_employee_deduction_arrear=summary.employee_deductions,
        total_employer_contribution_arrear=summary.employer_contributions,
        total_net_payable=summary.net_payable,
        affected_filings=summary.affected_filings,
        computed_at=datetime.now(UTC),
        computed_by_user_id=user.id,
        requires_additional_approval=summary.net_payable > limit,
    )
    session.add(batch)
    await session.flush()

    for employee_id_str, arrears in per_employee.items():
        employee_id = uuid.UUID(employee_id_str)
        for arrear in arrears:
            for diff in arrear.diffs:
                if diff.difference == ZERO:
                    continue
                session.add(
                    ArrearLine(
                        tenant_id=user.tenant_id,
                        batch_id=batch.id,
                        employee_id=employee_id,
                        period=arrear.period,
                        component_code=diff.code,
                        component_name=diff.name,
                        component_type=diff.component_type,
                        paid_amount=diff.paid,
                        should_have_been_amount=diff.should_have_been,
                        difference=diff.difference,
                        rule_set_id=(
                            uuid.UUID(arrear.rule_set_ids["pf"])
                            if "pf" in arrear.rule_set_ids
                            else None
                        ),
                    )
                )

    for employee_id, salary in salaries.items():
        previous = next(
            (
                item.get("previous_salary_id")
                for item in revisions
                if uuid.UUID(str(item["employee_id"])) == employee_id
            ),
            None,
        )
        session.add(
            SalaryRevision(
                tenant_id=user.tenant_id,
                employee_id=employee_id,
                previous_salary_id=uuid.UUID(str(previous)) if previous else None,
                new_salary_id=salary.id,
                effective_from=effective_from,
                revision_type=revision_type,
                reason=reason,
                processed_at=datetime.now(UTC),
                arrear_batch_id=batch.id,
            )
        )

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="arrear_batch",
        entity_id=batch.id,
        summary=(
            f"Computed arrears for {batch.employee_count} employee(s) from "
            f"{batch.from_period} to {batch.to_period}: {batch.total_net_payable} net"
            f"{' — above the approval threshold' if batch.requires_additional_approval else ''}"
        ),
        changes={
            "revision_type": revision_type.value,
            "effective_from": str(effective_from),
            "net_payable": str(batch.total_net_payable),
            "employer_contributions": str(batch.total_employer_contribution_arrear),
            "affected_filings": batch.affected_filings,
        },
        request=request,
    )
    return batch


async def approve_batch(
    session: AsyncSession,
    user: User,
    *,
    batch_id: uuid.UUID,
    acknowledge_filings: bool = False,
    request: Request | None = None,
) -> ArrearBatch:
    """Sign off an arrear batch for payment in a future run.

    Four refusals:

    - **The person who computed the batch cannot approve it.** An arrear is
      money leaving the company; the second pair of eyes is the control.
    - **Nobody approves a batch that pays themselves.**
    - **Affected filings must be acknowledged.** A revised ECR for a closed
      month is a decision with consequences, and approving past it silently
      means nobody made it.
    - **Above the threshold, two different approvers.** The first signature is
      recorded and the batch waits for the second.
    """
    batch = await session.scalar(
        tenant_scoped(
            select(ArrearBatch).where(ArrearBatch.id == batch_id), ArrearBatch, user
        )
    )
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Arrear batch not found")
    if batch.status is not ArrearStatus.PENDING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A batch in status '{batch.status.value}' is not awaiting approval.",
        )
    if batch.computed_by_user_id is not None and batch.computed_by_user_id == user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You committed this revision, so you cannot approve its arrears. A "
                "different person has to look at it."
            ),
        )
    if user.employee_id is not None:
        own = await session.scalar(
            tenant_scoped(
                select(ArrearLine.id).where(
                    ArrearLine.batch_id == batch.id,
                    ArrearLine.employee_id == user.employee_id,
                ),
                ArrearLine,
                user,
            )
        )
        if own is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This batch pays arrears to you, so somebody else has to approve it.",
            )
    if batch.affected_filings and not acknowledge_filings:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This arrear changes figures in {len(batch.affected_filings)} already-"
                f"filed return(s): {', '.join(batch.affected_filings[:3])}"
                f"{'…' if len(batch.affected_filings) > 3 else ''}. Decide what to do "
                "about each — a revised filing, or carrying the difference forward — "
                "and approve with acknowledgement."
            ),
        )

    now = datetime.now(UTC)
    first_of_two = False
    if batch.requires_additional_approval:
        if batch.approved_by_user_id is None:
            batch.approved_by_user_id = user.id
            batch.approved_at = now
            first_of_two = True
        elif batch.approved_by_user_id == user.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "You have already given the first approval. This batch is above the "
                    "approval threshold, so the second approval has to come from "
                    "somebody else."
                ),
            )
        else:
            batch.additional_approved_by_user_id = user.id
            batch.additional_approved_at = now
            batch.status = ArrearStatus.APPROVED
    else:
        batch.status = ArrearStatus.APPROVED
        batch.approved_by_user_id = user.id
        batch.approved_at = now
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE,
        entity_type="arrear_batch",
        entity_id=batch.id,
        summary=(
            f"First of two approvals for arrears of {batch.total_net_payable}; a second "
            "approver is required above the threshold"
            if first_of_two
            else (
                f"Approved arrears of {batch.total_net_payable} for {batch.employee_count} "
                f"employee(s), acknowledging {len(batch.affected_filings)} affected filing(s)"
            )
        ),
        changes={
            "acknowledged_filings": batch.affected_filings,
            "stage": "first" if first_of_two else "final",
        },
        request=request,
    )
    return batch


# ═══════════════════════════════════════════════════════════════════════════
# Guardrails as a preview, and read models
# ═══════════════════════════════════════════════════════════════════════════


async def preview_guardrails(
    session: AsyncSession,
    user: User,
    *,
    employee_ids: list[uuid.UUID],
    effective_from: date,
) -> list[dict]:
    """The refusals a commit would raise, collected rather than raised.

    The preview shows them before anybody presses commit.
    """
    problems: list[dict] = []
    for employee_id in employee_ids:
        employee = await session.scalar(
            tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
        )
        if employee is None:
            problems.append(
                {"employee_id": str(employee_id), "employee_code": None,
                 "message": "Employee not found."}
            )
            continue
        try:
            await _guard_revision(session, user, employee, effective_from)
        except HTTPException as exc:
            problems.append(
                {
                    "employee_id": str(employee_id),
                    "employee_code": employee.employee_code,
                    "message": str(exc.detail),
                }
            )
    return problems


async def employee_labels(
    session: AsyncSession, user: User, ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, str]]:
    if not ids:
        return {}
    rows = (
        await session.scalars(
            tenant_scoped(select(Employee).where(Employee.id.in_(ids)), Employee, user)
        )
    ).all()
    return {row.id: (row.employee_code, row.full_name) for row in rows}


async def get_batch(session: AsyncSession, user: User, batch_id: uuid.UUID) -> ArrearBatch:
    batch = await session.scalar(
        tenant_scoped(select(ArrearBatch).where(ArrearBatch.id == batch_id), ArrearBatch, user)
    )
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Arrear batch not found")
    return batch


async def batch_lines(
    session: AsyncSession,
    user: User,
    batch_id: uuid.UUID,
    *,
    employee_id: uuid.UUID | None = None,
) -> list[tuple[ArrearLine, tuple[str, str] | None]]:
    await get_batch(session, user, batch_id)
    stmt = (
        select(ArrearLine)
        .where(ArrearLine.batch_id == batch_id)
        .order_by(ArrearLine.employee_id, ArrearLine.period, ArrearLine.component_code)
    )
    if employee_id is not None:
        stmt = stmt.where(ArrearLine.employee_id == employee_id)
    lines = (await session.scalars(tenant_scoped(stmt, ArrearLine, user))).all()
    labels = await employee_labels(session, user, {line.employee_id for line in lines})
    return [(line, labels.get(line.employee_id)) for line in lines]


# ═══════════════════════════════════════════════════════════════════════════
# For the payroll run (workstream W2): what is payable, and marking it paid
# ═══════════════════════════════════════════════════════════════════════════


async def payable_arrears(
    session: AsyncSession,
    user: User,
    *,
    legal_entity_id: uuid.UUID | None = None,
    employee_ids: set[uuid.UUID] | None = None,
) -> list[dict]:
    """Approved, unpaid arrears, per batch and employee, per component.

    The payroll run reads this to add arrear lines to the next payslip. Each
    component keeps its own type, because basic and an allowance are taxed and
    counted towards PF differently.
    """
    stmt = select(ArrearBatch).where(
        ArrearBatch.status == ArrearStatus.APPROVED,
        ArrearBatch.paid_in_run_id.is_(None),
    )
    if legal_entity_id is not None:
        stmt = stmt.where(ArrearBatch.legal_entity_id == legal_entity_id)
    batches = (await session.scalars(tenant_scoped(stmt, ArrearBatch, user))).all()

    out: list[dict] = []
    for batch in batches:
        lines = (
            await session.scalars(
                tenant_scoped(
                    select(ArrearLine).where(ArrearLine.batch_id == batch.id),
                    ArrearLine,
                    user,
                )
            )
        ).all()
        per_employee: dict[uuid.UUID, dict[tuple[str, str, str], Decimal]] = {}
        for line in lines:
            if employee_ids is not None and line.employee_id not in employee_ids:
                continue
            key = (line.component_code, line.component_name, line.component_type)
            bucket = per_employee.setdefault(line.employee_id, {})
            bucket[key] = bucket.get(key, ZERO) + Decimal(str(line.difference))
        labels = await employee_labels(session, user, set(per_employee))
        for employee_id, components in per_employee.items():
            earnings = sum(
                (v for (_, _, kind), v in components.items() if kind == "earning"), ZERO
            )
            deductions = sum(
                (v for (_, _, kind), v in components.items() if kind == "deduction"), ZERO
            )
            code, name = labels.get(employee_id, (None, None))
            out.append(
                {
                    "batch_id": batch.id,
                    "employee_id": employee_id,
                    "employee_code": code,
                    "employee_name": name,
                    "from_period": batch.from_period,
                    "to_period": batch.to_period,
                    "components": [
                        {
                            "component_code": comp_code,
                            "component_name": comp_name,
                            "component_type": kind,
                            "amount": amount,
                        }
                        for (comp_code, comp_name, kind), amount in sorted(components.items())
                        if amount != ZERO
                    ],
                    "net_payable": engine_net(earnings, deductions),
                }
            )
    return out


def engine_net(earnings: Decimal, deductions: Decimal) -> Decimal:
    """Earnings less employee deductions. Employer contributions never net in."""
    return earnings - deductions


async def mark_arrears_paid(
    session: AsyncSession,
    user: User,
    *,
    batch_id: uuid.UUID,
    payroll_run_id: uuid.UUID,
    request: Request | None = None,
) -> ArrearBatch:
    """Record the future run that paid a batch. The caller commits.

    Only the batch changes: the run is referenced, never written.
    """
    batch = await get_batch(session, user, batch_id)
    if batch.status is not ArrearStatus.APPROVED or batch.paid_in_run_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only an approved, unpaid arrear batch can be marked paid.",
        )
    batch.paid_in_run_id = payroll_run_id
    batch.status = ArrearStatus.PAID
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="arrear_batch",
        entity_id=batch.id,
        summary=f"Arrears of {batch.total_net_payable} paid in run {payroll_run_id}",
        changes={"paid_in_run_id": str(payroll_run_id)},
        request=request,
    )
    return batch
