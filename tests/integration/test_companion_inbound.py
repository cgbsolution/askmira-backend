"""Ask Mira from Slack, Microsoft Teams and email.

* Slack: an event not signed with the app's signing secret, or too old, is
  refused; the URL check is answered; a direct message from a Slack account
  whose email is an AskMira login is answered as that person in the same DM,
  and the next message continues the same conversation; anyone else is told
  only AskMira logins are answered, and nothing runs for them.
* Teams: an activity without Microsoft's valid token is refused; one from
  another channel (Web Chat writes its own `from`) or another Microsoft 365
  tenant is not answered; a message is answered as the person found by their
  Entra id, back in the conversation.
* Email: only to the workspace's secret address, only when the From address is
  vouched for by its own domain (DKIM for that domain, or SPF for that same
  envelope sender) — a pass for another domain, a display name, a field sent
  twice or an unsigned Mailgun post is refused — and an offer is confirmed in
  AskMira, never by reply. Only an admin switches it on; a new address replaces
  the old one; Mailgun needs its signing key.
* Everywhere: a suspended workspace is not answered, the two-step rule sends
  people to AskMira, a sub-admin keeps their scope, and records of pay or leave
  are not written into the chat.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import encrypt_secret
from app.db.session import get_session_factory
from app.main import app
from app.models.companion import CompanionAction, CompanionActionStatus, CompanionConversation
from app.models.notifications import Channel, TenantChatApp
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.ai.client import use_backend
from app.services.companion import inbound
from app.services.notifications import dispatch
from app.services.notifications.adapters import ChannelAdapter, DeliveryResult, OutboundMessage
from tests.integration import platform_support
from tests.integration.conftest import as_caller, requires_db
from tests.integration.platform_support import make_person
from tests.integration.test_companion_flow import FakeModel, _ai_on, _consent, plan, tool_call

pytestmark = requires_db

client = platform_support.client
SIGNING = "0123456789abcdef0123456789abcdef"


class _Same:
    """A session factory that hands the background task the test's own session."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def __call__(self) -> _Same:
        return self

    async def __aenter__(self) -> AsyncSession:
        return self.session

    async def __aexit__(self, *_: Any) -> bool:
        return False


class FakeSlack:
    def __init__(self, emails: dict[str, str]) -> None:
        self.emails = emails
        self.posted: list[dict[str, Any]] = []

    async def _call(self, method: str, *, json: dict | None = None, params: dict | None = None):
        if method == "users.info":
            email = self.emails.get(str((params or {}).get("user")))
            return {"ok": True, "user": {"profile": {"email": email} if email else {}}}
        if method == "chat.postMessage":
            self.posted.append(json or {})
            return {"ok": True}
        raise AssertionError(method)


class Mail(ChannelAdapter):
    name = "test-mail"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        self.sent.append(message)
        return DeliveryResult(provider_message_id="m-1")


@pytest.fixture(autouse=True)
def _setup(session: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "WEB_APP_URL", "https://portal.test")
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://api.test")
    app.dependency_overrides[get_session_factory] = lambda: _Same(session)
    original = dict(dispatch._ADAPTERS)
    yield
    app.dependency_overrides.pop(get_session_factory, None)
    dispatch._ADAPTERS.clear()
    dispatch._ADAPTERS.update(original)


@pytest.fixture
async def asha(session: AsyncSession, tenant: Tenant) -> User:
    person = await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    await _ai_on(session, person)
    await _consent(session, tenant.id, person.employee_id)
    return await as_caller(session, person)


async def _slack_app(session: AsyncSession, tenant: Tenant, *, ask_mira: bool = True) -> None:
    session.add(
        TenantChatApp(
            tenant_id=tenant.id,
            provider=Channel.SLACK,
            enabled=True,
            ask_mira=ask_mira,
            secret_encrypted=encrypt_secret("xoxb-test", tenant.id),
            signing_secret_encrypted=encrypt_secret(SIGNING, tenant.id),
        )
    )
    await session.commit()


def _signed(body: bytes, *, secret: str = SIGNING, age: int = 0) -> dict[str, str]:
    ts = str(int(time.time()) - age)
    mac = hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256)
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": "v0=" + mac.hexdigest(),
        "Content-Type": "application/json",
    }


def _dm(text: str, user: str = "U1", channel: str = "D123") -> bytes:
    event = {"type": "message", "channel_type": "im", "user": user, "text": text}
    event["channel"] = channel
    return json.dumps({"type": "event_callback", "event": event}).encode()


async def test_slack_refuses_what_it_did_not_sign_and_answers_its_url_check(
    client: httpx.AsyncClient, session: AsyncSession, tenant: Tenant
) -> None:
    url = f"/api/v1/companion/inbound/slack/{tenant.id}"
    body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    # Not set up for Ask Mira: nothing here.
    assert (await client.post(url, content=body, headers=_signed(body))).status_code == 404
    await _slack_app(session, tenant)
    forged = _signed(body, secret="f" * 32)
    assert (await client.post(url, content=body, headers=forged)).status_code == 401
    assert (await client.post(url, content=body, headers=_signed(body, age=600))).status_code == 401
    answered = await client.post(url, content=body, headers=_signed(body))
    assert answered.status_code == 200 and answered.json() == {"challenge": "abc"}


async def test_a_slack_dm_is_answered_as_the_person_and_continues_one_conversation(
    client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    asha: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _slack_app(session, tenant)
    slack = FakeSlack({"U1": asha.email.upper()})
    monkeypatch.setattr(dispatch, "chat_adapter", lambda row: slack)
    url = f"/api/v1/companion/inbound/slack/{tenant.id}"
    with use_backend(FakeModel(plan(reply="Hello Asha."), plan(reply="Still here."))):
        first = _dm("hi mira")
        assert (await client.post(url, content=first, headers=_signed(first))).status_code == 200
        second = _dm("and again")
        assert (await client.post(url, content=second, headers=_signed(second))).status_code == 200
    assert [p["channel"] for p in slack.posted] == ["D123", "D123"]
    assert slack.posted[0]["text"].startswith("Hello Asha.")
    assert "https://portal.test/ask-mira?c=" in slack.posted[0]["text"]
    conversations = (await session.scalars(select(CompanionConversation))).all()
    assert len(conversations) == 1 and conversations[0].title.startswith("[Slack D123]")
    assert conversations[0].user_id == asha.id


async def test_a_slack_account_that_is_not_an_askmira_login_gets_nothing(
    client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    asha: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _slack_app(session, tenant)
    slack = FakeSlack({"U9": "stranger@else.test"})
    monkeypatch.setattr(dispatch, "chat_adapter", lambda row: slack)
    body = _dm("show me everyone's salary", user="U9")
    url = f"/api/v1/companion/inbound/slack/{tenant.id}"
    with use_backend(FakeModel()):
        assert (await client.post(url, content=body, headers=_signed(body))).status_code == 200
    [posted] = slack.posted
    assert "I can only answer people who sign in to AskMira" in posted["text"]
    assert await session.scalar(select(func.count(CompanionConversation.id))) == 0


async def test_teams_refuses_an_activity_without_microsofts_token(
    client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session.add(
        TenantChatApp(
            tenant_id=tenant.id, provider=Channel.TEAMS, enabled=True, ask_mira=True,
            secret_encrypted=encrypt_secret("s", tenant.id),
            app_id="0f8fad5b-d9cb-469f-a165-70867728950e",
            directory_tenant_id="1f8fad5b-d9cb-469f-a165-70867728950e",
        )
    )  # fmt: skip
    await session.commit()
    url = f"/api/v1/companion/inbound/teams/{tenant.id}"
    activity = {"type": "message", "text": "hi", "serviceUrl": "https://smba.test/"}
    assert (await client.post(url, json=activity)).status_code == 401

    async def refused(token: str, app_id: str, service: str) -> None:
        return None

    monkeypatch.setattr(inbound, "teams_claims", refused)
    headers = {"Authorization": "Bearer forged"}
    assert (await client.post(url, json=activity, headers=headers)).status_code == 401


async def test_a_teams_message_is_answered_as_the_person_found_in_entra(
    session: AsyncSession, tenant: Tenant, asha: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    session.add(
        TenantChatApp(
            tenant_id=tenant.id, provider=Channel.TEAMS, enabled=True, ask_mira=True,
            secret_encrypted=encrypt_secret("s", tenant.id),
            app_id="0f8fad5b-d9cb-469f-a165-70867728950e",
            directory_tenant_id="1f8fad5b-d9cb-469f-a165-70867728950e",
        )
    )  # fmt: skip
    await session.commit()
    aad = "2f8fad5b-d9cb-469f-a165-70867728950e"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "graph.microsoft.com":
            return httpx.Response(200, json={"mail": asha.email, "userPrincipalName": "x"})
        return httpx.Response(201, json={"id": "reply-1"})

    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    class Bot:
        GRAPH = "https://graph.microsoft.com/v1.0"

        async def _token(self, scope: str) -> str:
            return "token"

    monkeypatch.setattr(dispatch, "chat_adapter", lambda row: Bot())
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    activity = {
        "type": "message", "id": "act-1", "text": "<at>AskMira</at> hello",
        "serviceUrl": "https://smba.test/", "from": {"aadObjectId": aad},
        "conversation": {"id": "a:conv-1"}, "channelId": "msteams",
        "channelData": {"tenant": {"id": "1F8FAD5B-D9CB-469F-A165-70867728950E"}},
    }  # fmt: skip
    # Web Chat or Direct Line let the client write `from`; another tenant is not ours.
    webchat = {**activity, "channelId": "webchat"}
    assert await inbound.handle_teams(session, tenant.id, webchat) == "not from Teams"
    elsewhere = {**activity, "channelData": {"tenant": {"id": str(uuid.uuid4())}}}
    assert (
        await inbound.handle_teams(session, tenant.id, elsewhere)
        == "from another Microsoft 365 tenant"
    )
    assert seen == [], "nothing is looked up for either"
    with use_backend(FakeModel(plan(reply="Hi from Mira."))):
        outcome = await inbound.handle_teams(session, tenant.id, activity)
    assert outcome == "answered"
    graph, reply = seen
    assert graph.url.path == f"/v1.0/users/{aad}"
    assert reply.url.path == "/v3/conversations/a:conv-1/activities/act-1"
    assert json.loads(reply.content)["text"].startswith("Hi from Mira.")


def _sendgrid(sender: str, *, envelope: str | None = None, spf: str = "", dkim: str = "") -> dict:
    """What SendGrid Inbound Parse posts: the From header, the envelope, its own checks."""
    return {
        "from": sender,
        "envelope": json.dumps({"to": ["mira@acme-demo.com"], "from": envelope or ""}),
        "SPF": spf,
        "dkim": dkim,
        "subject": "Leave balance",
        "text": "How many leaves?",
    }


async def test_email_is_answered_only_from_a_vouched_sender_to_the_secret_address(
    client: httpx.AsyncClient, session: AsyncSession, tenant: Tenant, asha: User
) -> None:
    mail = Mail()
    dispatch.register_adapter(Channel.EMAIL, mail)
    switched = await inbound.set_email(session, asha, enabled=True)
    assert switched["provider"] == "sendgrid"
    url = switched["inbound_url"].removeprefix("https://api.test")
    assert url.startswith(f"/api/v1/companion/inbound/email/{tenant.id}/")
    domain = asha.email.rsplit("@", 1)[1]
    aligned = _sendgrid(f"Asha <{asha.email}>", envelope=asha.email, spf="pass")

    wrong = url.rsplit("/", 1)[0] + "/not-the-token"
    assert (await client.post(wrong, data=aligned)).status_code == 404
    spoofs = [
        # SPF passed, but for the attacker's own envelope domain.
        _sendgrid(asha.email, envelope="bounce@evil.example", spf="pass"),
        # DKIM passed, but signed by another domain.
        _sendgrid(asha.email, envelope="bounce@evil.example", dkim="{@evil.example : pass}"),
        # A display name that looks like the address.
        _sendgrid(f'"{asha.email}" <x@evil.example>', envelope="x@evil.example", spf="pass"),
        # Two addresses in From.
        _sendgrid(f"{asha.email}, x@evil.example", envelope=asha.email, spf="pass"),
        # Nothing passed.
        _sendgrid(asha.email, envelope=asha.email, spf="fail"),
    ]
    with use_backend(FakeModel(plan(reply="You have 4 days."), plan(reply="Still 4."))):
        for forged in spoofs:
            assert (await client.post(url, data=forged)).status_code == 200
        assert mail.sent == [], "a From address its own domain did not vouch for gets nothing"
        # A field sent twice (a header the sender wrote) is refused too.
        doubled = urlencode([*aligned.items(), ("SPF", "pass")])
        form_type = {"Content-Type": "application/x-www-form-urlencoded"}
        assert (await client.post(url, content=doubled, headers=form_type)).status_code == 200
        assert mail.sent == []

        assert (await client.post(url, data=aligned)).status_code == 200
        # DKIM for the From domain itself is enough on its own.
        dkim = f"{{@{domain} : pass}}"
        signed = _sendgrid(asha.email, envelope="bounce@mailer.example", dkim=dkim)
        assert (await client.post(url, data=signed)).status_code == 200
    first, second = mail.sent
    assert first.recipient_email == asha.email
    assert first.subject == "Re: Leave balance"
    assert first.body.startswith("You have 4 days.")
    assert second.body.startswith("Still 4.")

    # A new address replaces the old one.
    await inbound.set_email(session, asha, enabled=True)
    assert (await client.post(url, data=aligned)).status_code == 404


async def test_mailgun_posts_are_answered_only_when_mailgun_signed_them(
    client: httpx.AsyncClient, session: AsyncSession, tenant: Tenant, asha: User
) -> None:
    from fastapi import HTTPException

    mail = Mail()
    dispatch.register_adapter(Channel.EMAIL, mail)
    with pytest.raises(HTTPException) as no_key:
        await inbound.set_email(session, asha, enabled=True, provider="mailgun")
    assert no_key.value.status_code == 422
    key = "key-" + "a1b2c3d4" * 4
    switched = await inbound.set_email(
        session, asha, enabled=True, provider="mailgun", signing_key=key
    )
    assert switched["provider"] == "mailgun" and switched["has_signing_key"] is True
    url = switched["inbound_url"].removeprefix("https://api.test")

    def posted(token: str, *, signed_with: str = key, sender: str = asha.email) -> dict:
        stamp = str(int(time.time()))
        mac = hmac.new(signed_with.encode(), (stamp + token).encode(), hashlib.sha256)
        return {
            "from": f"Asha <{asha.email}>", "sender": sender, "X-Mailgun-Spf": "Pass",
            "subject": "Leave", "stripped-text": "How many leaves?",
            "timestamp": stamp, "token": token, "signature": mac.hexdigest(),
        }  # fmt: skip

    with use_backend(FakeModel(plan(reply="You have 4 days."))):
        # Not Mailgun's signature; SendGrid-looking fields a sender wrote as headers.
        forged = {**posted("t" * 50, signed_with="not-the-key-000000"), "SPF": "pass"}
        assert (await client.post(url, data=forged)).status_code == 200
        # Signed, but the envelope sender is someone else.
        other = posted("u" * 50, sender="bounce@evil.example")
        assert (await client.post(url, data=other)).status_code == 200
        assert mail.sent == []
        good = posted("v" * 50)
        assert (await client.post(url, data=good)).status_code == 200
        assert len(mail.sent) == 1
        # The same post again (a replay) is not answered twice.
        assert (await client.post(url, data=good)).status_code == 200
    assert len(mail.sent) == 1


async def test_an_emailed_yes_never_carries_out_an_offer(
    session: AsyncSession, tenant: Tenant, asha: User
) -> None:
    from app.services.companion import actions

    mail = Mail()
    dispatch.register_adapter(Channel.EMAIL, mail)
    await inbound.set_email(session, asha, enabled=True)
    from datetime import UTC, datetime, timedelta

    # The conversation an email with subject "Leave" (or "Re: Leave") continues.
    thread = hashlib.sha256(b"leave").hexdigest()[:12]
    conversation = CompanionConversation(
        tenant_id=tenant.id,
        user_id=asha.id,
        title=f"[Email {thread}] Leave",
        last_message_at=datetime.now(UTC),
    )
    session.add(conversation)
    await session.flush()
    offer = CompanionAction(
        tenant_id=tenant.id, user_id=asha.id, conversation_id=conversation.id,
        kind="hr_ticket", status=CompanionActionStatus.PROPOSED,
        payload={"category_id": str(uuid.uuid4()), "subject": "x", "description": "y"},
        summary={"title": "HR ticket"},
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )  # fmt: skip
    session.add(offer)
    await session.commit()
    offer.created_at = datetime.now(UTC) - timedelta(minutes=5)
    await session.commit()
    # Refused at the action itself, whatever the model does.
    ctx = actions.ToolContext(
        conversation_id=conversation.id, turn_started=datetime.now(UTC), latest_text="yes",
        place="email",
    )  # fmt: skip
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as refused:
        await actions.confirm(
            session, asha, offer.id, owner_id=asha.id, via="message", request=None, ctx=ctx
        )
    assert refused.value.status_code == 409 and "never confirms" in str(refused.value.detail)
    # And over email the model is not even given the tool: asking for it changes nothing.
    form = _sendgrid(asha.email, envelope=asha.email, spf="pass")
    form.update({"subject": "Re: Leave", "text": "yes"})
    model = FakeModel([tool_call("confirm_action", action_id=str(offer.id))], plan(reply="OK."))
    with use_backend(model):
        tenant_row = await session.get(Tenant, tenant.id)
        assert tenant_row is not None
        await inbound.handle_email(session, tenant_row, form)
    await session.refresh(offer)
    assert offer.status is CompanionActionStatus.PROPOSED
    assert model.calls, "the email was answered"
    assert "confirm_action" not in model.calls[0]["tools"]
    assert "cancel_action" not in model.calls[0]["tools"]


async def test_a_suspended_workspace_and_the_two_step_rule_are_kept_in_chat(
    client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    asha: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.opsec import TenantSessionPolicy

    await _slack_app(session, tenant)
    slack = FakeSlack({"U1": asha.email})
    monkeypatch.setattr(dispatch, "chat_adapter", lambda row: slack)
    url = f"/api/v1/companion/inbound/slack/{tenant.id}"
    policy = TenantSessionPolicy(tenant_id=tenant.id, require_two_step="privileged")
    session.add(policy)
    await session.commit()
    with use_backend(FakeModel()):
        body = _dm("salary of everyone")
        assert (await client.post(url, content=body, headers=_signed(body))).status_code == 200
    assert "two-step sign-in" in slack.posted[-1]["text"]

    policy.require_two_step = "off"
    row = await session.get(Tenant, tenant.id)
    assert row is not None
    row.is_active = False
    await session.commit()
    with use_backend(FakeModel()):
        body = _dm("hello")
        assert (await client.post(url, content=body, headers=_signed(body))).status_code == 200
    assert slack.posted[-1]["text"] == "Ask Mira isn't available for your company right now."
    assert await session.scalar(select(func.count(CompanionConversation.id))) == 0
    row.is_active = True
    await session.commit()


async def test_a_sub_admin_asking_in_chat_keeps_their_scope(
    session: AsyncSession, tenant: Tenant
) -> None:
    from app.core import hr_scope
    from app.models.access import AdminScope
    from app.models.employee import Employee
    from app.models.org import Department
    from app.services.companion import channels

    sales = Department(tenant_id=tenant.id, name="Sales", code="SAL")
    finance = Department(tenant_id=tenant.id, name="Finance", code="FIN")
    session.add_all([sales, finance])
    await session.flush()
    neha = await make_person(session, tenant, role=UserRole.SUB_ADMIN, name="Neha Sub")
    arjun = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Arjun Fin")
    ravi = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Ravi Sales")
    for person, department in ((arjun, finance), (ravi, sales)):
        employee = await session.get(Employee, person.employee_id)
        assert employee is not None
        employee.department_id = department.id
    session.add(AdminScope(tenant_id=tenant.id, user_id=neha.id, department_ids=[str(sales.id)]))
    await session.commit()

    hr_scope.set_scope(None)
    acting = await channels.acting_person(session, tenant.id, neha.email)
    assert acting.user is not None
    scope = hr_scope.current_scope()
    assert scope is not None, "the same narrowing a portal request gets"
    assert ravi.employee_id in scope.employee_ids
    assert arjun.employee_id not in scope.employee_ids
    hr_scope.set_scope(None)


def test_a_chat_reply_leaves_records_of_pay_and_leave_in_askmira() -> None:
    from app.services.companion import channels

    salary = {
        "type": "record", "title": "Arjun Fin", "from_tool": "employee_record",
        "figures": [{"label": "Annual CTC", "value": "₹18,00,000"}],
    }  # fmt: skip
    people = {
        "type": "table", "title": "People", "from_tool": "search_people",
        "columns": ["Name", "Department"], "rows": [["Ravi Sales", "Sales"]],
    }  # fmt: skip
    text = channels.render("Here.", [salary, people], place="slack", conversation_id="c-1")
    assert "₹18,00,000" not in text and "Annual CTC" not in text
    assert "kept in AskMira" in text
    assert "Ravi Sales · Sales" in text
    held = {
        "type": "proposal", "title": "Book PO-1", "status": "proposed",
        "waiting": "Held: the Won waits for a manager's approval in Saleinx.",
        "confirm_label": "Open positions",
    }  # fmt: skip
    text = channels.render("", [held], place="slack", conversation_id="c-1")
    assert "Held: the Won waits" in text and "Reply “yes”" not in text


async def test_only_an_admin_switches_email_on(
    session: AsyncSession, tenant: Tenant, asha: User
) -> None:
    from fastapi import HTTPException

    staff = await as_caller(
        session, await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Eve Emp")
    )
    with pytest.raises(HTTPException) as refused:
        await inbound.set_email(session, staff, enabled=True)
    assert refused.value.status_code == 403


def test_the_reply_says_how_to_confirm_in_each_place() -> None:
    from app.services.companion import channels

    offer = {
        "type": "proposal", "title": "Requisition", "status": "proposed",
        "figures": [{"label": "Role", "value": "Sales Executive"}],
        "notes": ["Approval path: HR head."], "confirm_label": "Raise and send for approval",
    }  # fmt: skip
    chat = channels.render("Here it is.", [offer], place="slack", conversation_id="c-1")
    assert "Reply “yes” to raise and send for approval, or “no”." in chat
    mailed = channels.render("Here it is.", [offer], place="email", conversation_id="c-1")
    assert "Reply “yes”" not in mailed
    assert "Confirm it in AskMira: https://portal.test/ask-mira?c=c-1" in mailed
