"""Flexi-benefit declarations: HR sets a pot, the person splits it, HR approves.

What these hold:

- a split above a component's own limit, or above the pot, is refused;
- the person cannot approve or change an approved split; HR can reopen it;
- once approved, the split is the person's entitlement wherever entitlement is read.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee
from app.models.loans import ReimbursementComponent
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.loans import flexi
from app.services.loans import service as loan_service
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db

YEAR = "2026-2027"


async def _setup(session: AsyncSession, tenant: Tenant, employee: Employee) -> User:
    session.add_all(
        [
            ReimbursementComponent(
                tenant_id=tenant.id, code="FUEL", name="Fuel", annual_entitlement=Decimal(30_000),
                exemption_basis="Against bills.",
            ),
            ReimbursementComponent(
                tenant_id=tenant.id, code="PHONE", name="Telephone",
                annual_entitlement=Decimal(12_000), exemption_basis="Against bills.",
            ),
        ]
    )
    me = User(
        tenant_id=tenant.id, email="anita@acme.test", password_hash="x", full_name="Anita Sharma",
        role=UserRole.EMPLOYEE, employee_id=employee.id, is_active=True,
    )
    session.add(me)
    await session.commit()
    await session.refresh(me)
    return await as_caller(session, me)


async def test_a_split_is_declared_within_limits_and_approved_by_hr(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    me = await _setup(session, tenant, employee)

    with pytest.raises(HTTPException) as no_pot:
        await flexi.declare(session, me, YEAR, {"FUEL": Decimal(1000)}, submit=True)
    assert no_pot.value.status_code == 409

    await flexi.set_pot(session, hr_user, employee.id, YEAR, Decimal(36_000))
    with pytest.raises(HTTPException) as over_limit:
        await flexi.declare(session, me, YEAR, {"PHONE": Decimal(15_000)}, submit=True)
    assert "capped at" in over_limit.value.detail
    with pytest.raises(HTTPException) as over_pot:
        await flexi.declare(
            session, me, YEAR, {"FUEL": Decimal(30_000), "PHONE": Decimal(12_000)}, submit=True
        )
    assert "more than your flexi pot" in over_pot.value.detail

    declared = await flexi.declare(
        session, me, YEAR, {"FUEL": Decimal(24_000), "PHONE": Decimal(6_000)}, submit=True
    )
    assert declared["status"] == "submitted"
    assert declared["declared"] == "30000.00" and declared["undeclared"] == "6000.00"

    with pytest.raises(HTTPException) as self_decide:
        await flexi.decide(
            session, me, uuid.UUID(declared["id"]), approve=True, note=None
        )
    assert self_decide.value.status_code == 403

    # Before approval the component defaults still apply.
    before = await loan_service.entitlement_positions(
        session, me, employee_id=employee.id, financial_year=YEAR
    )
    assert Decimal({p["component_code"]: p["entitlement"] for p in before}["FUEL"]) == 30_000

    approved = await flexi.decide(
        session, hr_user, uuid.UUID(declared["id"]), approve=True, note=None
    )
    assert approved["status"] == "approved"

    after = {
        p["component_code"]: Decimal(str(p["entitlement"]))
        for p in await loan_service.entitlement_positions(
            session, me, employee_id=employee.id, financial_year=YEAR
        )
    }
    assert after == {"FUEL": Decimal(24_000), "PHONE": Decimal(6_000)}

    with pytest.raises(HTTPException) as locked:
        await flexi.declare(session, me, YEAR, {"FUEL": Decimal(1)}, submit=False)
    assert locked.value.status_code == 409

    reopened = await flexi.decide(
        session, hr_user, uuid.UUID(declared["id"]), approve=None, note="New year"
    )
    assert reopened["status"] == "draft"
    assert (await flexi.approved_entitlements(session, me, employee.id, YEAR)) is None
