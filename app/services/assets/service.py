"""Asset management: the register, who holds what, requests, and returns.

Who may do what, enforced here rather than trusted from the router:

- **HR** (tenant admin, HR admin, HR ops) keeps the register: categories,
  assets, hand-overs, returns and status changes. Nobody records a custody
  change on an asset they hold themselves — somebody else receives it back.
- **Everybody** sees what they hold, acknowledges receipt (only they can — an
  acknowledgement recorded by somebody else is not one), and asks for assets.
- **A line manager or HR** approves or rejects a request; nobody decides their
  own. HR fulfils an approved request by handing over an in-stock asset of the
  requested category, which closes the request.

An assignment is closed, never edited back; see the model module. Exit
integration lives in `exit_hooks` and in `sync_exit_clearance` below.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.assets import (
    Asset,
    AssetAssignment,
    AssetCategory,
    AssetCondition,
    AssetRequest,
    AssetRequestStatus,
    AssetStatus,
    AssignmentOutcome,
)
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.exit_workflow import ClearanceItem, ClearanceStatus
from app.models.notifications import Channel, DigestMode
from app.models.org import Location
from app.models.user import User, UserRole
from app.services import audit
from app.services.assets import exit_hooks
from app.services.assets.depreciation import book_value
from app.services.notifications.dispatch import notify, register_event

__all__ = [
    "EVENT_ASSIGNED",
    "EVENT_REQUEST_DECIDED",
    "EVENT_REQUEST_SUBMITTED",
    "EVENT_RETURN_DUE",
    "acknowledge",
    "approvals_inbox",
    "asset_detail",
    "assign_asset",
    "cancel_request",
    "change_status",
    "create_asset",
    "create_category",
    "create_request",
    "custodian_candidates",
    "decide_request",
    "employee_assets",
    "export_csv",
    "fulfil_request",
    "list_assets",
    "list_categories",
    "list_requests",
    "my_assets",
    "remind_return",
    "return_asset",
    "send_return_due_reminders",
    "summary",
    "update_asset",
    "update_category",
]

IST = ZoneInfo("Asia/Kolkata")
ZERO = Decimal(0)

# ── Notification events ─────────────────────────────────────────────────────

EVENT_ASSIGNED = "assets.assigned"
EVENT_REQUEST_SUBMITTED = "assets.request.submitted"
EVENT_REQUEST_DECIDED = "assets.request.decided"
EVENT_RETURN_DUE = "assets.return.due"

_NOW = {Channel.IN_APP: DigestMode.IMMEDIATE, Channel.EMAIL: DigestMode.IMMEDIATE}

register_event(
    EVENT_ASSIGNED,
    "{asset_name} has been assigned to you",
    "{asset_name} ({asset_tag}) was handed over to you on {assigned_on} by {assigned_by}."
    " {acknowledge_line}",
    preferences=_NOW,
)
register_event(
    EVENT_REQUEST_SUBMITTED,
    "{employee_name} has asked for a {category_name}",
    "{employee_name} has requested a {category_name}{needed_by_line}. Reason: {reason}",
    preferences=_NOW,
)
register_event(
    EVENT_REQUEST_DECIDED,
    "Your {category_name} request was {decision}",
    "Your request for a {category_name} was {decision} by {decided_by}.{note_line}",
    preferences=_NOW,
)
register_event(
    EVENT_RETURN_DUE,
    "{asset_name} is due back on {due_date}",
    "{asset_name} ({asset_tag}) is due to be returned on {due_date}. Hand it to the IT or "
    "admin team, or ask HR to extend the date.",
    preferences=_NOW,
)

#: Logins that may own an asset clearance item: the exit workflow's own set.
CUSTODIAN_ROLES = frozenset(
    {UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS, UserRole.MANAGER}
)

#: Manual status changes, from → allowed targets. `assigned` changes only by
#: hand-over and return, or by being reported lost while somebody holds it.
_TRANSITIONS: dict[AssetStatus, frozenset[AssetStatus]] = {
    AssetStatus.IN_STOCK: frozenset(
        {AssetStatus.IN_REPAIR, AssetStatus.LOST, AssetStatus.RETIRED, AssetStatus.DISPOSED}
    ),
    AssetStatus.IN_REPAIR: frozenset(
        {AssetStatus.IN_STOCK, AssetStatus.LOST, AssetStatus.RETIRED, AssetStatus.DISPOSED}
    ),
    AssetStatus.LOST: frozenset({AssetStatus.IN_STOCK, AssetStatus.RETIRED, AssetStatus.DISPOSED}),
    AssetStatus.RETIRED: frozenset({AssetStatus.IN_STOCK, AssetStatus.DISPOSED}),
    AssetStatus.ASSIGNED: frozenset({AssetStatus.LOST}),
    AssetStatus.DISPOSED: frozenset(),
}

_OPEN_REQUEST = (AssetRequestStatus.PENDING, AssetRequestStatus.APPROVED)


def today() -> date:
    """India's date. A UTC server's date is yesterday until 05:30 IST."""
    return datetime.now(IST).date()


def _humanise(value: str) -> str:
    return value.replace("_", " ")


def _require_hr(user: User) -> None:
    permissions.require_hr(user, detail="Only HR keeps the asset register.")


def _not_yourself(user: User, employee_id: uuid.UUID, action: str) -> None:
    if user.employee_id is not None and employee_id == user.employee_id:
        raise permissions.forbidden(
            f"This asset is yours. Somebody else in HR has to {action} it."
        )


# ═══════════════════════════════════════════════════════════════════════════
# Lookups
# ═══════════════════════════════════════════════════════════════════════════


async def _category(session: AsyncSession, user: User, category_id: uuid.UUID) -> AssetCategory:
    row = await session.scalar(
        tenant_scoped(
            select(AssetCategory).where(AssetCategory.id == category_id), AssetCategory, user
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    return row


async def _asset(
    session: AsyncSession, user: User, asset_id: uuid.UUID, *, lock: bool = False
) -> Asset:
    stmt = tenant_scoped(select(Asset).where(Asset.id == asset_id), Asset, user)
    row = await session.scalar(stmt.with_for_update() if lock else stmt)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    return row


async def _assignment(
    session: AsyncSession, user: User, assignment_id: uuid.UUID
) -> AssetAssignment:
    row = await session.scalar(
        tenant_scoped(
            select(AssetAssignment).where(AssetAssignment.id == assignment_id),
            AssetAssignment,
            user,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")
    return row


async def _open_assignment(
    session: AsyncSession, user: User, asset_id: uuid.UUID
) -> AssetAssignment | None:
    return await session.scalar(
        tenant_scoped(
            select(AssetAssignment).where(
                AssetAssignment.asset_id == asset_id, AssetAssignment.closed_at.is_(None)
            ),
            AssetAssignment,
            user,
        )
    )


async def _employee(session: AsyncSession, user: User, employee_id: uuid.UUID) -> Employee:
    row = await session.scalar(
        tenant_scoped(
            select(Employee).where(Employee.id == employee_id, Employee.deleted_at.is_(None)),
            Employee,
            user,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    return row


async def _employees(
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


async def _user_names(
    session: AsyncSession, user: User, ids: set[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
    wanted = {uid for uid in ids if uid is not None}
    if not wanted:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(select(User.id, User.full_name).where(User.id.in_(wanted)), User, user)
        )
    ).all()
    return {row_id: name for row_id, name in rows}


async def _user_ids_for_employees(
    session: AsyncSession, user: User, employee_ids: set[uuid.UUID | None]
) -> list[uuid.UUID]:
    wanted = {eid for eid in employee_ids if eid is not None}
    if not wanted:
        return []
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(User.id).where(
                        User.employee_id.in_(wanted), User.is_active.is_(True)
                    ),
                    User,
                    user,
                )
            )
        ).all()
    )


async def _hr_user_ids(session: AsyncSession, user: User) -> list[uuid.UUID]:
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(User.id).where(
                        User.is_active.is_(True), User.role.in_(permissions.HR_ROLES)
                    ),
                    User,
                    user,
                )
            )
        ).all()
    )


# ═══════════════════════════════════════════════════════════════════════════
# Categories
# ═══════════════════════════════════════════════════════════════════════════


async def list_categories(
    session: AsyncSession, user: User, *, include_inactive: bool = False
) -> list[dict[str, Any]]:
    """Every category for HR; the active ones for anybody choosing what to ask for."""
    stmt = select(AssetCategory).order_by(AssetCategory.name)
    if not (include_inactive and permissions.is_hr(user)):
        stmt = stmt.where(AssetCategory.active.is_(True))
    rows = (await session.scalars(tenant_scoped(stmt, AssetCategory, user))).all()
    counts: dict[uuid.UUID, dict[str, int]] = {}
    if rows:
        tallies = (
            await session.execute(
                tenant_scoped(
                    select(Asset.category_id, Asset.status, func.count())
                    .where(Asset.category_id.in_([row.id for row in rows]))
                    .group_by(Asset.category_id, Asset.status),
                    Asset,
                    user,
                )
            )
        ).all()
        for category_id, asset_status, count in tallies:
            entry = counts.setdefault(category_id, {"all": 0, "in_stock": 0})
            if asset_status is not AssetStatus.DISPOSED:
                entry["all"] += count
            if asset_status is AssetStatus.IN_STOCK:
                entry["in_stock"] += count
    names = await _user_names(session, user, {row.custodian_user_id for row in rows})
    return [
        {
            "id": row.id,
            "code": row.code,
            "name": row.name,
            "description": row.description,
            "custodian_user_id": row.custodian_user_id,
            "custodian_name": names.get(row.custodian_user_id) if row.custodian_user_id else None,
            "depreciation_months": row.depreciation_months,
            "requires_acknowledgement": row.requires_acknowledgement,
            "active": row.active,
            "asset_count": counts.get(row.id, {}).get("all", 0),
            "in_stock_count": counts.get(row.id, {}).get("in_stock", 0),
        }
        for row in rows
    ]


async def category_read(session: AsyncSession, user: User, category_id: uuid.UUID) -> dict:
    rows = await list_categories(session, user, include_inactive=True)
    return next(row for row in rows if row["id"] == category_id)


async def _check_custodian(
    session: AsyncSession, user: User, custodian_user_id: uuid.UUID | None
) -> None:
    if custodian_user_id is None:
        return
    custodian = await session.scalar(
        tenant_scoped(
            select(User).where(User.id == custodian_user_id, User.is_active.is_(True)),
            User,
            user,
        )
    )
    if custodian is None or custodian.role not in CUSTODIAN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "The custodian must be an active HR or manager login in this workspace — they "
                "sign off the asset's return at exit."
            ),
        )


async def custodian_candidates(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    _require_hr(user)
    rows = (
        await session.scalars(
            tenant_scoped(
                select(User)
                .where(User.is_active.is_(True), User.role.in_(CUSTODIAN_ROLES))
                .order_by(User.full_name),
                User,
                user,
            )
        )
    ).all()
    return [{"user_id": row.id, "full_name": row.full_name, "role": row.role.value} for row in rows]


async def create_category(
    session: AsyncSession,
    user: User,
    *,
    code: str,
    name: str,
    description: str | None,
    custodian_user_id: uuid.UUID | None,
    depreciation_months: int | None,
    requires_acknowledgement: bool,
    request: Request | None = None,
) -> AssetCategory:
    _require_hr(user)
    code = code.strip().upper()
    duplicate = await session.scalar(
        tenant_scoped(
            select(AssetCategory.id).where(AssetCategory.code == code), AssetCategory, user
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A category with the code {code} already exists.",
        )
    await _check_custodian(session, user, custodian_user_id)
    category = AssetCategory(
        tenant_id=user.tenant_id,
        code=code,
        name=name.strip(),
        description=(description or "").strip() or None,
        custodian_user_id=custodian_user_id,
        depreciation_months=depreciation_months,
        requires_acknowledgement=requires_acknowledgement,
    )
    session.add(category)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="asset_category",
        entity_id=category.id,
        summary=f"Added the asset category {code} ({name})",
        changes={
            "code": code,
            "depreciation_months": depreciation_months,
            "requires_acknowledgement": requires_acknowledgement,
            "custodian_user_id": str(custodian_user_id) if custodian_user_id else None,
        },
        request=request,
    )
    return category


async def update_category(
    session: AsyncSession,
    user: User,
    category_id: uuid.UUID,
    changes: dict[str, Any],
    *,
    request: Request | None = None,
) -> AssetCategory:
    _require_hr(user)
    category = await _category(session, user, category_id)
    if "custodian_user_id" in changes:
        await _check_custodian(session, user, changes["custodian_user_id"])
    if "name" in changes and changes["name"] is None:
        changes.pop("name")
    before: dict[str, Any] = {}
    for field, value in changes.items():
        if field == "description":
            value = (value or "").strip() or None
        if getattr(category, field) != value:
            before[field] = getattr(category, field)
            setattr(category, field, value)
    if not before:
        return category
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="asset_category",
        entity_id=category.id,
        summary=f"Changed the asset category {category.code}: {', '.join(sorted(before))}",
        changes={
            field: {"from": str(old) if old is not None else None,
                    "to": str(getattr(category, field)) if getattr(category, field) is not None
                    else None}
            for field, old in before.items()
        },
        request=request,
    )
    return category


# ═══════════════════════════════════════════════════════════════════════════
# Reads
# ═══════════════════════════════════════════════════════════════════════════


async def assignment_reads(
    session: AsyncSession, user: User, rows: list[AssetAssignment]
) -> list[dict[str, Any]]:
    if not rows:
        return []
    assets = {
        row.id: row
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(Asset).where(Asset.id.in_({row.asset_id for row in rows})), Asset, user
                )
            )
        ).all()
    }
    categories = {
        row.id: row
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(AssetCategory).where(
                        AssetCategory.id.in_({asset.category_id for asset in assets.values()})
                    ),
                    AssetCategory,
                    user,
                )
            )
        ).all()
    }
    people = await _employees(session, user, {row.employee_id for row in rows})
    names = await _user_names(
        session,
        user,
        {row.assigned_by_user_id for row in rows} | {row.returned_to_user_id for row in rows},
    )
    # Open hand-overs waiting on an exit clearance item: returning one signs it off.
    on_checklist = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(ClearanceItem.asset_assignment_id).where(
                        ClearanceItem.asset_assignment_id.in_([row.id for row in rows]),
                        ClearanceItem.status == ClearanceStatus.PENDING,
                    ),
                    ClearanceItem,
                    user,
                )
            )
        ).all()
    )
    now_day = today()
    out = []
    for row in rows:
        asset = assets[row.asset_id]
        category = categories.get(asset.category_id)
        person = people.get(row.employee_id)
        out.append(
            {
                "id": row.id,
                "asset_id": asset.id,
                "asset_tag": asset.asset_tag,
                "asset_name": asset.name,
                "serial_number": asset.serial_number,
                "category_name": category.name if category else "",
                "requires_acknowledgement": bool(category and category.requires_acknowledgement),
                "employee_id": row.employee_id,
                "employee_code": person.employee_code if person else None,
                "employee_name": person.full_name if person else None,
                "assigned_at": row.assigned_at,
                "assigned_by_name": names.get(row.assigned_by_user_id)
                if row.assigned_by_user_id
                else None,
                "expected_return_date": row.expected_return_date,
                "condition_out": row.condition_out,
                "assignment_notes": row.assignment_notes,
                "acknowledged_at": row.acknowledged_at,
                "returned_at": row.returned_at,
                "returned_to_name": names.get(row.returned_to_user_id)
                if row.returned_to_user_id
                else None,
                "condition_in": row.condition_in,
                "return_notes": row.return_notes,
                "recovery_amount": row.recovery_amount,
                "exit_record_id": row.exit_record_id,
                "exit_clearance_pending": row.id in on_checklist,
                "closed_at": row.closed_at,
                "outcome": row.outcome,
                "is_open": row.closed_at is None,
                "is_overdue": row.closed_at is None
                and row.expected_return_date is not None
                and row.expected_return_date < now_day,
                "return_reminder_sent_at": row.return_reminder_sent_at,
            }
        )
    return out


async def asset_reads(
    session: AsyncSession, user: User, assets: list[Asset]
) -> list[dict[str, Any]]:
    if not assets:
        return []
    categories = {
        row.id: row
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(AssetCategory).where(
                        AssetCategory.id.in_({asset.category_id for asset in assets})
                    ),
                    AssetCategory,
                    user,
                )
            )
        ).all()
    }
    location_ids = {asset.location_id for asset in assets if asset.location_id}
    locations: dict[uuid.UUID, str] = {}
    if location_ids:
        locations = {
            row_id: name
            for row_id, name in (
                await session.execute(
                    tenant_scoped(
                        select(Location.id, Location.name).where(Location.id.in_(location_ids)),
                        Location,
                        user,
                    )
                )
            ).all()
        }
    open_rows = (
        await session.scalars(
            tenant_scoped(
                select(AssetAssignment).where(
                    AssetAssignment.asset_id.in_([asset.id for asset in assets]),
                    AssetAssignment.closed_at.is_(None),
                ),
                AssetAssignment,
                user,
            )
        )
    ).all()
    current = {
        read["asset_id"]: read for read in await assignment_reads(session, user, list(open_rows))
    }
    on = today()
    out = []
    for asset in assets:
        category = categories[asset.category_id]
        value = book_value(
            purchase_cost=asset.purchase_cost,
            purchase_date=asset.purchase_date,
            depreciation_months=category.depreciation_months,
            as_of=on,
        )
        out.append(
            {
                "id": asset.id,
                "asset_tag": asset.asset_tag,
                "name": asset.name,
                "category_id": asset.category_id,
                "category_code": category.code,
                "category_name": category.name,
                "serial_number": asset.serial_number,
                "make": asset.make,
                "model": asset.model,
                "specs": asset.specs,
                "notes": asset.notes,
                "purchase_date": asset.purchase_date,
                "purchase_cost": asset.purchase_cost,
                "vendor": asset.vendor,
                "invoice_number": asset.invoice_number,
                "warranty_until": asset.warranty_until,
                "location_id": asset.location_id,
                "location_name": locations.get(asset.location_id) if asset.location_id else None,
                "status": asset.status,
                "condition": asset.condition,
                "status_reason": asset.status_reason,
                "status_changed_at": asset.status_changed_at,
                "depreciation_months": category.depreciation_months,
                "book_value": value.amount if value else None,
                "book_value_basis": value.basis if value else None,
                "current_assignment": current.get(asset.id),
                "created_at": asset.created_at,
                "updated_at": asset.updated_at,
            }
        )
    return out


def _filtered(
    user: User,
    *,
    asset_status: AssetStatus | None,
    category_id: uuid.UUID | None,
    location_id: uuid.UUID | None,
    q: str | None,
    employee_id: uuid.UUID | None,
):
    stmt = tenant_scoped(select(Asset), Asset, user)
    if asset_status is not None:
        stmt = stmt.where(Asset.status == asset_status)
    if category_id is not None:
        stmt = stmt.where(Asset.category_id == category_id)
    if location_id is not None:
        stmt = stmt.where(Asset.location_id == location_id)
    if q and q.strip():
        term = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Asset.asset_tag.ilike(term),
                Asset.serial_number.ilike(term),
                Asset.name.ilike(term),
                Asset.make.ilike(term),
                Asset.model.ilike(term),
            )
        )
    if employee_id is not None:
        held = tenant_scoped(
            select(AssetAssignment.asset_id).where(
                AssetAssignment.employee_id == employee_id, AssetAssignment.closed_at.is_(None)
            ),
            AssetAssignment,
            user,
        )
        stmt = stmt.where(Asset.id.in_(held))
    return stmt


async def list_assets(
    session: AsyncSession,
    user: User,
    *,
    asset_status: AssetStatus | None = None,
    category_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    q: str | None = None,
    employee_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[dict[str, Any]], int]:
    """One page of the register, with who holds each asset."""
    _require_hr(user)
    stmt = _filtered(
        user,
        asset_status=asset_status,
        category_id=category_id,
        location_id=location_id,
        q=q,
        employee_id=employee_id,
    )
    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = (
        await session.scalars(
            stmt.order_by(Asset.asset_tag).offset((page - 1) * page_size).limit(page_size)
        )
    ).all()
    return await asset_reads(session, user, list(rows)), total


async def asset_detail(session: AsyncSession, user: User, asset_id: uuid.UUID) -> dict[str, Any]:
    _require_hr(user)
    asset = await _asset(session, user, asset_id)
    read = (await asset_reads(session, user, [asset]))[0]
    history = (
        await session.scalars(
            tenant_scoped(
                select(AssetAssignment)
                .where(AssetAssignment.asset_id == asset.id)
                .order_by(AssetAssignment.assigned_at.desc()),
                AssetAssignment,
                user,
            )
        )
    ).all()
    read["history"] = await assignment_reads(session, user, list(history))
    return read


async def summary(session: AsyncSession, user: User) -> dict[str, Any]:
    """The figures on the register's header. Counted, never estimated."""
    _require_hr(user)
    by_status = {member.value: 0 for member in AssetStatus}
    for asset_status, count in (
        await session.execute(
            tenant_scoped(
                select(Asset.status, func.count()).group_by(Asset.status), Asset, user
            )
        )
    ).all():
        by_status[asset_status.value] = count

    on = today()
    open_rows = (
        await session.execute(
            tenant_scoped(
                select(AssetAssignment, AssetCategory.requires_acknowledgement)
                .join(Asset, Asset.id == AssetAssignment.asset_id)
                .join(AssetCategory, AssetCategory.id == Asset.category_id)
                .where(AssetAssignment.closed_at.is_(None)),
                AssetAssignment,
                user,
            )
        )
    ).all()
    overdue = sum(
        1
        for row, _ in open_rows
        if row.expected_return_date is not None and row.expected_return_date < on
    )
    unacknowledged = sum(
        1 for row, needs_ack in open_rows if needs_ack and row.acknowledged_at is None
    )

    request_counts = {
        request_status: count
        for request_status, count in (
            await session.execute(
                tenant_scoped(
                    select(AssetRequest.status, func.count())
                    .where(AssetRequest.status.in_(_OPEN_REQUEST))
                    .group_by(AssetRequest.status),
                    AssetRequest,
                    user,
                )
            )
        ).all()
    }

    valued = (
        await session.execute(
            tenant_scoped(
                select(Asset, AssetCategory.depreciation_months)
                .join(AssetCategory, AssetCategory.id == Asset.category_id)
                .where(Asset.status != AssetStatus.DISPOSED),
                Asset,
                user,
            )
        )
    ).all()
    cost_total = ZERO
    book_total = ZERO
    without_cost = 0
    for asset, months in valued:
        value = book_value(
            purchase_cost=asset.purchase_cost,
            purchase_date=asset.purchase_date,
            depreciation_months=months,
            as_of=on,
        )
        if value is None:
            without_cost += 1
            continue
        cost_total += Decimal(str(asset.purchase_cost))
        book_total += value.amount

    return {
        "total": sum(count for key, count in by_status.items() if key != "disposed"),
        "by_status": by_status,
        "assigned": by_status["assigned"],
        "in_stock": by_status["in_stock"],
        "in_repair": by_status["in_repair"],
        "lost": by_status["lost"],
        "overdue_returns": overdue,
        "unacknowledged": unacknowledged,
        "pending_requests": request_counts.get(AssetRequestStatus.PENDING, 0),
        "approved_awaiting_fulfilment": request_counts.get(AssetRequestStatus.APPROVED, 0),
        "purchase_cost_total": cost_total.quantize(Decimal("0.01")),
        "book_value_total": book_total.quantize(Decimal("0.01")),
        "assets_without_cost": without_cost,
    }


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

EXPORT_COLUMNS = (
    "asset_tag",
    "name",
    "category",
    "serial_number",
    "make",
    "model",
    "status",
    "condition",
    "location",
    "held_by_code",
    "held_by_name",
    "assigned_on",
    "expected_return_date",
    "purchase_date",
    "purchase_cost",
    "book_value",
    "vendor",
    "invoice_number",
    "warranty_until",
)


def _cell(value: Any) -> str:
    """A cell a spreadsheet shows rather than runs."""
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_PREFIXES) and not re.fullmatch(r"-\d+(\.\d+)?", text):
        return "'" + text
    return text


async def export_csv(
    session: AsyncSession,
    user: User,
    *,
    asset_status: AssetStatus | None = None,
    category_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    q: str | None = None,
    employee_id: uuid.UUID | None = None,
    request: Request | None = None,
) -> str:
    """The register as CSV, with the same filters as the screen. Recorded as an export."""
    _require_hr(user)
    stmt = _filtered(
        user,
        asset_status=asset_status,
        category_id=category_id,
        location_id=location_id,
        q=q,
        employee_id=employee_id,
    )
    assets = list((await session.scalars(stmt.order_by(Asset.asset_tag))).all())
    reads = await asset_reads(session, user, assets)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(EXPORT_COLUMNS)
    for row in reads:
        held = row["current_assignment"]
        writer.writerow(
            [
                _cell(value)
                for value in (
                    row["asset_tag"],
                    row["name"],
                    row["category_name"],
                    row["serial_number"],
                    row["make"],
                    row["model"],
                    row["status"].value,
                    row["condition"].value,
                    row["location_name"],
                    held["employee_code"] if held else None,
                    held["employee_name"] if held else None,
                    held["assigned_at"].astimezone(IST).date().isoformat() if held else None,
                    held["expected_return_date"] if held else None,
                    row["purchase_date"],
                    row["purchase_cost"],
                    row["book_value"],
                    row["vendor"],
                    row["invoice_number"],
                    row["warranty_until"],
                )
            ]
        )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.EXPORT,
        entity_type="asset_register",
        summary=f"Exported the asset register ({len(reads)} asset(s)) as CSV",
        changes={
            "rows": len(reads),
            "status": asset_status.value if asset_status else None,
            "category_id": str(category_id) if category_id else None,
            "location_id": str(location_id) if location_id else None,
            "q": q,
            "employee_id": str(employee_id) if employee_id else None,
        },
        request=request,
    )
    return buffer.getvalue()


# ═══════════════════════════════════════════════════════════════════════════
# The register
# ═══════════════════════════════════════════════════════════════════════════


async def _check_location(session: AsyncSession, user: User, location_id: uuid.UUID | None) -> None:
    if location_id is None:
        return
    found = await session.scalar(
        tenant_scoped(select(Location.id).where(Location.id == location_id), Location, user)
    )
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Location not found"
        )


async def create_asset(
    session: AsyncSession, user: User, payload: dict[str, Any], *, request: Request | None = None
) -> Asset:
    _require_hr(user)
    category = await _category(session, user, payload["category_id"])
    if not category.active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"The category {category.name} is inactive. Reactivate it to add assets.",
        )
    await _check_location(session, user, payload.get("location_id"))
    # Tags are compared as printed on the label: "lt-001" and "LT-001" are one asset.
    tag = payload["asset_tag"] = payload["asset_tag"].strip().upper()
    duplicate = await session.scalar(
        tenant_scoped(select(Asset.id).where(Asset.asset_tag == tag), Asset, user)
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"The asset tag {tag} is already in the register.",
        )
    asset = Asset(
        tenant_id=user.tenant_id,
        status=AssetStatus.IN_STOCK,
        created_by_user_id=user.id,
        status_changed_at=datetime.now(UTC),
        **payload,
    )
    session.add(asset)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="asset",
        entity_id=asset.id,
        summary=f"Added {tag} ({asset.name}) to the asset register",
        changes={
            "asset_tag": tag,
            "category": category.code,
            "serial_number": asset.serial_number,
            "purchase_cost": str(asset.purchase_cost) if asset.purchase_cost is not None else None,
        },
        request=request,
    )
    return asset


async def update_asset(
    session: AsyncSession,
    user: User,
    asset_id: uuid.UUID,
    changes: dict[str, Any],
    *,
    request: Request | None = None,
) -> Asset:
    _require_hr(user)
    asset = await _asset(session, user, asset_id)
    if asset.status is AssetStatus.DISPOSED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A disposed asset's record is kept as it was. It cannot be edited.",
        )
    if changes.get("category_id") is not None and changes["category_id"] != asset.category_id:
        await _category(session, user, changes["category_id"])
    if "location_id" in changes:
        await _check_location(session, user, changes["location_id"])
    for required in ("category_id", "name", "condition"):
        if required in changes and changes[required] is None:
            changes.pop(required)
    before: dict[str, Any] = {}
    for field, value in changes.items():
        if getattr(asset, field) != value:
            before[field] = getattr(asset, field)
            setattr(asset, field, value)
    if not before:
        return asset
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="asset",
        entity_id=asset.id,
        summary=f"Edited {asset.asset_tag}: {', '.join(sorted(before))}",
        changes={
            field: {
                "from": None if old is None else str(getattr(old, "value", old)),
                "to": None
                if getattr(asset, field) is None
                else str(getattr(getattr(asset, field), "value", getattr(asset, field))),
            }
            for field, old in before.items()
        },
        request=request,
    )
    return asset


async def change_status(
    session: AsyncSession,
    user: User,
    asset_id: uuid.UUID,
    *,
    target: AssetStatus,
    reason: str,
    recovery_amount: Decimal | None = None,
    request: Request | None = None,
) -> Asset:
    """Repair, loss, retirement, disposal — each with the reason it happened."""
    _require_hr(user)
    reason = reason.strip()
    if not reason:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A status change needs a reason. The next person to look will ask why.",
        )
    asset = await _asset(session, user, asset_id, lock=True)
    if target is asset.status:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{asset.asset_tag} is already {_humanise(target.value)}.",
        )
    if target not in _TRANSITIONS[asset.status]:
        hint = (
            " Take it back from its holder first."
            if asset.status is AssetStatus.ASSIGNED
            else ""
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{asset.asset_tag} is {_humanise(asset.status.value)} and cannot be marked "
                f"{_humanise(target.value)}.{hint}"
            ),
        )
    recovery = recovery_amount or ZERO
    holding: AssetAssignment | None = None
    if asset.status is AssetStatus.ASSIGNED:
        holding = await _open_assignment(session, user, asset.id)
    if recovery > ZERO and holding is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A recovery amount applies only to an asset lost while somebody held it.",
        )

    now = datetime.now(UTC)
    previous = asset.status
    if holding is not None:
        _not_yourself(user, holding.employee_id, "report lost")
        holding.closed_at = now
        holding.outcome = AssignmentOutcome.LOST
        holding.return_notes = reason
        holding.recovery_amount = recovery if recovery > ZERO else None
    asset.status = target
    asset.status_reason = reason
    asset.status_changed_at = now
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="asset",
        entity_id=asset.id,
        summary=(
            f"{asset.asset_tag} moved from {_humanise(previous.value)} to "
            f"{_humanise(target.value)}: {reason}"
        ),
        changes={
            "from": previous.value,
            "to": target.value,
            "reason": reason,
            "assignment_closed": str(holding.id) if holding else None,
            "recovery_amount": str(recovery) if recovery > ZERO else None,
        },
        request=request,
    )
    if holding is not None and recovery > ZERO:
        await sync_exit_clearance(session, user, holding, request=request)
    return asset


# ═══════════════════════════════════════════════════════════════════════════
# Hand-over, acknowledgement, return
# ═══════════════════════════════════════════════════════════════════════════


async def assign_asset(
    session: AsyncSession,
    user: User,
    asset_id: uuid.UUID,
    *,
    employee_id: uuid.UUID,
    expected_return_date: date | None = None,
    condition_out: AssetCondition | None = None,
    notes: str | None = None,
    request: Request | None = None,
) -> AssetAssignment:
    """Hand an in-stock asset to somebody. They are told, and asked to acknowledge."""
    _require_hr(user)
    _not_yourself(user, employee_id, "hand over")
    asset = await _asset(session, user, asset_id, lock=True)
    if asset.status is not AssetStatus.IN_STOCK:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{asset.asset_tag} is {_humanise(asset.status.value)}. Only an asset in stock "
                "can be handed over."
            ),
        )
    employee = await _employee(session, user, employee_id)
    if employee.status is EmploymentStatus.EXITED or not employee.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{employee.employee_code} has left. Assets go to current employees.",
        )
    on = today()
    if expected_return_date is not None and expected_return_date < on:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The expected return date is in the past.",
        )
    category = await _category(session, user, asset.category_id)
    now = datetime.now(UTC)
    assignment = AssetAssignment(
        tenant_id=user.tenant_id,
        asset_id=asset.id,
        employee_id=employee.id,
        assigned_at=now,
        assigned_by_user_id=user.id,
        expected_return_date=expected_return_date,
        condition_out=condition_out or asset.condition,
        assignment_notes=(notes or "").strip() or None,
    )
    session.add(assignment)
    asset.status = AssetStatus.ASSIGNED
    asset.status_reason = None
    asset.status_changed_at = now
    if condition_out is not None:
        asset.condition = condition_out
    await session.flush()

    # Handed over during somebody's notice period: it goes on their checklist too.
    exit_id = await exit_hooks.open_exit_for(session, user, employee.id)
    if exit_id is not None:
        await exit_hooks.clearance_item_for(session, user, exit_id, assignment, asset, category)
        await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="asset_assignment",
        entity_id=assignment.id,
        summary=f"Handed {asset.asset_tag} ({asset.name}) to {employee.employee_code}",
        changes={
            "asset_tag": asset.asset_tag,
            "employee_code": employee.employee_code,
            "condition_out": assignment.condition_out.value,
            "expected_return_date": expected_return_date.isoformat()
            if expected_return_date
            else None,
            "added_to_exit_checklist": str(exit_id) if exit_id else None,
        },
        request=request,
    )
    recipients = await _user_ids_for_employees(session, user, {employee.id})
    if recipients:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key=EVENT_ASSIGNED,
            recipient_user_ids=recipients,
            payload={
                "asset_name": asset.name,
                "asset_tag": asset.asset_tag,
                "assigned_on": on.strftime("%d %b %Y"),
                "assigned_by": user.full_name,
                "acknowledge_line": (
                    "Please acknowledge receipt under My assets."
                    if category.requires_acknowledgement
                    else ""
                ),
                "link": "/assets",
            },
            commit=False,
        )
    return assignment


async def acknowledge(
    session: AsyncSession, user: User, assignment_id: uuid.UUID, *, request: Request | None = None
) -> AssetAssignment:
    """The holder confirms they have it. Only the holder can."""
    assignment = await _assignment(session, user, assignment_id)
    permissions.require_self(
        user,
        assignment.employee_id,
        detail="Only the person holding an asset can acknowledge receiving it.",
    )
    if assignment.closed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This asset has already been returned.",
        )
    if assignment.acknowledged_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You have already acknowledged this asset.",
        )
    asset = await _asset(session, user, assignment.asset_id)
    assignment.acknowledged_at = datetime.now(UTC)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="asset_assignment",
        entity_id=assignment.id,
        summary=f"Acknowledged receiving {asset.asset_tag} ({asset.name})",
        changes={"acknowledged_at": assignment.acknowledged_at.isoformat()},
        request=request,
    )
    return assignment


async def return_asset(
    session: AsyncSession,
    user: User,
    assignment_id: uuid.UUID,
    *,
    condition_in: AssetCondition,
    notes: str | None = None,
    recovery_amount: Decimal | None = None,
    next_status: AssetStatus = AssetStatus.IN_STOCK,
    request: Request | None = None,
) -> AssetAssignment:
    """Take an asset back, in the condition it came back in."""
    _require_hr(user)
    assignment = await _assignment(session, user, assignment_id)
    _not_yourself(user, assignment.employee_id, "receive back")
    if assignment.closed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This assignment is already closed. The next holder is a new assignment.",
        )
    if next_status not in (AssetStatus.IN_STOCK, AssetStatus.IN_REPAIR):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A returned asset goes back in stock or in for repair.",
        )
    recovery = recovery_amount or ZERO
    if recovery > ZERO and condition_in is not AssetCondition.DAMAGED:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A recovery applies only to an asset returned damaged.",
        )
    asset = await _asset(session, user, assignment.asset_id, lock=True)
    now = datetime.now(UTC)
    assignment.returned_at = now
    assignment.returned_to_user_id = user.id
    assignment.condition_in = condition_in
    assignment.return_notes = (notes or "").strip() or None
    assignment.recovery_amount = recovery if recovery > ZERO else None
    assignment.closed_at = now
    assignment.outcome = AssignmentOutcome.RETURNED
    asset.status = next_status
    asset.condition = condition_in
    asset.status_reason = assignment.return_notes if next_status is AssetStatus.IN_REPAIR else None
    asset.status_changed_at = now
    await session.flush()
    holder = (await _employees(session, user, {assignment.employee_id})).get(
        assignment.employee_id
    )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="asset_assignment",
        entity_id=assignment.id,
        summary=(
            f"{asset.asset_tag} returned by {holder.employee_code if holder else 'its holder'} in "
            f"{condition_in.value} condition"
            + (f", {recovery} to recover" if recovery > ZERO else "")
        ),
        changes={
            "condition_in": condition_in.value,
            "next_status": next_status.value,
            "recovery_amount": str(recovery) if recovery > ZERO else None,
        },
        request=request,
    )
    await sync_exit_clearance(session, user, assignment, request=request)
    return assignment


async def sync_exit_clearance(
    session: AsyncSession,
    user: User,
    assignment: AssetAssignment,
    *,
    request: Request | None = None,
) -> int:
    """Sign off the exit clearance items waiting on an assignment that just closed.

    Through the exit workflow's own `clear_item`, so its permission rule and
    audit apply. Returned → cleared (with the recovery, if any); lost with a
    recovery → cleared with recovery. Lost with nothing to recover is left
    pending: whether to recover its book value or waive it is a clearance
    decision, not one the register makes. An item the caller may not sign off
    stays pending for its owner.
    """
    from app.services.exit_workflow import service as exit_service

    items = (
        await session.scalars(
            tenant_scoped(
                select(ClearanceItem).where(
                    ClearanceItem.asset_assignment_id == assignment.id,
                    ClearanceItem.status == ClearanceStatus.PENDING,
                ),
                ClearanceItem,
                user,
            )
        )
    ).all()
    recovery = Decimal(str(assignment.recovery_amount or 0))
    signed = 0
    for item in items:
        if recovery > ZERO:
            outcome = ClearanceStatus.CLEARED_WITH_RECOVERY
        elif assignment.outcome is AssignmentOutcome.RETURNED:
            outcome = ClearanceStatus.CLEARED
        else:
            continue
        try:
            await exit_service.clear_item(
                session,
                user,
                item_id=item.id,
                outcome=outcome,
                recovery_amount=recovery,
                notes=(
                    "Returned through the asset register."
                    if assignment.outcome is AssignmentOutcome.RETURNED
                    else "Reported lost in the asset register."
                ),
                request=request,
            )
        except HTTPException:
            continue
        signed += 1
    return signed


async def my_assets(session: AsyncSession, user: User) -> dict[str, list[dict[str, Any]]]:
    """What I hold now, and what I held before."""
    employee_id = permissions.own_employee_id(user)
    rows = (
        await session.scalars(
            tenant_scoped(
                select(AssetAssignment)
                .where(AssetAssignment.employee_id == employee_id)
                .order_by(AssetAssignment.assigned_at.desc()),
                AssetAssignment,
                user,
            )
        )
    ).all()
    reads = await assignment_reads(session, user, list(rows))
    return {
        "current": [row for row in reads if row["is_open"]],
        "history": [row for row in reads if not row["is_open"]],
    }


async def employee_assets(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> list[dict[str, Any]]:
    """One person's assets, current first: for HR, their line manager, or themselves."""
    visible = await permissions.visible_employee_ids(session, user)
    if visible is not None and employee_id not in visible:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    rows = (
        await session.scalars(
            tenant_scoped(
                select(AssetAssignment)
                .where(AssetAssignment.employee_id == employee_id)
                .order_by(AssetAssignment.closed_at.desc().nulls_first(),
                          AssetAssignment.assigned_at.desc()),
                AssetAssignment,
                user,
            )
        )
    ).all()
    return await assignment_reads(session, user, list(rows))


# ── Return reminders ────────────────────────────────────────────────────────


async def _remind(
    session: AsyncSession, user: User, rows: list[tuple[AssetAssignment, Asset]]
) -> int:
    sent = 0
    now = datetime.now(UTC)
    for assignment, asset in rows:
        recipients = await _user_ids_for_employees(session, user, {assignment.employee_id})
        if not recipients or assignment.expected_return_date is None:
            continue
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key=EVENT_RETURN_DUE,
            recipient_user_ids=recipients,
            payload={
                "asset_name": asset.name,
                "asset_tag": asset.asset_tag,
                "due_date": assignment.expected_return_date.strftime("%d %b %Y"),
                "link": "/assets",
            },
            commit=False,
        )
        assignment.return_reminder_sent_at = now
        sent += 1
    await session.flush()
    return sent


async def remind_return(
    session: AsyncSession, user: User, assignment_id: uuid.UUID, *, request: Request | None = None
) -> int:
    """HR nudges one holder about a return date, now."""
    _require_hr(user)
    assignment = await _assignment(session, user, assignment_id)
    if assignment.closed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This asset has already come back."
        )
    if assignment.expected_return_date is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This hand-over has no expected return date to remind anybody of.",
        )
    asset = await _asset(session, user, assignment.asset_id)
    sent = await _remind(session, user, [(assignment, asset)])
    if not sent:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The holder has no active login to remind.",
        )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="asset_assignment",
        entity_id=assignment.id,
        summary=f"Reminded the holder of {asset.asset_tag} that it is due back",
        changes={"due": assignment.expected_return_date.isoformat()},
        request=request,
    )
    return sent


async def send_return_due_reminders(
    session: AsyncSession, user: User, *, on: date, within_days: int = 3
) -> dict[str, int]:
    """The daily sweep: one reminder per hand-over due within a few days or overdue.

    Once per due date: a hand-over already reminded inside its due window is
    skipped, while one HR nudged months early is still reminded when it falls due.
    """
    horizon = on + timedelta(days=within_days)
    candidates = (
        await session.execute(
            tenant_scoped(
                select(AssetAssignment, Asset)
                .join(Asset, Asset.id == AssetAssignment.asset_id)
                .where(
                    AssetAssignment.closed_at.is_(None),
                    AssetAssignment.expected_return_date.is_not(None),
                    AssetAssignment.expected_return_date <= horizon,
                ),
                AssetAssignment,
                user,
            )
        )
    ).all()
    rows = [
        (assignment, asset)
        for assignment, asset in candidates
        if assignment.return_reminder_sent_at is None
        or assignment.return_reminder_sent_at.astimezone(IST).date()
        < assignment.expected_return_date - timedelta(days=within_days)
    ]
    sent = await _remind(session, user, rows)
    if sent:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            actor=user,
            actor_type="system",
            action=AuditAction.UPDATE,
            entity_type="asset_register",
            summary=f"Sent {sent} asset return reminder(s) for returns due by {horizon}",
            changes={"reminded": sent, "due_by": horizon.isoformat()},
        )
    return {"due": len(rows), "reminded": sent}


# ═══════════════════════════════════════════════════════════════════════════
# Requests
# ═══════════════════════════════════════════════════════════════════════════


async def _asked(session: AsyncSession, user: User, request_id: uuid.UUID) -> AssetRequest:
    row = await session.scalar(
        tenant_scoped(select(AssetRequest).where(AssetRequest.id == request_id), AssetRequest, user)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return row


async def request_reads(
    session: AsyncSession, user: User, rows: list[AssetRequest]
) -> list[dict[str, Any]]:
    if not rows:
        return []
    people = await _employees(session, user, {row.employee_id for row in rows})
    categories = {
        row.id: row
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(AssetCategory).where(
                        AssetCategory.id.in_({row.category_id for row in rows})
                    ),
                    AssetCategory,
                    user,
                )
            )
        ).all()
    }
    asset_ids = {row.fulfilled_asset_id for row in rows if row.fulfilled_asset_id}
    tags: dict[uuid.UUID, str] = {}
    if asset_ids:
        tags = {
            asset_id: tag
            for asset_id, tag in (
                await session.execute(
                    tenant_scoped(
                        select(Asset.id, Asset.asset_tag).where(Asset.id.in_(asset_ids)),
                        Asset,
                        user,
                    )
                )
            ).all()
        }
    names = await _user_names(session, user, {row.decided_by_user_id for row in rows})
    is_hr = permissions.is_hr(user)
    line: set[uuid.UUID] = set()
    if not is_hr and user.role is UserRole.MANAGER:
        line = await permissions.reporting_line(session, user)
    out = []
    for row in rows:
        own = user.employee_id is not None and row.employee_id == user.employee_id
        person = people.get(row.employee_id)
        category = categories.get(row.category_id)
        out.append(
            {
                "id": row.id,
                "employee_id": row.employee_id,
                "employee_code": person.employee_code if person else None,
                "employee_name": person.full_name if person else None,
                "category_id": row.category_id,
                "category_name": category.name if category else "",
                "reason": row.reason,
                "needed_by": row.needed_by,
                "status": row.status,
                "created_at": row.created_at,
                "decided_by_name": names.get(row.decided_by_user_id)
                if row.decided_by_user_id
                else None,
                "decided_at": row.decided_at,
                "decision_note": row.decision_note,
                "fulfilled_asset_id": row.fulfilled_asset_id,
                "fulfilled_asset_tag": tags.get(row.fulfilled_asset_id)
                if row.fulfilled_asset_id
                else None,
                "fulfilled_at": row.fulfilled_at,
                "can_decide": row.status is AssetRequestStatus.PENDING
                and not own
                and (is_hr or row.employee_id in line),
                "can_fulfil": row.status in _OPEN_REQUEST and not own and is_hr,
                "can_cancel": own and row.status in _OPEN_REQUEST,
            }
        )
    return out


async def list_requests(
    session: AsyncSession,
    user: User,
    *,
    scope: str = "mine",
    request_status: AssetRequestStatus | None = None,
) -> list[dict[str, Any]]:
    """`mine`: what I asked for. `review`: what I may decide or fulfil — never my own."""
    stmt = select(AssetRequest).order_by(AssetRequest.created_at.desc())
    if scope == "mine":
        stmt = stmt.where(AssetRequest.employee_id == permissions.own_employee_id(user))
    else:
        permissions.require_role(
            user,
            permissions.MANAGER_ROLES,
            detail="Only line managers and HR review asset requests.",
        )
        visible = await permissions.visible_employee_ids(session, user)
        if visible is not None:
            team = visible - ({user.employee_id} if user.employee_id else set())
            if not team:
                return []
            stmt = stmt.where(AssetRequest.employee_id.in_(team))
        if user.employee_id is not None:
            stmt = stmt.where(AssetRequest.employee_id != user.employee_id)
    if request_status is not None:
        stmt = stmt.where(AssetRequest.status == request_status)
    rows = (await session.scalars(tenant_scoped(stmt, AssetRequest, user))).all()
    return await request_reads(session, user, list(rows))


async def create_request(
    session: AsyncSession,
    user: User,
    *,
    category_id: uuid.UUID,
    reason: str,
    needed_by: date | None,
    request: Request | None = None,
) -> AssetRequest:
    """Ask for an asset. Your line manager and the category's custodian are told."""
    employee_id = permissions.own_employee_id(user)
    category = await _category(session, user, category_id)
    if not category.active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{category.name} is not offered any more. Pick another category.",
        )
    if needed_by is not None and needed_by < today():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The needed-by date is in the past.",
        )
    reason = reason.strip()
    if not reason:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Say what you need it for — that is what your approver decides on.",
        )
    duplicate = await session.scalar(
        tenant_scoped(
            select(AssetRequest.id).where(
                AssetRequest.employee_id == employee_id,
                AssetRequest.category_id == category.id,
                AssetRequest.status.in_(_OPEN_REQUEST),
            ),
            AssetRequest,
            user,
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"You already have an open request for a {category.name}.",
        )
    employee = await _employee(session, user, employee_id)
    asked = AssetRequest(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        category_id=category.id,
        reason=reason,
        needed_by=needed_by,
        requested_by_user_id=user.id,
    )
    session.add(asked)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="asset_request",
        entity_id=asked.id,
        summary=f"{employee.employee_code} asked for a {category.name}",
        changes={
            "category": category.code,
            "needed_by": needed_by.isoformat() if needed_by else None,
        },
        request=request,
    )
    recipients = set(await _user_ids_for_employees(session, user, {employee.manager_id}))
    if category.custodian_user_id:
        recipients.add(category.custodian_user_id)
    if not recipients:
        recipients = set(await _hr_user_ids(session, user))
    recipients.discard(user.id)
    if recipients:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key=EVENT_REQUEST_SUBMITTED,
            recipient_user_ids=sorted(recipients, key=str),
            payload={
                "employee_name": employee.full_name,
                "category_name": category.name,
                "needed_by_line": f" by {needed_by.strftime('%d %b %Y')}" if needed_by else "",
                "reason": reason,
                "link": "/assets/requests",
            },
            commit=False,
        )
    return asked


async def cancel_request(
    session: AsyncSession, user: User, request_id: uuid.UUID, *, request: Request | None = None
) -> AssetRequest:
    asked = await _asked(session, user, request_id)
    permissions.require_self(
        user, asked.employee_id, detail="Only the person who asked can withdraw a request."
    )
    if asked.status not in _OPEN_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This request is already {asked.status.value}.",
        )
    previous = asked.status
    asked.status = AssetRequestStatus.CANCELLED
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="asset_request",
        entity_id=asked.id,
        summary="Withdrew an asset request",
        changes={"from": previous.value, "to": AssetRequestStatus.CANCELLED.value},
        request=request,
    )
    return asked


async def _tell_requester(
    session: AsyncSession, user: User, asked: AssetRequest, *, decision: str, note: str | None
) -> None:
    recipients = await _user_ids_for_employees(session, user, {asked.employee_id})
    if not recipients:
        return
    category = await _category(session, user, asked.category_id)
    await notify(
        session,
        tenant_id=user.tenant_id,
        event_key=EVENT_REQUEST_DECIDED,
        recipient_user_ids=recipients,
        payload={
            "category_name": category.name,
            "decision": decision,
            "decided_by": user.full_name,
            "note_line": f" Note: {note}" if note else "",
            "link": "/assets",
        },
        commit=False,
    )


async def decide_request(
    session: AsyncSession,
    user: User,
    request_id: uuid.UUID,
    *,
    approve: bool,
    note: str | None,
    request: Request | None = None,
) -> AssetRequest:
    """Approve or reject: the requester's line manager or HR, never the requester."""
    asked = await _asked(session, user, request_id)
    await permissions.require_manager_or_hr(
        session,
        user,
        asked.employee_id,
        detail="Only the requester's line manager or HR can decide this request.",
    )
    if asked.status is not AssetRequestStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This request is already {asked.status.value}.",
        )
    note = (note or "").strip() or None
    if not approve and not note:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A rejection needs a reason. The person who asked will read it.",
        )
    asked.status = AssetRequestStatus.APPROVED if approve else AssetRequestStatus.REJECTED
    asked.decided_by_user_id = user.id
    asked.decided_at = datetime.now(UTC)
    asked.decision_note = note
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="asset_request",
        entity_id=asked.id,
        summary=f"{'Approved' if approve else 'Rejected'} an asset request"
        + (f": {note}" if note else ""),
        changes={"status": asked.status.value, "note": note},
        request=request,
    )
    await _tell_requester(
        session, user, asked, decision="approved" if approve else "rejected", note=note
    )
    return asked


async def fulfil_request(
    session: AsyncSession,
    user: User,
    request_id: uuid.UUID,
    *,
    asset_id: uuid.UUID,
    expected_return_date: date | None = None,
    condition_out: AssetCondition | None = None,
    notes: str | None = None,
    request: Request | None = None,
) -> AssetRequest:
    """Hand over an in-stock asset of the requested category, closing the request.

    HR may fulfil a request still pending — handing the asset over is then the
    approval, and it is recorded as one.
    """
    _require_hr(user)
    asked = await _asked(session, user, request_id)
    if user.employee_id is not None and asked.employee_id == user.employee_id:
        raise permissions.forbidden("This is your own request. Somebody else in HR fulfils it.")
    if asked.status not in _OPEN_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This request is already {asked.status.value}.",
        )
    asset = await _asset(session, user, asset_id)
    if asset.category_id != asked.category_id:
        category = await _category(session, user, asked.category_id)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{asset.asset_tag} is not a {category.name}, which is what was asked for.",
        )
    assignment = await assign_asset(
        session,
        user,
        asset.id,
        employee_id=asked.employee_id,
        expected_return_date=expected_return_date,
        condition_out=condition_out,
        notes=notes,
        request=request,
    )
    now = datetime.now(UTC)
    was_pending = asked.status is AssetRequestStatus.PENDING
    if was_pending:
        asked.decided_by_user_id = user.id
        asked.decided_at = now
    asked.status = AssetRequestStatus.FULFILLED
    asked.fulfilled_asset_id = asset.id
    asked.fulfilled_by_user_id = user.id
    asked.fulfilled_at = now
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE,
        entity_type="asset_request",
        entity_id=asked.id,
        summary=(
            f"Fulfilled an asset request with {asset.asset_tag}"
            + (" (approved in the same step)" if was_pending else "")
        ),
        changes={"asset_tag": asset.asset_tag, "assignment_id": str(assignment.id)},
        request=request,
    )
    return asked


async def approvals_inbox(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Pending asset requests the caller may decide, for the unified inbox.

    The same rule as `decide_request`: HR sees everybody's, a manager their
    reporting line's, and nobody their own.
    """
    stmt = select(AssetRequest, Employee, AssetCategory).join(
        Employee, Employee.id == AssetRequest.employee_id
    ).join(AssetCategory, AssetCategory.id == AssetRequest.category_id).where(
        AssetRequest.status == AssetRequestStatus.PENDING
    )
    if not permissions.is_hr(user):
        line = await permissions.reporting_line(session, user)
        if not line:
            return []
        stmt = stmt.where(AssetRequest.employee_id.in_(line))
    if user.employee_id is not None:
        stmt = stmt.where(AssetRequest.employee_id != user.employee_id)
    rows = (
        await session.execute(
            tenant_scoped(stmt, AssetRequest, user).order_by(AssetRequest.created_at)
        )
    ).all()
    return [
        {
            "id": asked.id,
            "title": f"{category.name} request",
            "subject_name": person.full_name,
            "detail": asked.reason[:120],
            "submitted_at": asked.created_at,
            "due_on": asked.needed_by,
        }
        for asked, person, category in rows
    ]
