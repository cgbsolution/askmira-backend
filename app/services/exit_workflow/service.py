"""Exit lifecycle: clearance gating, settlement, holds, and access revocation.

Three refusals live here rather than in a router, because each is a correctness
condition rather than input validation:

1. **Settlement is blocked while a mandatory clearance is outstanding.** The
   recovery amounts come from clearance items, so settling first means settling
   without knowing what is owed.
2. **A waiver needs a reason.** Waiving a mandatory clearance silently is how an
   unreturned laptop becomes nobody's problem.
3. **Exit deactivates the user and deletes nothing.** See the retention note in
   the model module — this is the slice 26 intersection.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.exit_workflow import (
    ClearanceItem,
    ClearanceStatus,
    ClearanceTemplate,
    ExitRecord,
    ExitStatus,
    ExitType,
    Settlement,
    SettlementLine,
    SettlementStatus,
)
from app.models.user import User
from app.services import audit
from app.services.assets import exit_hooks as asset_exit_hooks
from app.services.exit_workflow import settlement as settlement_engine

__all__ = [
    "clear_item",
    "compute_settlement",
    "hold_settlement",
    "initiate_exit",
    "notice_shortfall",
    "open_exit",
    "revoke_access",
    "status_for_shortfall",
]

ZERO = Decimal(0)


async def _exit_record(
    session: AsyncSession, user: User, exit_id: uuid.UUID
) -> ExitRecord:
    record = await session.scalar(
        tenant_scoped(select(ExitRecord).where(ExitRecord.id == exit_id), ExitRecord, user)
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exit record not found")
    return record


# ═══════════════════════════════════════════════════════════════════════════
# Initiating an exit
# ═══════════════════════════════════════════════════════════════════════════


async def initiate_exit(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    exit_type: ExitType,
    resignation_date: date,
    requested_last_working_day: date | None = None,
    approved_last_working_day: date | None = None,
    notice_period_days: int | None = None,
    reason: str | None = None,
    request: Request | None = None,
) -> ExitRecord:
    """Open an exit and instantiate the tenant's clearance checklist.

    The notice shortfall is computed here rather than at settlement, so the
    employee can see it during their notice period instead of discovering it on
    their final statement.
    """
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    # HR for anyone; a manager for their reporting line; nobody for themselves.
    await permissions.require_manager_or_hr(
        session,
        user,
        employee_id,
        detail="You can only start an exit for somebody in your reporting line.",
    )
    record = await open_exit(
        session,
        user,
        employee,
        exit_type=exit_type,
        resignation_date=resignation_date,
        requested_last_working_day=requested_last_working_day,
        approved_last_working_day=approved_last_working_day,
        notice_period_days=notice_period_days,
        reason=reason,
        request=request,
    )
    # Opened by HR or the line manager, so accepted as it is opened. Only an
    # exit the employee submitted waits for somebody to accept it.
    record.accepted_at = datetime.now(UTC)
    record.accepted_by_user_id = user.id
    await session.flush()
    return record


def notice_shortfall(notice_days: int, resignation_date: date, last_day: date | None) -> int:
    """Days of notice not served, if the exit ends on `last_day`."""
    if last_day is None or not notice_days:
        return 0
    return max(0, notice_days - (last_day - resignation_date).days)


def status_for_shortfall(shortfall: int) -> ExitStatus:
    """Serving notice when the notice is served in full; otherwise initiated,
    where the shortfall is waived or recovered before anything else moves."""
    return ExitStatus.NOTICE_PERIOD if shortfall == 0 else ExitStatus.INITIATED


async def open_exit(
    session: AsyncSession,
    user: User,
    employee: Employee,
    *,
    exit_type: ExitType,
    resignation_date: date,
    requested_last_working_day: date | None = None,
    approved_last_working_day: date | None = None,
    notice_period_days: int | None = None,
    reason: str | None = None,
    initiated_by_employee: bool = False,
    employee_comments: str | None = None,
    request: Request | None = None,
) -> ExitRecord:
    """The exit itself, once the caller is known to be allowed to open it.

    Shared by HR's path (`initiate_exit`) and the employee's own resignation
    (`app.services.exit_workflow.resignation`), so the duplicate check, the
    notice arithmetic, the clearance checklist and the asset hook are one
    implementation. Does not commit.
    """
    employee_id = employee.id

    existing = await session.scalar(
        tenant_scoped(
            select(ExitRecord).where(
                ExitRecord.employee_id == employee_id,
                ExitRecord.status.notin_([ExitStatus.CANCELLED]),
            ),
            ExitRecord,
            user,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{employee.employee_code} already has an open exit from "
                f"{existing.resignation_date}. Cancel it before opening another."
            ),
        )

    notice_days = (
        notice_period_days
        if notice_period_days is not None
        else (employee.notice_period_days or 0)
    )

    last_day = approved_last_working_day or requested_last_working_day
    shortfall = notice_shortfall(notice_days, resignation_date, last_day)

    record = ExitRecord(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        exit_type=exit_type,
        # A resignation the employee submitted waits in `initiated` for somebody
        # to accept it, whatever its notice arithmetic says.
        status=(
            ExitStatus.INITIATED if initiated_by_employee else status_for_shortfall(shortfall)
        ),
        resignation_date=resignation_date,
        requested_last_working_day=requested_last_working_day,
        approved_last_working_day=approved_last_working_day,
        notice_period_days=notice_days,
        notice_shortfall_days=shortfall,
        reason=reason,
        initiated_by_employee=initiated_by_employee,
        employee_comments=employee_comments,
    )
    session.add(record)
    await session.flush()

    # The tenant's checklist, instantiated. A company with no library does not
    # get a library clearance item.
    templates = (
        await session.scalars(
            tenant_scoped(
                select(ClearanceTemplate)
                .where(ClearanceTemplate.active.is_(True))
                .order_by(ClearanceTemplate.display_order),
                ClearanceTemplate,
                user,
            )
        )
    ).all()
    for template in templates:
        session.add(
            ClearanceItem(
                tenant_id=user.tenant_id,
                exit_record_id=record.id,
                department=template.department,
                description=template.description,
                is_mandatory=template.is_mandatory,
                owner_role=template.owner_role,
                owner_user_id=template.owner_user_id,
            )
        )
    # Every asset the leaver still holds becomes a "return it" item.
    await asset_exit_hooks.add_exit_clearance(session, user, record, request=request)

    employee.status = EmploymentStatus.ON_NOTICE
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="exit_record",
        entity_id=record.id,
        summary=(
            f"{'Resignation submitted by' if initiated_by_employee else 'Opened a'} "
            f"{'' if initiated_by_employee else exit_type.value + ' exit for '}"
            f"{employee.employee_code} from "
            f"{resignation_date}"
            f"{f', {shortfall} day(s) of notice short' if shortfall else ''}"
        ),
        changes={
            "exit_type": exit_type.value,
            "notice_period_days": notice_days,
            "notice_shortfall_days": shortfall,
            "clearance_items": len(templates),
        },
        request=request,
    )
    return record


# ═══════════════════════════════════════════════════════════════════════════
# Clearance
# ═══════════════════════════════════════════════════════════════════════════


async def clear_item(
    session: AsyncSession,
    user: User,
    *,
    item_id: uuid.UUID,
    outcome: ClearanceStatus,
    recovery_amount: Decimal = ZERO,
    waiver_reason: str | None = None,
    notes: str | None = None,
    request: Request | None = None,
) -> ClearanceItem:
    """Sign off one department's clearance, or name what is owed."""
    item = await session.scalar(
        tenant_scoped(
            select(ClearanceItem).where(ClearanceItem.id == item_id), ClearanceItem, user
        )
    )
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Clearance item not found"
        )
    # The item's owner, the leaver's line manager for a manager item, or HR —
    # and never on your own exit.
    from app.services.exit_workflow.operations import require_item_permission

    await require_item_permission(session, user, item)
    if outcome is ClearanceStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Signing an item off needs an outcome other than 'pending'.",
        )
    # An asset item marked not returned recovers its book value unless a figure was typed.
    recovery_amount = await asset_exit_hooks.recovery_for_clearance(
        session, user, item, outcome, recovery_amount
    )

    if outcome is ClearanceStatus.WAIVED and not (waiver_reason or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Waiving a clearance requires a reason. Waiving one silently is how an "
                "unreturned asset becomes nobody's problem."
            ),
        )
    if outcome is ClearanceStatus.CLEARED_WITH_RECOVERY and recovery_amount <= ZERO:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "A clearance with recovery needs an amount. If nothing is owed, clear it "
                "outright."
            ),
        )

    item.status = outcome
    item.recovery_amount = recovery_amount
    item.waiver_reason = waiver_reason
    item.notes = notes
    item.cleared_by_user_id = user.id
    item.cleared_at = datetime.now(UTC)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="clearance_item",
        entity_id=item.id,
        summary=(
            f"{item.department} clearance '{item.description}' marked "
            f"{outcome.value}"
            f"{f' with {recovery_amount} to recover' if recovery_amount else ''}"
        ),
        changes={
            "status": outcome.value,
            "recovery_amount": str(recovery_amount),
            "waiver_reason": waiver_reason,
        },
        request=request,
    )
    # An asset item closes the assignment it stands for in the asset register.
    await asset_exit_hooks.on_clearance_signed(session, user, item, request=request)
    return item


async def outstanding_clearances(
    session: AsyncSession, user: User, exit_id: uuid.UUID
) -> list[ClearanceItem]:
    """Mandatory items still pending. Empty means settlement can proceed."""
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(ClearanceItem).where(
                        ClearanceItem.exit_record_id == exit_id,
                        ClearanceItem.is_mandatory.is_(True),
                        ClearanceItem.status == ClearanceStatus.PENDING,
                    ),
                    ClearanceItem,
                    user,
                )
            )
        ).all()
    )


# ═══════════════════════════════════════════════════════════════════════════
# Settlement
# ═══════════════════════════════════════════════════════════════════════════


async def release_settled_claims(
    session: AsyncSession, user: User, settlement_id: uuid.UUID
) -> None:
    """Undo what approving a settlement did to its claims, before it is recomputed."""
    from app.models.expenses import ExpenseClaim, ExpenseClaimStatusEnum
    from app.models.loans import ReimbursementClaim

    for claim in (
        await session.scalars(
            tenant_scoped(
                select(ReimbursementClaim).where(
                    ReimbursementClaim.settled_in_settlement_id == settlement_id
                ),
                ReimbursementClaim,
                user,
            )
        )
    ).all():
        claim.settled_in_settlement_id = None
    for claim in (
        await session.scalars(
            tenant_scoped(
                select(ExpenseClaim).where(ExpenseClaim.settled_in_settlement_id == settlement_id),
                ExpenseClaim,
                user,
            )
        )
    ).all():
        claim.settled_in_settlement_id = None
        claim.status = ExpenseClaimStatusEnum.APPROVED
        claim.reimbursed_at = None


async def compute_settlement(
    session: AsyncSession,
    user: User,
    *,
    exit_id: uuid.UUID,
    inputs: settlement_engine.SettlementInput,
    request: Request | None = None,
) -> Settlement:
    """Compute the settlement, blocked until clearance is done.

    The recovery amounts come from clearance items, so settling before
    clearance is settling without knowing what is owed. The asset recovery is
    read from the cleared items rather than passed in, precisely so the two
    cannot disagree.
    """
    record = await _exit_record(session, user, exit_id)
    if user.employee_id is not None and record.employee_id == user.employee_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This is your own exit. Somebody else computes its settlement.",
        )

    outstanding = await outstanding_clearances(session, user, exit_id)
    if outstanding:
        names = ", ".join(f"{i.department}: {i.description}" for i in outstanding[:3])
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{len(outstanding)} mandatory clearance item(s) are still pending "
                f"({names}{'…' if len(outstanding) > 3 else ''}). The settlement's "
                "recoveries come from these, so computing it now would settle without "
                "knowing what is owed."
            ),
        )

    # Recovery flows from clearance rather than being typed in twice.
    cleared = (
        await session.scalars(
            tenant_scoped(
                select(ClearanceItem).where(ClearanceItem.exit_record_id == exit_id),
                ClearanceItem,
                user,
            )
        )
    ).all()
    asset_recovery = sum(
        (Decimal(str(item.recovery_amount)) for item in cleared), start=ZERO
    )
    recovery_sources = [
        f"{item.department}: {item.description} ({item.recovery_amount})"
        for item in cleared
        if Decimal(str(item.recovery_amount)) > ZERO
    ]

    from dataclasses import replace

    if asset_recovery > ZERO:
        inputs = replace(
            inputs,
            asset_recovery=asset_recovery,
            asset_basis="; ".join(recovery_sources),
        )

    # Notice shortfall, where it has not already been supplied.
    if record.notice_shortfall_days and inputs.notice_shortfall == ZERO:
        if record.notice_waived:
            inputs = replace(
                inputs,
                notice_basis=(
                    f"{record.notice_shortfall_days} day(s) short, waived: "
                    f"{record.notice_waiver_reason or 'no reason recorded'}."
                ),
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"This exit is {record.notice_shortfall_days} day(s) short of notice "
                    "and no recovery figure was supplied. The wage basis for notice "
                    "recovery is a contractual term, so it is not assumed — supply the "
                    "amount, or waive the shortfall with a reason."
                ),
            )

    try:
        result = settlement_engine.compute_settlement(inputs)
    except settlement_engine.SettlementError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    existing = await session.scalar(
        tenant_scoped(
            select(Settlement).where(Settlement.exit_record_id == exit_id), Settlement, user
        )
    )
    if existing is not None:
        if existing.status in (SettlementStatus.PAID, SettlementStatus.DEMAND_RAISED):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"This settlement is already '{existing.status.value}'. A correction "
                    "is a fresh computation on a new exit record, not an edit of a "
                    "settled one."
                ),
            )
        # Queried rather than read off `existing.lines`: that relationship is
        # lazily loaded, and touching it here is a synchronous load inside an
        # async function, which raises MissingGreenlet.
        previous = (
            await session.scalars(
                tenant_scoped(
                    select(SettlementLine).where(
                        SettlementLine.settlement_id == existing.id
                    ),
                    SettlementLine,
                    user,
                )
            )
        ).all()
        for line in previous:
            await session.delete(line)
        await release_settled_claims(session, user, existing.id)
        await session.flush()
        settlement = existing
    else:
        settlement = Settlement(
            tenant_id=user.tenant_id,
            exit_record_id=exit_id,
            employee_id=record.employee_id,
        )
        session.add(settlement)
        await session.flush()

    settlement.status = SettlementStatus.DRAFT
    settlement.total_earnings = result.total_earnings
    settlement.total_recoveries = result.total_recoveries
    settlement.total_tax = result.total_tax
    settlement.net_amount = result.net_amount
    settlement.tax_working = inputs.tax_working
    settlement.computed_at = datetime.now(UTC)

    for line in result.lines:
        session.add(
            SettlementLine(
                tenant_id=user.tenant_id,
                settlement_id=settlement.id,
                line_type=line.line_type,
                code=line.code,
                label=line.label,
                amount=line.amount,
                basis=line.basis,
                source=line.source,
                display_order=line.display_order,
            )
        )

    record.status = ExitStatus.SETTLEMENT_PENDING
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="settlement",
        entity_id=settlement.id,
        summary=(
            f"Computed the settlement: {result.total_earnings} earnings less "
            f"{result.total_recoveries} recoveries and {result.total_tax} tax = "
            f"{result.net_amount}"
            f"{' (a demand on the employee)' if result.is_demand else ''}"
        ),
        changes={
            "net_amount": str(result.net_amount),
            "is_demand": result.is_demand,
            "line_count": len(result.lines),
        },
        request=request,
    )
    return settlement


async def hold_settlement(
    session: AsyncSession,
    user: User,
    *,
    settlement_id: uuid.UUID,
    reason: str,
    request: Request | None = None,
) -> Settlement:
    """Compute but do not pay, with the reason recorded.

    "Why has my settlement not been paid" is the question that follows, and a
    hold with no reason cannot answer it.
    """
    if not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A hold requires a reason. The employee will ask.",
        )

    settlement = await session.scalar(
        tenant_scoped(
            select(Settlement).where(Settlement.id == settlement_id), Settlement, user
        )
    )
    if settlement is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Settlement not found")
    if settlement.status is SettlementStatus.PAID:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This settlement has been paid. A hold after payment is not a hold.",
        )
    if user.employee_id is not None and settlement.employee_id == user.employee_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This is your own settlement. Somebody else decides a hold on it.",
        )

    settlement.status = SettlementStatus.ON_HOLD
    settlement.hold_reason = reason
    settlement.held_by_user_id = user.id
    settlement.held_at = datetime.now(UTC)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="settlement",
        entity_id=settlement.id,
        summary=f"Settlement placed on hold: {reason}",
        changes={"hold_reason": reason},
        request=request,
    )
    return settlement


# ═══════════════════════════════════════════════════════════════════════════
# Access revocation — and what is deliberately kept
# ═══════════════════════════════════════════════════════════════════════════


async def revoke_access(
    session: AsyncSession,
    user: User,
    *,
    exit_id: uuid.UUID,
    request: Request | None = None,
) -> ExitRecord:
    """Deactivate the user's login. Delete nothing.

    ┌───────────────────────────────────────────────────────────────────────┐
    │ INTERSECTION WITH SLICE 26 (data retention & deletion policies)       │
    │                                                                       │
    │ This is where the retention clock starts. It is not where deletion    │
    │ happens, and nothing here removes an Employee, a Payslip, an          │
    │ AttendanceDay or an audit row.                                        │
    │                                                                       │
    │ Statutory retention runs to years after employment ends: a filed      │
    │ return has to remain explainable, and a payslip the ex-employee still │
    │ has must still reconcile to something. Slice 26 owns the schedule and │
    │ the erasure; anything that deletes here is a bug in both slices.      │
    └───────────────────────────────────────────────────────────────────────┘
    """
    record = await _exit_record(session, user, exit_id)
    if user.employee_id is not None and record.employee_id == user.employee_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot revoke your own access. Somebody else does that.",
        )
    employee = await session.scalar(
        tenant_scoped(
            select(Employee).where(Employee.id == record.employee_id), Employee, user
        )
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    # The login goes. The record stays. The link runs User -> Employee, and
    # not every employee has a login (a factory worker with no email), so this
    # is a query rather than an attribute walk.
    accounts = (
        await session.scalars(
            tenant_scoped(
                select(User).where(User.employee_id == employee.id), User, user
            )
        )
    ).all()
    for account in accounts:
        account.is_active = False

    employee.status = EmploymentStatus.EXITED
    if record.approved_last_working_day is not None:
        employee.date_of_exit = record.approved_last_working_day
    else:
        # The workspace's date, not the server's UTC one.
        from app.services.attendance.service import local_today

        employee.date_of_exit = await local_today(session, user)
    employee.is_active = False

    record.access_revoked_at = datetime.now(UTC)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="exit_record",
        entity_id=record.id,
        summary=(
            f"Revoked access for {employee.employee_code}. Employment record, payroll "
            "history, attendance and audit trail retained per the statutory retention "
            "period."
        ),
        changes={
            "logins_deactivated": len(accounts),
            "date_of_exit": str(employee.date_of_exit),
            "records_deleted": 0,
        },
        request=request,
    )
    return record


def month_days(on: date) -> int:
    return calendar.monthrange(on.year, on.month)[1]
