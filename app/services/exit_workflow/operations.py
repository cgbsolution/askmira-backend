"""Exit operations around the core workflow in `service.py` (slice 13).

Who sees an exit and who signs a clearance item off; the figures other modules
already hold for a leaver (to prefill the settlement); holds and approval; the
settlement statement issued through the letter engine; and the gate that keeps
relieving and experience letters back while a settlement is on hold.

For the letter engine (workstream W1): `letter_gate(session, user, employee_id)`
is the public check a relieving or experience letter should pass before issue.
"""

from __future__ import annotations

import calendar
import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.documents import IssuedLetter, LetterTemplate
from app.models.employee import Employee
from app.models.exit_benefits import EncashmentRequest, EncashmentStatus, GratuityPayout
from app.models.exit_workflow import (
    ClearanceItem,
    ClearanceOwnerRole,
    ClearanceStatus,
    ClearanceTemplate,
    ExitRecord,
    ExitStatus,
    Settlement,
    SettlementLine,
    SettlementStatus,
)
from app.models.expenses import (
    AdvanceStatusEnum,
    ExpenseClaim,
    ExpenseClaimStatusEnum,
    TravelAdvance,
)
from app.models.loans import ClaimStatus, LoanAccount, LoanStatus, ReimbursementClaim
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.arrears import service as arrears_service
from app.services.documents import service as documents_service
from app.services.documents.indian_numbers import group_indian
from app.services.exit_workflow import statement
from app.services.filings import support
from app.services.notifications.dispatch import notify, register_event

ZERO = Decimal(0)

LETTER_TEMPLATE_CODES = {"relieving": "RELIEVING_STD", "experience": "EXPERIENCE_STD"}
SETTLED_STATUSES = {
    SettlementStatus.APPROVED,
    SettlementStatus.PAID,
    SettlementStatus.DEMAND_RAISED,
}


def _amount(value: object) -> Decimal:
    return Decimal(str(value or ZERO))


async def _labels(
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


async def _user_names(
    session: AsyncSession, user: User, ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(select(User.id, User.full_name).where(User.id.in_(ids)), User, user)
        )
    ).all()
    return {row_id: name for row_id, name in rows}


# ═══════════════════════════════════════════════════════════════════════════
# Visibility and clearance ownership
# ═══════════════════════════════════════════════════════════════════════════


async def _record(session: AsyncSession, user: User, exit_id: uuid.UUID) -> ExitRecord:
    record = await session.scalar(
        tenant_scoped(select(ExitRecord).where(ExitRecord.id == exit_id), ExitRecord, user)
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exit record not found")
    return record


async def _owns_an_item(session: AsyncSession, user: User, exit_id: uuid.UUID) -> bool:
    return (
        await session.scalar(
            tenant_scoped(
                select(ClearanceItem.id).where(
                    ClearanceItem.exit_record_id == exit_id,
                    ClearanceItem.owner_user_id == user.id,
                ),
                ClearanceItem,
                user,
            )
        )
    ) is not None


async def visible_exit(session: AsyncSession, user: User, exit_id: uuid.UUID) -> ExitRecord:
    """An exit the caller may see: HR, the leaver's line, or a named item owner.

    Anyone else is told it does not exist.
    """
    record = await _record(session, user, exit_id)
    visible = await permissions.visible_employee_ids(session, user)
    if visible is None or record.employee_id in visible:
        return record
    if await _owns_an_item(session, user, exit_id):
        return record
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exit record not found")


async def can_update_item(
    session: AsyncSession, user: User, item: ClearanceItem, record: ExitRecord
) -> bool:
    """Whether the caller may sign this clearance item off.

    Never on your own exit. HR always; otherwise the named owner, or the line
    manager for a reporting-manager item.
    """
    if user.employee_id is not None and record.employee_id == user.employee_id:
        return False
    if item.status is not ClearanceStatus.PENDING and not permissions.is_hr(user):
        return False
    if permissions.is_hr(user):
        return True
    if item.owner_role == ClearanceOwnerRole.NAMED_USER.value:
        return item.owner_user_id == user.id
    if item.owner_role == ClearanceOwnerRole.REPORTING_MANAGER.value:
        return await permissions.is_line_manager_of(session, user, record.employee_id)
    return False


async def require_item_permission(
    session: AsyncSession, user: User, item: ClearanceItem
) -> ExitRecord:
    record = await _record(session, user, item.exit_record_id)
    if user.employee_id is not None and record.employee_id == user.employee_id:
        raise permissions.forbidden(
            "This is your own exit. Somebody else signs its clearance off."
        )
    if not await can_update_item(session, user, item, record):
        raise permissions.forbidden(
            f"The {item.department} item '{item.description}' is signed off by its owner or HR."
        )
    return record


# ═══════════════════════════════════════════════════════════════════════════
# Exit lists
# ═══════════════════════════════════════════════════════════════════════════


async def list_exits(
    session: AsyncSession, user: User, *, exit_status: ExitStatus | None = None
) -> list[dict[str, Any]]:
    """Exits the caller can see, with progress and settlement state."""
    stmt = select(ExitRecord).order_by(ExitRecord.resignation_date.desc())
    if exit_status is not None:
        stmt = stmt.where(ExitRecord.status == exit_status)
    visible = await permissions.visible_employee_ids(session, user)
    if visible is not None:
        owned = set(
            (
                await session.scalars(
                    tenant_scoped(
                        select(ClearanceItem.exit_record_id).where(
                            ClearanceItem.owner_user_id == user.id
                        ),
                        ClearanceItem,
                        user,
                    )
                )
            ).all()
        )
        stmt = stmt.where(
            ExitRecord.employee_id.in_(visible or {uuid.uuid4()})
            | ExitRecord.id.in_(owned or {uuid.uuid4()})
        )
    records = (await session.scalars(tenant_scoped(stmt, ExitRecord, user))).all()
    return await _enrich(session, user, list(records))


async def exit_read(session: AsyncSession, user: User, record: ExitRecord) -> dict[str, Any]:
    return (await _enrich(session, user, [record]))[0]


async def _enrich(
    session: AsyncSession, user: User, records: list[ExitRecord]
) -> list[dict[str, Any]]:
    if not records:
        return []
    ids = [record.id for record in records]
    labels = await _labels(session, user, {record.employee_id for record in records})
    acceptors = await _user_names(
        session, user, {r.accepted_by_user_id for r in records if r.accepted_by_user_id}
    )
    items = (
        await session.scalars(
            tenant_scoped(
                select(ClearanceItem).where(ClearanceItem.exit_record_id.in_(ids)),
                ClearanceItem,
                user,
            )
        )
    ).all()
    settlements = {
        row.exit_record_id: row
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(Settlement).where(Settlement.exit_record_id.in_(ids)),
                    Settlement,
                    user,
                )
            )
        ).all()
    }
    from app.core import permissions
    from app.models.exit_workflow import ExitInterview

    hr = permissions.is_hr(user)
    interviewed = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(ExitInterview.exit_id).where(ExitInterview.exit_id.in_(ids)),
                    ExitInterview,
                    user,
                )
            )
        ).all()
    )
    out = []
    for record in records:
        mine = [item for item in items if item.exit_record_id == record.id]
        settlement = settlements.get(record.id)
        code, name = labels.get(record.employee_id, (None, None))
        out.append(
            {
                "id": record.id,
                "employee_id": record.employee_id,
                "employee_code": code,
                "employee_name": name,
                "exit_type": record.exit_type,
                "status": record.status,
                "resignation_date": record.resignation_date,
                "requested_last_working_day": record.requested_last_working_day,
                "approved_last_working_day": record.approved_last_working_day,
                "notice_period_days": record.notice_period_days,
                "notice_shortfall_days": record.notice_shortfall_days,
                "notice_waived": record.notice_waived,
                "notice_waiver_reason": record.notice_waiver_reason,
                "reason": record.reason,
                "access_revoked_at": record.access_revoked_at,
                "clearance_total": len(mine),
                "clearance_pending_mandatory": sum(
                    1
                    for item in mine
                    if item.is_mandatory and item.status is ClearanceStatus.PENDING
                ),
                "settlement_id": settlement.id if settlement else None,
                "settlement_status": settlement.status if settlement else None,
                "created_at": record.created_at,
                "initiated_by_employee": record.initiated_by_employee,
                "employee_comments": record.employee_comments,
                "accepted_at": record.accepted_at,
                "accepted_by_name": acceptors.get(record.accepted_by_user_id)
                if record.accepted_by_user_id
                else None,
                "withdrawn_at": record.withdrawn_at,
                "awaiting_acceptance": (
                    record.accepted_at is None and record.status is not ExitStatus.CANCELLED
                ),
                "is_rehire_eligible": record.is_rehire_eligible if hr else None,
                "rehire_note": record.rehire_note if hr else None,
                "has_interview": record.id in interviewed,
            }
        )
    return out


async def cancel_exit(
    session: AsyncSession,
    user: User,
    exit_id: uuid.UUID,
    *,
    reason: str,
    request: Request | None = None,
) -> ExitRecord:
    """Withdraw an exit (a retracted resignation). Nothing is deleted."""
    record = await _record(session, user, exit_id)
    if user.employee_id is not None and record.employee_id == user.employee_id:
        raise permissions.forbidden("This is your own exit. Somebody else has to cancel it.")
    return await cancel_record(
        session, user, record, reason=reason, summary=f"Cancelled the exit: {reason}",
        request=request,
    )


async def cancel_record(
    session: AsyncSession,
    user: User,
    record: ExitRecord,
    *,
    reason: str,
    summary: str,
    request: Request | None = None,
) -> ExitRecord:
    """The cancellation itself, once the caller is known to be allowed to.

    Shared by HR's cancel and the employee's own withdrawal of a resignation
    nobody has accepted yet, so the settled-or-revoked refusals and the return
    to active status are one implementation.
    """
    exit_id = record.id
    if record.status in (ExitStatus.SETTLED, ExitStatus.CANCELLED) or record.access_revoked_at:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "An exit that is settled, cancelled or has had access revoked cannot be "
                "cancelled."
            ),
        )
    settlement = await _settlement_for(session, user, exit_id)
    if settlement is not None and settlement.status in SETTLED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The settlement for this exit has been approved; it cannot be cancelled.",
        )
    record.status = ExitStatus.CANCELLED
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == record.employee_id), Employee, user)
    )
    if employee is not None and employee.status.value == "on_notice":
        from app.models.employee import EmploymentStatus

        employee.status = EmploymentStatus.ACTIVE
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="exit_record",
        entity_id=record.id,
        summary=summary,
        changes={"status": ExitStatus.CANCELLED.value, "reason": reason},
        request=request,
    )
    return record


async def waive_notice(
    session: AsyncSession,
    user: User,
    exit_id: uuid.UUID,
    *,
    reason: str,
    request: Request | None = None,
) -> ExitRecord:
    """Waive a notice shortfall, with a reason. Waiving recovery gives money away."""
    record = await _record(session, user, exit_id)
    if user.employee_id is not None and record.employee_id == user.employee_id:
        raise permissions.forbidden("This is your own exit. Somebody else has to waive it.")
    if record.notice_shortfall_days <= 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This exit has no notice shortfall to waive.",
        )
    record.notice_waived = True
    record.notice_waived_by_user_id = user.id
    record.notice_waiver_reason = reason
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE,
        entity_type="exit_record",
        entity_id=record.id,
        summary=(
            f"Waived {record.notice_shortfall_days} day(s) of notice shortfall: {reason}"
        ),
        changes={"notice_waiver_reason": reason},
        request=request,
    )
    return record


# ═══════════════════════════════════════════════════════════════════════════
# Clearance lists and templates
# ═══════════════════════════════════════════════════════════════════════════


async def list_clearance(
    session: AsyncSession, user: User, exit_id: uuid.UUID
) -> list[dict[str, Any]]:
    record = await visible_exit(session, user, exit_id)
    items = (
        await session.scalars(
            tenant_scoped(
                select(ClearanceItem)
                .where(ClearanceItem.exit_record_id == exit_id)
                .order_by(ClearanceItem.department, ClearanceItem.created_at),
                ClearanceItem,
                user,
            )
        )
    ).all()
    names = await _user_names(
        session,
        user,
        {uid for item in items for uid in (item.owner_user_id, item.cleared_by_user_id) if uid},
    )
    out = []
    for item in items:
        out.append(
            {
                "id": item.id,
                "exit_record_id": item.exit_record_id,
                "department": item.department,
                "description": item.description,
                "is_mandatory": item.is_mandatory,
                "status": item.status,
                "recovery_amount": _amount(item.recovery_amount),
                "cleared_at": item.cleared_at,
                "cleared_by_name": names.get(item.cleared_by_user_id)
                if item.cleared_by_user_id
                else None,
                "waiver_reason": item.waiver_reason,
                "notes": item.notes,
                "owner_role": item.owner_role,
                "owner_user_id": item.owner_user_id,
                "owner_name": names.get(item.owner_user_id) if item.owner_user_id else None,
                # An asset item recovers the asset's book value when marked not
                # returned with no figure typed; the screen says so.
                "asset_assignment_id": item.asset_assignment_id,
                "can_update": await can_update_item(session, user, item, record),
            }
        )
    return out


async def list_templates(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(ClearanceTemplate).order_by(
                    ClearanceTemplate.display_order, ClearanceTemplate.department
                ),
                ClearanceTemplate,
                user,
            )
        )
    ).all()
    owners = {row.owner_user_id for row in rows if row.owner_user_id}
    names = await _user_names(session, user, owners)
    return [
        {
            "id": row.id,
            "department": row.department,
            "description": row.description,
            "is_mandatory": row.is_mandatory,
            "display_order": row.display_order,
            "active": row.active,
            "owner_role": row.owner_role,
            "owner_user_id": row.owner_user_id,
            "owner_name": names.get(row.owner_user_id) if row.owner_user_id else None,
        }
        for row in rows
    ]


async def create_template(
    session: AsyncSession,
    user: User,
    *,
    department: str,
    description: str,
    is_mandatory: bool,
    display_order: int,
    owner_role: ClearanceOwnerRole,
    owner_user_id: uuid.UUID | None,
    request: Request | None = None,
) -> ClearanceTemplate:
    if owner_role is ClearanceOwnerRole.NAMED_USER:
        if owner_user_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A named owner needs a person chosen.",
            )
        owner = await session.scalar(
            tenant_scoped(
                select(User).where(
                    User.id == owner_user_id,
                    User.is_active.is_(True),
                    permissions.manager_or_hr_login(user),
                ),
                User,
                user,
            )
        )
        if owner is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="The owner must be an active HR or manager login in this workspace.",
            )
    else:
        owner_user_id = None

    duplicate = await session.scalar(
        tenant_scoped(
            select(ClearanceTemplate.id).where(
                ClearanceTemplate.department == department,
                ClearanceTemplate.description == description,
            ),
            ClearanceTemplate,
            user,
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This checklist item already exists for that department.",
        )
    template = ClearanceTemplate(
        tenant_id=user.tenant_id,
        department=department,
        description=description,
        is_mandatory=is_mandatory,
        display_order=display_order,
        owner_role=owner_role.value,
        owner_user_id=owner_user_id,
    )
    session.add(template)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="clearance_template",
        entity_id=template.id,
        summary=f"Added the {department} clearance item '{description}'",
        changes={"owner_role": owner_role.value, "is_mandatory": is_mandatory},
        request=request,
    )
    return template


async def owner_candidates(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(User)
                .where(User.is_active.is_(True), permissions.manager_or_hr_login(user))
                .order_by(User.full_name),
                User,
                user,
            )
        )
    ).all()
    return [{"user_id": row.id, "full_name": row.full_name, "role": row.role.value} for row in rows]


# ═══════════════════════════════════════════════════════════════════════════
# Settlements
# ═══════════════════════════════════════════════════════════════════════════


async def _settlement_for(
    session: AsyncSession, user: User, exit_id: uuid.UUID
) -> Settlement | None:
    return await session.scalar(
        tenant_scoped(
            select(Settlement).where(Settlement.exit_record_id == exit_id), Settlement, user
        )
    )


async def _settlement(session: AsyncSession, user: User, settlement_id: uuid.UUID) -> Settlement:
    row = await session.scalar(
        tenant_scoped(select(Settlement).where(Settlement.id == settlement_id), Settlement, user)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Settlement not found")
    return row


async def settlement_reads(
    session: AsyncSession, user: User, settlements: list[Settlement]
) -> list[dict[str, Any]]:
    if not settlements:
        return []
    labels = await _labels(session, user, {row.employee_id for row in settlements})
    payers = await _user_names(
        session, user, {row.paid_by_user_id for row in settlements if row.paid_by_user_id}
    )
    records = {
        row.id: row
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(ExitRecord).where(
                        ExitRecord.id.in_([row.exit_record_id for row in settlements])
                    ),
                    ExitRecord,
                    user,
                )
            )
        ).all()
    }
    out = []
    for row in settlements:
        record = records.get(row.exit_record_id)
        code, name = labels.get(row.employee_id, (None, None))
        net = _amount(row.net_amount)
        out.append(
            {
                "id": row.id,
                "exit_record_id": row.exit_record_id,
                "employee_id": row.employee_id,
                "employee_code": code,
                "employee_name": name,
                "exit_type": record.exit_type if record else None,
                "last_working_day": (
                    record.approved_last_working_day or record.requested_last_working_day
                )
                if record
                else None,
                "status": row.status,
                "total_earnings": _amount(row.total_earnings),
                "total_recoveries": _amount(row.total_recoveries),
                "total_tax": _amount(row.total_tax),
                "net_amount": net,
                "is_demand": net < ZERO,
                "hold_reason": row.hold_reason,
                "computed_at": row.computed_at,
                "approved_at": row.approved_at,
                "paid_at": row.paid_at,
                "paid_on": row.paid_on,
                "payment_reference": row.payment_reference,
                "paid_by_name": payers.get(row.paid_by_user_id) if row.paid_by_user_id else None,
                "statement_letter_id": row.statement_letter_id,
            }
        )
    return out


async def list_settlements(
    session: AsyncSession, user: User, *, settlement_status: SettlementStatus | None = None
) -> list[dict[str, Any]]:
    stmt = select(Settlement).order_by(Settlement.computed_at.desc().nulls_last())
    if settlement_status is not None:
        stmt = stmt.where(Settlement.status == settlement_status)
    rows = (await session.scalars(tenant_scoped(stmt, Settlement, user))).all()
    return await settlement_reads(session, user, list(rows))


async def settlement_for_exit(
    session: AsyncSession, user: User, exit_id: uuid.UUID
) -> Settlement:
    await _record(session, user, exit_id)
    row = await _settlement_for(session, user, exit_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No settlement has been computed"
        )
    return row


async def open_claims(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> tuple[list[ReimbursementClaim], list[ExpenseClaim]]:
    """Claims owed to the leaver and not yet paid anywhere: what F&F must settle.

    The same tests payroll applies before paying a claim, so a claim is due in
    exactly one place. An expense claim on a trip whose travel advance is still
    open is left out: settling that advance decides what is owed for the trip.
    """
    flexi = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(ReimbursementClaim).where(
                        ReimbursementClaim.employee_id == employee_id,
                        ReimbursementClaim.status.in_([ClaimStatus.VERIFIED, ClaimStatus.PARTIAL]),
                        ReimbursementClaim.paid_in_run_id.is_(None),
                        ReimbursementClaim.settled_in_settlement_id.is_(None),
                    ),
                    ReimbursementClaim,
                    user,
                )
            )
        ).all()
    )
    expenses = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(ExpenseClaim).where(
                        ExpenseClaim.employee_id == employee_id,
                        ExpenseClaim.status == ExpenseClaimStatusEnum.APPROVED,
                        ExpenseClaim.reimbursed_in_run_id.is_(None),
                        ExpenseClaim.advance_settlement_id.is_(None),
                        ExpenseClaim.settled_in_settlement_id.is_(None),
                        ExpenseClaim.amount > 0,
                    ),
                    ExpenseClaim,
                    user,
                )
            )
        ).all()
    )
    trips = {claim.travel_request_id for claim in expenses if claim.travel_request_id}
    if trips:
        open_trips = set(
            (
                await session.scalars(
                    tenant_scoped(
                        select(TravelAdvance.travel_request_id).where(
                            TravelAdvance.travel_request_id.in_(trips),
                            TravelAdvance.status == AdvanceStatusEnum.ISSUED,
                        ),
                        TravelAdvance,
                        user,
                    )
                )
            ).all()
        )
        expenses = [claim for claim in expenses if claim.travel_request_id not in open_trips]
    return flexi, expenses


def _claims_total(flexi: list[ReimbursementClaim], expenses: list[ExpenseClaim]) -> Decimal:
    return sum((_amount(row.accepted_amount) for row in flexi), ZERO) + sum(
        (_amount(row.amount) for row in expenses), ZERO
    )


def _suggest(
    field: str, basis_field: str, label: str, amount: Decimal, basis: str, source: str,
    source_id: uuid.UUID | None,
) -> dict[str, Any]:
    return {
        "field": field,
        "basis_field": basis_field,
        "label": label,
        "amount": amount,
        "basis": basis,
        "source": source,
        "source_id": source_id,
    }


async def settlement_sources(
    session: AsyncSession, user: User, exit_id: uuid.UUID
) -> dict[str, Any]:
    """What other modules already hold for this leaver, offered as prefill.

    Each figure comes from the record its own module stored, with that module's
    basis — the settlement takes it rather than re-deriving it.
    """
    record = await _record(session, user, exit_id)
    employee_id = record.employee_id
    suggestions: list[dict[str, Any]] = []
    notes: list[str] = []

    payout = await session.scalar(
        tenant_scoped(
            select(GratuityPayout).where(
                GratuityPayout.employee_id == employee_id,
                GratuityPayout.paid_in_run_id.is_(None),
            ),
            GratuityPayout,
            user,
        )
    )
    if payout is not None and _amount(payout.gross_amount) > ZERO:
        suggestions.append(
            _suggest(
                "gratuity", "gratuity_basis", "Gratuity", _amount(payout.gross_amount),
                (
                    f"{payout.service_years} years of service on monthly wages of "
                    f"{_amount(payout.monthly_wages)}, exit {payout.exit_date.isoformat()}"
                    f"{' (eligibility period waived)' if payout.eligibility_waived else ''}; "
                    f"{_amount(payout.exempt_amount)} exempt, "
                    f"{_amount(payout.taxable_amount)} taxable."
                ),
                "exit_benefits", payout.id,
            )
        )
    else:
        notes.append(
            "No gratuity payout has been computed for this leaver. Compute it under "
            "Gratuity & encashment first if they are eligible."
        )

    encashments = (
        await session.scalars(
            tenant_scoped(
                select(EncashmentRequest).where(
                    EncashmentRequest.employee_id == employee_id,
                    EncashmentRequest.is_exit_encashment.is_(True),
                    EncashmentRequest.status == EncashmentStatus.APPROVED,
                    EncashmentRequest.paid_in_run_id.is_(None),
                ),
                EncashmentRequest,
                user,
            )
        )
    ).all()
    if encashments:
        total = sum((_amount(row.gross_amount) for row in encashments), ZERO)
        basis = "; ".join(
            f"{_amount(row.days_approved)} day(s) of {row.leave_type_code} at "
            f"{_amount(row.daily_rate)} per day"
            for row in encashments
        )
        suggestions.append(
            _suggest(
                "leave_encashment", "leave_encashment_basis", "Leave encashment", total,
                basis + ".", "exit_benefits", encashments[0].id,
            )
        )

    # Leave still encashable on exit, not yet computed: the dialog offers to
    # compute it from the balance rather than leaving HR to work it out by hand.
    from app.services.exit_benefits.service import exit_encashable

    encashable = await exit_encashable(session, user, employee_id)

    flexi, expenses = await open_claims(session, user, employee_id)
    if flexi or expenses:
        parts = [f"{row.component_code} {_amount(row.accepted_amount)}" for row in flexi] + [
            f"{row.category_key} expense of {row.incurred_on:%d %b %Y} {_amount(row.amount)}"
            for row in expenses
        ]
        suggestions.append(
            _suggest(
                "pending_reimbursements", "reimbursement_basis", "Pending reimbursements",
                _claims_total(flexi, expenses),
                f"{len(parts)} approved claim(s) not yet paid: " + "; ".join(parts) + ". "
                "Approving the settlement marks them paid here, so payroll does not pay "
                "them again.",
                "reimbursements" if flexi else "expenses",
                (flexi[0].id if flexi else expenses[0].id),
            )
        )

    arrears = await arrears_service.payable_arrears(session, user, employee_ids={employee_id})
    if arrears:
        total = sum((_amount(row["net_payable"]) for row in arrears), ZERO)
        suggestions.append(
            _suggest(
                "pending_arrears", "arrears_basis", "Pending arrears", total,
                "Approved arrears not yet paid: "
                + "; ".join(
                    f"{row['from_period']} to {row['to_period']} ({row['net_payable']})"
                    for row in arrears
                )
                + ".",
                "arrears", arrears[0]["batch_id"],
            )
        )

    loans = (
        await session.scalars(
            tenant_scoped(
                select(LoanAccount).where(
                    LoanAccount.employee_id == employee_id,
                    LoanAccount.status.in_([LoanStatus.DISBURSED, LoanStatus.ACTIVE]),
                ),
                LoanAccount,
                user,
            )
        )
    ).all()
    if loans:
        total = sum((_amount(row.outstanding_principal) for row in loans), ZERO)
        suggestions.append(
            _suggest(
                "loan_outstanding", "loan_basis", "Outstanding loans", total,
                "; ".join(
                    f"Principal outstanding {_amount(row.outstanding_principal)} on a loan of "
                    f"{_amount(row.principal)}"
                    + (
                        f" ({_amount(row.carried_forward)} of skipped instalments carried forward)"
                        if _amount(row.carried_forward) > ZERO
                        else ""
                    )
                    for row in loans
                )
                + ".",
                "loans", loans[0].id,
            )
        )

    items = (
        await session.scalars(
            tenant_scoped(
                select(ClearanceItem).where(ClearanceItem.exit_record_id == exit_id),
                ClearanceItem,
                user,
            )
        )
    ).all()
    recovering = [item for item in items if _amount(item.recovery_amount) > ZERO]
    notes.append(
        "Final-month salary, pro-rata bonus and the full-year tax at exit are entered with "
        "their basis: payroll and the tax projection hold those figures, and the tax at exit "
        "needs Compliance SME review."
    )
    # A regular run computed while the leaver was still on notice paid them up
    # to their last working day. Entering those days again here pays them twice.
    last_day = record.approved_last_working_day or record.requested_last_working_day
    if last_day is not None:
        from app.models.payroll import PayrollRun, PayrollRunStatus, Payslip

        paid_slip = (
            await session.execute(
                tenant_scoped(
                    select(Payslip.paid_days, PayrollRun.period_month, PayrollRun.period_year)
                    .join(PayrollRun, Payslip.run_id == PayrollRun.id)
                    .where(
                        Payslip.employee_id == employee_id,
                        PayrollRun.period_year == last_day.year,
                        PayrollRun.period_month == last_day.month,
                        PayrollRun.status.in_([PayrollRunStatus.APPROVED, PayrollRunStatus.PAID]),
                        # A supplementary payslip paid extra lines, not days.
                        Payslip.is_supplementary.is_(False),
                    ),
                    Payslip,
                    user,
                )
            )
        ).first()
        if paid_slip is not None:
            paid_days, month, year = paid_slip
            notes.append(
                f"The {calendar.month_name[month]} {year} payroll run already paid "
                f"{format(Decimal(str(paid_days)).normalize(), 'f')} day(s) of the final month, "
                "up to the "
                f"last working day. Do not enter those days again as final-month salary."
            )
    return {
        "exit_id": exit_id,
        "suggestions": suggestions,
        "notice": {
            "shortfall_days": record.notice_shortfall_days,
            "waived": record.notice_waived,
            "waiver_reason": record.notice_waiver_reason,
        },
        "clearance_recovery": sum((_amount(item.recovery_amount) for item in recovering), ZERO),
        "clearance_basis": "; ".join(
            f"{item.department}: {item.description} ({_amount(item.recovery_amount)})"
            for item in recovering
        )
        or "Nothing to recover from clearance.",
        "notes": notes,
        "encashable_leave": [
            {"leave_type_code": row["leave_type_code"], "days": _amount(row["days"])}
            for row in encashable
        ],
    }


def _not_own_settlement(user: User, settlement: Settlement, action: str) -> None:
    if user.employee_id is not None and settlement.employee_id == user.employee_id:
        raise permissions.forbidden(
            f"This is your own settlement. Somebody else has to {action} it."
        )


async def release_hold(
    session: AsyncSession,
    user: User,
    settlement_id: uuid.UUID,
    *,
    note: str,
    request: Request | None = None,
) -> Settlement:
    settlement = await _settlement(session, user, settlement_id)
    _not_own_settlement(user, settlement, "release")
    if settlement.status is not SettlementStatus.ON_HOLD:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This settlement is not on hold."
        )
    previous = settlement.hold_reason
    settlement.status = SettlementStatus.PENDING_APPROVAL
    settlement.hold_reason = None
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="settlement",
        entity_id=settlement.id,
        summary=f"Released the hold on the settlement: {note}",
        changes={"previous_hold_reason": previous, "note": note},
        request=request,
    )
    return settlement


async def approve_settlement(
    session: AsyncSession, user: User, settlement_id: uuid.UUID, *, request: Request | None = None
) -> Settlement:
    """Sign off for payment — or, where the net is negative, for a demand."""
    settlement = await _settlement(session, user, settlement_id)
    _not_own_settlement(user, settlement, "approve")
    if settlement.status in (SettlementStatus.PAID, SettlementStatus.DEMAND_RAISED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This settlement is already '{settlement.status.value}'.",
        )
    if settlement.status is SettlementStatus.ON_HOLD:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This settlement is on hold ({settlement.hold_reason}). Release the hold "
                "before approving it."
            ),
        )
    # Every claim still owed is settled here or nowhere: a leaver is not in the
    # runs that would otherwise pay it. What the settlement pays for claims has
    # to be exactly what is open, and approving it marks those claims paid.
    flexi, expenses = await open_claims(session, user, settlement.employee_id)
    owed = _claims_total(flexi, expenses)
    paying = sum(
        (
            _amount(line.amount)
            for line in (
                await session.scalars(
                    tenant_scoped(
                        select(SettlementLine).where(
                            SettlementLine.settlement_id == settlement.id,
                            SettlementLine.code == "REIMBURSEMENTS",
                        ),
                        SettlementLine,
                        user,
                    )
                )
            ).all()
        ),
        ZERO,
    )
    if paying != owed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"The settlement pays {paying} in reimbursements, but {owed} is open in "
                f"{len(flexi) + len(expenses)} approved claim(s). Compute the settlement "
                "again with the suggested figure, or reject the claims that are not owed, "
                "so that nothing is paid twice or left unpaid."
            ),
        )
    now = datetime.now(UTC)
    for claim in flexi:
        claim.settled_in_settlement_id = settlement.id
    for claim in expenses:
        claim.settled_in_settlement_id = settlement.id
        claim.status = ExpenseClaimStatusEnum.REIMBURSED
        claim.reimbursed_at = now

    is_demand = _amount(settlement.net_amount) < ZERO
    settlement.status = SettlementStatus.DEMAND_RAISED if is_demand else SettlementStatus.APPROVED
    settlement.approved_by_user_id = user.id
    settlement.approved_at = datetime.now(UTC)
    record = await _record(session, user, settlement.exit_record_id)
    record.status = ExitStatus.SETTLED
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE,
        entity_type="settlement",
        entity_id=settlement.id,
        summary=(
            f"Raised a demand for {-_amount(settlement.net_amount)} against the employee"
            if is_demand
            else f"Approved a settlement of {settlement.net_amount} for payment"
        ),
        changes={
            "net_amount": str(settlement.net_amount),
            "is_demand": is_demand,
            "claims_settled": [str(c.id) for c in [*flexi, *expenses]],
        },
        request=request,
    )
    await _tell_the_leaver(
        session,
        user,
        settlement,
        EVENT_SETTLEMENT_APPROVED,
        {
            "outcome": (
                f"a demand for {_rupees(-_amount(settlement.net_amount))} that you owe the "
                "company"
                if is_demand
                else f"{_rupees(settlement.net_amount)} due to you"
            ),
            "next_line": (
                " HR will contact you about repaying it."
                if is_demand
                else " You will be told when it has been paid."
            ),
        },
    )
    return settlement


# ═══════════════════════════════════════════════════════════════════════════
# Payment
# ═══════════════════════════════════════════════════════════════════════════

EVENT_SETTLEMENT_APPROVED = "exits.settlement.approved"
EVENT_SETTLEMENT_PAID = "exits.settlement.paid"

register_event(
    EVENT_SETTLEMENT_APPROVED,
    "Your full and final settlement has been approved",
    "Your full and final settlement was approved: {outcome}.{next_line}",
)
register_event(
    EVENT_SETTLEMENT_PAID,
    "Your full and final settlement has been paid",
    "{amount} was paid to you on {paid_on}, bank reference {payment_reference}.",
)

#: A bank reference is a UTR, an IMPS/NEFT reference or a cheque number:
#: letters, digits and a little punctuation, never free text.
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 /._-]{2,119}$")


def _rupees(value: object) -> str:
    return f"₹{group_indian(_amount(value).quantize(Decimal('0.01')))}"


async def _tell_the_leaver(
    session: AsyncSession,
    user: User,
    settlement: Settlement,
    event_key: str,
    payload: dict[str, Any],
) -> None:
    """Notify the leaver, inside the caller's transaction.

    Only while they still have an active login: once access is revoked there is
    nobody to notify in the product, and the issued statement is how they hear.
    """
    recipients = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(User.id).where(
                        User.employee_id == settlement.employee_id, User.is_active.is_(True)
                    ),
                    User,
                    user,
                )
            )
        ).all()
    )
    if recipients:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key=event_key,
            recipient_user_ids=recipients,
            payload={**payload, "settlement_id": str(settlement.id)},
            commit=False,
        )


async def mark_settlement_paid(
    session: AsyncSession,
    user: User,
    settlement_id: uuid.UUID,
    *,
    paid_on: date,
    payment_reference: str,
    request: Request | None = None,
) -> Settlement:
    """Record that an approved settlement's money reached the leaver. The caller commits.

    Approval says the company owes it; this says the company paid it, when,
    and under which bank reference — the three things a leaver asking "where
    is my money" and an auditor reconciling the bank statement both need.
    """
    settlement = await _settlement(session, user, settlement_id)
    _not_own_settlement(user, settlement, "record payment of")
    if settlement.status is SettlementStatus.PAID:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This settlement was already paid on {settlement.paid_on:%d %b %Y}"
                if settlement.paid_on
                else "This settlement was already paid."
            ),
        )
    if settlement.status is SettlementStatus.DEMAND_RAISED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This settlement is a demand on the employee: they owe the company. A demand "
                "is recovered from them, not paid to them."
            ),
        )
    if settlement.status is not SettlementStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Only an approved settlement can be paid; this one is "
                f"{settlement.status.value.replace('_', ' ')}."
            ),
        )

    reference = " ".join(payment_reference.split())
    if not _REFERENCE.match(reference):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Give the bank's reference for the payment (UTR, NEFT/IMPS reference or "
                "cheque number): 3 to 120 letters, digits, spaces, '/', '.', '_' or '-'."
            ),
        )
    from app.services.attendance.service import local_today, tenant_timezone

    today = await local_today(session, user)
    if paid_on > today:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A payment cannot be recorded for a date in the future.",
        )
    if settlement.approved_at is not None:
        approved_on = settlement.approved_at.astimezone(await tenant_timezone(session, user))
        if paid_on < approved_on.date():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"The settlement was approved on {approved_on:%d %b %Y}; it cannot "
                    "have been paid before that."
                ),
            )

    settlement.status = SettlementStatus.PAID
    settlement.paid_on = paid_on
    settlement.payment_reference = reference
    settlement.paid_by_user_id = user.id
    settlement.paid_at = datetime.now(UTC)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="settlement",
        entity_id=settlement.id,
        summary=(
            f"Recorded payment of the settlement: {settlement.net_amount} on "
            f"{paid_on.isoformat()}, reference {reference}"
        ),
        changes={
            "status": SettlementStatus.PAID.value,
            "net_amount": str(settlement.net_amount),
            "paid_on": paid_on.isoformat(),
            "payment_reference": reference,
        },
        request=request,
    )
    await _tell_the_leaver(
        session,
        user,
        settlement,
        EVENT_SETTLEMENT_PAID,
        {
            "amount": _rupees(settlement.net_amount),
            "paid_on": f"{paid_on:%d %b %Y}",
            "payment_reference": reference,
        },
    )
    return settlement


async def issue_statement(
    session: AsyncSession, user: User, settlement_id: uuid.UUID, *, request: Request | None = None
) -> IssuedLetter:
    """The itemised statement as a letter-engine document. The caller commits."""
    settlement = await _settlement(session, user, settlement_id)
    lines = (
        await session.scalars(
            tenant_scoped(
                select(SettlementLine)
                .where(SettlementLine.settlement_id == settlement.id)
                .order_by(SettlementLine.display_order),
                SettlementLine,
                user,
            )
        )
    ).all()
    record = await _record(session, user, settlement.exit_record_id)
    last_day = record.approved_last_working_day or record.requested_last_working_day
    net = _amount(settlement.net_amount)
    notes = []
    if settlement.status is SettlementStatus.ON_HOLD:
        notes.append(f"Payment is on hold: {settlement.hold_reason}.")
    if net < ZERO:
        notes.append(
            "Recoveries exceed what is due, so this is a demand on the employee, not a payment."
        )
    tds_line = next((line for line in lines if line.code == "TDS_FINAL"), None)
    if tds_line is not None and _amount(settlement.total_tax) < ZERO:
        notes.append("The tax line is a refund to the employee on the final computation.")

    values = statement.manual_values(
        lines=[(line.code, _amount(line.amount), line.basis) for line in lines],
        total_earnings=_amount(settlement.total_earnings),
        total_recoveries=_amount(settlement.total_recoveries),
        net_amount=net,
        separation=(
            f"{record.exit_type.value.replace('_', ' ').capitalize()}; resigned "
            f"{record.resignation_date.isoformat()}"
            + (f"; last working day {last_day.isoformat()}" if last_day else "")
        ),
        notes=notes,
    )
    template = await support.ensure_letter_template(
        session, user, statement.STATEMENT_CODE, statement.seed_template
    )
    letter = await documents_service.issue_letter(
        session,
        user,
        employee_id=settlement.employee_id,
        template_id=template.id,
        manual_values=values,
        request=request,
        commit=False,
    )
    settlement.statement_letter_id = letter.id
    await session.flush()
    return letter


async def statement_pdf(
    session: AsyncSession, user: User, settlement_id: uuid.UUID
) -> tuple[bytes, str]:
    settlement = await _settlement(session, user, settlement_id)
    if settlement.statement_letter_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No statement has been issued for this settlement.",
        )
    letter, intact = await documents_service.verify_letter(
        session, user, settlement.statement_letter_id
    )
    if not intact:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The stored statement does not match its content hash. Issue it again.",
        )
    data = await documents_service.stored_pdf(letter.pdf_storage_key or "")
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The rendered PDF is not available. Issue the statement again.",
        )
    return data, f"{letter.reference_number.replace('/', '_')}.pdf"


# ═══════════════════════════════════════════════════════════════════════════
# Letters gated on the settlement, and the tenant's policy
# ═══════════════════════════════════════════════════════════════════════════


async def exit_policy(session: AsyncSession, user: User) -> dict[str, bool]:
    tenant = await session.get(Tenant, user.tenant_id)
    configured = ((tenant.settings or {}) if tenant else {}).get("exit") or {}
    return {"letters_require_settlement": bool(configured.get("letters_require_settlement", True))}


async def set_exit_policy(
    session: AsyncSession,
    user: User,
    *,
    letters_require_settlement: bool,
    request: Request | None = None,
) -> dict[str, bool]:
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    settings = dict(tenant.settings or {})
    exit_settings = dict(settings.get("exit") or {})
    exit_settings["letters_require_settlement"] = letters_require_settlement
    settings["exit"] = exit_settings
    tenant.settings = settings
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.RULE_CHANGE,
        entity_type="exit_policy",
        entity_id=tenant.id,
        summary=(
            "Relieving and experience letters now "
            + ("wait for" if letters_require_settlement else "do not wait for")
            + " an approved settlement"
        ),
        changes={"letters_require_settlement": letters_require_settlement},
        request=request,
    )
    return {"letters_require_settlement": letters_require_settlement}


async def letter_gate(session: AsyncSession, user: User, employee_id: uuid.UUID) -> dict[str, Any]:
    """Whether a relieving or experience letter may be issued to this person now.

    A settlement on hold always blocks — the hold is usually the dispute the
    letter would pre-empt. Where the tenant configures it, the settlement must
    also be approved first.
    """
    policy = await exit_policy(session, user)
    record = await session.scalar(
        tenant_scoped(
            select(ExitRecord)
            .where(
                ExitRecord.employee_id == employee_id,
                ExitRecord.status != ExitStatus.CANCELLED,
            )
            .order_by(ExitRecord.resignation_date.desc()),
            ExitRecord,
            user,
        )
    )
    settlement = await _settlement_for(session, user, record.id) if record else None
    result = {
        "allowed": True,
        "reason": "",
        "requires_settlement": policy["letters_require_settlement"],
        "settlement_status": settlement.status if settlement else None,
        "hold_reason": settlement.hold_reason if settlement else None,
    }
    if record is None:
        result.update(allowed=False, reason="There is no open exit for this employee.")
    elif settlement is not None and settlement.status is SettlementStatus.ON_HOLD:
        result.update(
            allowed=False,
            reason=f"The settlement is on hold ({settlement.hold_reason}). Letters wait for it.",
        )
    elif policy["letters_require_settlement"] and (
        settlement is None or settlement.status not in SETTLED_STATUSES
    ):
        result.update(
            allowed=False,
            reason=(
                "This workspace issues relieving and experience letters only after the "
                "settlement is approved, and it is not yet."
            ),
        )
    return result


async def issue_exit_letter(
    session: AsyncSession,
    user: User,
    exit_id: uuid.UUID,
    *,
    kind: str,
    request: Request | None = None,
) -> IssuedLetter:
    """Issue a relieving or experience letter through the letter engine, if the gate allows."""
    record = await _record(session, user, exit_id)
    if user.employee_id is not None and record.employee_id == user.employee_id:
        raise permissions.forbidden("Your own relieving letter is issued by somebody else.")
    gate = await letter_gate(session, user, record.employee_id)
    if not gate["allowed"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=gate["reason"])
    code = LETTER_TEMPLATE_CODES[kind]
    template = await session.scalar(
        tenant_scoped(
            select(LetterTemplate)
            .where(LetterTemplate.code == code, LetterTemplate.active.is_(True))
            .order_by(LetterTemplate.version.desc()),
            LetterTemplate,
            user,
        )
    )
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"There is no active {kind} letter template. Install the standard templates "
                "under Documents → Templates first."
            ),
        )
    return await documents_service.issue_letter(
        session,
        user,
        employee_id=record.employee_id,
        template_id=template.id,
        manual_values={},
        request=request,
        commit=False,
    )


async def settlement_lines(
    session: AsyncSession, user: User, exit_id: uuid.UUID
) -> list[SettlementLine]:
    """Every statement line with its basis, in statement order."""
    settlement = await settlement_for_exit(session, user, exit_id)
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(SettlementLine)
                    .where(SettlementLine.settlement_id == settlement.id)
                    .order_by(SettlementLine.display_order),
                    SettlementLine,
                    user,
                )
            )
        ).all()
    )
