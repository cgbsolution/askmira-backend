"""Saleinx and ProcureFlow linked to a workspace, and an order win across products.

What these hold, scenario by scenario (the user's test prompts A1-C6):

* The person's role in Saleinx and ProcureFlow is the product's own, from its
  people sync (`member_roles`); AskMira grants none. Without one, nothing of
  that product is offered and launching it is refused.
* A lead from a pasted note (A1) becomes one offer with what the note said — no
  close date on a Lead, the decision date in the next step, the follow-up on
  the coming Thursday — and only the confirmed offer reaches Saleinx, signed
  use=act. An update (A2) changes only what changed.
* A stage move checks Saleinx's rules before offering: margin from Qualified
  (B1), Hold's reason and revisit date (B2), one deal when several match (B3),
  a rep's Won waits for a manager (B4), a viewer only reads (B5).
* An order win (C) is held while its Won waits for approval (C6): checked now,
  never opened early, made ready (or booked, if the workspace lets the agent
  act) by the background job once approved, withdrawn if not; it is refused
  when the PO does not match the team's billing (C2); otherwise its figures are worked
  out here, not by the model (C1), the positions open in AnytimeHire with one
  key each, the forecast is written to the finance provider and the deal is
  noted; the same PO again does nothing (C3); a position refused half-way
  (the job quota) withdraws the ones already opened and writes no forecast
  (C4); another workspace sees none of it (C5).
* A message sent twice with the same client_ref is answered once.
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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto
from app.models.accounts import AccountForecast, AccountForecastLine, ClientAccount
from app.models.audit import AuditLog
from app.models.companion import CompanionAction, CompanionRun, CompanionRunStatus
from app.models.product_link import ProductLink
from app.models.tenant import Tenant
from app.models.user import User
from app.services.ai.client import use_backend
from app.services.companion import service as companion
from app.services.finance import list_forecasts
from app.services.products import client as product_client
from app.services.products import connectors
from app.services.products import service as links
from app.services.products import settings as product_settings
from tests.integration.conftest import as_caller, requires_db
from tests.integration.test_companion_flow import (  # noqa: F401  (fixtures)
    FakeModel,
    _consent,
    me,
    plan,
    text,
    tool_call,
)

pytestmark = requires_db

SECRETS = {
    "saleinx": "saleinx-secret-0123456789abcdef0123456789",
    "interview": "interview-secret-0123456789abcdef012345678",
    "procurement": "procure-secret-0123456789abcdef0123456789",
}
HOSTS = {"saleinx": "saleinx.test", "interview": "hire-api.test", "procurement": "pf-api.test"}
PREFIX = {
    "saleinx": "SALEINX_TOOL",
    "interview": "INTERVIEW_TOOL",
    "procurement": "PROCUREMENT_TOOL",
}

KESTREL_ID = "11111111-1111-4111-8111-111111111111"
ORION_ID = "22222222-2222-4222-8222-222222222222"
ORION2_ID = "33333333-3333-4333-8333-333333333333"
LOTUS_ID = "44444444-4444-4444-8444-444444444444"


def _deal(deal_id: str, company: str, name: str, stage: str, **extra: Any) -> dict[str, Any]:
    labels = {"lead": "Lead", "qualified": "Qualified", "proposal": "Proposal", "won": "Won",
              "hold": "Hold", "lost": "Lost", "negotiation": "Negotiation"}  # fmt: skip
    return {
        "id": deal_id,
        "name": name,
        "company": company,
        "title": f"{company} - {name}",
        "contact": "Someone",
        "stage": stage,
        "stage_label": labels[stage],
        "amount": "7110000.00",
        "currency": "INR",
        "margin_pct": None,
        "expected_close_date": None,
        "approval_status": "none",
        "owner": "Ravi Rep",
        "next_step": None,
        **extra,
    }


class FakeProducts:
    """Saleinx, AnytimeHire and ProcureFlow's AskMira endpoints, checking every call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.deals: list[dict[str, Any]] = []
        self.role = "manager"
        self.job_quota = 100
        self.jobs: dict[str, dict[str, Any]] = {}
        self.approvals: list[dict[str, Any]] = []

    def acts(self, product: str, kind: str | None = None) -> list[dict[str, Any]]:
        return [
            c["body"]
            for c in self.calls
            if c["product"] == product
            and c["use"] == "act"
            and (kind is None or c["body"]["kind"] == kind)
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        product = next(p for p, host in HOSTS.items() if host == request.url.host)
        path = request.url.path.rsplit("/integrations/askmira/", 1)[1]
        claims = jwt.decode(
            request.headers["Authorization"].removeprefix("Bearer "),
            SECRETS[product],
            algorithms=["HS256"],
            audience=f"askmira:{product}",
            issuer="askmira",
        )
        expected = {"query": "query", "act": "act", "users/sync": "provision"}[path]
        assert claims["use"] == expected, (path, claims["use"])
        assert claims["bh"] == hashlib.sha256(request.content).hexdigest()
        body = json.loads(request.content)
        assert body["tenant_id"] == claims["sub"]
        self.calls.append({"product": product, "use": claims["use"], "path": path, "body": body})
        if path == "users/sync":
            return httpx.Response(
                200,
                json={"created": 0, "updated": 1, "deactivated": 0, "members": [
                    {"email": u["email"], "role": "member"} for u in body["users"][:1]
                ]},
            )  # fmt: skip
        kind, params = body["kind"], body.get("params") or {}
        actor = {"role": self.role, "sees": "workspace"}
        if product == "saleinx" and path == "query":
            if kind == "deals":
                q = str(params.get("query") or "").lower()
                found = (
                    [d for d in self.deals if q in d["title"].lower()] if q else list(self.deals)
                )
                if params.get("stage"):
                    found = [d for d in found if d["stage"] == params["stage"]]
                return httpx.Response(
                    200, json={"kind": kind, "actor": actor, "total": len(found), "deals": found}
                )
            if kind == "options":
                return httpx.Response(200, json={"kind": kind, "actor": actor, "offerings_required": True,
                    "offerings": [{"id": "o1", "name": "OutSystems"}, {"id": "o2", "name": "Power Platform"}]})  # fmt: skip
            if kind == "approvals":
                waiting = [
                    d
                    for d in self.deals
                    if d["stage"] == "won" and d["approval_status"] == "pending"
                ]
                return httpx.Response(200, json={"kind": kind, "actor": actor, "deals": waiting})
        if product == "saleinx" and path == "act":
            deal = next((d for d in self.deals if d["id"] == params.get("deal_id")), None)
            if kind == "deal.stage" and deal is not None:
                deal["stage"] = params["stage"]
                awaiting = params["stage"] == "won" and self.role == "member"
                deal["approval_status"] = "pending" if awaiting else "none"
                return httpx.Response(
                    200, json={"kind": kind, "deal": deal, "awaiting_approval": awaiting}
                )
            if kind == "lead.save":
                made = deal or _deal(str(uuid.uuid4()), params.get("company", "?"),
                                     (params.get("deal") or {}).get("name", "?"), "lead")  # fmt: skip
                return httpx.Response(
                    200, json={"kind": kind, "deal": made, "done": ["lead added at Lead"]}
                )
            if kind == "deal.note":
                return httpx.Response(200, json={"kind": kind, "deal": deal, "note_id": "n1"})
        if product == "interview" and path == "act":
            if kind == "job.open":
                key = params["key"]
                if key in self.jobs:
                    return httpx.Response(
                        200, json={"kind": kind, "job": self.jobs[key], "existing": True}
                    )
                if len(self.jobs) >= self.job_quota:
                    return httpx.Response(
                        409, json={"detail": "The plan allows 6 job openings and all 6 are used."}
                    )
                self.jobs[key] = {"ref": f"j{len(self.jobs) + 1}", "title": params["title"],
                                  "openings": params["openings"]}  # fmt: skip
                return httpx.Response(
                    200, json={"kind": kind, "job": self.jobs[key], "existing": False}
                )
            if kind == "job.withdraw":
                gone = self.jobs.pop(params["key"], None)
                return httpx.Response(200, json={"kind": kind, "withdrawn": gone is not None})
        if product == "procurement":
            if path == "query" and kind == "approvals":
                return httpx.Response(
                    200, json={"kind": kind, "actor": actor, "approvals": self.approvals}
                )
            if path == "act" and kind == "approval.decide":
                return httpx.Response(
                    200, json={"kind": kind, "approval_id": params["approval_id"]}
                )
        return httpx.Response(422, json={"detail": f"Unexpected {product} {path} {kind}"})


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProducts]:
    for product, prefix in PREFIX.items():
        api = f"https://{HOSTS[product]}" + ("/api" if product == "saleinx" else "")
        monkeypatch.setenv(f"{prefix}_API_URL", api)
        monkeypatch.setenv(f"{prefix}_APP_URL", f"https://{product}-app.test")
        monkeypatch.setenv(f"{prefix}_SECRET", SECRETS[product])
    product_settings.reset_cache()
    products = FakeProducts()
    real = httpx.AsyncClient

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(products.handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(
        product_client,
        "httpx",
        types.SimpleNamespace(
            AsyncClient=client_factory, HTTPError=httpx.HTTPError, Timeout=httpx.Timeout
        ),
    )
    yield products
    product_settings.reset_cache()


async def _link(
    session: AsyncSession, tenant: Tenant, product: str, roles: dict[str, str] | None
) -> None:
    session.add(
        ProductLink(
            tenant_id=tenant.id,
            product=product,
            status="active",
            external_ref="ext-1",
            member_roles=roles,
        )
    )
    settings = dict(tenant.settings or {})
    settings["linked_products"] = {**settings.get("linked_products", {}), product: True}
    tenant.settings = settings
    await session.commit()


@pytest.fixture
async def hr(session: AsyncSession, hr_user: User, me: User) -> User:
    await _consent(session, hr_user.tenant_id, hr_user.id)
    return await as_caller(session, hr_user)


async def _offered(session: AsyncSession, user: User) -> set[str]:
    status = await companion.status_for(session, user)
    return {tool["key"] for tool in status["tools"]}


async def _say(
    session: AsyncSession, user: User, words: str, model: FakeModel, **extra: Any
) -> dict[str, Any]:
    with use_backend(model):
        return await companion.send(
            session, user, companion.MessageIn(text=words, **extra), request=None
        )


def _cards(result: dict[str, Any]) -> list[dict[str, Any]]:
    return result["messages"][1]["cards"]


def _offer(result: dict[str, Any]) -> dict[str, Any]:
    offers = [c for c in _cards(result) if c.get("type") == "proposal"]
    assert offers, _cards(result)
    return offers[-1]


# ── Roles come from the product ─────────────────────────────────────────────


async def test_saleinx_tools_follow_the_role_saleinx_gives(
    session: AsyncSession,
    tenant: Tenant,
    me: User,
    fake: FakeProducts,
) -> None:
    with use_backend(FakeModel()):
        before = await _offered(session, me)
    assert not {"sales_deals", "move_deal", "save_lead"} & before
    # Linked, but Saleinx has not said she is a member: still nothing.
    await _link(session, tenant, "saleinx", {})
    with use_backend(FakeModel()):
        assert not {"sales_deals", "move_deal"} & await _offered(session, me)
    with pytest.raises(Exception) as refused:
        await links.launch(session, me, "saleinx")
    assert "not a member in Saleinx" in str(getattr(refused.value, "detail", refused.value))
    row = await session.scalar(select(ProductLink).where(ProductLink.product == "saleinx"))
    row.member_roles = {me.email: "member"}
    await session.commit()
    with use_backend(FakeModel()):
        offered = await _offered(session, me)
    assert {"sales_deals", "sales_pipeline", "move_deal", "save_lead", "book_order"} <= offered
    assert fake.calls == []


async def test_people_sync_keeps_each_persons_role_there(
    session: AsyncSession, tenant: Tenant, hr: User, fake: FakeProducts
) -> None:
    await _link(session, tenant, "saleinx", None)
    outcome = await links.sync(session, tenant.id, "saleinx", actor=hr)
    assert "error" not in outcome, outcome
    sync = next(c for c in fake.calls if c["path"] == "users/sync")
    assert sync["use"] == "provision" and sync["body"]["complete"] is True
    row = await session.scalar(select(ProductLink).where(ProductLink.product == "saleinx"))
    first = sync["body"]["users"][0]["email"]
    assert row.member_roles == {first: "member"}


# ── A: a lead from free text ────────────────────────────────────────────────


async def test_a_pasted_lead_becomes_one_offer_and_only_the_confirmed_one_reaches_saleinx(
    session: AsyncSession,
    tenant: Tenant,
    me: User,
    fake: FakeProducts,
) -> None:
    await _link(session, tenant, "saleinx", {me.email: "member"})
    fake.role = "member"
    lead = plan(
        "save_lead",
        "Here is the lead; confirm to add it.",
        company="TEST Meridian Steelworks Pvt Ltd",
        contact_name="Rakesh Menon",
        contact_title="VP IT",
        contact_email="rakesh.menon@meridian-steel.example",
        contact_phone="+91 90000 00001",
        deal_name="Dealer portal rebuild",
        description="A Power Platform and OutSystems team to rebuild their dealer portal over about 9 months.",
        amount="12000000",
        decision_by="2026-11-30",
        referred_by="Anita from Bhagat Motors",
        follow_up="Call back Rakesh Menon",
        follow_up_date="2026-11-05",
    )
    result = await _say(
        session, me, "Lead from the Pune manufacturing expo today...", FakeModel(lead)
    )
    offer = _offer(result)
    assert offer["kind"] == "saleinx_lead" and offer["status"] == "proposed"
    figures = {f["label"]: f["value"] for f in offer["figures"]}
    assert figures["Value"] == "₹1,20,00,000"
    assert figures["Next step"] == "Decision expected by 30 Nov 2026"
    assert figures["Sells"] == "OutSystems, Power Platform"
    assert "Thu 05 Nov 2026" in figures["Follow-up"]
    assert any("no close date" in n for n in offer["notes"])
    assert fake.acts("saleinx") == []  # nothing changed yet

    await companion.confirm_offer(session, me, uuid.UUID(offer["action_id"]), request=None)
    [sent] = fake.acts("saleinx", "lead.save")
    params = sent["params"]
    assert params["company"] == "TEST Meridian Steelworks Pvt Ltd"
    assert params["contact"]["title"] == "VP IT" and params["contact"]["first_name"] == "Rakesh"
    assert params["deal"]["amount"] == "12000000.00"
    assert params["deal"]["next_step"] == "Decision expected by 30 Nov 2026"
    assert "expected_close_date" not in json.dumps(params)
    assert params["follow_up"] == {
        "title": "Call back Rakesh Menon",
        "due_date": "2026-11-05",
        "type": "call",
    }
    assert "Referred by Anita from Bhagat Motors." in params["note"]
    assert sent["actor"]["email"] == me.email and sent["actor"]["role"] == "member"


async def test_new_details_for_a_known_lead_change_only_what_changed(
    session: AsyncSession,
    tenant: Tenant,
    me: User,
    fake: FakeProducts,
) -> None:
    await _link(session, tenant, "saleinx", {me.email: "member"})
    meridian = "55555555-5555-4555-8555-555555555555"
    fake.deals = [
        _deal(meridian, "TEST Meridian Steelworks Pvt Ltd", "Dealer portal rebuild", "lead")
    ]
    update = plan(
        "save_lead",
        "Updated.",
        deal="TEST Meridian Steelworks",
        contact_title="CIO",
        contact_phone="+91 90000 00002",
    )
    result = await _say(
        session, me, "Update on Rakesh Menon: now CIO, new number", FakeModel(update)
    )
    offer = _offer(result)
    await companion.confirm_offer(session, me, uuid.UUID(offer["action_id"]), request=None)
    [sent] = fake.acts("saleinx", "lead.save")
    assert sent["params"] == {
        "deal_id": meridian,
        "contact": {"title": "CIO", "phone": "+91 90000 00002"},
    }


async def test_a_viewer_is_told_they_only_read(
    session: AsyncSession,
    tenant: Tenant,
    me: User,
    fake: FakeProducts,
) -> None:
    await _link(session, tenant, "saleinx", {me.email: "viewer"})
    fake.deals = [_deal(ORION_ID, "TEST Orion Logistics", "Fleet Portal", "qualified")]
    move = plan("move_deal", "You can't.", deal="TEST Orion Logistics - Fleet Portal", deal_stage="lost",
                reason="went with a competitor")  # fmt: skip
    result = await _say(session, me, "Move it to Lost", FakeModel(move))
    [card] = _cards(result)
    assert card["type"] == "notice" and "view-only access in Saleinx" in card["text"]
    assert fake.calls == []  # not even read: the role said it all


# ── B: stage changes ────────────────────────────────────────────────────────


async def test_qualified_needs_a_margin_and_the_offer_carries_it(
    session: AsyncSession,
    tenant: Tenant,
    me: User,
    fake: FakeProducts,
) -> None:
    await _link(session, tenant, "saleinx", {me.email: "member"})
    fake.role = "member"
    fake.deals = [_deal(ORION_ID, "TEST Orion Logistics", "Fleet Portal", "lead")]
    first = plan("move_deal", "What margin?", deal="TEST Orion Logistics - Fleet Portal",
                 deal_stage="qualified", next_step="Proposal presentation on 15 Nov 2026")  # fmt: skip
    asked = await _say(session, me, "Move it to Qualified", FakeModel(first))
    [card] = _cards(asked)
    assert card["type"] == "notice" and "margin" in card["text"]
    assert not fake.acts("saleinx")
    second = plan("move_deal", "Confirm?", deal="TEST Orion Logistics - Fleet Portal",
                  deal_stage="qualified", margin_pct="32",
                  next_step="Proposal presentation on 15 Nov 2026")  # fmt: skip
    result = await _say(session, me, "Margin is about 32%.", FakeModel(second),
                        conversation_id=uuid.UUID(asked["conversation"]["id"]))  # fmt: skip
    offer = _offer(result)
    figures = {f["label"]: f["value"] for f in offer["figures"]}
    assert figures["Move"] == "Lead → Qualified" and figures["Margin"] == "32%"
    await companion.confirm_offer(session, me, uuid.UUID(offer["action_id"]), request=None)
    [sent] = fake.acts("saleinx", "deal.stage")
    assert sent["params"] == {
        "deal_id": ORION_ID,
        "stage": "qualified",
        "margin_pct": "32",
        "next_step": "Proposal presentation on 15 Nov 2026",
    }


async def test_hold_needs_a_reason_and_a_revisit_date(
    session: AsyncSession,
    tenant: Tenant,
    me: User,
    fake: FakeProducts,
) -> None:
    await _link(session, tenant, "saleinx", {me.email: "member"})
    fake.deals = [
        _deal(ORION_ID, "TEST Orion Logistics", "Fleet Portal", "qualified", margin_pct="32.00")
    ]
    bare = plan("move_deal", "Why?", deal="TEST Orion Logistics - Fleet Portal", deal_stage="hold")
    [card] = _cards(await _say(session, me, "On hold", FakeModel(bare)))
    assert "Why is it on hold" in card["text"] and "revisit" in card["text"]
    full = plan("move_deal", "Confirm?", deal="TEST Orion Logistics - Fleet Portal", deal_stage="hold",
                hold_reason="budget_deferred", revisit_on="2027-01-01",
                reason="CFO froze spend until January")  # fmt: skip
    offer = _offer(await _say(session, me, "CFO froze spend until January", FakeModel(full)))
    figures = {f["label"]: f["value"] for f in offer["figures"]}
    assert (
        figures["Why on hold"] == "Budget deferred" and figures["Revisit on"] == "Fri 01 Jan 2027"
    )


async def test_two_matching_deals_are_listed_not_guessed(
    session: AsyncSession,
    tenant: Tenant,
    me: User,
    fake: FakeProducts,
) -> None:
    await _link(session, tenant, "saleinx", {me.email: "member"})
    fake.deals = [
        _deal(ORION_ID, "TEST Orion Logistics", "Fleet Portal", "qualified", margin_pct="32.00"),
        _deal(ORION2_ID, "TEST Orion Logistics", "Warehouse App", "qualified", margin_pct="28.00"),
    ]
    move = plan("move_deal", "Which one?", deal="Orion Logistics", deal_stage="proposal")
    cards = _cards(await _say(session, me, "Move Orion Logistics to Proposal.", FakeModel(move)))
    assert cards[0]["type"] == "table" and len(cards[0]["rows"]) == 2
    assert "Which one did you mean" in cards[1]["text"]
    assert not fake.acts("saleinx")


async def test_a_reps_won_waits_for_a_manager(
    session: AsyncSession,
    tenant: Tenant,
    me: User,
    fake: FakeProducts,
) -> None:
    await _link(session, tenant, "saleinx", {me.email: "member"})
    fake.role = "member"
    fake.deals = [_deal(KESTREL_ID, "TEST Kestrel Auto Components", "Support Pod", "negotiation",
                        margin_pct="37.00", expected_close_date="2026-11-03")]  # fmt: skip
    move = plan(
        "move_deal", "Confirm?", deal="TEST Kestrel Auto Components - Support Pod", deal_stage="won"
    )
    offer = _offer(await _say(session, me, "Kestrel signed. Mark it won.", FakeModel(move)))
    assert any("waits for a manager's approval" in n for n in offer["notes"])
    done = await companion.confirm_offer(session, me, uuid.UUID(offer["action_id"]), request=None)
    assert "waits for a manager's approval" in done["message"]["text"]


# ── C: the order win ────────────────────────────────────────────────────────

TEAM = [
    {"title": "OutSystems developer", "count": "2", "experience": "4 to 6", "allocation": "100",
     "bill_rate": "190000", "cost_rate": "115000", "skills": "OutSystems"},
    {"title": "Power Platform consultant", "count": "1", "experience": "3 to 5", "allocation": "100",
     "bill_rate": "170000", "cost_rate": "100000", "skills": "Power Apps, Power Automate"},
    {"title": "QA engineer", "count": "1", "experience": "3 to 5", "allocation": "100",
     "bill_rate": "120000", "cost_rate": "70000"},
    {"title": "Project manager", "count": "1", "experience": "8+", "allocation": "50",
     "bill_rate": "240000", "cost_rate": "140000"},
]  # fmt: skip


def _order(
    po: str = "KAC-PO-2291", value: str = "7110000", deal: str = "TEST Kestrel Auto Components"
) -> dict[str, Any]:
    return plan(
        "book_order",
        "Here is the plan.",
        deal=deal,
        po_number=po,
        po_value=value,
        start_date="2026-12-01",
        months=9,
        location="Pune",
        work_mode="Hybrid",
        recruitment_cost_per_hire="50000",
        roles=TEAM,
    )


async def _orders(
    session: AsyncSession, tenant: Tenant, hr: User, fake: FakeProducts, *, approved: bool
) -> None:
    await _link(session, tenant, "saleinx", {hr.email: "manager"})
    await _link(session, tenant, "interview", None)
    fake.deals = [
        _deal(KESTREL_ID, "TEST Kestrel Auto Components", "Support Pod", "won",
              approval_status="approved" if approved else "pending", margin_pct="37.00"),
        _deal(LOTUS_ID, "TEST Lotus Retail", "Store Ops", "won", approval_status="approved", margin_pct="37.00"),
    ]  # fmt: skip


async def test_an_order_is_held_while_its_won_waits_for_approval(
    session: AsyncSession, tenant: Tenant, hr: User, fake: FakeProducts
) -> None:
    """C6: checked and held, never opened before the manager approves; then ready to book."""
    from datetime import UTC, datetime

    from app.models.companion import CompanionMessage
    from app.services.companion import order_win

    await _orders(session, tenant, hr, fake, approved=False)
    offer = _offer(await _say(session, hr, "Kestrel has signed...", FakeModel(_order())))
    assert offer["status"] == "proposed" and "waits for a manager's approval" in offer["waiting"]
    figures = {f["label"]: f["value"] for f in offer["figures"]}
    assert figures["Billing in all"] == "₹71,10,000"  # checked now, not later
    # It can't be booked while held: refused, and still held.
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as refused:
        await companion.confirm_offer(session, hr, uuid.UUID(offer["action_id"]), request=None)
    assert refused.value.status_code == 409 and "manager's approval" in refused.value.detail
    row = await session.get(CompanionAction, uuid.UUID(offer["action_id"]))
    assert row.status.value == "proposed"
    assert not [c for c in fake.calls if c["use"] == "act"]

    # The background job: still waiting, nothing changes.
    actor = await as_caller(session, hr)
    tally = await order_win.resume_held(session, actor, now=datetime.now(UTC))
    assert tally["waiting"] == 1 and tally["ready"] == 0
    assert not [c for c in fake.calls if c["use"] == "act"]

    # The manager approves the Won in Saleinx: the order is ready, and the person is told.
    fake.deals[0]["approval_status"] = "approved"
    tally = await order_win.resume_held(session, actor, now=datetime.now(UTC))
    assert tally["ready"] == 1 and tally["booked"] == 0
    assert not [c for c in fake.calls if c["use"] == "act"]  # told, not booked
    said = (
        await session.scalars(
            select(CompanionMessage.text).where(CompanionMessage.role == "assistant")
        )
    ).all()
    assert any("is ready" in t and "KAC-PO-2291" in t for t in said)
    done = await companion.confirm_offer(session, hr, uuid.UUID(offer["action_id"]), request=None)
    assert done["action"]["status"] == "done", done["action"]
    assert len(fake.acts("interview", "job.open")) == 4


async def test_a_held_order_is_booked_by_itself_when_the_workspace_lets_the_agent_act(
    session: AsyncSession, tenant: Tenant, hr: User, fake: FakeProducts
) -> None:
    from datetime import UTC, datetime

    from app.services.autonomy import service as autonomy
    from app.services.autonomy.catalogue import AutonomyLevel
    from app.services.companion import order_win

    await _orders(session, tenant, hr, fake, approved=False)
    await autonomy.configure(
        session,
        hr,
        action_key=order_win.BOOK_AFTER_APPROVAL,
        autonomy=AutonomyLevel.AUTO_APPLY_WITH_NOTIFY,
        threshold_value=None,
        request=None,
    )
    offer = _offer(await _say(session, hr, "Kestrel has signed...", FakeModel(_order())))
    fake.deals[0]["approval_status"] = "approved"
    tally = await order_win.resume_held(
        session, await as_caller(session, hr), now=datetime.now(UTC)
    )
    assert tally["booked"] == 1
    assert len(fake.acts("interview", "job.open")) == 4
    row = await session.get(CompanionAction, uuid.UUID(offer["action_id"]))
    assert row.decided_via == "automation" and row.status.value == "done"


async def test_a_held_order_is_withdrawn_when_the_won_is_not_approved(
    session: AsyncSession, tenant: Tenant, hr: User, fake: FakeProducts
) -> None:
    from datetime import UTC, datetime

    from app.services.companion import order_win

    await _orders(session, tenant, hr, fake, approved=False)
    offer = _offer(await _say(session, hr, "Kestrel has signed...", FakeModel(_order())))
    # The manager rejects the Won: Saleinx puts the deal back to Negotiation.
    fake.deals[0].update(stage="negotiation", stage_label="Negotiation", approval_status="rejected")
    tally = await order_win.resume_held(
        session, await as_caller(session, hr), now=datetime.now(UTC)
    )
    assert tally["withdrawn"] == 1
    row = await session.get(CompanionAction, uuid.UUID(offer["action_id"]))
    assert row.status.value == "cancelled" and "no longer Won" in row.result["text"]
    assert not [c for c in fake.calls if c["use"] == "act"]


async def test_booking_checks_the_won_again(
    session: AsyncSession, tenant: Tenant, hr: User, fake: FakeProducts
) -> None:
    """Approved when offered, taken back before Confirm: nothing is opened."""
    await _orders(session, tenant, hr, fake, approved=True)
    offer = _offer(await _say(session, hr, "Kestrel has signed...", FakeModel(_order())))
    fake.deals[0]["approval_status"] = "pending"
    done = await companion.confirm_offer(session, hr, uuid.UUID(offer["action_id"]), request=None)
    assert done["action"]["status"] == "failed"
    assert "not Won and approved" in done["action"]["result"]["text"]
    assert not fake.acts("interview", "job.open")


async def test_a_po_that_does_not_match_the_team_is_refused_with_both_figures(
    session: AsyncSession, tenant: Tenant, hr: User, fake: FakeProducts
) -> None:
    await _orders(session, tenant, hr, fake, approved=True)
    [card] = _cards(
        await _say(
            session, hr, "Kestrel signed for 1.8 crore", FakeModel(_order(value="1.8 crore"))
        )
    )
    assert "The PO says ₹1,80,00,000" in card["text"]
    assert "₹7,90,000 a month: ₹71,10,000 over 9 months" in card["text"]
    assert not [c for c in fake.calls if c["use"] == "act"]


async def test_an_order_win_opens_positions_writes_the_forecast_and_is_never_done_twice(
    session: AsyncSession, tenant: Tenant, hr: User, fake: FakeProducts
) -> None:
    await _orders(session, tenant, hr, fake, approved=True)
    result = await _say(session, hr, "Kestrel has signed...", FakeModel(_order()))
    offer = _offer(result)
    figures = {f["label"]: f["value"] for f in offer["figures"]}
    assert figures["Billing a month"] == "₹7,90,000"
    assert figures["Billing in all"] == "₹71,10,000"
    assert figures["People cost"] == "₹42,30,000"
    assert figures["Recruitment"] == "₹2,50,000"
    assert figures["Margin"] == "₹26,30,000 (37.0%)"
    assert offer["table"]["rows"][3][2] == "50%"
    assert not [c for c in fake.calls if c["use"] == "act"]

    done = await companion.confirm_offer(session, hr, uuid.UUID(offer["action_id"]), request=None)
    assert done["action"]["status"] == "done", done["action"]
    opened = fake.acts("interview", "job.open")
    assert [o["params"]["key"] for o in opened] == [f"KAC-PO-2291:{i}" for i in range(1, 5)]
    assert opened[0]["params"]["openings"] == 2 and opened[3]["params"]["experience_max"] == 15
    assert opened[0]["actor"]["role"] == "admin"
    [note] = fake.acts("saleinx", "deal.note")
    assert note["params"]["deal_id"] == KESTREL_ID and "KAC-PO-2291" in note["params"]["body"]

    [forecast] = await list_forecasts(session, hr)
    assert (
        forecast["reference"] == "KAC-PO-2291"
        and forecast["account_name"] == "TEST Kestrel Auto Components"
    )
    assert Decimal(forecast["billing"]) == Decimal("7110000")
    assert Decimal(forecast["cost"]) == Decimal(
        "4480000"
    )  # 42.3 lakh people + 2.5 lakh recruitment
    assert Decimal(forecast["margin"]) == Decimal("2630000")
    assert forecast["start_month"] == "2026-12" and len(forecast["by_month"]) == 9
    assert forecast["by_month"][0]["recruitment"] == "250000.00"
    lines = await session.scalar(select(func.count(AccountForecastLine.id)))
    assert lines == 4 * 2 * 9 + 1
    run = await session.scalar(select(CompanionRun))
    assert run.status is CompanionRunStatus.DONE and run.business_key == "order_win:KAC-PO-2291"
    trail = (
        await session.scalars(
            select(AuditLog.summary).where(AuditLog.entity_type == "companion_run")
        )
    ).all()
    assert any("booked" in s for s in trail)

    # C3: the same message again books nothing.
    before = len(fake.calls)
    [again] = _cards(await _say(session, hr, "Kestrel has signed...", FakeModel(_order())))
    assert again["type"] == "notice" and "already booked" in again["text"]
    assert len([c for c in fake.calls[before:] if c["use"] == "act"]) == 0
    assert await session.scalar(select(func.count(AccountForecast.id))) == 1


async def test_a_position_refused_half_way_undoes_the_ones_opened_and_writes_no_forecast(
    session: AsyncSession, tenant: Tenant, hr: User, fake: FakeProducts
) -> None:
    await _orders(session, tenant, hr, fake, approved=True)
    fake.job_quota = 2  # the plan runs out at the third position
    offer = _offer(await _say(session, hr, "Lotus signed, same team", FakeModel(
        _order(po="LR-PO-1180", deal="TEST Lotus Retail"))))  # fmt: skip
    done = await companion.confirm_offer(session, hr, uuid.UUID(offer["action_id"]), request=None)
    assert done["action"]["status"] == "failed"
    text_ = done["message"]["text"]
    assert "AnytimeHire refused the position “QA engineer”" in text_
    assert (
        "2 position(s) already opened were withdrawn" in text_
        and "Nothing from this order was kept" in text_
    )
    assert [w["params"]["key"] for w in fake.acts("interview", "job.withdraw")] == [
        "LR-PO-1180:2",
        "LR-PO-1180:1",
    ]
    assert fake.jobs == {}
    assert await session.scalar(select(func.count(AccountForecast.id))) == 0
    assert not fake.acts("saleinx", "deal.note")
    run = await session.scalar(select(CompanionRun))
    assert run.status is CompanionRunStatus.UNDONE
    # Undone, the PO may be tried again once the plan has room.
    fake.job_quota = 100
    retry = _offer(await _say(session, hr, "Try Lotus again", FakeModel(
        _order(po="LR-PO-1180", deal="TEST Lotus Retail"))))  # fmt: skip
    assert retry["status"] == "proposed"


async def test_a_forecast_that_cannot_be_written_withdraws_the_positions(
    session: AsyncSession,
    tenant: Tenant,
    hr: User,
    fake: FakeProducts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    from app.services.finance import provider

    await _orders(session, tenant, hr, fake, approved=True)
    offer = _offer(await _say(session, hr, "Kestrel has signed...", FakeModel(_order())))

    async def refuse(*args: Any, **kwargs: Any) -> Any:
        raise HTTPException(status_code=409, detail="the ledger is closed for maintenance")

    monkeypatch.setattr(provider.AskMiraLedger, "write_forecast", refuse)
    done = await companion.confirm_offer(session, hr, uuid.UUID(offer["action_id"]), request=None)
    assert done["action"]["status"] == "failed"
    assert (
        "The P&L could not be written: the ledger is closed for maintenance"
        in done["message"]["text"]
    )
    assert len(fake.acts("interview", "job.withdraw")) == 4 and fake.jobs == {}
    assert await session.scalar(select(func.count(ClientAccount.id))) == 0


async def test_another_workspace_sees_none_of_it(
    session: AsyncSession, tenant: Tenant, hr: User, other_hr_user: User, fake: FakeProducts
) -> None:
    await _orders(session, tenant, hr, fake, approved=True)
    offer = _offer(await _say(session, hr, "Kestrel has signed...", FakeModel(_order())))
    await companion.confirm_offer(session, hr, uuid.UUID(offer["action_id"]), request=None)
    other = await as_caller(session, other_hr_user)
    assert await list_forecasts(session, other) == []
    with use_backend(FakeModel()):
        assert "sales_deals" not in await _offered(session, other)
    assert (
        await session.scalar(
            select(func.count(CompanionAction.id)).where(
                CompanionAction.tenant_id == other.tenant_id
            )
        )
        == 0
    )


# ── ProcureFlow ─────────────────────────────────────────────────────────────


async def test_procurement_approvals_are_decided_as_the_person(
    session: AsyncSession,
    tenant: Tenant,
    me: User,
    fake: FakeProducts,
) -> None:
    await _link(session, tenant, "procurement", {me.email: "DEPT_HEAD"})
    fake.approvals = [{"ref": "apr_1", "document": "REQUISITION", "number": "PR-0042",
                       "title": "Laptops", "submitted_by": "Ravi", "amount": "240000.00",
                       "currency": "INR", "days_pending": 2}]  # fmt: skip
    look = plan("procurement_approvals", "One waits on you.")
    looked = await _say(session, me, "what is waiting on me in procurement?", FakeModel(look))
    [table] = _cards(looked)
    assert "Ravi" not in table["model_view"] and "pf:apr_1" in table["model_view"]
    decide = plan("decide_procurement", "Confirm?", refs="pf:apr_1", decision="approve")
    offer = _offer(await _say(session, me, "approve it", FakeModel(decide)))
    await companion.confirm_offer(session, me, uuid.UUID(offer["action_id"]), request=None)
    [sent] = fake.acts("procurement", "approval.decide")
    assert sent["params"] == {"approval_id": "apr_1", "decision": "approve", "comment": None}


# ── A message sent twice ────────────────────────────────────────────────────


async def test_a_message_sent_twice_is_answered_once(
    session: AsyncSession,
    tenant: Tenant,
    me: User,
    fake: FakeProducts,
) -> None:
    model = FakeModel(plan(reply="Hello Anita."))
    first = await _say(session, me, "hello", model, client_ref="msg-0001-abcdef")
    second = await _say(session, me, "hello", model, client_ref="msg-0001-abcdef")
    assert second["repeat"] is True
    assert [m["id"] for m in second["messages"]] == [m["id"] for m in first["messages"]]
    assert len(model.calls) == 1


# ── Where the products are ──────────────────────────────────────────────────


async def test_connectors_find_saleinx_and_procureflow_at_their_own_domains(
    session: AsyncSession, hr: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing set on this server: the products' production domains are built in.
    for prefix in ("SALEINX_TOOL", "PROCUREMENT_TOOL"):
        for suffix in ("API_URL", "APP_URL"):
            monkeypatch.setenv(f"{prefix}_{suffix}", "")
    product_settings.reset_cache()
    monkeypatch.setattr(connectors, "can_encrypt", lambda: True)
    monkeypatch.setattr(connectors, "encrypt_field", lambda text: f"encf:{text}")
    monkeypatch.setattr(crypto, "can_encrypt", lambda: True)
    try:
        listed = {c["key"]: c for c in await connectors.overview(session, hr)}
        sales, procure = listed["saleinx"], listed["procurement"]
        assert sales["available"] is True and procure["available"] is True
        assert (sales["app_url"], sales["api_url"]) == (
            "https://crm.artboxsolutions.ai",
            "https://crm.artboxsolutions.ai/api",
        )
        assert (procure["app_url"], procure["api_url"]) == (
            "https://vendor.artboxsolutions.ai",
            "https://vendor.artboxsolutions.ai/api/v1",
        )
        for key, host in (("saleinx", "crm"), ("procurement", "vendor")):
            started = await connectors.start(session, hr, key)
            assert started["authorize_url"].startswith(
                f"https://{host}.artboxsolutions.ai/connect/askmira?"
            )
    finally:
        product_settings.reset_cache()
