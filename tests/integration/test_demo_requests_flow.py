"""Demo requests from the website: anyone may send one, only the platform reads them."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.demo_request import DemoRequest, DemoRequestStatus
from app.models.notifications import Channel, Notification
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.demo_requests import EVENT_KEY
from app.services.hiring import public_guard
from tests.integration.conftest import requires_db
from tests.integration.platform_support import make_person

pytestmark = requires_db

URL = "/api/v1/public/demo-requests"


@pytest.fixture(autouse=True)
def _fresh_limits() -> None:
    public_guard.LIMITER.reset()


@pytest.fixture
async def client(session: AsyncSession) -> AsyncGenerator[httpx.AsyncClient, None]:
    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def super_admin(session: AsyncSession, tenant: Tenant) -> User:
    return await make_person(
        session, tenant, role=UserRole.SUPER_ADMIN, name="Platform Team", with_employee=False
    )


def _bearer(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
    )
    return {"Authorization": f"Bearer {token}"}


def _form(**overrides) -> dict:
    body = {
        "full_name": "Priya Ramesh",
        "work_email": "Priya@NewCo-Demo.com",
        "company": "NewCo",
        "phone": "+91 98765 43210",
        "company_size": "51-200",
        "role": "HR or People",
        "interests": ["payroll", "attendance", "payroll"],
        "message": "Three entities, biometric devices at two plants.",
        "topic": "demo",
        "consent": True,
        "source_path": "/demo",
        "website": "",
    }
    body.update(overrides)
    return body


async def _count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(DemoRequest)) or 0


async def test_a_request_is_stored_and_the_platform_team_is_told(
    client: httpx.AsyncClient, session: AsyncSession, super_admin: User
) -> None:
    response = await client.post(URL, json=_form())
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "received"

    row = await session.scalar(select(DemoRequest))
    assert row is not None
    assert row.work_email == "priya@newco-demo.com"
    assert row.interests == ["payroll", "attendance"]  # repeats dropped, order kept
    assert row.status is DemoRequestStatus.NEW
    assert row.consented_at is not None

    notes = list(
        await session.scalars(select(Notification).where(Notification.event_key == EVENT_KEY))
    )
    assert {n.recipient_user_id for n in notes} == {super_admin.id}
    assert {n.channel for n in notes} >= {Channel.EMAIL, Channel.IN_APP}
    email = next(n for n in notes if n.channel is Channel.EMAIL)
    assert "NewCo" in (email.rendered_subject or "")
    assert "priya@newco-demo.com" in email.rendered_body


async def test_interest_in_the_connected_apps_is_kept(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    apps = ["expenses", "helpdesk", "crm", "procurement"]
    response = await client.post(URL, json=_form(interests=["payroll", *apps]))
    assert response.status_code == 201, response.text
    row = await session.scalar(select(DemoRequest))
    assert row is not None
    assert row.interests == ["payroll", *apps]


async def test_names_in_any_script_are_accepted(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    for name in ("प्रिया शर्मा", "Anne-Marie D'Souza", "Dr. K. Rao", "Joan D\u2019Souza"):
        response = await client.post(
            URL, json=_form(full_name=name, work_email=f"{len(name)}@x-demo.com")
        )
        assert response.status_code == 201, (name, response.text)


@pytest.mark.parametrize(
    "overrides",
    [
        {"full_name": "Priya 123"},
        {"full_name": "@@"},
        {"full_name": " "},
        {"work_email": "not-an-email"},
        {"work_email": ""},
        {"company": "A"},
        {"phone": "98765abcde"},
        {"phone": "12345"},
        {"phone": "+91 98765 43210 99999"},
        {"company_size": "lots"},
        {"role": "CEO"},
        {"interests": ["payroll", "crypto"]},
        {"topic": "spam"},
        {"message": "x" * 1001},
        {"consent": False},
        {"consent": None},
        {"unknown_field": "x"},
    ],
)
async def test_bad_input_is_refused_and_nothing_is_stored(
    client: httpx.AsyncClient, session: AsyncSession, overrides: dict
) -> None:
    response = await client.post(URL, json=_form(**overrides))
    assert response.status_code == 422, (overrides, response.text)
    assert await _count(session) == 0


async def test_a_bot_filling_the_hidden_field_is_refused(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    response = await client.post(URL, json=_form(website="https://spam.example"))
    assert response.status_code == 400
    assert await _count(session) == 0


async def test_one_address_cannot_flood_the_form(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    for i in range(5):
        ok = await client.post(URL, json=_form(work_email=f"p{i}@newco-demo.com"))
        assert ok.status_code == 201
    blocked = await client.post(URL, json=_form(work_email="p6@newco-demo.com"))
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
    assert await _count(session) == 5


async def test_only_the_super_admin_reads_demo_requests(
    client: httpx.AsyncClient, session: AsyncSession, tenant: Tenant, super_admin: User
) -> None:
    await client.post(URL, json=_form(company="First"))
    await client.post(URL, json=_form(company="Second", work_email="second@newco-demo.com"))

    admin = await make_person(session, tenant, role=UserRole.ADMIN, name="Workspace Admin")
    employee = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Some Employee")
    for outsider in (admin, employee):
        refused = await client.get("/api/v1/platform/demo-requests", headers=_bearer(outsider))
        assert refused.status_code == 403
    assert (await client.get("/api/v1/platform/demo-requests")).status_code == 401

    listed = await client.get("/api/v1/platform/demo-requests", headers=_bearer(super_admin))
    assert listed.status_code == 200
    assert [r["company"] for r in listed.json()] == ["Second", "First"]  # newest first


async def test_following_up_is_audited_without_the_notes(
    client: httpx.AsyncClient, session: AsyncSession, tenant: Tenant, super_admin: User
) -> None:
    created = await client.post(URL, json=_form())
    request_id = created.json()["id"]

    updated = await client.patch(
        f"/api/v1/platform/demo-requests/{request_id}",
        json={"status": "contacted", "notes": "Call booked for Thursday"},
        headers=_bearer(super_admin),
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["status"] == "contacted"
    assert body["notes"] == "Call booked for Thursday"
    assert body["handled_by_name"] == "Platform Team"

    entry = await session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "demo_request", AuditLog.entity_id == body["id"]
        )
    )
    assert entry is not None
    assert entry.tenant_id == super_admin.tenant_id
    assert entry.changes["status"] == {"from": "new", "to": "contacted"}
    assert entry.changes["notes"] == "changed"
    assert "Thursday" not in str(entry.changes)

    filtered = await client.get(
        "/api/v1/platform/demo-requests",
        params={"status_filter": "contacted"},
        headers=_bearer(super_admin),
    )
    assert [r["id"] for r in filtered.json()] == [request_id]

    admin = await make_person(session, tenant, role=UserRole.ADMIN, name="Workspace Admin")
    refused = await client.patch(
        f"/api/v1/platform/demo-requests/{request_id}",
        json={"status": "closed"},
        headers=_bearer(admin),
    )
    assert refused.status_code == 403


async def test_an_unknown_request_is_not_found(
    client: httpx.AsyncClient, super_admin: User
) -> None:
    response = await client.patch(
        "/api/v1/platform/demo-requests/00000000-0000-0000-0000-000000000000",
        json={"status": "closed"},
        headers=_bearer(super_admin),
    )
    assert response.status_code == 404
