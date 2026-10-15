"""Customer API keys and webhooks: scoped reads, events from the audit log, signed delivery."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.main import app
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.integrations import DeliveryStatus, WebhookDelivery
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit, integrations
from tests.integration.conftest import requires_db

pytestmark = requires_db


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


async def test_a_key_reads_only_its_scopes_and_its_workspace(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee,
    other_hr_user: User, client: httpx.AsyncClient,
) -> None:
    created = await integrations.create_key(
        session, hr_user, integrations.ApiKeyIn(name="Payroll sync", scopes=["employees:read"]),
        request=None,
    )
    key = created["key"]
    listed = await integrations.list_keys(session, hr_user)
    assert key.startswith("amk_") and "key" not in listed[0]

    ok = await client.get("/api/v1/ext/employees", headers={"Authorization": f"Bearer {key}"})
    assert ok.status_code == 200
    assert [e["employee_code"] for e in ok.json()["items"]] == [employee.employee_code]

    wrong_scope = await client.get(
        "/api/v1/ext/leave-requests?from=2026-01-01&to=2026-01-31",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert wrong_scope.status_code == 403
    no_key = await client.get("/api/v1/ext/employees")
    assert no_key.status_code == 401

    other = await integrations.create_key(
        session, other_hr_user,
        integrations.ApiKeyIn(name="Theirs", scopes=["employees:read"]), request=None,
    )
    theirs = await client.get(
        "/api/v1/ext/employees", headers={"Authorization": f"Bearer {other['key']}"}
    )
    assert theirs.json()["items"] == []

    await integrations.revoke_key(session, hr_user, uuid.UUID(created["id"]), request=None)
    revoked = await client.get("/api/v1/ext/employees", headers={"Authorization": f"Bearer {key}"})
    assert revoked.status_code == 401


async def test_committed_changes_become_signed_deliveries_once(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    with pytest.raises(HTTPException):
        await integrations.create_endpoint(
            session, hr_user,
            integrations.EndpointIn(name="Plain", url="http://hooks.example.com/x"), request=None,
        )
    with pytest.raises(HTTPException):
        await integrations.create_endpoint(
            session, hr_user,
            integrations.EndpointIn(name="Inside", url="https://10.0.0.5/x"), request=None,
        )
    endpoint = await integrations.create_endpoint(
        session, hr_user,
        integrations.EndpointIn(name="ERP", url="https://hooks.example.com/askmira",
                                entity_types=["employee"]),
        request=None,
    )
    secret = endpoint["secret"]

    await audit.record(
        session, tenant_id=tenant.id, action=AuditAction.UPDATE, entity_type="employee",
        entity_id=uuid.uuid4(), actor=hr_user, summary="Updated employee E001",
    )
    await audit.record(
        session, tenant_id=tenant.id, action=AuditAction.UPDATE, entity_type="leave_request",
        entity_id=uuid.uuid4(), actor=hr_user, summary="Not subscribed",
    )
    await session.commit()

    later = datetime.now(UTC) + timedelta(seconds=5)
    first = await integrations.pump_events(session, tenant.id, now=later)
    second = await integrations.pump_events(session, tenant.id, now=later + timedelta(seconds=30))
    assert first["queued"] == 1 and second["queued"] == 0

    sent: list[tuple[bytes, str]] = []

    async def fake_send(endpoint_row, delivery, body: bytes) -> int:
        signature = hmac.new(
            endpoint_row.secret_encrypted.encode(), body, hashlib.sha256
        ).hexdigest()
        sent.append((body, signature))
        return 500 if len(sent) == 1 else 200

    failed_once = await integrations.deliver_due(session, tenant.id, now=later, sender=fake_send)
    assert failed_once == {"sent": 0, "failed": 0, "tried": 1}
    retry_at = later + timedelta(minutes=3)
    delivered = await integrations.deliver_due(session, tenant.id, now=retry_at, sender=fake_send)
    assert delivered["sent"] == 1

    body, signature = sent[-1]
    assert json.loads(body)["event"] == "employee.update"
    assert signature == hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    row = await session.scalar(select(WebhookDelivery))
    assert row is not None and row.status is DeliveryStatus.SENT and row.attempts == 2


def test_a_hostname_that_resolves_inside_is_refused() -> None:
    assert integrations._public_address("localhost") is False


async def test_a_write_key_adds_people_and_pushes_punches_audited_as_itself(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee,
    client: httpx.AsyncClient,
) -> None:
    from app.models.attendance import AttendancePunch
    from app.models.audit import AuditLog

    created = await integrations.create_key(
        session, hr_user,
        integrations.ApiKeyIn(
            name="HRIS bridge", scopes=["employees:read", "employees:write", "attendance:write"]
        ),
        request=None,
    )
    auth = {"Authorization": f"Bearer {created['key']}"}
    added = await client.post(
        "/api/v1/ext/employees",
        json={
            "employee_code": "API001", "first_name": "Kiran", "last_name": "Rao",
            "work_email": "kiran@acme-demo.com", "legal_entity_id": str(employee.legal_entity_id),
            "date_of_joining": "2026-10-01",
        },
        headers=auth,
    )
    assert added.status_code == 201, added.text
    changed = await client.patch(
        f"/api/v1/ext/employees/{added.json()['id']}", json={"designation": "Analyst"},
        headers=auth,
    )
    assert changed.status_code == 200 and changed.json()["designation"] == "Analyst"

    punches = {
        "punches": [
            {"employee_code": "API001", "punched_at": "2026-10-05T09:30:00+05:30",
             "direction": "in"},
            {"employee_code": "API001", "punched_at": "2026-10-05T09:30:00+05:30",
             "direction": "in"},
            {"employee_code": "NOBODY", "punched_at": "2026-10-05T09:31:00+05:30",
             "direction": "in"},
        ]
    }
    pushed = await client.post("/api/v1/ext/attendance-punches", json=punches, headers=auth)
    assert pushed.json() == {"stored": 1, "duplicates": 1, "unknown_codes": ["NOBODY"]}
    again = await client.post("/api/v1/ext/attendance-punches", json=punches, headers=auth)
    assert again.json()["stored"] == 0, "a repeat push stores nothing twice"
    assert len((await session.scalars(select(AttendancePunch))).all()) == 1

    by_key = (
        await session.scalars(select(AuditLog).where(AuditLog.actor_type == "api_key"))
    ).all()
    assert by_key and all(row.changes.get("api_key") == "HRIS bridge" for row in by_key)

    read_only = await integrations.create_key(
        session, hr_user, integrations.ApiKeyIn(name="Reader", scopes=["employees:read"]),
        request=None,
    )
    refused = await client.post(
        "/api/v1/ext/attendance-punches", json=punches,
        headers={"Authorization": f"Bearer {read_only['key']}"},
    )
    assert refused.status_code == 403


async def test_quick_links_are_the_workspaces_own_and_only_safe_addresses(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    from app.services import quick_links

    assert (await quick_links.quick_links(session, hr_user))["is_default"] is True
    with pytest.raises(HTTPException) as unsafe:
        await quick_links.set_quick_links(
            session, hr_user, [quick_links.QuickLinkIn(label="x", url="javascript:alert(1)")]
        )
    assert unsafe.value.status_code == 422
    saved = await quick_links.set_quick_links(
        session, hr_user,
        [
            quick_links.QuickLinkIn(label="Intranet", url="https://intranet.example.com"),
            quick_links.QuickLinkIn(label="Leave", url="/time"),
        ],
    )
    assert [link["label"] for link in saved["links"]] == ["Intranet", "Leave"]
    assert (await quick_links.set_quick_links(session, hr_user, None))["is_default"] is True
