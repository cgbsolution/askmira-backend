"""The rewards catalogue: items with a points price, and redeeming one.

Redemption goes through `service.redeem_points`, so the points are valued and
the perquisite is recorded exactly as for any redemption. The item's stock is
taken under a row lock in the same transaction.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.reward_catalogue import RewardCatalogueItem
from app.models.user import User
from app.services import audit
from app.services.rewards import service as rewards

__all__ = [
    "CatalogueItemIn",
    "CatalogueItemUpdate",
    "create_item",
    "has_catalogue",
    "list_items",
    "redeem_item",
    "update_item",
]


class CatalogueItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    name: str = Field(min_length=2, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=64)
    points_cost: int = Field(ge=1, le=10_000_000)
    stock: int | None = Field(default=None, ge=0, le=1_000_000)


class CatalogueItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=64)
    points_cost: int | None = Field(default=None, ge=1, le=10_000_000)
    stock: int | None = Field(default=None, ge=0, le=1_000_000)
    unlimited_stock: bool = False
    active: bool | None = None


def _read(row: RewardCatalogueItem) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "sku": row.sku,
        "name": row.name,
        "description": row.description,
        "category": row.category,
        "points_cost": row.points_cost,
        "stock": row.stock,
        "in_stock": row.stock is None or row.stock > 0,
        "active": row.active,
    }


async def has_catalogue(session: AsyncSession, user: User) -> bool:
    count = await session.scalar(
        tenant_scoped(
            select(func.count())
            .select_from(RewardCatalogueItem)
            .where(RewardCatalogueItem.active.is_(True)),
            RewardCatalogueItem,
            user,
        )
    )
    return bool(count)


async def list_items(
    session: AsyncSession, user: User, *, include_inactive: bool = False
) -> list[dict[str, Any]]:
    stmt = select(RewardCatalogueItem).order_by(
        RewardCatalogueItem.category, RewardCatalogueItem.points_cost
    )
    if not (include_inactive and permissions.is_hr(user)):
        stmt = stmt.where(RewardCatalogueItem.active.is_(True))
    rows = (await session.scalars(tenant_scoped(stmt, RewardCatalogueItem, user))).all()
    return [_read(r) for r in rows]


async def create_item(
    session: AsyncSession, user: User, payload: CatalogueItemIn, *, request: Request | None
) -> dict[str, Any]:
    taken = await session.scalar(
        tenant_scoped(
            select(RewardCatalogueItem.id).where(RewardCatalogueItem.sku == payload.sku),
            RewardCatalogueItem,
            user,
        )
    )
    if taken is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"SKU {payload.sku} already exists."
        )
    row = RewardCatalogueItem(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="reward_catalogue_item",
        entity_id=row.id,
        actor=user,
        changes=_read(row),
        summary=f"Added {row.name} to the rewards catalogue at {row.points_cost} points",
        request=request,
    )
    await session.commit()
    return _read(row)


async def update_item(
    session: AsyncSession,
    user: User,
    item_id: uuid.UUID,
    payload: CatalogueItemUpdate,
    *,
    request: Request | None,
) -> dict[str, Any]:
    row = await session.scalar(
        tenant_scoped(
            select(RewardCatalogueItem).where(RewardCatalogueItem.id == item_id),
            RewardCatalogueItem,
            user,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    before = _read(row)
    changes = payload.model_dump(exclude_unset=True)
    unlimited = changes.pop("unlimited_stock", False)
    for key, value in changes.items():
        if value is None and key in ("name", "points_cost", "active"):
            continue
        setattr(row, key, value)
    if unlimited:
        row.stock = None
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="reward_catalogue_item",
        entity_id=row.id,
        actor=user,
        changes={"before": before, "after": _read(row)},
        summary=f"Changed the catalogue item {row.name}",
        request=request,
    )
    await session.commit()
    return _read(row)


async def redeem_item(
    session: AsyncSession, user: User, item_id: uuid.UUID, *, request: Request | None
) -> dict[str, Any]:
    """Spend the item's points price on it, for the caller."""
    permissions.own_employee_id(user)
    row = await session.scalar(
        tenant_scoped(
            select(RewardCatalogueItem)
            .where(RewardCatalogueItem.id == item_id, RewardCatalogueItem.active.is_(True))
            .with_for_update(),
            RewardCatalogueItem,
            user,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    if row.stock is not None:
        if row.stock <= 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=f"{row.name} is out of stock."
            )
        row.stock -= 1
    # Commits with the stock change; a refusal (too few points) puts it back.
    try:
        result = await rewards.redeem_points(
            session,
            user,
            employee_id=user.employee_id,
            points=row.points_cost,
            requested_on=date.today(),
            item_sku=row.sku,
            item_description=row.name,
            request=request,
        )
    except HTTPException:
        await session.rollback()
        raise
    return {**result, "item": _read(row)}
