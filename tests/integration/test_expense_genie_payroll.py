"""Expense Genie claims paid with salary in AskMiraa payroll, against a real database.

Expense Genie is a fake (httpx MockTransport) answering its documented
``payable`` query and ``claims.paid`` action; the payroll run is real.

* An approved, unpaid claim of someone in the run becomes an untaxed
  reimbursement line on their payslip; claims of strangers and in another
  currency are left out (the latter said so).
* Approving the run records the claim as paid (the double-pay guard) and tells
  Expense Genie, which marks it paid.
* A claim already paid is never paid by a later run, even if Expense Genie
  still lists it.
* If Expense Genie is down at approval, the scheduler tells it later.
* Without Expense Genie linked, nothing is asked.
"""

from __future__ import annotations

import hashlib
import json
import types
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import httpx
import jwt
import pytest
from sqlalchemy import select

from app.models.linked_claims import LinkedClaimPayment
from app.services.payroll import linked_expenses
from app.services.payroll import run as run_service
from app.services.products import client as product_client
from app.services.products import settings as product_settings
from tests.integration.conftest import requires_db
from tests.integration.test_payroll_money_flow import _org, _run, _slip
from tests.integration.test_sales_procurement_link import _link

pytestmark = requires_db

SECRET = "expense-secret-0123456789abcdef0123456789ab"


class FakeExpenseGenie:
    def __init__(self) -> None:
        self.claims: list[dict[str, Any]] = []
        self.paid: list[str] = []
        self.calls: list[dict[str, Any]] = []
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        claims = jwt.decode(
            request.headers["Authorization"].removeprefix("Bearer "),
            SECRET,
            algorithms=["HS256"],
            audience="askmira:expense",
            issuer="askmira",
        )
        assert claims["bh"] == hashlib.sha256(request.content).hexdigest()
        body = json.loads(request.content)
        path = request.url.path.rsplit("/integrations/askmira/", 1)[1]
        self.calls.append({"path": path, "use": claims["use"], "body": body})
        if path == "users/sync":
            return httpx.Response(200, json={"created": 0, "updated": 0, "deactivated": 0})
        if self.down:
            return httpx.Response(503, json={"error": "down"})
        if path == "query" and body["kind"] == "payable":
            assert body["params"] == {"scope": "company"}
            assert body["actor"]["role"] == "admin"
            return httpx.Response(200, json={"kind": "payable", "scope": "company", "claims": self.claims})
        if path == "act" and body["kind"] == "claims.paid":
            assert claims["use"] == "act"
            done = [r for r in body["params"]["references"] if r not in self.paid]
            self.paid.extend(done)
            return httpx.Response(200, json={"kind": "claims.paid", "paid": done, "skipped": []})
        return httpx.Response(422, json={"error": f"unexpected {path} {body.get('kind')}"})


@pytest.fixture
def genie(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeExpenseGenie]:
    monkeypatch.setenv("EXPENSE_TOOL_API_URL", "https://genie-api.test")
    monkeypatch.setenv("EXPENSE_TOOL_APP_URL", "https://genie.test")
    monkeypatch.setenv("EXPENSE_TOOL_SECRET", SECRET)
    product_settings.reset_cache()
    fake = FakeExpenseGenie()
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


def _claim(reference: str, email: str, amount: str, currency: str = "INR") -> dict[str, Any]:
    return {
        "reference": reference,
        "title": "Client visit cab",
        "submitter": "Someone",
        "submitter_email": email,
        "amount": amount,
        "currency": currency,
        "category": "Travel",
        "submitted_at": "2026-09-10T10:00:00Z",
    }


def _genie_lines(slip) -> list[dict[str, Any]]:
    return [line for line in (slip.input_lines or []) if line.get("code") == "EXPENSE_GENIE"]


async def test_an_approved_claim_is_paid_with_salary_once_and_recorded_there(
    session, tenant, legal_entity, location, genie
) -> None:
    org = await _org(session, tenant, legal_entity, location)
    await _link(session, tenant, "expense", None)
    staff_email = org["staff"].email
    genie.claims = [
        _claim("EG-1", staff_email, "1250.50"),
        _claim("EG-2", "stranger@elsewhere.test", "900.00"),
        _claim("EG-3", staff_email, "40.00", currency="USD"),
    ]

    run = await _run(session, org, 2026, 9)
    await run_service.compute_run(session, org["maker"], run.id)
    slip = await _slip(session, run.id, org["staff_emp"].id)
    [line] = _genie_lines(slip)
    assert Decimal(line["amount"]) == Decimal("1250.50")
    assert Decimal(line["taxable_amount"]) == Decimal("0")
    assert line["source"]["id"] == "EG-1"
    for other in ("loner_emp", "boss_emp"):
        assert not _genie_lines(await _slip(session, run.id, org[other].id))

    await run_service.approve_run(session, org["checker"], run.id)
    paid = (await session.scalars(select(LinkedClaimPayment))).all()
    assert [(p.reference, p.amount, p.run_id) for p in paid] == [("EG-1", Decimal("1250.50"), run.id)]
    assert paid[0].marked_at is not None and paid[0].mark_error is None
    assert genie.paid == ["EG-1"]
    act = next(c for c in genie.calls if c["path"] == "act")
    assert act["body"]["actor"]["email"] == org["checker"].email
    assert "2026-09" in act["body"]["params"]["note"]

    # Next month Expense Genie still lists it (say its mark was lost): never paid again.
    genie.paid.clear()
    october = await _run(session, org, 2026, 10)
    await run_service.compute_run(session, org["maker"], october.id)
    assert not _genie_lines(await _slip(session, october.id, org["staff_emp"].id))


async def test_when_expense_genie_is_down_at_approval_the_scheduler_tells_it_later(
    session, tenant, legal_entity, location, genie
) -> None:
    org = await _org(session, tenant, legal_entity, location)
    await _link(session, tenant, "expense", None)
    genie.claims = [_claim("EG-9", org["staff"].email, "500.00")]
    run = await _run(session, org, 2026, 9)
    await run_service.compute_run(session, org["maker"], run.id)

    genie.down = True
    approved = await run_service.approve_run(session, org["checker"], run.id)
    assert approved.status.value == "approved"
    row = await session.scalar(select(LinkedClaimPayment))
    assert row.marked_at is None and row.mark_error

    genie.down = False
    outcome = await linked_expenses.hand_back_pending(session, org["maker"])
    assert outcome == {"runs": 1, "marked": 1}
    await session.refresh(row)
    assert row.marked_at is not None and genie.paid == ["EG-9"]


async def test_without_expense_genie_nothing_is_asked(
    session, tenant, legal_entity, location, genie
) -> None:
    org = await _org(session, tenant, legal_entity, location)
    genie.claims = [_claim("EG-1", org["staff"].email, "1250.50")]
    run = await _run(session, org, 2026, 9)
    await run_service.compute_run(session, org["maker"], run.id)
    assert not _genie_lines(await _slip(session, run.id, org["staff_emp"].id))
    assert [c for c in genie.calls if c["path"] != "users/sync"] == []
