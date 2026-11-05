"""Each department's year against its budget, across AskMira and the linked products.

Real database; Expense Genie, ProcureFlow and Saleinx are fakes answering the
queries AskMira already uses (summary, budgets, overview).

* Without products: the headcount budget and AskMira's own approved claims by
  department.
* With Expense Genie: its approved claims added up by AskMira's own
  departments, through each submitter's email.
* A ProcureFlow cost centre with a department's name sits on that row; others
  are listed apart. The Saleinx pipeline is shown beside it, never summed in.
* Payroll paid shows only with payroll access; employees get nothing.
"""

from __future__ import annotations

import hashlib
import json
import types
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import httpx
import jwt
import pytest
from fastapi import HTTPException

from app.models.employee import Employee
from app.models.org import Department
from app.models.user import UserRole
from app.schemas.positions import BudgetUpsert
from app.services import budget_year as spend
from app.services.positions import service as positions
from app.services.products import client as product_client
from app.services.products import settings as product_settings
from tests.integration.conftest import as_caller, requires_db
from tests.integration.platform_support import make_person
from tests.integration.test_sales_procurement_link import _link

pytestmark = requires_db

SECRETS = {
    "expense": "expense-secret-0123456789abcdef0123456789ab",
    "procurement": "procure-secret-0123456789abcdef0123456789",
    "saleinx": "saleinx-secret-0123456789abcdef0123456789",
}
HOSTS = {"expense": "genie-api.test", "procurement": "pf-api.test", "saleinx": "saleinx.test"}
PREFIX = {"expense": "EXPENSE_TOOL", "procurement": "PROCUREMENT_TOOL", "saleinx": "SALEINX_TOOL"}


class Fakes:
    def __init__(self) -> None:
        self.by_submitter: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        product = next(p for p, h in HOSTS.items() if h == request.url.host)
        claims = jwt.decode(
            request.headers["Authorization"].removeprefix("Bearer "),
            SECRETS[product],
            algorithms=["HS256"],
            audience=f"askmira:{product}",
            issuer="askmira",
        )
        assert claims["bh"] == hashlib.sha256(request.content).hexdigest()
        body = json.loads(request.content)
        if request.url.path.endswith("users/sync"):
            return httpx.Response(200, json={"created": 0, "updated": 0, "deactivated": 0, "members": []})
        kind = body["kind"]
        self.calls.append((product, kind))
        actor = {"role": "manager", "sees": "workspace"}
        if product == "expense" and kind == "summary":
            assert body["params"]["scope"] == "company" and body["params"]["status"] == "approved"
            return httpx.Response(200, json={"kind": kind, "scope": "company", "by_status": [],
                "by_category": [], "approved_unpaid": [], "by_month": [],
                "by_submitter": self.by_submitter})  # fmt: skip
        if product == "procurement" and kind == "budgets":
            return httpx.Response(200, json={"kind": kind, "actor": actor, "budgets": [
                {"cost_center": "Sales West", "code": "SW", "allocated": "500000.00",
                 "committed": "120000.00", "consumed": "80000.00", "available": "300000.00"},
                {"cost_center": "Facilities", "code": "FAC", "allocated": "200000.00",
                 "committed": "0.00", "consumed": "50000.00", "available": "150000.00"},
            ]})  # fmt: skip
        if product == "saleinx" and kind == "overview":
            return httpx.Response(200, json={"kind": kind, "actor": actor, "stages": [
                {"stage": "proposal", "stage_label": "Proposal", "deals": 3, "amount": "4500000.00"},
                {"stage": "negotiation", "stage_label": "Negotiation", "deals": 1, "amount": "2000000.00"},
                {"stage": "won", "stage_label": "Won", "deals": 2, "amount": "7110000.00"},
                {"stage": "lost", "stage_label": "Lost", "deals": 1, "amount": "900000.00"},
            ]})  # fmt: skip
        return httpx.Response(422, json={"detail": f"unexpected {product} {kind}"})


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Iterator[Fakes]:
    for product, prefix in PREFIX.items():
        monkeypatch.setenv(f"{prefix}_API_URL", f"https://{HOSTS[product]}")
        monkeypatch.setenv(f"{prefix}_APP_URL", f"https://{product}-app.test")
        monkeypatch.setenv(f"{prefix}_SECRET", SECRETS[product])
    product_settings.reset_cache()
    fake = Fakes()
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(
        product_client,
        "httpx",
        types.SimpleNamespace(AsyncClient=factory, HTTPError=httpx.HTTPError, Timeout=httpx.Timeout),
    )
    yield fake
    product_settings.reset_cache()


def _fy(today) -> str:
    start = today.year if today.month >= 4 else today.year - 1
    return f"FY {start}-{str(start + 1)[-2:]}"


async def _world(session, tenant):
    from datetime import date

    admin = await as_caller(
        session, await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    )
    sales = Department(tenant_id=tenant.id, name="Sales West", code=f"SW{uuid.uuid4().hex[:3]}")
    session.add(sales)
    await session.commit()
    rep = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Ravi Rep")
    employee = await session.get(Employee, rep.employee_id)
    employee.department_id = sales.id
    await session.commit()
    await positions.upsert_budget(
        session,
        admin,
        BudgetUpsert(
            financial_year=_fy(date.today()),
            department_id=sales.id,
            approved_positions=4,
            approved_cost=Decimal("4000000"),
        ),
    )
    await session.commit()
    return admin, sales, rep


async def test_with_the_products_each_figure_sits_on_its_department(session, tenant, fakes) -> None:
    admin, sales, rep = await _world(session, tenant)
    for product in ("expense", "procurement", "saleinx"):
        await _link(session, tenant, product, {admin.email: "manager"} if product != "expense" else None)
    fakes.by_submitter = [
        {"submitter": "Ravi Rep", "email": rep.email, "currency": "INR", "count": 2, "amount": "1849.75"},
        {"submitter": "Stranger", "email": "x@elsewhere.test", "currency": "INR", "count": 1, "amount": "500.00"},
        {"submitter": "Abroad", "email": rep.email, "currency": "USD", "count": 1, "amount": "40.00"},
    ]

    year = await spend.department_year(session, admin)
    row = next(r for r in year["departments"] if r["department_id"] == str(sales.id))
    assert row["approved_positions"] == 4 and row["approved_cost"] == "4000000.00"
    assert row["expenses"] == "1849.75", "INR only, by the submitter's department"
    assert year["expenses"]["source"] == "Expense Genie"
    assert year["expenses"]["unassigned"] == "500.00"
    assert row["purchase_budget"]["cost_center"] == "Sales West"
    assert row["purchase_budget"]["available"] == "300000.00"
    assert [b["cost_center"] for b in year["purchasing"]["other_cost_centres"]] == ["Facilities"]
    assert year["sales"]["open_pipeline"] == "6500000.00" and year["sales"]["won"] == "7110000.00"
    assert year["payroll_visible"] is True and row["payroll_paid"] == "0.00"
    # Different budgets, never added into one number.
    assert "left" not in json.dumps({k: v for k, v in row.items() if k != "positions_left" and k != "people_cost_left"})


async def test_without_products_askmira_claims_and_who_may_see_payroll(session, tenant, fakes) -> None:
    admin, sales, _ = await _world(session, tenant)
    year = await spend.department_year(session, admin)
    assert year["expenses"]["source"] == "AskMira"
    assert year["purchasing"] is None and year["sales"] is None
    assert fakes.calls == []

    sub = await as_caller(
        session, await make_person(session, tenant, role=UserRole.SUB_ADMIN, name="Sam Sub")
    )
    seen = await spend.department_year(session, sub)
    assert seen["payroll_visible"] is False
    assert all(r["payroll_paid"] is None for r in seen["departments"])

    staff = await as_caller(
        session, await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Eve Emp")
    )
    with pytest.raises(HTTPException) as refused:
        await spend.department_year(session, staff)
    assert refused.value.status_code == 403
