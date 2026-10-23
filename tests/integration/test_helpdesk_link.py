"""CGB Helpdesk linked to a workspace: its tickets and assets in the portal and in Ask Mira.

What these hold: the helpdesk is read for the caller's own workspace and as the
caller (a signed `query` token bound to the body), with the role AskMira gives
them there (HR "admin", everyone else "user"); raising a ticket and replying
go as signed `act` tokens, never `query`; each is audited in AskMira without
the subject or the message; a refusal from the helpdesk reaches the person in
its own words and with its own status; nothing is asked when the helpdesk is
not linked; and the Helpdesk and Assets pages fix whose records they show.
"""

from __future__ import annotations

import hashlib
import json
import types
from collections.abc import Iterator
from typing import Any

import httpx
import jwt
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditAction, AuditLog
from app.models.employee import Employee
from app.models.product_link import ProductLink
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.ai.client import use_backend
from app.services.companion import service as companion
from app.services.products import client as product_client
from app.services.products import connectors, helpdesk, views
from app.services.products import settings as product_settings
from app.services.products.catalogue import PRODUCTS
from tests.integration.conftest import as_caller, requires_db
from tests.integration.test_companion_flow import (  # noqa: F401  (fixtures)
    FakeModel,
    _consent,
    me,
    plan,
)

pytestmark = requires_db

SECRET = "helpdesk-secret-0123456789abcdef0123456789"
TICKET = "3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
HELPDESK_TOOLS = {"helpdesk_tickets", "helpdesk_overview", "helpdesk_assets"}


class FakeHelpdesk:
    """The helpdesk's query and act endpoints, checking every call as the helpdesk does."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answers: dict[tuple[str, str], Any] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "helpdesk.test"
        path = request.url.path.removeprefix("/api/integrations/askmira/")
        assert path in ("query", "act"), path
        claims = jwt.decode(
            request.headers["Authorization"].removeprefix("Bearer "),
            SECRET,
            algorithms=["HS256"],
            audience="askmira:helpdesk",
            issuer="askmira",
        )
        # A read is signed use=query; anything that changes the helpdesk, use=act.
        assert claims["use"] == path
        assert claims["bh"] == hashlib.sha256(request.content).hexdigest()
        body = json.loads(request.content)
        assert body["tenant_id"] == claims["sub"]
        self.calls.append({"use": claims["use"], "body": body})
        answer = self.answers.get((path, body["kind"]), {})
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json={"kind": body["kind"], **answer})


@pytest.fixture
def desk(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeHelpdesk]:
    monkeypatch.setenv("HELPDESK_TOOL_API_URL", "https://helpdesk.test/api")
    monkeypatch.setenv("HELPDESK_TOOL_APP_URL", "https://helpdesk.test")
    monkeypatch.setenv("HELPDESK_TOOL_SECRET", SECRET)
    product_settings.reset_cache()
    fake = FakeHelpdesk()
    real = httpx.AsyncClient

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(
        product_client,
        "httpx",
        types.SimpleNamespace(
            AsyncClient=client_factory, HTTPError=httpx.HTTPError, Timeout=httpx.Timeout
        ),
    )
    yield fake
    product_settings.reset_cache()


async def _link(session: AsyncSession, tenant: Tenant) -> None:
    session.add(
        ProductLink(tenant_id=tenant.id, product="helpdesk", status="active", external_ref="org-1")
    )
    settings = dict(tenant.settings or {})
    settings["linked_products"] = {"helpdesk": True}
    tenant.settings = settings
    await session.commit()


@pytest.fixture
async def hr(session: AsyncSession, hr_user: User, me: User) -> User:  # noqa: F811
    await _consent(session, hr_user.tenant_id, hr_user.id)
    return await as_caller(session, hr_user)


def test_everyone_uses_the_helpdesk_and_hr_looks_after_it() -> None:
    product = PRODUCTS["helpdesk"]
    assert product.brand == "CGB Helpdesk"
    for role in (UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.SUB_ADMIN):
        assert product.role_for(role, is_manager=False) == "admin"
    assert product.role_for(UserRole.EMPLOYEE, is_manager=False) == "user"
    assert product.role_for(UserRole.EMPLOYEE, is_manager=True) == "user"


async def test_helpdesk_tools_follow_the_link(
    session: AsyncSession,
    tenant: Tenant,
    me: User,  # noqa: F811
    desk: FakeHelpdesk,
) -> None:
    async def offered() -> set[str]:
        status = await companion.status_for(session, me)
        return {tool["key"] for tool in status["tools"]} & HELPDESK_TOOLS

    with use_backend(FakeModel()):
        assert await offered() == set()
        await _link(session, tenant)
        assert await offered() == HELPDESK_TOOLS
    assert desk.calls == []


async def test_my_tickets_are_read_as_me_and_open_in_the_portal(
    session: AsyncSession,
    tenant: Tenant,
    me: User,  # noqa: F811
    desk: FakeHelpdesk,
) -> None:
    await _link(session, tenant)
    desk.answers[("query", "tickets")] = {
        "scope": "mine",
        "total": 2,
        "tickets": [
            {
                "id": TICKET,
                "ref": "3F2B1C4D",
                "subject": "VPN drops every hour",
                "category": "Network & Connectivity",
                "priority": "high",
                "status": "in_progress",
                "assignee": "Ravi (IT)",
                "created_at": "2026-10-21T05:30:00Z",
            },
            # An id that is not a plain token gives no link, never a crafted one.
            {"id": "../../admin", "ref": "X", "subject": "Odd", "status": "new"},
        ],
    }
    # The page fixes whose tickets; asking for the company's on "My tickets" changes nothing.
    page = await views.render(
        session, me, "helpdesk", "tickets", {"scope": "company", "status": "in_progress"}
    )
    body = desk.calls[-1]["body"]
    assert body["kind"] == "tickets" and body["tenant_id"] == str(tenant.id)
    assert body["params"]["scope"] == "mine" and body["params"]["status"] == "in_progress"
    assert body["actor"] == {"id": str(me.id), "email": "anita@acme.test", "role": "user"}
    card = page["cards"][0]
    assert card["source"] == "CGB Helpdesk" and card["open"] == "/launch/helpdesk"
    assert card["rows"][0][:5] == [
        "#3F2B1C4D",
        "VPN drops every hour",
        "Network & Connectivity",
        "High",
        "In progress",
    ]
    assert card["links"] == [f"/linked/helpdesk/tickets/{TICKET}", None]


async def test_hr_reads_the_company_as_the_helpdesk_admin_and_refusals_are_shown(
    session: AsyncSession,
    tenant: Tenant,
    me: User,  # noqa: F811
    hr: User,
    desk: FakeHelpdesk,
) -> None:
    await _link(session, tenant)
    desk.answers[("query", "assets")] = {
        "scope": "company",
        "total": 1,
        "assets": [
            {
                "code": "LAP-001",
                "type": "Laptop",
                "model": "ThinkPad T14",
                "serial_no": "SN123",
                "status": "in_use",
                "organization": "Hindustan Times",
                "holder": "Anita Sharma",
                "assigned_at": "2026-09-01T04:00:00Z",
                "warranty_expiry": "2028-08-31",
            }
        ],
    }
    page = await views.render(session, hr, "helpdesk", "register", {"status": "in_use"})
    body = desk.calls[-1]["body"]
    assert body["actor"]["role"] == "admin" and body["params"]["scope"] == "company"
    card = page["cards"][0]
    assert card["title"] == "Asset register"
    # The helpdesk serves several organizations: the register says whose each asset is.
    assert card["columns"][1] == "Organization" and card["rows"][0][1] == "Hindustan Times"
    assert card["columns"][6] == "Held by" and card["rows"][0][6] == "Anita Sharma"

    # An employee asking for the register gets the helpdesk's own refusal, not the data.
    desk.answers[("query", "assets")] = httpx.Response(
        403, json={"error": "Only CGB Helpdesk's IT team and your HR see the asset register."}
    )
    refused = await views.render(session, me, "helpdesk", "register", {})
    assert refused["cards"][0]["tone"] == "warning"
    assert "IT team and your HR" in refused["cards"][0]["text"]


async def test_the_register_and_ticket_lists_page_through_the_helpdesk(
    session: AsyncSession,
    tenant: Tenant,
    hr: User,
    desk: FakeHelpdesk,
) -> None:
    await _link(session, tenant)
    desk.answers[("query", "assets")] = {
        "scope": "company",
        "total": 1604,
        "assets": [{"code": f"HT-{n}", "organization": "HT", "status": "spare"} for n in range(50)],
    }
    # A portal page asks for one page of 50 rows at a time, from where that page starts.
    second = await views.render(session, hr, "helpdesk", "register", {"page": "2"})
    params = desk.calls[-1]["body"]["params"]
    assert params["limit"] == 50 and params["offset"] == 50
    card = second["cards"][0]
    assert card["page"] == {"number": 2, "size": 50, "total": 1604}
    assert card["caption"].endswith("51 to 100 of 1604 assets")

    # No page given (Ask Mira, a first visit): the first rows, and no pager.
    first = await views.render(session, hr, "helpdesk", "register", {})
    params = desk.calls[-1]["body"]["params"]
    assert params["offset"] == 0 and "page" not in first["cards"][0]
    # Rubbish for a page is the first page.
    await views.render(session, hr, "helpdesk", "register", {"page": "x"})
    assert desk.calls[-1]["body"]["params"]["offset"] == 0

    desk.answers[("query", "tickets")] = {"scope": "company", "total": 120, "tickets": []}
    await views.render(session, hr, "helpdesk", "all", {"page": "3"})
    params = desk.calls[-1]["body"]["params"]
    assert params["offset"] == 100 and params["limit"] == 50 and params["scope"] == "company"


async def test_one_ticket_and_its_conversation(
    session: AsyncSession,
    tenant: Tenant,
    me: User,  # noqa: F811
    desk: FakeHelpdesk,
) -> None:
    await _link(session, tenant)
    desk.answers[("query", "ticket")] = {
        "ticket": {"id": TICKET, "ref": "3F2B1C4D", "subject": "VPN", "can_reply": True},
        "messages": [{"id": "m1", "from": "Ravi", "side": "support", "text": "On it", "at": "x"}],
        "activity": [{"action": "created", "by": "Anita Sharma", "at": "x"}],
    }
    page = await helpdesk.ticket(session, me, TICKET.upper())
    assert desk.calls[-1]["body"]["params"] == {"id": TICKET}
    assert page["messages"][0]["text"] == "On it"
    assert page["open"] == f"/launch/helpdesk?to=/ticket/{TICKET}"

    # Not an id: the helpdesk is never asked.
    calls = len(desk.calls)
    with pytest.raises(HTTPException) as missing:
        await helpdesk.ticket(session, me, "../../etc")
    assert missing.value.status_code == 404 and len(desk.calls) == calls

    # Someone else's ticket: the helpdesk refuses, and so does the page.
    desk.answers[("query", "ticket")] = httpx.Response(
        403, json={"error": "There is no such ticket in CGB Helpdesk for you."}
    )
    with pytest.raises(HTTPException) as refused:
        await helpdesk.ticket(session, me, TICKET)
    assert refused.value.status_code == 403
    assert refused.value.detail == "There is no such ticket in CGB Helpdesk for you."


async def test_raising_a_ticket_is_an_action_and_is_audited_without_its_words(
    session: AsyncSession,
    tenant: Tenant,
    me: User,  # noqa: F811
    desk: FakeHelpdesk,
) -> None:
    await _link(session, tenant)
    desk.answers[("act", "create_ticket")] = {
        "ticket": {"id": TICKET, "ref": "3F2B1C4D", "subject": "Laptop fan noisy", "status": "new"}
    }
    fields = {
        "subject": "Laptop fan noisy",
        "description": "Loud since Monday",
        "category": "Hardware",
        "sub_category": "Laptop Issue",
        "priority": "medium",
        "asset_id": "a1",
    }
    result = await helpdesk.raise_ticket(session, me, fields, request=None)
    call = desk.calls[-1]
    assert call["use"] == "act" and call["body"]["kind"] == "create_ticket"
    assert call["body"]["params"] == fields
    assert call["body"]["actor"]["email"] == "anita@acme.test"
    assert result["page"] == f"/linked/helpdesk/tickets/{TICKET}"

    row = (
        await session.scalars(select(AuditLog).where(AuditLog.entity_type == "helpdesk_ticket"))
    ).one()
    assert row.action is AuditAction.CREATE and row.actor_user_id == me.id
    assert str(row.entity_id) == TICKET
    assert row.changes["category"] == "Hardware" and row.changes["asset_linked"] is True
    assert "Laptop fan" not in json.dumps(row.changes) and "Loud" not in json.dumps(row.changes)


async def test_replying_is_an_action_and_a_closed_ticket_says_so(
    session: AsyncSession,
    tenant: Tenant,
    me: User,  # noqa: F811
    desk: FakeHelpdesk,
) -> None:
    await _link(session, tenant)
    desk.answers[("act", "reply")] = {
        "message": {"id": "m2", "from": "Anita Sharma", "side": "requester", "text": "Thanks"}
    }
    written = await helpdesk.reply(session, me, TICKET, "Thanks, that worked", request=None)
    assert written["message"]["id"] == "m2"
    assert desk.calls[-1]["use"] == "act"
    assert desk.calls[-1]["body"]["params"] == {
        "ticket_id": TICKET,
        "message": "Thanks, that worked",
    }
    row = (
        await session.scalars(select(AuditLog).where(AuditLog.entity_type == "helpdesk_reply"))
    ).one()
    assert row.changes["length"] == len("Thanks, that worked")
    assert "Thanks" not in json.dumps(row.changes)

    desk.answers[("act", "reply")] = httpx.Response(
        409,
        json={"error": "This ticket is closed. Reopen it in CGB Helpdesk to write on it again."},
    )
    with pytest.raises(HTTPException) as closed:
        await helpdesk.reply(session, me, TICKET, "Still broken", request=None)
    assert closed.value.status_code == 409
    assert "This ticket is closed" in closed.value.detail
    # A refused reply leaves no audit entry: nothing happened.
    rows = (
        await session.scalars(select(AuditLog).where(AuditLog.entity_type == "helpdesk_reply"))
    ).all()
    assert len(rows) == 1


async def test_nothing_is_asked_or_done_when_the_helpdesk_is_not_linked(
    session: AsyncSession,
    me: User,  # noqa: F811
    desk: FakeHelpdesk,
) -> None:
    page = await views.render(session, me, "helpdesk", "tickets", {})
    assert page["cards"][0]["tone"] == "warning"
    with pytest.raises(HTTPException) as refused:
        await helpdesk.raise_ticket(
            session,
            me,
            {"subject": "Help", "category": "Hardware", "priority": "low"},
            request=None,
        )
    assert refused.value.status_code == 502
    assert desk.calls == []


async def test_ask_mira_reads_my_tickets_without_ai(
    session: AsyncSession,
    tenant: Tenant,
    me: User,  # noqa: F811
    desk: FakeHelpdesk,
) -> None:
    await _link(session, tenant)
    desk.answers[("query", "tickets")] = {"scope": "mine", "total": 0, "tickets": []}
    with use_backend(FakeModel(plan("helpdesk_tickets", "Here are your tickets.", scope="mine"))):
        result = await companion.send(
            session, me, companion.MessageIn(text="my open tickets"), request=None
        )
    reply = result["messages"][1]
    assert reply["tool"] == "helpdesk_tickets"
    assert desk.calls[-1]["body"]["params"]["scope"] == "mine"
    assert reply["cards"][0]["text"] == "No tickets match that in CGB Helpdesk."


async def test_connectors_offer_the_helpdesk(
    session: AsyncSession, hr: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import crypto

    monkeypatch.setattr(connectors, "can_encrypt", lambda: True)
    monkeypatch.setattr(connectors, "encrypt_field", lambda text: f"encf:{text}")
    monkeypatch.setattr(crypto, "can_encrypt", lambda: True)
    listed = {c["key"]: c for c in await connectors.overview(session, hr)}
    card = listed["helpdesk"]
    assert card["brand"] == "CGB Helpdesk" and card["available"] is True
    # Its production address is built in: the web app, and the web app's /api.
    assert card["app_url"] == "https://cgb-helpdesk.vercel.app"
    assert card["api_url"] == "https://cgb-helpdesk.vercel.app/api"
    started = await connectors.start(session, hr, "helpdesk")
    assert started["authorize_url"].startswith("https://cgb-helpdesk.vercel.app/connect/askmira?")


async def test_the_roster_sent_to_the_helpdesk_has_everyone(
    session: AsyncSession,
    tenant: Tenant,
    hr: User,
    me: User,  # noqa: F811
    employee: Employee,
) -> None:
    from app.services.products import service

    people = await service._roster(session, tenant.id, PRODUCTS["helpdesk"])
    roles = {person["email"]: person["role"] for person in people}
    assert roles[hr.email.lower()] == "admin"
    assert roles["anita@acme.test"] == "user"
