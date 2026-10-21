"""Where the asset register meets the exit checklist.

Called from `app.services.exit_workflow.service` at two points, and from nowhere
else:

1. **An exit opens** (`add_exit_clearance`): every asset the leaver still holds
   becomes a mandatory "Return <tag> <name>" clearance item, owned by the
   category's custodian (or HR), and linked to the assignment it stands for.
2. **That item is signed off** (`recovery_for_clearance`, then
   `on_clearance_signed`): cleared means returned, so the assignment closes and
   the asset goes back in stock; cleared with recovery means not returned, so
   the asset is marked lost and — unless HR typed a figure — its book value on
   the last working day is what the item recovers; waived means written off.

The settlement's "Unreturned assets" line already sums clearance recoveries, so
the book value reaches the leaver's statement without being typed twice.

The other direction — returning an asset through the register while its
clearance item is still pending — is `service.sync_exit_clearance`, which signs
the item off through the exit workflow's own `clear_item`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.assets import (
    Asset,
    AssetAssignment,
    AssetCategory,
    AssetStatus,
    AssignmentOutcome,
)
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.exit_workflow import (
    ClearanceItem,
    ClearanceOwnerRole,
    ClearanceStatus,
    ExitRecord,
    ExitStatus,
)
from app.models.user import User
from app.services import audit
from app.services.assets.depreciation import book_value

__all__ = [
    "CLEARANCE_DEPARTMENT",
    "add_exit_clearance",
    "clearance_item_for",
    "on_clearance_signed",
    "open_exit_for",
    "recovery_for_clearance",
]

CLEARANCE_DEPARTMENT = "IT/Admin"
IST = ZoneInfo("Asia/Kolkata")
ZERO = Decimal(0)

#: Exits whose checklist is still being worked. A settled or cancelled exit
#: does not grow new items.
_OPEN_EXIT = (
    ExitStatus.INITIATED,
    ExitStatus.NOTICE_PERIOD,
    ExitStatus.CLEARANCE_PENDING,
    ExitStatus.SETTLEMENT_PENDING,
    ExitStatus.ON_HOLD,
)


def _description(asset: Asset) -> str:
    return f"Return {asset.asset_tag} {asset.name}"[:512]


async def open_exit_for(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> uuid.UUID | None:
    """The id of the employee's exit that is still in progress, if any."""
    return await session.scalar(
        tenant_scoped(
            select(ExitRecord.id)
            .where(ExitRecord.employee_id == employee_id, ExitRecord.status.in_(_OPEN_EXIT))
            .order_by(ExitRecord.resignation_date.desc()),
            ExitRecord,
            user,
        )
    )


async def clearance_item_for(
    session: AsyncSession,
    user: User,
    exit_record_id: uuid.UUID,
    assignment: AssetAssignment,
    asset: Asset,
    category: AssetCategory,
) -> ClearanceItem:
    """Add the "return this asset" item for one assignment to an exit's checklist."""
    item = ClearanceItem(
        tenant_id=user.tenant_id,
        exit_record_id=exit_record_id,
        department=CLEARANCE_DEPARTMENT,
        description=_description(asset),
        is_mandatory=True,
        owner_role=(
            ClearanceOwnerRole.NAMED_USER.value
            if category.custodian_user_id
            else ClearanceOwnerRole.HR.value
        ),
        owner_user_id=category.custodian_user_id,
        asset_assignment_id=assignment.id,
    )
    session.add(item)
    assignment.exit_record_id = exit_record_id
    return item


async def add_exit_clearance(
    session: AsyncSession, user: User, record: ExitRecord, *, request: Request | None = None
) -> int:
    """One clearance item per asset the leaver still holds. Returns how many."""
    rows = (
        await session.execute(
            tenant_scoped(
                select(AssetAssignment, Asset, AssetCategory)
                .join(Asset, Asset.id == AssetAssignment.asset_id)
                .join(AssetCategory, AssetCategory.id == Asset.category_id)
                .where(
                    AssetAssignment.employee_id == record.employee_id,
                    AssetAssignment.closed_at.is_(None),
                )
                .order_by(Asset.asset_tag),
                AssetAssignment,
                user,
            )
        )
    ).all()
    if not rows:
        return 0
    already = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(ClearanceItem.asset_assignment_id).where(
                        ClearanceItem.exit_record_id == record.id,
                        ClearanceItem.asset_assignment_id.is_not(None),
                    ),
                    ClearanceItem,
                    user,
                )
            )
        ).all()
    )
    added = []
    for assignment, asset, category in rows:
        if assignment.id in already:
            continue
        await clearance_item_for(session, user, record.id, assignment, asset, category)
        added.append(asset.asset_tag)
    if not added:
        return 0
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="exit_record",
        entity_id=record.id,
        summary=(
            f"Added {len(added)} asset return item(s) to the exit checklist: " + ", ".join(added)
        ),
        changes={"asset_tags": added},
        request=request,
    )
    return len(added)


async def _linked(
    session: AsyncSession, user: User, item: ClearanceItem, *, lock: bool = False
) -> tuple[AssetAssignment, Asset, AssetCategory] | None:
    if item.asset_assignment_id is None:
        return None
    assignment = await session.scalar(
        tenant_scoped(
            select(AssetAssignment).where(AssetAssignment.id == item.asset_assignment_id),
            AssetAssignment,
            user,
        )
    )
    if assignment is None:
        return None
    stmt = tenant_scoped(select(Asset).where(Asset.id == assignment.asset_id), Asset, user)
    asset = await session.scalar(stmt.with_for_update() if lock else stmt)
    if asset is None:
        return None
    category = await session.scalar(
        tenant_scoped(
            select(AssetCategory).where(AssetCategory.id == asset.category_id),
            AssetCategory,
            user,
        )
    )
    if category is None:
        return None
    return assignment, asset, category


async def recovery_for_clearance(
    session: AsyncSession,
    user: User,
    item: ClearanceItem,
    outcome: ClearanceStatus,
    recovery_amount: Decimal,
) -> Decimal:
    """What an asset clearance item recovers when it is marked not returned.

    A figure HR typed wins. Otherwise it is the asset's book value on the
    leaver's last working day — and where there is no book value to take, the
    refusal says so rather than recovering nothing by default.
    """
    if outcome is not ClearanceStatus.CLEARED_WITH_RECOVERY or recovery_amount > ZERO:
        return recovery_amount
    linked = await _linked(session, user, item)
    if linked is None:
        return recovery_amount
    _, asset, category = linked
    dates = (
        await session.execute(
            tenant_scoped(
                select(
                    ExitRecord.approved_last_working_day, ExitRecord.requested_last_working_day
                ).where(ExitRecord.id == item.exit_record_id),
                ExitRecord,
                user,
            )
        )
    ).first()
    as_of = (dates and (dates[0] or dates[1])) or datetime.now(IST).date()
    value = book_value(
        purchase_cost=asset.purchase_cost,
        purchase_date=asset.purchase_date,
        depreciation_months=category.depreciation_months,
        as_of=as_of,
    )
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{asset.asset_tag} has no purchase cost in the asset register, so there is no "
                "book value to recover. Enter the amount to recover, or record the cost first."
            ),
        )
    if value.amount <= ZERO:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{asset.asset_tag} is fully depreciated ({value.basis}) Enter an amount to "
                "recover, or waive the item with a reason."
            ),
        )
    return value.amount


async def on_clearance_signed(
    session: AsyncSession, user: User, item: ClearanceItem, *, request: Request | None = None
) -> None:
    """Close the assignment an asset clearance item stands for.

    An assignment already closed — returned through the register first — is
    left closed; only its recovery figure follows the item.
    """
    linked = await _linked(session, user, item, lock=True)
    if linked is None:
        return
    assignment, asset, _ = linked
    if assignment.closed_at is not None:
        if item.status is ClearanceStatus.CLEARED_WITH_RECOVERY:
            assignment.recovery_amount = item.recovery_amount
            await session.flush()
        return

    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == assignment.employee_id), Employee, user)
    )
    who = employee.employee_code if employee else "the leaver"
    now = datetime.now(UTC)
    assignment.closed_at = now
    assignment.exit_record_id = item.exit_record_id
    if item.status is ClearanceStatus.CLEARED:
        assignment.outcome = AssignmentOutcome.RETURNED
        assignment.returned_at = now
        assignment.returned_to_user_id = user.id
        assignment.return_notes = item.notes or "Returned at exit clearance."
        asset.status = AssetStatus.IN_STOCK
        asset.status_reason = f"Returned by {who} at exit clearance."
        summary = f"{asset.asset_tag} returned by {who} at exit clearance"
    elif item.status is ClearanceStatus.CLEARED_WITH_RECOVERY:
        assignment.outcome = AssignmentOutcome.LOST
        assignment.recovery_amount = item.recovery_amount
        assignment.return_notes = item.notes or "Not returned at exit."
        asset.status = AssetStatus.LOST
        asset.status_reason = (
            f"Not returned by {who} at exit; {item.recovery_amount} recovered through the "
            "settlement."
        )
        summary = (
            f"{asset.asset_tag} not returned by {who} at exit; {item.recovery_amount} to recover"
        )
    elif item.status is ClearanceStatus.WAIVED:
        assignment.outcome = AssignmentOutcome.WRITTEN_OFF
        assignment.return_notes = f"Written off at exit: {item.waiver_reason}"
        asset.status = AssetStatus.DISPOSED
        asset.status_reason = f"Written off to {who} at exit: {item.waiver_reason}"
        summary = f"{asset.asset_tag} written off to {who} at exit"
    else:
        return
    asset.status_changed_at = now
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="asset_assignment",
        entity_id=assignment.id,
        summary=summary,
        changes={
            "outcome": assignment.outcome.value if assignment.outcome else None,
            "asset_status": asset.status.value,
            "recovery_amount": (
                str(assignment.recovery_amount) if assignment.recovery_amount else None
            ),
            "clearance_item_id": str(item.id),
        },
        request=request,
    )
