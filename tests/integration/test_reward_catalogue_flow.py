"""Spending recognition points on catalogue items, with stock."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee
from app.models.reward_catalogue import RewardCatalogueItem
from app.models.rewards import PointsRedemption
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.rewards import catalogue
from app.services.rewards import service as rewards
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db


async def _login(session: AsyncSession, tenant: Tenant, employee: Employee) -> User:
    user = User(
        tenant_id=tenant.id,
        email="anita.rw@acme-demo.com",
        password_hash="x",
        full_name="Anita",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(user)
    await session.commit()
    return await as_caller(session, user)


async def test_redeeming_an_item_spends_its_price_and_takes_stock(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await rewards.set_valuation(
        session, hr_user, points_per_rupee=Decimal("2"), effective_from=date(2026, 1, 1)
    )
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=1000, awarded_on=date(2026, 3, 1)
    )
    me = await _login(session, tenant, employee)

    mug = await catalogue.create_item(
        session,
        hr_user,
        catalogue.CatalogueItemIn(sku="MUG-01", name="Company mug", points_cost=400, stock=1),
        request=None,
    )
    voucher = await catalogue.create_item(
        session,
        hr_user,
        catalogue.CatalogueItemIn(sku="AMZ-500", name="Shopping voucher", points_cost=900),
        request=None,
    )
    assert await catalogue.has_catalogue(session, me)

    done = await catalogue.redeem_item(session, me, uuid.UUID(mug["id"]), request=None)
    assert done["points"] == 400 and done["rupee_value"] == "200.00"
    assert done["item"]["stock"] == 0

    redemption = await session.scalar(select(PointsRedemption))
    assert redemption is not None and redemption.item_sku == "MUG-01"

    with pytest.raises(HTTPException) as gone:
        await catalogue.redeem_item(session, me, uuid.UUID(mug["id"]), request=None)
    assert gone.value.status_code == 409

    # 600 left, the voucher costs 900: refused, and nothing is taken.
    with pytest.raises(HTTPException):
        await catalogue.redeem_item(session, me, uuid.UUID(voucher["id"]), request=None)
    item = await session.scalar(
        select(RewardCatalogueItem).where(RewardCatalogueItem.sku == "AMZ-500")
    )
    assert item is not None and item.stock is None
    count = len((await session.scalars(select(PointsRedemption))).all())
    assert count == 1
