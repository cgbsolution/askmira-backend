"""Showcase links end to end: the super admin's QR code, and a visitor's story from a scan.

A visitor never signs in; every step re-checks the link, so switching it off or
letting it end stops a page that is already open.
"""

from __future__ import annotations

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
from app.models.showcase import ShowcaseLink, ShowcaseRun
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.hiring import public_guard
from app.services.showcase import story
from tests.integration.conftest import requires_db

pytestmark = requires_db

BASE = "/api/v1"


@pytest.fixture(autouse=True)
def _fresh_limits() -> None:
    public_guard.LIMITER.reset()


@pytest.fixture
async def super_admin(session: AsyncSession, tenant: Tenant) -> User:
    user = User(
        tenant_id=tenant.id,
        email="platform@acme.test",
        password_hash="x",
        full_name="Platform",
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    return user


@pytest.fixture
async def admin(session: AsyncSession, tenant: Tenant) -> User:
    user = User(
        tenant_id=tenant.id,
        email="admin@acme.test",
        password_hash="x",
        full_name="Asha Admin",
        role=UserRole.ADMIN,
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


def _body(tenant: Tenant | None = None, **overrides: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    body: dict[str, Any] = {
        "label": "HR Tech Expo, Bengaluru",
        "brand_tenant_id": str(tenant.id) if tenant else None,
        "client_name": "Acme Technologies",
        "role_title": "Java Developer",
        "headcount": 6,
        "months": 12,
        "order_value": "5000000",
        "team_budget": "3000000",
        "laptop_price": "55000",
        "payment_days": 30,
        "is_active": True,
        "starts_at": (now - timedelta(hours=1)).isoformat(),
        "ends_at": (now + timedelta(days=2)).isoformat(),
    }
    body.update(overrides)
    return body


async def _create(http: httpx.AsyncClient, user: User, **overrides: Any) -> dict[str, Any]:
    response = await http.post(
        f"{BASE}/platform/showcase-links", json=_body(**overrides), headers=_bearer(user)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _audits(session: AsyncSession, action: AuditAction) -> list[AuditLog]:
    rows = await session.scalars(
        select(AuditLog).where(AuditLog.entity_type == "showcase_link", AuditLog.action == action)
    )
    return list(rows)


# ── The super admin ──────────────────────────────────────────────────────────


async def test_the_super_admin_makes_a_link_with_its_qr_and_an_audit_row(
    http: httpx.AsyncClient, session: AsyncSession, super_admin: User, tenant: Tenant
) -> None:
    link = await _create(http, super_admin, tenant=tenant)
    assert link["status"] == "live"
    assert link["company"] == tenant.name
    assert link["margin_floor"] == "40.0"
    assert link["url"].endswith(f"/showcase/{link['code']}")
    assert len(link["code"]) >= 12

    # The code is kept encrypted and found by its hash, never stored as typed.
    row = await session.get(ShowcaseLink, link["id"])
    assert row is not None and row.code_hash != link["code"]
    raw = await session.scalar(
        select(
            func.cast(ShowcaseLink.__table__.c.code_encrypted, ShowcaseLink.__table__.c.label.type)
        ).where(ShowcaseLink.__table__.c.id == row.id)
    )
    assert raw is not None and link["code"] not in raw

    svg = await http.get(
        f"{BASE}/platform/showcase-links/{link['id']}/qr?format=svg", headers=_bearer(super_admin)
    )
    assert svg.status_code == 200 and svg.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in svg.content
    png = await http.get(
        f"{BASE}/platform/showcase-links/{link['id']}/qr?format=png", headers=_bearer(super_admin)
    )
    assert png.status_code == 200 and png.content[:8] == b"\x89PNG\r\n\x1a\n"

    created = await _audits(session, AuditAction.CREATE)
    assert len(created) == 1 and created[0].actor_user_id == super_admin.id
    assert created[0].changes["team_budget"] == "3000000.00"

    listed = await http.get(f"{BASE}/platform/showcase-links", headers=_bearer(super_admin))
    assert [item["id"] for item in listed.json()] == [link["id"]]


async def test_nobody_but_the_super_admin_manages_links(
    http: httpx.AsyncClient, super_admin: User, admin: User
) -> None:
    link = await _create(http, super_admin)
    for method, path in [
        ("GET", "/platform/showcase-links"),
        ("POST", "/platform/showcase-links"),
        ("PATCH", f"/platform/showcase-links/{link['id']}"),
        ("DELETE", f"/platform/showcase-links/{link['id']}"),
        ("GET", f"/platform/showcase-links/{link['id']}/qr"),
    ]:
        response = await http.request(
            method,
            f"{BASE}{path}",
            json=_body() if method in ("POST", "PATCH") else None,
            headers=_bearer(admin),
        )
        assert response.status_code == 403, (method, path, response.status_code)
        response = await http.request(method, f"{BASE}{path}")
        assert response.status_code == 401, (method, path, response.status_code)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"team_budget": "5000000"}, "budget equal to the order"),
        ({"team_budget": "6000000"}, "budget over the order"),
        ({"order_value": "fifty lakh"}, "letters in an amount"),
        ({"order_value": "-5"}, "negative amount"),
        ({"order_value": "50000.123"}, "more than two decimals"),
        ({"headcount": 11}, "team over ten"),
        ({"headcount": 0}, "empty team"),
        ({"months": 2}, "contract under three months"),
        ({"laptop_price": "10"}, "laptop under a thousand"),
        ({"payment_days": 120}, "payment terms over 90 days"),
        ({"label": "  "}, "blank label"),
        ({"client_name": ""}, "no client"),
    ],
)
async def test_a_link_that_does_not_add_up_is_refused(
    http: httpx.AsyncClient, super_admin: User, overrides: dict[str, Any], reason: str
) -> None:
    response = await http.post(
        f"{BASE}/platform/showcase-links", json=_body(**overrides), headers=_bearer(super_admin)
    )
    assert response.status_code == 422, reason


async def test_a_window_that_ends_before_it_starts_or_in_the_past_is_refused(
    http: httpx.AsyncClient, super_admin: User
) -> None:
    now = datetime.now(UTC)
    backwards = _body(starts_at=now.isoformat(), ends_at=(now - timedelta(hours=1)).isoformat())
    past = _body(
        starts_at=(now - timedelta(days=3)).isoformat(),
        ends_at=(now - timedelta(days=1)).isoformat(),
    )
    for body in (backwards, past):
        response = await http.post(
            f"{BASE}/platform/showcase-links", json=body, headers=_bearer(super_admin)
        )
        assert response.status_code == 422


async def test_an_edit_that_breaks_the_margin_is_refused_and_nothing_changes(
    http: httpx.AsyncClient, session: AsyncSession, super_admin: User
) -> None:
    link = await _create(http, super_admin)
    response = await http.patch(
        f"{BASE}/platform/showcase-links/{link['id']}",
        json={"team_budget": "5000000"},
        headers=_bearer(super_admin),
    )
    assert response.status_code == 422
    await session.rollback()
    row = await session.get(ShowcaseLink, link["id"])
    await session.refresh(row)
    assert str(row.team_budget) == "3000000.00"
    assert await _audits(session, AuditAction.UPDATE) == []


# ── A visitor ────────────────────────────────────────────────────────────────


async def _start(http: httpx.AsyncClient, code: str) -> dict[str, Any]:
    response = await http.post(f"{BASE}/public/showcase/{code}/runs")
    assert response.status_code == 201, response.text
    return response.json()


async def test_a_visitor_runs_the_story_without_signing_in(
    http: httpx.AsyncClient, session: AsyncSession, super_admin: User, tenant: Tenant
) -> None:
    link = await _create(http, super_admin, tenant=tenant)
    code = link["code"]
    described = await http.get(f"{BASE}/public/showcase/{code}")
    assert described.status_code == 200
    assert described.json()["status"] == "live" and described.json()["company"] == tenant.name
    assert described.headers["cache-control"] == "private, no-store"

    run = await _start(http, code)
    key, run_id = run["key"], run["run_id"]
    assert run["chapters"][0]["state"] == "current"
    assert run["messages"][0]["role"] == "assistant"
    headers = {"X-Showcase-Key": key}

    said = await http.post(
        f"{BASE}/public/showcase/{code}/runs/{run_id}/say",
        json={"text": run["suggestions"][0]},
        headers=headers,
    )
    assert said.status_code == 200, said.text
    action = said.json()["autoplay"]["decide"]
    decided = await http.post(
        f"{BASE}/public/showcase/{code}/runs/{run_id}/decide",
        json={"action_id": action, "confirm": True},
        headers=headers,
    )
    assert decided.status_code == 200
    assert [c["state"] for c in decided.json()["chapters"][:2]] == ["done", "done"]

    # The same click again is refused, not run twice.
    again = await http.post(
        f"{BASE}/public/showcase/{code}/runs/{run_id}/decide",
        json={"action_id": action, "confirm": True},
        headers=headers,
    )
    assert again.status_code == 409

    read = await http.get(f"{BASE}/public/showcase/{code}/runs/{run_id}", headers=headers)
    assert read.status_code == 200 and read.json()["chapters"][1]["state"] == "done"
    assert (await http.get(f"{BASE}/public/showcase/{code}/runs/{run_id}")).status_code == 404
    wrong = await http.get(
        f"{BASE}/public/showcase/{code}/runs/{run_id}", headers={"X-Showcase-Key": key + "x"}
    )
    assert wrong.status_code == 404

    # Another link's code does not reach this story, even with the key.
    other = await _create(http, super_admin, label="Another event")
    crossed = await http.get(
        f"{BASE}/public/showcase/{other['code']}/runs/{run_id}", headers=headers
    )
    assert crossed.status_code == 404

    row = await session.get(ShowcaseLink, link["id"])
    await session.refresh(row)
    assert row.visits == 1 and row.last_visit_at is not None
    stored = await session.get(ShowcaseRun, run_id)
    assert stored is not None and stored.key_hash != key and stored.turns == 2


async def test_switching_a_link_off_stops_an_open_page_on_its_next_step(
    http: httpx.AsyncClient, session: AsyncSession, super_admin: User
) -> None:
    link = await _create(http, super_admin)
    run = await _start(http, link["code"])
    headers = {"X-Showcase-Key": run["key"]}
    off = await http.patch(
        f"{BASE}/platform/showcase-links/{link['id']}",
        json={"is_active": False},
        headers=_bearer(super_admin),
    )
    assert off.status_code == 200 and off.json()["status"] == "off"
    said = await http.post(
        f"{BASE}/public/showcase/{link['code']}/runs/{run['run_id']}/say",
        json={"text": "Book the order"},
        headers=headers,
    )
    assert said.status_code == 410
    assert said.headers["x-showcase-status"] == "off"
    assert said.json()["detail"] == "This showcase link has been switched off."
    described = (await http.get(f"{BASE}/public/showcase/{link['code']}")).json()
    assert described == {
        "status": "off",
        "company": "AskMiraa",
        "logo_url": None,
        "message": "This showcase link has been switched off.",
    }
    assert (await http.post(f"{BASE}/public/showcase/{link['code']}/runs")).status_code == 410

    switched = await _audits(session, AuditAction.UPDATE)
    assert len(switched) == 1 and switched[0].summary.startswith("Showcase link switched off")

    on = await http.patch(
        f"{BASE}/platform/showcase-links/{link['id']}",
        json={"is_active": True},
        headers=_bearer(super_admin),
    )
    assert on.json()["status"] == "live"
    said = await http.post(
        f"{BASE}/public/showcase/{link['code']}/runs/{run['run_id']}/say",
        json={"text": "Book the order"},
        headers=headers,
    )
    assert said.status_code == 200


async def test_a_link_outside_its_window_starts_no_story(
    http: httpx.AsyncClient, session: AsyncSession, super_admin: User
) -> None:
    now = datetime.now(UTC)
    later = await _create(
        http,
        super_admin,
        starts_at=(now + timedelta(days=1)).isoformat(),
        ends_at=(now + timedelta(days=2)).isoformat(),
    )
    assert later["status"] == "scheduled"
    described = (await http.get(f"{BASE}/public/showcase/{later['code']}")).json()
    assert described["status"] == "scheduled" and "starts_at" in described
    assert "client" not in described
    refused = await http.post(f"{BASE}/public/showcase/{later['code']}/runs")
    assert refused.status_code == 410 and refused.headers["x-showcase-status"] == "scheduled"

    # Ended: move a live link's window into the past.
    live = await _create(http, super_admin, label="Ended event")
    row = await session.get(ShowcaseLink, live["id"])
    row.starts_at = now - timedelta(days=3)
    row.ends_at = now - timedelta(minutes=1)
    await session.commit()
    assert (await http.get(f"{BASE}/public/showcase/{live['code']}")).json()["status"] == "ended"
    assert (await http.post(f"{BASE}/public/showcase/{live['code']}/runs")).status_code == 410


async def test_an_unknown_code_is_not_found(http: httpx.AsyncClient) -> None:
    assert (await http.get(f"{BASE}/public/showcase/not-a-real-code")).status_code == 404
    assert (await http.post(f"{BASE}/public/showcase/x/runs")).status_code == 404


async def test_deleting_a_link_deletes_its_stories(
    http: httpx.AsyncClient, session: AsyncSession, super_admin: User
) -> None:
    link = await _create(http, super_admin)
    await _start(http, link["code"])
    await _start(http, link["code"])
    deleted = await http.delete(
        f"{BASE}/platform/showcase-links/{link['id']}", headers=_bearer(super_admin)
    )
    assert deleted.status_code == 204
    assert await session.scalar(select(func.count()).select_from(ShowcaseRun)) == 0
    assert await session.get(ShowcaseLink, link["id"]) is None
    assert (await http.get(f"{BASE}/public/showcase/{link['code']}")).status_code == 404
    assert len(await _audits(session, AuditAction.DELETE)) == 1


async def test_bad_messages_are_refused_and_a_long_story_asks_for_a_fresh_start(
    http: httpx.AsyncClient, super_admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = await _create(http, super_admin)
    run = await _start(http, link["code"])
    url = f"{BASE}/public/showcase/{link['code']}/runs/{run['run_id']}/say"
    headers = {"X-Showcase-Key": run["key"]}
    assert (await http.post(url, json={"text": ""}, headers=headers)).status_code == 422
    assert (await http.post(url, json={"text": "x" * 501}, headers=headers)).status_code == 422
    assert (await http.post(url, json={"text": "   "}, headers=headers)).status_code == 409

    monkeypatch.setattr(story, "MAX_TURNS", 2)
    assert (await http.post(url, json={"text": "help"}, headers=headers)).status_code == 200
    assert (await http.post(url, json={"text": "help"}, headers=headers)).status_code == 200
    capped = await http.post(url, json={"text": "help"}, headers=headers)
    assert capped.status_code == 409 and "Start over" in capped.json()["detail"]


async def test_the_whole_story_plays_through_the_api(
    http: httpx.AsyncClient, super_admin: User
) -> None:
    link = await _create(http, super_admin)
    run = await _start(http, link["code"])
    base = f"{BASE}/public/showcase/{link['code']}/runs/{run['run_id']}"
    headers = {"X-Showcase-Key": run["key"]}
    view = run
    for _ in range(40):
        step = view["autoplay"]
        if step is None:
            break
        if "say" in step:
            response = await http.post(f"{base}/say", json={"text": step["say"]}, headers=headers)
        else:
            response = await http.post(
                f"{base}/decide",
                json={"action_id": step["decide"], "confirm": True},
                headers=headers,
            )
        assert response.status_code == 200, response.text
        view = response.json()
    assert view["finished"]
    assert view["board"]["pnl"]["net"] == "1634980.00"
    assert view["board"]["cash"]["positive_from"] is not None
