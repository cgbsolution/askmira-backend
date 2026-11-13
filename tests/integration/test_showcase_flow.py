"""Showcase links end to end: the super admin's QR code, and the Ask Miraa chat a scan opens.

A visitor never signs in: the page can only chat, as the admin the link names,
in a conversation of the visitor's own. Every message re-checks the link, so
switching it off, letting it end, or switching the admin's login off stops a
page that is already open.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.audit import AuditAction, AuditLog
from app.models.companion import CompanionConversation, CompanionMessage
from app.models.employee import Employee
from app.models.leave import LeaveType
from app.models.showcase import ShowcaseLink, ShowcaseRun
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.ai import client as ai_client
from app.services.ai.client import use_backend
from app.services.companion import config as companion_config
from app.services.hiring import public_guard
from app.services.showcase import showcase_links
from tests.integration.conftest import requires_db
from tests.integration.test_companion_agent import _weekday_ahead, casual  # noqa: F401
from tests.integration.test_companion_flow import (
    FakeModel,
    _ai_on,
    _consent,
    tool_call,
)

pytestmark = requires_db

BASE = "/api/v1"


@pytest.fixture(autouse=True)
def _fresh_limits() -> None:
    public_guard.LIMITER.reset()


@pytest.fixture
def no_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    """No model on this server: Ask Miraa answers by matching words to a lookup."""
    monkeypatch.setattr(ai_client.settings, "ANTHROPIC_API_KEY", "")


@pytest.fixture
async def super_admin(session: AsyncSession, other_tenant: Tenant) -> User:
    user = User(
        tenant_id=other_tenant.id,
        email="platform@askmiraa.test",
        password_hash="x",
        full_name="Platform",
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    return user


@pytest.fixture
async def http(session: AsyncSession) -> AsyncGenerator[httpx.AsyncClient, None]:
    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def _bearer(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
    )
    return {"Authorization": f"Bearer {token}"}


def _body(tenant: Tenant, admin: User, **overrides: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    body: dict[str, Any] = {
        "label": "HR Tech Expo, Bengaluru",
        "workspace_id": str(tenant.id),
        "acting_user_id": str(admin.id),
        "daily_messages": 300,
        "is_active": True,
        "starts_at": (now - timedelta(hours=1)).isoformat(),
        "ends_at": (now + timedelta(days=2)).isoformat(),
    }
    body.update(overrides)
    return body


async def _create(
    http: httpx.AsyncClient, user: User, tenant: Tenant, admin: User, **overrides: Any
) -> dict[str, Any]:
    response = await http.post(
        f"{BASE}/platform/showcase-links",
        json=_body(tenant, admin, **overrides),
        headers=_bearer(user),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _audits(session: AsyncSession, action: AuditAction) -> list[AuditLog]:
    rows = await session.scalars(
        select(AuditLog).where(AuditLog.entity_type == "showcase_link", AuditLog.action == action)
    )
    return list(rows)


async def _start(http: httpx.AsyncClient, code: str, pin: str | None = None) -> dict[str, Any]:
    response = await http.post(f"{BASE}/public/showcase/{code}/runs", json={"pin": pin})
    assert response.status_code == 201, response.text
    return response.json()


async def _say(
    http: httpx.AsyncClient, code: str, run: dict[str, Any], text: str, **extra: Any
) -> httpx.Response:
    return await http.post(
        f"{BASE}/public/showcase/{code}/runs/{run['run_id']}/messages",
        json={"text": text, **extra},
        headers={"X-Showcase-Key": run["key"]},
    )


async def _patch(
    http: httpx.AsyncClient, user: User, link: dict[str, Any], **fields: Any
) -> httpx.Response:
    return await http.patch(
        f"{BASE}/platform/showcase-links/{link['id']}", json=fields, headers=_bearer(user)
    )


# ── The super admin ──────────────────────────────────────────────────────────


async def test_the_super_admin_makes_a_link_with_its_qr_and_an_audit_row(
    http: httpx.AsyncClient,
    session: AsyncSession,
    super_admin: User,
    tenant: Tenant,
    hr_user: User,
) -> None:
    link = await _create(http, super_admin, tenant, hr_user, pin="4321")
    assert link["status"] == "live"
    assert link["company"] == tenant.name
    assert link["acting_name"] == hr_user.full_name and link["acting_email"] == hr_user.email
    assert link["pin"] == "4321" and link["daily_messages"] == 300
    assert link["url"].endswith(f"/showcase/{link['code']}")
    assert "client_name" not in link and "order_value" not in link

    # The code and PIN are kept encrypted; the code is found by its hash.
    row = await session.get(ShowcaseLink, uuid.UUID(link["id"]))
    assert row is not None and row.code_hash != link["code"]
    raw = await session.scalar(
        select(func.count()).select_from(ShowcaseLink).where(ShowcaseLink.code_hash == link["code"])
    )
    assert raw == 0

    created = await _audits(session, AuditAction.CREATE)
    assert len(created) == 1 and created[0].changes["pin"] == "set"
    assert "4321" not in str(created[0].changes)

    for kind, media in (("svg", "image/svg+xml"), ("png", "image/png")):
        qr = await http.get(
            f"{BASE}/platform/showcase-links/{link['id']}/qr?format={kind}",
            headers=_bearer(super_admin),
        )
        assert qr.status_code == 200 and qr.headers["content-type"].startswith(media)
        assert "attachment" in qr.headers["content-disposition"]


async def test_the_chat_answers_only_as_an_active_admin_of_that_workspace(
    http: httpx.AsyncClient,
    session: AsyncSession,
    super_admin: User,
    tenant: Tenant,
    hr_user: User,
    other_hr_user: User,
    employee: Employee,
) -> None:
    staff = User(
        tenant_id=tenant.id,
        email="anita@acme.test",
        password_hash="x",
        full_name="Anita Sharma",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(staff)
    await session.commit()
    for admin in (staff, other_hr_user):
        refused = await http.post(
            f"{BASE}/platform/showcase-links",
            json=_body(tenant, admin),
            headers=_bearer(super_admin),
        )
        assert refused.status_code == 422, refused.text
        assert "active admin of that workspace" in refused.text
    missing = await http.post(
        f"{BASE}/platform/showcase-links",
        json={**_body(tenant, hr_user), "workspace_id": str(uuid.uuid4())},
        headers=_bearer(super_admin),
    )
    assert missing.status_code == 422


@pytest.mark.parametrize(
    "overrides",
    [
        {"pin": "12"},
        {"pin": "12ab"},
        {"pin": "123456789"},
        {"daily_messages": 5},
        {"daily_messages": 6000},
        {"label": "x"},
        {"ends_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat()},
        {"starts_at": "2026-11-13T10:00:00"},
        {"client_name": "Acme"},
    ],
)
async def test_bad_link_details_are_refused(
    http: httpx.AsyncClient,
    super_admin: User,
    tenant: Tenant,
    hr_user: User,
    overrides: dict[str, Any],
) -> None:
    response = await http.post(
        f"{BASE}/platform/showcase-links",
        json=_body(tenant, hr_user, **overrides),
        headers=_bearer(super_admin),
    )
    assert response.status_code == 422, response.text


async def test_only_the_super_admin_manages_links(
    http: httpx.AsyncClient, super_admin: User, tenant: Tenant, hr_user: User
) -> None:
    link = await _create(http, super_admin, tenant, hr_user)
    for method, path in (
        ("GET", "/platform/showcase-links"),
        ("POST", "/platform/showcase-links"),
        ("PATCH", f"/platform/showcase-links/{link['id']}"),
        ("DELETE", f"/platform/showcase-links/{link['id']}"),
        ("GET", f"/platform/showcase-links/{link['id']}/qr"),
    ):
        response = await http.request(
            method, BASE + path, json=_body(tenant, hr_user), headers=_bearer(hr_user)
        )
        assert response.status_code == 403, (method, path, response.text)


async def test_switching_off_editing_and_deleting_are_audited(
    http: httpx.AsyncClient,
    session: AsyncSession,
    super_admin: User,
    tenant: Tenant,
    hr_user: User,
) -> None:
    link = await _create(http, super_admin, tenant, hr_user)
    off = await _patch(http, super_admin, link, is_active=False)
    assert off.status_code == 200 and off.json()["status"] == "off"
    pinned = await _patch(http, super_admin, link, pin="9876", daily_messages=50)
    assert pinned.json()["pin"] == "9876" and pinned.json()["daily_messages"] == 50
    cleared = await _patch(http, super_admin, link, clear_pin=True)
    assert cleared.json()["pin"] is None
    same = await _patch(http, super_admin, link, label=link["label"])
    assert same.status_code == 200

    updates = await _audits(session, AuditAction.UPDATE)
    assert [row.summary for row in updates] == [
        f"Showcase link switched off: {link['label']}",
        f"Showcase link changed: {link['label']}",
        f"Showcase link changed: {link['label']}",
    ]
    assert all("9876" not in str(row.changes) for row in updates)

    gone = await http.delete(
        f"{BASE}/platform/showcase-links/{link['id']}", headers=_bearer(super_admin)
    )
    assert gone.status_code == 204
    assert len(await _audits(session, AuditAction.DELETE)) == 1
    listed = await http.get(f"{BASE}/platform/showcase-links", headers=_bearer(super_admin))
    assert listed.json() == []
    again = await _patch(http, super_admin, link, label="Edited after delete")
    assert again.status_code == 404


async def test_one_qr_per_workspace_and_its_code_never_changes(
    http: httpx.AsyncClient,
    session: AsyncSession,
    super_admin: User,
    tenant: Tenant,
    hr_user: User,
    other_tenant: Tenant,
    other_hr_user: User,
    no_ai: None,
) -> None:
    first = await _create(http, super_admin, tenant, hr_user, pin="1357")
    code = first["code"]

    # A second QR for the same workspace is refused: edit the one there is.
    second = await http.post(
        f"{BASE}/platform/showcase-links",
        json=_body(tenant, hr_user, label="Another event"),
        headers=_bearer(super_admin),
    )
    assert second.status_code == 409 and "code never changes" in second.text

    # A link never moves to another workspace (its printed QR is that workspace's).
    moved = await _patch(http, super_admin, first, workspace_id=str(other_tenant.id))
    assert moved.status_code == 422

    # Deleted: the printed QR says it is switched off, not that it does not exist.
    await http.delete(f"{BASE}/platform/showcase-links/{first['id']}", headers=_bearer(super_admin))
    seen = await http.get(f"{BASE}/public/showcase/{code}")
    assert seen.status_code == 200 and seen.json()["status"] == "off"
    refused = await http.post(f"{BASE}/public/showcase/{code}/runs", json={"pin": "1357"})
    assert refused.status_code == 410 and refused.headers["x-showcase-status"] == "off"

    # Made again for the same workspace, with new details: the same code, the same QR.
    remade = await _create(
        http, super_admin, tenant, hr_user, label="Second expo", pin="2468", daily_messages=50
    )
    assert remade["code"] == code and remade["url"] == first["url"]
    assert remade["id"] == first["id"]
    assert remade["label"] == "Second expo" and remade["pin"] == "2468"
    assert remade["status"] == "live"
    run = await _start(http, code, "2468")
    assert run["run_id"]
    qr_before = first["url"]
    qr = await http.get(
        f"{BASE}/platform/showcase-links/{remade['id']}/qr?format=svg",
        headers=_bearer(super_admin),
    )
    assert qr.status_code == 200 and remade["url"] == qr_before
    created = await _audits(session, AuditAction.CREATE)
    assert [row.summary for row in created] == [
        f"Showcase link created: {first['label']}",
        "Showcase link made again with its printed code: Second expo",
    ]

    # Another workspace gets its own code.
    other = await _create(http, super_admin, other_tenant, other_hr_user)
    assert other["code"] != code


# ── A visitor ────────────────────────────────────────────────────────────────


async def test_a_scan_opens_the_workspaces_ask_miraa_answering_as_the_admin(
    http: httpx.AsyncClient,
    session: AsyncSession,
    super_admin: User,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    no_ai: None,
) -> None:
    link = await _create(http, super_admin, tenant, hr_user)
    code = link["code"]

    seen = await http.get(f"{BASE}/public/showcase/{code}")
    assert seen.status_code == 200
    assert seen.json() == {
        "status": "live",
        "company": tenant.name,
        "logo_url": None,
        "ends_at": seen.json()["ends_at"],
        "needs_pin": False,
    }
    assert seen.headers["cache-control"] == "private, no-store"

    run = await _start(http, code)
    assert run["company"] == tenant.name and run["acting_name"] == hr_user.full_name
    assert run["status"]["mode"] == "direct" and run["status"]["can_configure"] is False

    # Real records of the workspace, as the admin: a colleague is found.
    first = await _say(http, code, run, "Find Anita")
    assert first.status_code == 200, first.text
    reply = first.json()["messages"][1]
    assert reply["tool"] == "search_people"
    assert "Anita" in str(reply["cards"])
    second = await _say(http, code, run, "who is Anita's manager?")
    assert second.json()["conversation"]["id"] == first.json()["conversation"]["id"]

    # A reload carries on with the same chat.
    again = await http.get(
        f"{BASE}/public/showcase/{code}/runs/{run['run_id']}",
        headers={"X-Showcase-Key": run["key"]},
    )
    assert again.status_code == 200
    assert [m["role"] for m in again.json()["messages"]] == ["user", "assistant"] * 2

    # It is the admin's own Ask Miraa conversation, audited as them.
    conversation = await session.get(
        CompanionConversation, uuid.UUID(first.json()["conversation"]["id"])
    )
    assert conversation is not None and conversation.user_id == hr_user.id
    assert conversation.tenant_id == tenant.id
    audited = (
        await session.scalars(select(AuditLog).where(AuditLog.entity_type == "companion_message"))
    ).all()
    assert len(audited) == 2 and {row.actor_user_id for row in audited} == {hr_user.id}

    row = await session.get(ShowcaseLink, uuid.UUID(link["id"]))
    assert row is not None and row.visits == 1 and row.messages_today == 2


async def test_a_pin_is_asked_for_and_wrong_ones_are_refused(
    http: httpx.AsyncClient, super_admin: User, tenant: Tenant, hr_user: User, no_ai: None
) -> None:
    link = await _create(http, super_admin, tenant, hr_user, pin="2468")
    code = link["code"]
    assert (await http.get(f"{BASE}/public/showcase/{code}")).json()["needs_pin"] is True
    for pin in (None, "", "1111", "24680"):
        refused = await http.post(f"{BASE}/public/showcase/{code}/runs", json={"pin": pin})
        assert refused.status_code == 403
    run = await _start(http, code, " 2468 ")
    assert run["run_id"]

    # Guessing is slowed down: twenty wrong PINs from one address, then a pause.
    public_guard.LIMITER.reset()
    statuses = [
        (await http.post(f"{BASE}/public/showcase/{code}/runs", json={"pin": "0000"})).status_code
        for _ in range(22)
    ]
    assert statuses[:20] == [403] * 20 and statuses[-1] == 429


async def test_visitors_never_see_each_others_chats_or_the_admins_own(
    http: httpx.AsyncClient,
    session: AsyncSession,
    super_admin: User,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    other_tenant: Tenant,
    other_hr_user: User,
    no_ai: None,
) -> None:
    link = await _create(http, super_admin, tenant, hr_user)
    code = link["code"]
    one, two = await _start(http, code), await _start(http, code)
    said = await _say(http, code, one, "Find Anita", client_ref="same-ref-123")
    assert said.status_code == 200

    # The second visitor's key does not open the first's chat.
    peek = await http.get(
        f"{BASE}/public/showcase/{code}/runs/{one['run_id']}",
        headers={"X-Showcase-Key": two["key"]},
    )
    assert peek.status_code == 404
    # The same message id from another visitor is a new message, never the first answer.
    copied = await _say(http, code, two, "hello", client_ref="same-ref-123")
    assert copied.status_code == 200
    assert copied.json()["conversation"]["id"] != said.json()["conversation"]["id"]
    assert copied.json().get("repeat") is not True
    # A conversation id in the body is not taken.
    named = await _say(http, code, two, "hi", conversation_id=said.json()["conversation"]["id"])
    assert named.status_code == 422

    # A retry of the same message (same id, same visitor) is answered once.
    retry = await _say(http, code, one, "Find Anita", client_ref="same-ref-123")
    assert retry.json()["repeat"] is True
    mine = await session.scalar(
        select(func.count()).select_from(CompanionMessage).where(CompanionMessage.role == "user")
    )
    assert mine == 2

    # A wrong code with a right run, or no key at all, is refused the same way.
    other = await _create(
        http, super_admin, other_tenant, other_hr_user, label="Another workspace's expo"
    )
    crossed = await http.get(
        f"{BASE}/public/showcase/{other['code']}/runs/{one['run_id']}",
        headers={"X-Showcase-Key": one["key"]},
    )
    assert crossed.status_code == 404
    keyless = await http.get(f"{BASE}/public/showcase/{code}/runs/{one['run_id']}")
    assert keyless.status_code == 404


async def test_switching_off_ending_or_the_admin_leaving_stops_open_pages(
    http: httpx.AsyncClient,
    session: AsyncSession,
    super_admin: User,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    no_ai: None,
) -> None:
    link = await _create(http, super_admin, tenant, hr_user)
    code = link["code"]
    run = await _start(http, code)

    await _patch(http, super_admin, link, is_active=False)
    stopped = await _say(http, code, run, "Find Anita")
    assert stopped.status_code == 410
    assert stopped.headers["x-showcase-status"] == "off"
    assert stopped.json()["detail"] == "This showcase link has been switched off."
    described = (await http.get(f"{BASE}/public/showcase/{code}")).json()
    assert described["status"] == "off" and "needs_pin" not in described

    later = (datetime.now(UTC) + timedelta(hours=3)).isoformat()
    await _patch(http, super_admin, link, is_active=True, starts_at=later)
    early = await _say(http, code, run, "Find Anita")
    assert early.headers["x-showcase-status"] == "scheduled"

    now = datetime.now(UTC)
    await _patch(
        http,
        super_admin,
        link,
        starts_at=(now - timedelta(days=2)).isoformat(),
        ends_at=(now - timedelta(minutes=1)).isoformat(),
    )
    assert (await _say(http, code, run, "Find Anita")).headers["x-showcase-status"] == "ended"

    await _patch(http, super_admin, link, ends_at=(now + timedelta(days=1)).isoformat())
    assert (await _say(http, code, run, "Find Anita")).status_code == 200
    hr_user.is_active = False
    await session.commit()
    gone = await _say(http, code, run, "Find Anita")
    assert gone.status_code == 410 and gone.headers["x-showcase-status"] == "unavailable"


async def test_the_links_own_daily_cap_and_each_chats_cap(
    http: httpx.AsyncClient,
    session: AsyncSession,
    super_admin: User,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    no_ai: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The admin's own daily count is not what stops a showcase: the link's is.
    config = await companion_config.get_config(session, hr_user)
    await companion_config.set_config(
        session, hr_user, config.model_copy(update={"daily_messages_per_person": 5}), request=None
    )
    link = await _create(http, super_admin, tenant, hr_user, daily_messages=10)
    code = link["code"]
    monkeypatch.setattr(showcase_links, "MAX_TURNS", 7)
    first = await _start(http, code)
    for _ in range(7):
        assert (await _say(http, code, first, "Find Anita")).status_code == 200
    full = await _say(http, code, first, "Find Anita")
    assert full.status_code == 429 and "Start a new chat" in full.json()["detail"]

    second = await _start(http, code)
    assert second["status"]["daily_limit"] - second["status"]["sent_today"] == 3
    for _ in range(3):
        assert (await _say(http, code, second, "Find Anita")).status_code == 200
    day = await _say(http, code, second, "Find Anita")
    assert day.status_code == 429 and "for today" in day.json()["detail"]


async def test_an_offer_is_confirmed_only_from_the_chat_it_was_made_in(
    http: httpx.AsyncClient,
    session: AsyncSession,
    super_admin: User,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    casual: LeaveType,  # noqa: F811
) -> None:
    # The admin has an employee record, Ask Miraa on and their consent given.
    hr_user.employee_id = employee.id
    await session.commit()
    await _ai_on(session, hr_user)
    await _consent(session, tenant.id, employee.id)
    link = await _create(http, super_admin, tenant, hr_user)
    code = link["code"]
    one, two = await _start(http, code), await _start(http, code)
    day = _weekday_ahead(9)
    model = FakeModel(
        [tool_call("apply_leave", leave_type="CL", **{"from": day.isoformat()})],
        "Press Confirm to send it.",
        "Hello.",
    )
    with use_backend(model):
        offered = await _say(http, code, one, "a casual leave next week")
        assert offered.status_code == 200, offered.text
        await _say(http, code, two, "hello")
    card = offered.json()["messages"][1]["cards"][0]
    assert card["type"] == "proposal"
    action = card["action_id"]

    stolen = await http.post(
        f"{BASE}/public/showcase/{code}/runs/{two['run_id']}/actions/{action}/confirm",
        headers={"X-Showcase-Key": two["key"]},
    )
    assert stolen.status_code == 404
    done = await http.post(
        f"{BASE}/public/showcase/{code}/runs/{one['run_id']}/actions/{action}/confirm",
        headers={"X-Showcase-Key": one["key"]},
    )
    assert done.status_code == 200, done.text
    assert done.json()["action"]["status"] == "done"
    again = await http.post(
        f"{BASE}/public/showcase/{code}/runs/{one['run_id']}/actions/{action}/cancel",
        headers={"X-Showcase-Key": one["key"]},
    )
    assert again.status_code == 409


async def test_a_bad_code_is_a_plain_not_found(http: httpx.AsyncClient) -> None:
    for code in ("short", "x" * 80, "notarealcode123"):
        response = await http.get(f"{BASE}/public/showcase/{code}")
        assert response.status_code == 404


async def test_old_chats_are_forgotten_when_a_new_one_starts(
    http: httpx.AsyncClient,
    session: AsyncSession,
    super_admin: User,
    tenant: Tenant,
    hr_user: User,
) -> None:
    link = await _create(http, super_admin, tenant, hr_user)
    run = await _start(http, link["code"])
    row = await session.get(ShowcaseRun, uuid.UUID(run["run_id"]))
    assert row is not None
    row.updated_at = datetime.now(UTC) - timedelta(days=4)
    await session.commit()
    await _start(http, link["code"])
    left = await session.scalar(select(func.count()).select_from(ShowcaseRun))
    assert left == 1
