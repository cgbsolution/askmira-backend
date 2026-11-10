"""Ask Mira's department budget answers, against a real database.

"Can Sales afford two more hires at 9 lakh?" is answered by the same engine that
refuses a seat past its budget: the figures must match what creating the seat
would do, a sub-admin and an admin may ask, an employee may not, and another
workspace's budget is never read.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.org import Department
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.positions import BudgetUpsert, PositionCreate
from app.services.ai import client as ai_client
from app.services.companion import service as companion
from app.services.positions import service as positions
from tests.integration.conftest import as_caller, requires_db
from tests.integration.platform_support import make_person

pytestmark = requires_db

TODAY = companion._today()


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_client.settings, "ANTHROPIC_API_KEY", "")


def _fy(on: date) -> str:
    start = on.year if on.month >= 4 else on.year - 1
    return f"FY {start}-{str(start + 1)[-2:]}"


async def _ask(session: AsyncSession, user: User, text: str) -> dict[str, Any]:
    result = await companion.send(session, user, companion.MessageIn(text=text), request=None)
    return result["messages"][1]


def _record(reply: dict[str, Any], scope: str) -> dict[str, Any]:
    cards = [c for c in reply["cards"] if c.get("type") == "record" and scope in c["title"]]
    assert cards, reply["cards"]
    return {f["label"]: f["value"] for f in cards[0]["figures"]} | {"note": cards[0]["note"]}


async def _sales(session: AsyncSession, tenant: Tenant, admin: User) -> Department:
    """Sales: 4 positions and ₹40 L approved; 3 seats of ₹10 L each already exist."""
    dept = Department(tenant_id=tenant.id, name="Sales West", code=f"SW{uuid.uuid4().hex[:4]}")
    session.add(dept)
    await session.commit()
    await positions.upsert_budget(
        session,
        admin,
        BudgetUpsert(
            financial_year=_fy(TODAY),
            department_id=dept.id,
            approved_positions=4,
            approved_cost=Decimal("4000000"),
        ),
    )
    await session.commit()
    for n in range(3):
        await positions.create_position(
            session,
            admin,
            PositionCreate(
                code=f"SW-{n}",
                title="Account Executive",
                department_id=dept.id,
                fte=Decimal(1),
                effective_from=TODAY,
                budgeted_ctc_max=Decimal("1000000"),
            ),
        )
    await session.commit()
    return dept


async def test_an_admin_sees_what_the_budget_still_allows(session, tenant) -> None:
    admin = await as_caller(
        session, await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    )
    await _sales(session, tenant, admin)

    reply = await _ask(session, admin, "headcount budget for sales west")
    figures = _record(reply, "Sales West")
    assert figures["Approved positions"] == "4"
    assert figures["Positions counted"] == "3.00"
    assert figures["Positions left"] == "1.00"
    assert figures["Approved cost"] == "₹40,00,000"
    assert figures["Committed on positions"] == "₹30,00,000"
    assert figures["Cost left"] == "₹10,00,000"


async def test_two_hires_past_the_budget_are_refused_with_the_engines_words(
    session, tenant
) -> None:
    admin = await as_caller(
        session, await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    )
    await _sales(session, tenant, admin)

    reply = await _ask(session, admin, "Can Sales afford two more hires at 9 lakh?")
    figures = _record(reply, "Sales West")
    # 3 + 2 = 5 positions against 4, and 30 L + 18 L = 48 L against 40 L.
    assert "5.00 FTE against 4.00 approved" in figures["note"]
    assert "48,00,000" in figures["note"] or "4800000" in figures["note"]

    one = _record(await _ask(session, admin, "can sales afford one more hire at 9 lakh"), "Sales")
    assert one["note"].startswith("1 more hire fits")

    # And it agrees with what creating the seats would do.
    with pytest.raises(HTTPException) as exc:
        for n in range(2):
            await positions.create_position(
                session,
                admin,
                PositionCreate(
                    code=f"SW-new-{n}",
                    title="Account Executive",
                    department_id=(await _dept_id(session, tenant)),
                    fte=Decimal(1),
                    effective_from=TODAY,
                    budgeted_ctc_max=Decimal("900000"),
                ),
            )
            await session.commit()
    assert exc.value.status_code == 409


async def _dept_id(session: AsyncSession, tenant: Tenant) -> uuid.UUID:
    from sqlalchemy import select

    return await session.scalar(
        select(Department.id).where(
            Department.tenant_id == tenant.id, Department.name == "Sales West"
        )
    )


async def test_a_sub_admin_may_ask_and_an_employee_may_not(session, tenant) -> None:
    admin = await as_caller(
        session, await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    )
    await _sales(session, tenant, admin)
    sub = await as_caller(
        session, await make_person(session, tenant, role=UserRole.SUB_ADMIN, name="Sam Sub")
    )
    staff = await as_caller(
        session, await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Eve Emp")
    )

    assert _record(await _ask(session, sub, "positions left in sales"), "Sales West")
    refused = await _ask(session, staff, "can sales afford 2 more hires")
    assert not [c for c in refused["cards"] if c.get("type") == "record"]
    assert "admins and sub-admins" in refused["text"]


async def test_another_workspace_budget_is_never_read(session, tenant) -> None:
    admin = await as_caller(
        session, await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    )
    await _sales(session, tenant, admin)
    other = Tenant(
        name="Other Co",
        slug=f"other-{uuid.uuid4().hex[:8]}",
        enabled_modules={"core_hr": True},
        settings={},
    )
    session.add(other)
    await session.commit()
    stranger = await as_caller(
        session, await make_person(session, other, role=UserRole.ADMIN, name="Olu Other")
    )
    reply = await _ask(session, stranger, "headcount budget for sales west")
    assert not [c for c in reply["cards"] if c.get("type") == "record"]
    said = " ".join(c.get("text", "") for c in reply["cards"])
    # Another workspace's department is not one here at all.
    assert "There is no department called" in said or "No headcount budget is set" in said
