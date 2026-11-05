"""Slack and Microsoft Teams: approvals and updates as direct messages, against a real database.

Slack and Microsoft are fakes here (an httpx MockTransport answering as their
documented APIs do); everything on AskMira's side is real.

* The admin's credentials are checked with the provider before they are kept,
  encrypted, never returned, and every change is audited.
* A leave request reaches the manager in Slack as a direct message with the
  same signed Approve and Reject links as the email, and the link decides it.
* The person is looked up by email once; later messages go straight there.
* "Approvals only" keeps other events out; "everything" lets them in; a person
  who turned Slack off for an event keeps it off.
* Someone with no Slack account fails on that channel alone: email still goes.
* Teams: the bot signs in, finds the person in the directory, opens a chat and
  posts an Adaptive Card; the app package names the bot.
* Another workspace's admin sees and changes nothing.
"""

from __future__ import annotations

import io
import json
import re
import uuid
import zipfile
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.leave import LeaveRequest, LeaveRequestStatus
from app.models.notifications import (
    Channel,
    ChatRecipient,
    Notification,
    NotificationPreference,
    NotificationStatus,
    TenantChatApp,
)
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.approvals import email_actions
from app.services.leave import service as leave_service
from app.services.notifications import admin as notification_admin
from app.services.notifications import chat as chat_module
from app.services.notifications import chat_admin, dispatch
from tests.integration.conftest import requires_db
from tests.integration.test_email_design_flow import (  # noqa: F401  (fixtures)
    _request,
    mail,
    org,
    restore_adapters,
)
from tests.integration.test_leave_flow import START, _payload

pytestmark = requires_db

TOKEN = "xoxb-1111-2222-abcdefghijklmnop"
APP_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
DIRECTORY = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


class FakeChat:
    """Slack's Web API and Microsoft's sign-in, Graph and Bot Framework."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.slack_users: dict[str, str] = {}
        self.slack_ok = True
        self.graph_users: dict[str, str] = {}
        self.posted: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = dict(httpx.QueryParams(request.content.decode()))
        self.calls.append((host, path, body))
        if host == "slack.com":
            if not self.slack_ok or request.headers["Authorization"] != f"Bearer {TOKEN}":
                return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
            if path == "/api/auth.test":
                return httpx.Response(200, json={"ok": True, "team": "Acme Corp"})
            if path == "/api/users.lookupByEmail":
                email = request.url.params["email"]
                if email not in self.slack_users:
                    return httpx.Response(200, json={"ok": False, "error": "users_not_found"})
                return httpx.Response(
                    200, json={"ok": True, "user": {"id": self.slack_users[email]}}
                )
            if path == "/api/chat.postMessage":
                self.posted.append({"to": "slack", **body})
                return httpx.Response(
                    200, json={"ok": True, "channel": f"D{body['channel']}", "ts": "1.2"}
                )
        if host == "login.microsoftonline.com":
            assert path == f"/{DIRECTORY}/oauth2/v2.0/token"
            assert body["client_id"] == APP_ID and body["client_secret"] == "teams-secret"
            return httpx.Response(
                200, json={"access_token": f"tok-{body['scope']}", "expires_in": 3600}
            )
        if host == "graph.microsoft.com":
            email = path.rsplit("/", 1)[-1]
            if email in self.graph_users:
                return httpx.Response(200, json={"id": self.graph_users[email]})
            return httpx.Response(404, json={"error": {"code": "Request_ResourceNotFound"}})
        if host == "smba.trafficmanager.net":
            if path == "/teams/v3/conversations":
                assert body["bot"]["id"] == f"28:{APP_ID}"
                assert body["tenantId"] == DIRECTORY
                return httpx.Response(
                    201, json={"id": f"a:chat-{body['members'][0]['aadObjectId']}"}
                )
            if path.endswith("/activities"):
                self.posted.append({"to": "teams", "path": path, **body})
                return httpx.Response(200, json={"id": "act-1"})
        return httpx.Response(404, json={"detail": f"unexpected {host}{path}"})


@pytest.fixture
def chat(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeChat]:
    fake = FakeChat()
    real = httpx.AsyncClient
    monkeypatch.setattr(
        chat_module,
        "_client",
        lambda: real(transport=httpx.MockTransport(fake.handler), timeout=5),
    )
    chat_module.TeamsAdapter._tokens.clear()
    yield fake


async def _slack(session, admin: User, **extra: Any) -> dict[str, Any]:
    return await chat_admin.save(
        session,
        admin,
        Channel.SLACK,
        enabled=True,
        events=extra.pop("events", "approvals"),
        secret=extra.pop("secret", TOKEN),
        app_id=None,
        directory_tenant_id=None,
    )


async def test_slack_is_checked_encrypted_and_never_returned(session, org, chat) -> None:
    saved = await _slack(session, org["hr"])
    assert saved["enabled"] and saved["connected_as"] == "Acme Corp"
    assert '"secret"' not in json.dumps(saved) and TOKEN not in json.dumps(saved)
    row = await session.scalar(select(TenantChatApp))
    assert row.secret_encrypted and TOKEN not in row.secret_encrypted
    listed = await chat_admin.chat_apps(session, org["hr"])
    assert TOKEN not in json.dumps(listed)
    assert await session.scalar(select(AuditLog).where(AuditLog.entity_type == "tenant_chat_app"))

    with pytest.raises(HTTPException) as wrong:
        await _slack(session, org["hr"], secret="xoxp-user-token")
    assert wrong.value.status_code == 422 and "xoxb-" in wrong.value.detail

    chat.slack_ok = False
    with pytest.raises(HTTPException) as refused:
        await _slack(session, org["hr"], secret="xoxb-9999-other")
    assert "did not accept" in refused.value.detail
    # Nothing of the refused token was kept.
    await session.refresh(row)
    assert row.connected_as == "Acme Corp"


async def test_a_leave_request_reaches_the_manager_in_slack_and_the_button_decides_it(
    session, org, chat, mail
) -> None:
    await _slack(session, org["hr"])
    chat.slack_users[org["boss"].email] = "UBOSS"

    created = await leave_service.apply(session, org["meera"], _payload(org))
    [message] = [p for p in chat.posted if p["channel"] == "UBOSS"]
    blocks = json.dumps(message["blocks"])
    assert "Meera Nair" in blocks
    approve = re.search(r"https://portal\.test/act/([^?\"]+)\?do=approve", blocks)
    assert approve and "do=reject" in blocks and "Open AskMira" in blocks
    # Email still went too.
    assert any(m.recipient_email == org["boss"].email for m in mail.sent)

    done = await email_actions.decide(
        session, approve.group(1), _request("POST"), approve=True, comment=None
    )
    assert done["decision"] == "approved"
    leave = await session.get(LeaveRequest, created.id)
    await session.refresh(leave)
    assert leave.status is LeaveRequestStatus.APPROVED

    kept = await session.scalar(
        select(ChatRecipient).where(ChatRecipient.user_id == org["boss"].id)
    )
    assert kept.address == "UBOSS"
    looked_up = [c for c in chat.calls if c[1] == "/api/users.lookupByEmail"]
    later = START + timedelta(days=20)
    second = await leave_service.apply(session, org["meera"], _payload(org, start=later))
    assert second
    assert [c for c in chat.calls if c[1] == "/api/users.lookupByEmail"] == looked_up


async def test_approvals_only_everything_and_a_persons_own_choice(session, org, chat) -> None:
    await _slack(session, org["hr"])
    chat.slack_users[org["meera"].email] = "UMEERA"
    await dispatch.notify(
        session,
        tenant_id=org["hr"].tenant_id,
        event_key="system.announcement",
        recipient_user_ids=[org["meera"].id],
        payload={"title": "Office closed Friday", "message": "Diwali."},
    )
    assert not [p for p in chat.posted if p["channel"] == "UMEERA"]

    await _slack(session, org["hr"], events="all", secret=None)
    await dispatch.notify(
        session,
        tenant_id=org["hr"].tenant_id,
        event_key="system.announcement",
        recipient_user_ids=[org["meera"].id],
        payload={"title": "Office closed Friday", "message": "Diwali."},
    )
    [sent] = [p for p in chat.posted if p["channel"] == "UMEERA"]
    assert "Office closed Friday" in json.dumps(sent["blocks"])

    session.add(
        NotificationPreference(
            tenant_id=org["hr"].tenant_id,
            user_id=org["meera"].id,
            event_key="system.announcement",
            channel=Channel.SLACK,
            enabled=False,
        )
    )
    await session.commit()
    await dispatch.notify(
        session,
        tenant_id=org["hr"].tenant_id,
        event_key="system.announcement",
        recipient_user_ids=[org["meera"].id],
        payload={"title": "Again", "message": "x"},
    )
    assert len([p for p in chat.posted if p["channel"] == "UMEERA"]) == 1


async def test_someone_not_in_slack_fails_there_alone(session, org, chat, mail) -> None:
    await _slack(session, org["hr"])
    await leave_service.apply(session, org["meera"], _payload(org))
    failed = await session.scalar(
        select(Notification).where(
            Notification.channel == Channel.SLACK,
            Notification.recipient_user_id == org["boss"].id,
        )
    )
    assert failed.status is NotificationStatus.FAILED
    assert "No one in the Slack workspace" in failed.failure_reason
    assert any(m.recipient_email == org["boss"].email for m in mail.sent)

    channels = {
        c["channel"]: c for c in await notification_admin.channel_status(session, org["meera"])
    }
    assert channels["slack"]["state"] == "live"
    assert channels["teams"]["state"] == "not_connected"


async def test_teams_finds_the_person_opens_a_chat_and_posts_a_card(
    session, org, chat, mail
) -> None:
    with pytest.raises(HTTPException) as bad:
        await chat_admin.save(
            session, org["hr"], Channel.TEAMS, enabled=True, events="approvals",
            secret="teams-secret", app_id="not-a-guid", directory_tenant_id=DIRECTORY,
        )  # fmt: skip
    assert "GUID" in bad.value.detail
    saved = await chat_admin.save(
        session, org["hr"], Channel.TEAMS, enabled=True, events="approvals",
        secret="teams-secret", app_id=APP_ID, directory_tenant_id=DIRECTORY,
    )  # fmt: skip
    assert saved["enabled"] and DIRECTORY in saved["connected_as"]
    chat.graph_users[org["boss"].email] = "aad-boss"

    await leave_service.apply(session, org["meera"], _payload(org))
    [posted] = [p for p in chat.posted if p["to"] == "teams"]
    assert "a:chat-aad-boss" in posted["path"]
    card = posted["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    titles = [a["title"] for a in card["actions"]]
    assert titles[:2] == ["Approve", "Reject"]
    assert card["actions"][0]["url"].startswith("https://portal.test/act/")

    name, media, package = await chat_admin.manifest(session, org["hr"], Channel.TEAMS)
    assert media == "application/zip"
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert {"color.png", "outline.png"} <= set(archive.namelist())
    assert manifest["bots"][0]["botId"] == APP_ID
    _, _, slack = await chat_admin.manifest(session, org["hr"], Channel.SLACK)
    assert json.loads(slack)["oauth_config"]["scopes"]["bot"] == [
        "chat:write",
        "users:read",
        "users:read.email",
    ]


async def test_a_test_message_and_disconnecting(session, org, chat) -> None:
    await _slack(session, org["hr"])
    chat.slack_users[org["hr"].email] = "UHR"
    result = await chat_admin.send_test(session, org["hr"], Channel.SLACK)
    assert result == {"sent": True, "error": None}
    assert [p for p in chat.posted if p["channel"] == "UHR"]

    await chat_admin.remove(session, org["hr"], Channel.SLACK)
    assert await session.scalar(select(TenantChatApp)) is None
    assert await session.scalar(select(ChatRecipient)) is None


async def test_another_workspace_sees_and_changes_nothing(session, org, chat) -> None:
    await _slack(session, org["hr"])
    other = Tenant(name="Other", slug=f"o-{uuid.uuid4().hex[:6]}", enabled_modules={}, settings={})
    session.add(other)
    await session.commit()
    stranger = User(
        tenant_id=other.id, email="admin@other.test", password_hash="x", full_name="O",
        role=UserRole.ADMIN, is_active=True,
    )  # fmt: skip
    session.add(stranger)
    await session.commit()
    listed = await chat_admin.chat_apps(session, stranger)
    assert all(not item["configured"] for item in listed)
    with pytest.raises(HTTPException) as gone:
        await chat_admin.remove(session, stranger, Channel.SLACK)
    assert gone.value.status_code == 404
    assert await session.scalar(
        select(TenantChatApp).where(TenantChatApp.tenant_id == org["hr"].tenant_id)
    )
