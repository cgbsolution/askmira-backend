"""Ask Miraa files expense claims and decides the ones waiting on you, in ExpenseGenie.

ExpenseGenie is a fake (httpx MockTransport) answering its documented
``categories`` and ``claims`` queries and ``claim.create`` / ``claim.decide``
actions, checking every call is signed use=query or use=act.

* A receipt becomes one offer, checked against ExpenseGenie's categories and the
  person's grade cap; only the yes reaches ExpenseGenie, keyed by the offer.
* A category that needs a bill is saved as a draft and the offer says so.
* An unknown category is asked about; an amount over the cap, a future date or
  a nonsense amount are refused before any offer.
* "Approve the ones waiting on me" lists them in the offer (on screen only) and
  decides each as the person; one decided elsewhere meanwhile is said, not forced.
* One bill split into lines is still over the cap; a bill claimed before is
  refused before the offer; "12.345" typed is never offered as ₹12,345.
* A rejection sends its reason and names each claim by its id; a lost answer is
  asked once more with the same key; a done action has its own audit row.
"""

from __future__ import annotations

import hashlib
import json
import types
import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx
import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.tenant import Tenant
from app.models.user import User
from app.services.ai.client import use_backend
from app.services.companion import service as companion
from app.services.products import client as product_client
from app.services.products import settings as product_settings
from tests.integration.conftest import as_caller, requires_db
from tests.integration.test_companion_flow import FakeModel, _ai_on, _consent, plan, tool_call
from tests.integration.test_sales_procurement_link import _link

pytestmark = requires_db

SECRET = "expense-secret-0123456789abcdef0123456789ab"
TODAY = date.today()


class FakeGenie:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.categories = [
            {"name": "Meals", "requires_bill": True, "cap": None},
            {"name": "Travel", "requires_bill": False, "cap": "2000"},
        ]
        self.waiting: list[dict[str, Any]] = []
        self.decided_elsewhere: set[str] = set()
        #: Invoice numbers this person claimed before (the `bills` query).
        self.claimed: set[str] = set()
        #: claim.create answers 502 this many times first (an answer lost on the way).
        self.lose: int = 0

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
        assert claims["use"] == path or path == "users/sync"
        self.calls.append({"path": path, "body": body})
        kind = body.get("kind")
        if path == "query" and kind == "categories":
            return httpx.Response(200, json={"kind": kind, "grade": "G1",
                                             "categories": self.categories})  # fmt: skip
        if path == "query" and kind == "bills":
            found = [
                {"line": i, "reference": "old-1", "title": "Earlier cab", "status": "Approved"}
                for i, line in enumerate(body["params"]["lines"], start=1)
                if line.get("invoice_number") in self.claimed
            ]
            return httpx.Response(200, json={"kind": kind, "duplicates": found})
        if path == "query" and kind == "claims":
            assert body["params"]["scope"] == "approvals"
            answer = {"kind": kind, "scope": "approvals", "total": len(self.waiting)}
            return httpx.Response(200, json={**answer, "claims": self.waiting})
        if path == "act" and kind == "claim.create":
            if self.lose:
                self.lose -= 1
                return httpx.Response(502, text="Bad gateway")
            params = body["params"]
            total = sum(float(line["amount"]) for line in params["lines"])
            draft = any(line["category"] == "Meals" for line in params["lines"])
            return httpx.Response(200, json={"kind": kind, "existing": False,
                "claim": {"reference": f"askmira:{params['key']}", "title": params["title"],
                          "status": "Draft" if draft else "Pending", "amount": f"{total:g}",
                          "approver": "mgr@acme.test"},
                "needs_bill": ["Meals"] if draft else []})  # fmt: skip
        if path == "act" and kind == "claim.decide":
            ref = body["params"]["reference"]
            if ref in self.decided_elsewhere:
                return httpx.Response(409, json={"error": "This claim is Approved, not waiting."})
            return httpx.Response(200, json={"kind": kind, "claim": {"reference": ref,
                "title": "x", "status": "Pending", "next_approver": "fin@acme.test"}})  # fmt: skip
        return httpx.Response(422, json={"error": f"unexpected {path} {kind}"})

    def acts(self, kind: str) -> list[dict[str, Any]]:
        return [c["body"] for c in self.calls if c["path"] == "act" and c["body"]["kind"] == kind]


@pytest.fixture
def genie(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGenie]:
    monkeypatch.setenv("EXPENSE_TOOL_API_URL", "https://genie-api.test")
    monkeypatch.setenv("EXPENSE_TOOL_APP_URL", "https://genie.test")
    monkeypatch.setenv("EXPENSE_TOOL_SECRET", SECRET)
    product_settings.reset_cache()
    fake = FakeGenie()
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(
        product_client,
        "httpx",
        types.SimpleNamespace(
            AsyncClient=factory, HTTPError=httpx.HTTPError, Timeout=httpx.Timeout
        ),
    )
    yield fake
    product_settings.reset_cache()


@pytest.fixture
async def hr(session: AsyncSession, tenant: Tenant, hr_user: User) -> User:
    await _ai_on(session, hr_user)
    await _consent(session, tenant.id, hr_user.id)
    await _link(session, tenant, "expense", None)
    return await as_caller(session, hr_user)


async def _say(session: AsyncSession, user: User, words: str, model: FakeModel) -> dict[str, Any]:
    with use_backend(model):
        return await companion.send(session, user, companion.MessageIn(text=words), request=None)


def _cards(result: dict[str, Any]) -> list[dict[str, Any]]:
    return result["messages"][1]["cards"]


def _offers(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in _cards(result) if c.get("type") == "proposal"]


def _receipt(**line: Any) -> dict[str, Any]:
    bill = {
        "category": "travel",
        "amount": "850.50",
        "date": TODAY.isoformat(),
        "vendor": "Uber",
        "invoice_number": "UB-11",
        "description": "Airport to office",
    }
    bill.update(line)
    return plan("file_expense_claim", "Here it is.", claim_title="Cab", claim_lines=[bill])


async def test_a_receipt_becomes_one_claim_filed_only_on_yes(
    session: AsyncSession, hr: User, genie: FakeGenie
) -> None:
    result = await _say(session, hr, "file this receipt", FakeModel(_receipt()))
    [offer] = _offers(result)
    assert {f["label"]: f["value"] for f in offer["figures"]}["Total"] == "₹850.50"
    assert offer["table"]["rows"] == [["Travel", f"{TODAY:%d %b %Y}", "Uber", "₹850.50"]]
    assert offer["confirm_label"] == "File claim"
    assert genie.acts("claim.create") == []

    done = await companion.confirm_offer(session, hr, uuid.UUID(offer["action_id"]), request=None)
    assert done["action"]["status"] == "done", done["action"]
    [sent] = genie.acts("claim.create")
    assert sent["actor"]["email"] == hr.email
    assert sent["params"]["key"] == f"askmira-{offer['action_id']}"
    assert sent["params"]["lines"][0] == {
        "category": "Travel", "amount": "850.50", "date": TODAY.isoformat(), "vendor": "Uber",
        "invoice_number": "UB-11", "description": "Airport to office",
    }  # fmt: skip
    assert "waiting for your manager's approval" in done["action"]["result"]["text"]


async def test_a_bill_category_is_saved_as_a_draft_and_said_so(
    session: AsyncSession, hr: User, genie: FakeGenie
) -> None:
    result = await _say(session, hr, "lunch", FakeModel(_receipt(category="Meals", amount="400")))
    [offer] = _offers(result)
    assert offer["confirm_label"] == "Save as draft"
    assert any("stays a draft" in n for n in offer["notes"])
    done = await companion.confirm_offer(session, hr, uuid.UUID(offer["action_id"]), request=None)
    assert "saved as a draft" in done["action"]["result"]["text"]


@pytest.mark.parametrize(
    ("line", "says"),
    [
        ({"category": "Yacht"}, "which category is it? Meals, Travel."),
        ({"amount": "2500"}, "Travel is capped at ₹2,000 for grade G1"),
        ({"amount": "abc"}, "what is the amount"),
        ({"amount": "-5"}, "what is the amount"),
        ({"amount": "12.345"}, "what is the amount"),
        ({"amount": "1e5"}, "what is the amount"),
        ({"date": (TODAY + timedelta(days=3)).isoformat()}, "can't be in the future"),
        ({"date": "yesterday"}, "what is the bill's date"),
    ],
)
async def test_what_expensegenie_would_refuse_is_never_offered(
    session: AsyncSession, hr: User, genie: FakeGenie, line: dict[str, Any], says: str
) -> None:
    result = await _say(session, hr, "file it", FakeModel(_receipt(**line)))
    assert not _offers(result)
    assert says in " ".join(c.get("text", "") for c in _cards(result))
    assert genie.acts("claim.create") == []


async def test_the_claims_waiting_on_me_are_decided_as_me(
    session: AsyncSession, hr: User, genie: FakeGenie
) -> None:
    genie.waiting = [
        {"reference": "c1", "title": "Client lunch", "submitter": "Ravi", "amount": "1250.50"},
        {"reference": "c2", "title": "Cab rides", "submitter": "Meera", "amount": "500"},
    ]
    model = FakeModel(plan("decide_expense_claims", "Here they are.", decision="approve"))
    [offer] = _offers(await _say(session, hr, "approve the ones waiting on me", model))
    assert offer["title"] == "Approve 2 claims in ExpenseGenie"
    assert {f["label"]: f["value"] for f in offer["figures"]}["Total"] == "₹1,750.50"
    assert [row[1] for row in offer["table"]["rows"]] == ["Ravi", "Meera"]
    assert genie.acts("claim.decide") == []

    genie.decided_elsewhere = {"c2"}
    done = await companion.confirm_offer(session, hr, uuid.UUID(offer["action_id"]), request=None)
    assert done["action"]["status"] == "done"
    decided = genie.acts("claim.decide")
    assert [d["params"]["reference"] for d in decided] == ["c1", "c2"]
    assert all(d["actor"]["email"] == hr.email for d in decided)
    text = done["action"]["result"]["text"]
    assert "1 claim(s) approved" in text and "1 now wait for finance" in text
    assert "Not decided: Cab rides (Meera)" in text


async def test_rejecting_needs_a_reason_and_nothing_waiting_is_said(
    session: AsyncSession, hr: User, genie: FakeGenie
) -> None:
    reject = FakeModel(plan("decide_expense_claims", "", decision="reject"))
    result = await _say(session, hr, "reject them", reject)
    assert not _offers(result) and "Why are they rejected?" in _cards(result)[0]["text"]
    approve = FakeModel(plan("decide_expense_claims", "", decision="approve"))
    result = await _say(session, hr, "approve", approve)
    assert "Nothing in ExpenseGenie is waiting on your approval." in _cards(result)[0]["text"]


async def test_one_bill_split_into_lines_is_still_over_the_cap(
    session: AsyncSession, hr: User, genie: FakeGenie
) -> None:
    split = _receipt(amount="2000")
    split["claim_lines"].append({**split["claim_lines"][0], "amount": "500"})
    result = await _say(session, hr, "split it into two lines", FakeModel(split))
    assert not _offers(result)
    [refusal] = _cards(result)
    assert "this bill is ₹2,500" in refusal["text"]
    assert "Never offer to split" in refusal["model_note"]

    two = _receipt(amount="1500", invoice_number="UB-A1")
    two["claim_lines"].append(
        {**two["claim_lines"][0], "amount": "1200", "invoice_number": "UB-A2"}
    )
    [offer] = _offers(await _say(session, hr, "two cabs", FakeModel(two)))
    assert {f["label"]: f["value"] for f in offer["figures"]}["Total"] == "₹2,700"


async def test_a_bill_claimed_before_is_refused_before_any_offer(
    session: AsyncSession, hr: User, genie: FakeGenie
) -> None:
    genie.claimed = {"UB-11"}
    result = await _say(session, hr, "file this receipt", FakeModel(_receipt()))
    assert not _offers(result)
    assert "already claimed in ExpenseGenie: “Earlier cab” (Approved)" in _cards(result)[0]["text"]
    assert genie.acts("claim.create") == []


async def test_typed_decimals_are_checked_against_the_persons_own_words(
    session: AsyncSession, hr: User, genie: FakeGenie
) -> None:
    # The model read "12.345" as twelve thousand: the person's own words decide.
    model = FakeModel(_receipt(amount="12345", category="Meals"))
    result = await _say(session, hr, "Claim 12.345 rupees for my hotel stay", model)
    assert not _offers(result)
    assert "“12.345” has more than two decimals" in _cards(result)[0]["text"]


async def test_the_category_question_goes_once_the_offer_is_made(
    session: AsyncSession, hr: User, genie: FakeGenie
) -> None:
    line = {"amount": "300", "date": TODAY.isoformat(), "vendor": "Ola", "invoice_number": "OL-9"}
    model = FakeModel(
        [
            tool_call(
                "file_expense_claim", claim_title="Cab", claim_lines=[{**line, "category": ""}]
            )
        ],
        [
            tool_call(
                "file_expense_claim",
                claim_title="Cab",
                claim_lines=[{**line, "category": "Travel"}],
            )
        ],
        plan(reply="This will file your cab claim."),
    )
    cards = _cards(await _say(session, hr, "claim 300 for a cab today", model))
    assert [c["type"] for c in cards] == ["proposal"], cards


async def test_a_rejection_sends_its_reason_and_names_each_claim_by_id(
    session: AsyncSession, hr: User, genie: FakeGenie
) -> None:
    genie.waiting = [
        {"id": "1794", "reference": "c1", "title": "Auto", "submitter": "Meera", "amount": "450"},
    ]
    model = FakeModel(
        plan("decide_expense_claims", "", decision="reject", note="Client visit was cancelled")
    )
    [offer] = _offers(await _say(session, hr, "reject Meera's auto claim", model))
    await companion.confirm_offer(session, hr, uuid.UUID(offer["action_id"]), request=None)
    [sent] = genie.acts("claim.decide")
    assert sent["params"]["id"] == "1794" and sent["params"]["reference"] == "c1"
    assert sent["params"]["reason"] == "Client visit was cancelled"


async def test_a_lost_answer_is_asked_once_more_with_the_same_key(
    session: AsyncSession, hr: User, genie: FakeGenie, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.companion import expense_actions

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(expense_actions.asyncio, "sleep", no_wait)
    [offer] = _offers(await _say(session, hr, "file this receipt", FakeModel(_receipt())))
    genie.lose = 1
    done = await companion.confirm_offer(session, hr, uuid.UUID(offer["action_id"]), request=None)
    assert done["action"]["status"] == "done", done["action"]
    first, second = genie.acts("claim.create")
    assert first["params"]["key"] == second["params"]["key"] == f"askmira-{offer['action_id']}"
    # Done in ExpenseGenie, and AskMiraa's own log says Ask Miraa did it, as whom.
    row = await session.scalar(
        select(AuditLog).where(AuditLog.entity_id == uuid.UUID(offer["action_id"]))
    )
    assert row is not None and "carried out a expense claim in ExpenseGenie" in row.summary
    assert row.changes["status"] == "done"


async def test_a_lost_answer_twice_says_it_may_have_been_filed(
    session: AsyncSession, hr: User, genie: FakeGenie, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.companion import expense_actions

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(expense_actions.asyncio, "sleep", no_wait)
    [offer] = _offers(await _say(session, hr, "file this receipt", FakeModel(_receipt())))
    genie.lose = 2
    done = await companion.confirm_offer(session, hr, uuid.UUID(offer["action_id"]), request=None)
    assert done["action"]["status"] == "failed"
    assert "check My claims in ExpenseGenie" in done["action"]["result"]["text"]


def test_a_products_float_total_is_said_to_the_paisa() -> None:
    from app.services.companion.expense_actions import _inr

    assert _inr(Decimal("7493.999999999999")) == "₹7,494"
    assert _inr(Decimal("4436.999999999999")) == "₹4,437"
    assert _inr(Decimal("850.5")) == "₹850.50"


def test_a_request_s_own_words_are_not_searched_for() -> None:
    from app.services.companion.expense_actions import _search_words

    assert _search_words("Meera's auto claim") == "Meera auto"
    assert _search_words("Meera" + chr(0x2019) + "s auto claim") == "Meera auto"
    assert _search_words("the claims waiting on me") == ""
