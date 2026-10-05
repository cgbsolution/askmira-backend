"""The access export holds the person's own data and nobody else's; erasure runs."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.models import (
    AuditAction,
    AuditLog,
    DataPrincipalKind,
    DataPrincipalRequest,
    DprRequestStatus,
    DprRequestType,
    Notification,
    UserRole,
)
from app.models.notifications import Channel, NotificationStatus
from app.services.dpdp import service as dpdp
from app.services.retention import service as retention
from tests.integration import platform_support
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, make_person

pytestmark = requires_db

client = platform_support.client


async def _configure(session, admin):
    await dpdp.upsert_configuration(session, admin, grievance_contact="dpo@acme-demo.com")


async def _note(session, tenant, user, body):
    session.add(
        Notification(
            tenant_id=tenant.id,
            recipient_user_id=user.id,
            event_key="system.announcement",
            channel=Channel.IN_APP,
            rendered_body=body,
            status=NotificationStatus.DELIVERED,
        )
    )
    await session.commit()


async def test_my_export_contains_my_rows_only(client, session, tenant):
    admin = await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    await _configure(session, admin)
    me = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Meera Nair")
    colleague = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Ravi Kumar")
    await _note(session, tenant, me, "Hello Meera")
    await _note(session, tenant, colleague, "Hello Ravi")

    response = await client.get("/api/v1/dpdp/me/export/download", headers=bearer(me))
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    body = response.json()
    tables = {section["table"]: section for section in body["contents"]}

    employees = tables["employees"]["rows"]
    assert [row["id"] for row in employees] == [str(me.employee_id)]
    notes = [row["rendered_body"] for row in tables["notifications"]["rows"]]
    assert notes == ["Hello Meera"]
    logins = tables["users"]
    assert "password_hash" not in logins["rows"][0]
    assert "password_hash" in logins["withheld_columns"]
    text = response.text
    assert "Ravi" not in text and str(colleague.employee_id) not in text

    audit = await session.scalar(
        select(AuditLog).where(
            AuditLog.action == AuditAction.EXPORT,
            AuditLog.entity_type == "personal_data_export",
        )
    )
    assert audit is not None and audit.entity_id == me.employee_id


async def _request(session, tenant, principal, kind):
    row = DataPrincipalRequest(
        tenant_id=tenant.id,
        principal_id=principal.employee_id,
        principal_kind=DataPrincipalKind.EMPLOYEE,
        principal_label=principal.full_name,
        request_type=kind,
        status=DprRequestStatus.RECEIVED,
        received_at=datetime.now(UTC),
        due_by=datetime.now(UTC).date(),
        escalate_at=datetime.now(UTC).date(),
        sla_days=30,
    )
    session.add(row)
    await session.commit()
    return row


async def test_a_request_export_is_for_admins_only(client, session, tenant):
    admin = await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    ops = await make_person(session, tenant, role=UserRole.SUB_ADMIN, name="Hari Ops")
    subject = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Meera Nair")
    req = await _request(session, tenant, subject, DprRequestType.ACCESS)
    path = f"/api/v1/dpdp/requests/{req.id}/export/download"
    assert (await client.get(path, headers=bearer(ops))).status_code == 403
    allowed = await client.get(path, headers=bearer(admin))
    assert allowed.status_code == 200
    assert allowed.json()["principal_id"] == str(subject.employee_id)


async def test_another_tenants_request_is_not_found(client, session, tenant, other_tenant):
    subject = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Meera Nair")
    foreign_admin = await make_person(
        session, other_tenant, role=UserRole.ADMIN, name="Far Admin"
    )
    req = await _request(session, tenant, subject, DprRequestType.ERASURE)
    response = await client.post(
        f"/api/v1/dpdp/requests/{req.id}/execute-erasure", headers=bearer(foreign_admin)
    )
    assert response.status_code == 404


async def test_erasure_purges_what_may_go_and_keeps_the_record(client, session, tenant):
    admin = await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    await _configure(session, admin)
    subject = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Meera Nair")
    await _note(session, tenant, subject, "Hello Meera")
    req = await _request(session, tenant, subject, DprRequestType.ERASURE)

    # Refused on your own data.
    own = await client.post(
        f"/api/v1/dpdp/requests/{req.id}/execute-erasure", headers=bearer(subject)
    )
    assert own.status_code == 403

    response = await client.post(
        f"/api/v1/dpdp/requests/{req.id}/execute-erasure", headers=bearer(admin)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "partially_completed"
    outcomes = {entry["table"]: entry["outcome"] for entry in body["executed"]}
    assert outcomes["employees"] in ("retained", "fields_cleared")
    await session.refresh(req)
    assert req.status is DprRequestStatus.PARTIALLY_COMPLETED
    assert req.response_payload["plan"]["decisions"]

    entry = await session.scalar(
        select(AuditLog).where(
            AuditLog.entity_id == req.id, AuditLog.action == AuditAction.DELETE
        )
    )
    assert entry is not None


async def test_a_legal_hold_blocks_erasure(client, session, tenant):
    admin = await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    subject = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Meera Nair")
    await retention.place_hold(
        session, admin, reason="Tribunal case", employee_ids=[subject.employee_id]
    )
    req = await _request(session, tenant, subject, DprRequestType.ERASURE)
    response = await client.post(
        f"/api/v1/dpdp/requests/{req.id}/execute-erasure", headers=bearer(admin)
    )
    assert response.status_code == 409
    await session.refresh(req)
    assert req.status is DprRequestStatus.RECEIVED
