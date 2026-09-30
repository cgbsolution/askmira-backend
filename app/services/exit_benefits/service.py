"""Accrual, liability reporting and encashment persistence.

The accrual job is the piece finance cares about: it lets them see the gratuity
liability at any point rather than discovering it when someone resigns. It runs
monthly and is idempotent — running it twice produces identical rows, because
the computation is a pure function of service, wages and configuration.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.exit_benefits import (
    EncashmentPolicy,
    EncashmentRequest,
    EncashmentStatus,
    ExitReason,
    GratuityAccrual,
    GratuityConfig,
    GratuityPayout,
)
from app.models.user import User
from app.services import audit
from app.services.attendance.service import local_today
from app.services.exit_benefits.encashment import EncashmentError, compute_encashment
from app.services.exit_benefits.gratuity import GratuityError, compute_gratuity
from app.services.leave import ledger as leave_ledger
from app.services.payroll.service import active_salary_on

ZERO = Decimal(0)


async def gratuity_config(session: AsyncSession, user: User) -> GratuityConfig:
    """The tenant's gratuity configuration, or a clear refusal.

    Unapproved configuration is refused for the same reason an unapproved
    payroll rule set is: a liability figure computed against rules nobody
    signed off is a number with no authority behind it, and finance will book
    against it.
    """
    config = await session.scalar(
        tenant_scoped(select(GratuityConfig), GratuityConfig, user)
    )
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "No gratuity configuration exists for this workspace. The Compliance "
                "SME sets the eligibility period, wage basis, divisor and exemption "
                "ceiling before gratuity can be computed."
            ),
        )
    if not config.is_approved:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The gratuity configuration has not been approved by a Compliance SME. "
                "Accruals and payouts will not compute against it."
            ),
        )
    return config


async def wage_basis_for(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    on: date,
    component_codes: list[str],
) -> Decimal:
    """Monthly wages under the configured basis.

    Reads the *materialised* salary components, so a later edit to a structure
    cannot move a historical accrual.
    """
    salary = await active_salary_on(session, user, employee_id, on)
    if salary is None:
        return ZERO

    wanted = {code.upper() for code in component_codes}
    return sum(
        (
            Decimal(str(component.monthly_amount))
            for component in salary.components
            if component.component_code.upper() in wanted
        ),
        start=ZERO,
    )


# ── Accrual ─────────────────────────────────────────────────────────────────


async def accrue_gratuity(
    session: AsyncSession,
    user: User,
    *,
    as_of: date,
    employee_ids: list[uuid.UUID] | None = None,
    request: Request | None = None,
) -> list[GratuityAccrual]:
    """Recompute accrued gratuity for every active employee.

    Idempotent: one row per (employee, as-of date), updated in place. Running
    the job twice on the same date produces identical values, because the
    computation depends only on service, wages and configuration — none of
    which the job itself changes.
    """
    config = await gratuity_config(session, user)

    if employee_ids is None:
        employees = (
            await session.scalars(
                tenant_scoped(
                    select(Employee).where(
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
    else:
        employees = (
            await session.scalars(
                tenant_scoped(
                    select(Employee).where(Employee.id.in_(employee_ids)), Employee, user
                )
            )
        ).all()

    existing_rows = (
        await session.scalars(
            tenant_scoped(
                select(GratuityAccrual).where(GratuityAccrual.as_of_date == as_of),
                GratuityAccrual,
                user,
            )
        )
    ).all()
    by_employee = {row.employee_id: row for row in existing_rows}

    accruals: list[GratuityAccrual] = []
    now = datetime.now(UTC)

    for employee in employees:
        if employee.date_of_joining is None:
            continue

        wages = await wage_basis_for(
            session, user, employee.id, as_of, config.formula_basis
        )
        if wages <= ZERO:
            continue

        try:
            result = compute_gratuity(
                joined_on=employee.date_of_joining,
                exit_date=as_of,
                # An accrual asks "what if they left today", which is a
                # resignation for eligibility purposes. Death and disablement
                # waive the period, and assuming a waiver would overstate the
                # liability for everyone under five years.
                exit_reason=ExitReason.RESIGNATION,
                monthly_wages=wages,
                config=config,
                notice_period_days=employee.notice_period_days or 0,
            )
        except GratuityError:
            # A misconfigured employee must not stop the whole workforce's
            # accrual from running.
            continue

        row = by_employee.get(employee.id)
        if row is None:
            row = GratuityAccrual(
                tenant_id=user.tenant_id,
                employee_id=employee.id,
                as_of_date=as_of,
                service_days=0,
                service_years=ZERO,
                monthly_wages=ZERO,
                computed_at=now,
            )
            session.add(row)

        row.service_days = result.service.countable_days
        row.service_years = result.service.rounded_years
        row.is_eligible = result.is_eligible
        row.monthly_wages = result.monthly_wages
        row.eligible_amount = result.gross_amount
        row.working = result.as_working()
        row.computed_at = now

        accruals.append(row)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="gratuity_accrual",
        actor=user,
        changes={
            "as_of": as_of.isoformat(),
            "employees": len(accruals),
            "total_liability": str(
                sum((Decimal(str(a.eligible_amount)) for a in accruals), start=ZERO)
            ),
        },
        summary=f"Recomputed gratuity accrual for {len(accruals)} employees as of {as_of}",
        request=request,
    )
    await session.commit()
    return accruals


async def liability_report(
    session: AsyncSession, user: User, as_of: date
) -> dict[str, Any]:
    """Accrued gratuity liability, by department and by tenure band.

    Finance and auditors both ask for this, and the totals must reconcile to
    the individual accruals to the rupee — a report that does not tie out is
    worse than no report, because someone will book against it.
    """
    accruals = (
        await session.scalars(
            tenant_scoped(
                select(GratuityAccrual).where(GratuityAccrual.as_of_date == as_of),
                GratuityAccrual,
                user,
            )
        )
    ).all()

    employees = {
        employee.id: employee
        for employee in (
            await session.scalars(tenant_scoped(select(Employee), Employee, user))
        ).all()
    }

    by_department: dict[str, Decimal] = defaultdict(lambda: ZERO)
    by_tenure: dict[str, Decimal] = defaultdict(lambda: ZERO)
    department_counts: dict[str, int] = defaultdict(int)
    total = ZERO
    eligible_total = ZERO

    for accrual in accruals:
        amount = Decimal(str(accrual.eligible_amount))
        total += amount
        if accrual.is_eligible:
            eligible_total += amount

        employee = employees.get(accrual.employee_id)
        department = (
            str(employee.department_id) if employee and employee.department_id else "unassigned"
        )
        by_department[department] += amount
        department_counts[department] += 1

        by_tenure[_tenure_band(Decimal(str(accrual.service_years)))] += amount

    from app.models.org import Department

    department_names = {
        str(row.id): row.name
        for row in (
            await session.scalars(tenant_scoped(select(Department), Department, user))
        ).all()
    }

    report = {
        "as_of": as_of.isoformat(),
        "employee_count": len(accruals),
        "total_accrued_liability": str(total),
        # Split out because the two answer different questions: total is the
        # actuarial provision, eligible is what would be payable today.
        "eligible_liability": str(eligible_total),
        "not_yet_eligible_liability": str(total - eligible_total),
        "by_department": [
            {
                "department_id": department,
                "department_name": department_names.get(department, "No department"),
                "employee_count": department_counts[department],
                "accrued_liability": str(amount),
            }
            for department, amount in sorted(by_department.items())
        ],
        "by_tenure_band": [
            {"band": band, "accrued_liability": str(amount)}
            for band, amount in sorted(by_tenure.items())
        ],
    }

    # The reconciliation the checklist asks for, asserted rather than assumed.
    department_sum = sum(by_department.values(), start=ZERO)
    tenure_sum = sum(by_tenure.values(), start=ZERO)
    report["reconciles"] = department_sum == total == tenure_sum

    return report


def _tenure_band(years: Decimal) -> str:
    """Bands finance actually asks for. Presentation, not policy."""
    if years < 1:
        return "under 1 year"
    if years < 3:
        return "1-3 years"
    if years < 5:
        return "3-5 years"
    if years < 10:
        return "5-10 years"
    return "10+ years"


# ── Payout at exit ──────────────────────────────────────────────────────────


async def compute_exit_gratuity(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    exit_date: date,
    exit_reason: ExitReason,
    unpaid_leave_days: int = 0,
    request: Request | None = None,
) -> GratuityPayout:
    """Compute and record a gratuity payout at exit.

    The taxable excess is recorded on the payout so the TDS computation can
    pick it up. A gratuity paid without reaching TDS under-deducts, and the
    employee finds out at filing.
    """
    config = await gratuity_config(session, user)

    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    if employee.date_of_joining is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This employee has no date of joining, so service cannot be computed",
        )

    wages = await wage_basis_for(
        session, user, employee_id, exit_date, config.formula_basis
    )
    if wages <= ZERO:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "No salary components matching the configured gratuity wage basis "
                f"({', '.join(config.formula_basis)}) are assigned to this employee."
            ),
        )

    try:
        result = compute_gratuity(
            joined_on=employee.date_of_joining,
            exit_date=exit_date,
            exit_reason=exit_reason,
            monthly_wages=wages,
            config=config,
            unpaid_leave_days=unpaid_leave_days,
            notice_period_days=employee.notice_period_days or 0,
        )
    except GratuityError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    existing = await session.scalar(
        tenant_scoped(
            select(GratuityPayout).where(GratuityPayout.employee_id == employee_id),
            GratuityPayout,
            user,
        )
    )
    if existing is not None and existing.paid_in_run_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Gratuity has already been paid for this employee",
        )

    payout = existing or GratuityPayout(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        exit_date=exit_date,
        exit_reason=exit_reason,
        service_days=0,
        service_years=ZERO,
        monthly_wages=ZERO,
        gross_amount=ZERO,
    )
    if existing is None:
        session.add(payout)

    payout.exit_date = exit_date
    payout.exit_reason = exit_reason
    payout.service_days = result.service.countable_days
    payout.service_years = result.service.rounded_years
    payout.monthly_wages = result.monthly_wages
    payout.gross_amount = result.gross_amount
    payout.exempt_amount = result.exempt_amount
    payout.taxable_amount = result.taxable_amount
    payout.eligibility_waived = result.eligibility_waived
    payout.working = result.as_working()
    payout.config_id = config.id

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="gratuity_payout",
        entity_id=payout.id,
        actor=user,
        changes={
            "employee_code": employee.employee_code,
            "exit_reason": exit_reason.value,
            "service_years": str(result.service.rounded_years),
            "gross": str(result.gross_amount),
            "taxable": str(result.taxable_amount),
            "eligibility_waived": result.eligibility_waived,
        },
        summary=(
            f"Computed gratuity for {employee.employee_code}: {result.gross_amount} "
            f"({result.taxable_amount} taxable)"
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(payout)
    return payout


# ── Encashment ──────────────────────────────────────────────────────────────


async def available_leave_balance(
    session: AsyncSession, user: User, employee_id: uuid.UUID, leave_type_code: str, year: int
) -> Decimal:
    """The balance the leave module shows on the Time page — read, never recomputed.

    Two places computing a leave balance is two places to disagree, and the one
    that disagrees at exit is the one an employee disputes. So this asks the
    leave ledger for its live figure (accrual to date, carry-forward, HR
    adjustments, leave taken, earlier encashments) rather than summing the
    stored row, whose `accrued` catches up only when the row is next written.
    """
    return await leave_ledger.live_available(
        session, user, employee_id=employee_id, leave_type_code=leave_type_code, year=year
    )


async def request_encashment(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    leave_type_code: str,
    days: Decimal,
    financial_year: str,
    is_exit: bool = False,
    is_retirement: bool = False,
    request: Request | None = None,
) -> EncashmentRequest:
    """Raise an encashment, computed against policy and the ledger balance."""
    policy = await session.scalar(
        tenant_scoped(
            select(EncashmentPolicy).where(
                EncashmentPolicy.leave_type_code == leave_type_code,
                EncashmentPolicy.active.is_(True),
            ),
            EncashmentPolicy,
            user,
        )
    )
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No encashment policy is configured for {leave_type_code}",
        )

    # The leave year is the calendar year the balance lives in today, whatever
    # the financial year: in February the balance is next year's row, the old
    # one having been carried forward and closed on 1 January.
    year = leave_ledger.leave_year_today()
    balance = await available_leave_balance(
        session, user, employee_id, leave_type_code, year
    )

    already = await session.scalar(
        tenant_scoped(
            select(EncashmentRequest).where(
                EncashmentRequest.employee_id == employee_id,
                EncashmentRequest.leave_type_code == leave_type_code,
                EncashmentRequest.financial_year == financial_year,
                EncashmentRequest.status.in_(
                    [EncashmentStatus.APPROVED, EncashmentStatus.PAID]
                ),
            ),
            EncashmentRequest,
            user,
        )
    )
    already_days = Decimal(str(already.days_approved)) if already else ZERO

    wages = await wage_basis_for(
        session, user, employee_id, await local_today(session, user), policy.formula_basis
    )

    try:
        result = compute_encashment(
            policy=policy,
            days_requested=days,
            available_balance=balance,
            monthly_wages=wages,
            is_exit=is_exit,
            is_retirement=is_retirement,
            days_already_encashed_this_year=already_days,
        )
    except EncashmentError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    record = EncashmentRequest(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        leave_type_code=leave_type_code,
        financial_year=financial_year,
        days_requested=result.days_requested,
        days_approved=result.days_approved,
        daily_rate=result.daily_rate,
        gross_amount=result.gross_amount,
        exempt_amount=result.exempt_amount,
        taxable_amount=result.taxable_amount,
        is_exit_encashment=is_exit,
        # An exit encashment is computed by Full and Final, not requested, so
        # it needs no separate approval step.
        status=EncashmentStatus.APPROVED if is_exit else EncashmentStatus.REQUESTED,
        working=result.as_working(),
    )
    session.add(record)
    await session.flush()
    if record.status is EncashmentStatus.APPROVED:
        # The days leave the balance in the same transaction as the approval.
        await leave_ledger.post_encashment(
            session,
            user,
            employee_id=employee_id,
            leave_type_code=leave_type_code,
            year=year,
            days=result.days_approved,
            source_id=record.id,
        )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="encashment_request",
        entity_id=record.id,
        actor=user,
        changes={
            "leave_type": leave_type_code,
            "requested": str(result.days_requested),
            "approved": str(result.days_approved),
            "limiting_factor": result.limiting_factor,
            "gross": str(result.gross_amount),
        },
        summary=(
            f"Encashment {'computed at exit' if is_exit else 'requested'}: "
            f"{result.days_approved} days of {leave_type_code}"
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(record)
    return record


async def decide_encashment(
    session: AsyncSession,
    user: User,
    encashment_id: uuid.UUID,
    *,
    approve: bool,
    note: str | None = None,
    request: Request | None = None,
) -> EncashmentRequest:
    record = await session.scalar(
        tenant_scoped(
            select(EncashmentRequest).where(EncashmentRequest.id == encashment_id),
            EncashmentRequest,
            user,
        )
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Encashment request not found"
        )
    if record.status is not EncashmentStatus.REQUESTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This request was already {record.status.value}",
        )
    # HR for anyone, a manager for their reporting line, nobody for themselves.
    await permissions.require_manager_or_hr(
        session,
        user,
        record.employee_id,
        detail="You can only decide encashment for somebody in your reporting line.",
    )

    if approve:
        # Debited now, against the live balance: a leave approved since the
        # request was raised may have spent the same days, which refuses it.
        await leave_ledger.post_encashment(
            session,
            user,
            employee_id=record.employee_id,
            leave_type_code=record.leave_type_code,
            year=leave_ledger.leave_year_today(),
            days=Decimal(str(record.days_approved)),
            source_id=record.id,
        )
    record.status = EncashmentStatus.APPROVED if approve else EncashmentStatus.REJECTED
    record.approver_user_id = user.id
    record.decided_at = datetime.now(UTC)
    record.decision_note = note

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="encashment_request",
        entity_id=record.id,
        actor=user,
        changes={"days": str(record.days_approved), "note": note},
        summary=f"Encashment {'approved' if approve else 'rejected'}",
        request=request,
    )
    await session.commit()
    await session.refresh(record)
    return record


# ═══════════════════════════════════════════════════════════════════════════
# For the scheduler, and read models with names
# ═══════════════════════════════════════════════════════════════════════════


async def accrue_gratuity_for_tenant(
    session: AsyncSession, tenant_id: uuid.UUID, *, as_of: date
) -> int:
    """The monthly accrual job, for a scheduler with no signed-in user. Commits.

    Idempotent for a date, like the manual run. Returns how many employees were
    accrued. Refuses (HTTP 409) if the tenant has no approved configuration.
    """
    from app.services.filings import support

    rows = await accrue_gratuity(session, support.system_actor(tenant_id), as_of=as_of)
    return len(rows)


async def accrual_dates(session: AsyncSession, user: User) -> list[date]:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(GratuityAccrual.as_of_date)
                .distinct()
                .order_by(GratuityAccrual.as_of_date.desc()),
                GratuityAccrual,
                user,
            )
        )
    ).all()
    return list(rows)


async def employee_details(
    session: AsyncSession, user: User, ids: set[uuid.UUID]
) -> dict[uuid.UUID, Employee]:
    if not ids:
        return {}
    rows = (
        await session.scalars(
            tenant_scoped(select(Employee).where(Employee.id.in_(ids)), Employee, user)
        )
    ).all()
    return {row.id: row for row in rows}


async def list_encashments(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID | None,
    pending_only: bool,
) -> list[EncashmentRequest]:
    """Encashments: everybody's for HR, a manager's reporting line, or your own."""
    stmt = select(EncashmentRequest).order_by(EncashmentRequest.created_at.desc())
    if employee_id is not None:
        target = permissions.resolve_employee(
            user, employee_id, detail="You can only see your own encashment requests"
        )
        stmt = stmt.where(EncashmentRequest.employee_id == target)
    else:
        visible = await permissions.visible_employee_ids(session, user)
        if visible is not None:
            stmt = stmt.where(EncashmentRequest.employee_id.in_(visible or {uuid.uuid4()}))
    if pending_only:
        stmt = stmt.where(EncashmentRequest.status == EncashmentStatus.REQUESTED)
    return list((await session.scalars(tenant_scoped(stmt, EncashmentRequest, user))).all())
