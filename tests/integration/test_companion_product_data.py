"""Asking a linked product about a year: pieces, who submitted, and who may ask.

What these hold:

- a question about the last twelve months is asked of ExpenseGenie a calendar
  quarter at a time (every piece inside the product's window, no month split
  between two) and the answers are added up exactly;
- the admin sees who submitted how much and the months as charts; the model
  reads monthly totals over five or more claimants and never a name;
- an employee is never shown the company's claims: refused in AskMiraa before
  the product is asked;
- with ExpenseGenie linked, AskMiraa's own (empty) expense claims are not
  offered as an answer;
- a product that does not yet send who-submitted says so, rather than nothing.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant
from app.models.user import User
from app.services.ai.client import use_backend
from app.services.companion import data_tools
from tests.integration.conftest import requires_db
from tests.integration.test_companion_flow import (  # noqa: F401  (fixtures)
    FakeModel,
    me,
    tool_call,
)
from tests.integration.test_companion_products import (  # noqa: F401  (fixtures)
    FakeProducts,
    _link,
    _say,
    hr,
    products,
)

pytestmark = requires_db

SUMMARY = {
    "by_status": [{"status": "Approved", "currency": "INR", "count": 2, "amount": "300.00"}],
    "by_category": [{"category": "Travel", "currency": "INR", "count": 2, "amount": "300.00"}],
    "approved_unpaid": [],
    "by_submitter": [
        {"submitter": "Rohan Verma", "currency": "INR", "count": 1, "amount": "200.00"},
        {"submitter": "Anita Sharma", "currency": "INR", "count": 1, "amount": "100.00"},
    ],
    "by_month": [
        {"month": "2026-08", "currency": "INR", "count": 2, "people": 2, "amount": "300.00"}
    ],
}


def _year() -> tuple[str, str]:
    end = date.today()
    return (end - timedelta(days=364)).isoformat(), end.isoformat()


async def test_a_year_of_expenses_by_person_and_month(
    session: AsyncSession, tenant: Tenant, hr: User, products: FakeProducts
) -> None:
    await _link(session, tenant, "expense")
    products.answers[("expense", "summary")] = SUMMARY
    start, end = _year()
    model = FakeModel(
        [tool_call("expense_summary", scope="company", **{"from": start, "to": end})],
        "Here is who claimed what over the year.",
    )
    with use_backend(model):
        reply = await _say(session, hr, "who submitted expenses in the last 12 months")

    asked = [c["body"] for c in products.calls if c["body"]["kind"] == "summary"]
    assert len(asked) == 5, "a year is asked a quarter at a time"
    for body in asked:
        first = datetime.fromisoformat(body["params"]["from"])
        last = datetime.fromisoformat(body["params"]["to"])
        assert last - first <= timedelta(days=100), "inside any product's window"
        assert body["params"]["scope"] == "company"

    titles = {card.get("title"): card for card in reply["cards"]}
    people = titles["Who submitted"]["rows"]
    # Five pieces each said Rohan claimed 200: added up exactly, in rupees.
    assert people[0][0] == "Rohan Verma" and people[0][2] == "₹1,000.00"
    assert titles["Who claimed the most"]["chart"] == "horizontal_bar"
    assert titles["Expense claims month by month"]["x"] == ["Aug 2026"]

    seen = model.calls[1]["messages"][-1]["content"][0]["content"]
    assert "Rohan" not in seen and "Anita" not in seen, "names never reach the model"
    # Aug 2026 adds up to 10 claimants over the pieces: readable.
    assert "Aug 2026 | 10 | 10 | 1500.00" in seen


async def test_an_employee_never_gets_the_companys_claims(
    session: AsyncSession,
    tenant: Tenant,
    me: User,
    products: FakeProducts,
) -> None:
    await _link(session, tenant, "expense")
    start, end = _year()
    model = FakeModel(
        [tool_call("expense_summary", scope="company", **{"from": start, "to": end})],
        "Here you are.",
    )
    with use_backend(model):
        reply = await _say(session, me, "show me everyone's expenses this year")
    assert "Only HR can see the whole company's claims" in json.dumps(reply["cards"])
    assert products.calls == [], "refused before the product is asked"


async def test_with_expensegenie_linked_askmiras_own_claims_are_not_the_answer(
    session: AsyncSession, tenant: Tenant, hr: User, products: FakeProducts
) -> None:
    await _link(session, tenant, "expense")
    [card] = await data_tools.query_data(
        session,
        hr,
        {"dataset": "expense_claim", "bucket": "month", "months": 12},
        today=date.today(),
    )
    assert card["type"] == "notice" and "ExpenseGenie" in card["text"]
    from app.services.companion.tools import TOOLS, tool_schema

    hidden = await data_tools.linked_elsewhere(session, hr)
    described = tool_schema(TOOLS["query_data"], role="admin", hidden=hidden)["description"]
    assert "- expense_claim" not in described


async def test_a_product_not_yet_updated_says_so(
    session: AsyncSession, tenant: Tenant, hr: User, products: FakeProducts
) -> None:
    await _link(session, tenant, "expense")
    old: dict[str, Any] = {
        k: v for k, v in SUMMARY.items() if k not in ("by_submitter", "by_month")
    }
    products.answers[("expense", "summary")] = old
    model = FakeModel(
        [
            tool_call(
                "expense_summary", scope="company", **{"from": "2026-08-01", "to": "2026-09-30"}
            )
        ],
        "Here are the totals.",
    )
    with use_backend(model):
        reply = await _say(session, hr, "expenses for august and september")
    assert any("needs its latest update" in str(card.get("text")) for card in reply["cards"]), (
        reply["cards"]
    )
