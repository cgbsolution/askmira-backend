"""Logins for many employees at once, from the directory's checkboxes.

Every selected person who can have a login gets one at their work email;
everyone else comes back with the reason. The welcome emails are queued for the
delivery job, not sent while the admin waits.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select

from app.models import AuditAction, AuditLog, User, UserRole
from app.models.employee import Employee, EmploymentStatus
from app.models.notifications import Channel, Notification, NotificationStatus
from app.services.identity.welcome import WELCOME_EVENT
from tests.integration import platform_support
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, make_person

pytestmark = requires_db

client = platform_support.client

API = "/api/v1/admin/users/bulk"


async def _employee(session, tenant, name, *, email=None, status=EmploymentStatus.ACTIVE):
    first, _, last = name.partition(" ")
    slug = uuid.uuid4().hex[:8]
    emp = Employee(
        tenant_id=tenant.id,
        employee_code=f"B{slug}",
        first_name=first,
        last_name=last or None,
        work_email=email if email is not None else f"{slug}@acme-demo.com",
        status=status,
        date_of_joining=date(2025, 4, 1),
    )
    session.add(emp)
    await session.commit()
    return emp


async def test_bulk_creates_logins_and_explains_everyone_else(client, session, tenant):
    owner = await make_person(session, tenant, role=UserRole.ADMIN, name="Owner One")
    ready = await _employee(session, tenant, "Asha Ready")
    pre_joiner = await _employee(
        session, tenant, "Pia Joiner", status=EmploymentStatus.PRE_JOINING
    )
    left = await _employee(session, tenant, "Lalit Left", status=EmploymentStatus.EXITED)
    has_login = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Has Login")
    twin = await _employee(session, tenant, "Twin Copy", email=ready.work_email.upper())
    taken = await _employee(session, tenant, "Taken Mail", email=owner.email)
    no_email = await _employee(session, tenant, "No Mail", email="")

    response = await client.post(
        API,
        json={
            "employee_ids": [
                str(ready.id),
                str(pre_joiner.id),
                str(left.id),
                str(has_login.employee_id),
                str(twin.id),
                str(taken.id),
                str(no_email.id),
                str(ready.id),  # selected twice: counted once
            ]
        },
        headers=bearer(owner),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] == 2
    assert body["skipped"] == 5
    by_id = {row["employee_id"]: row for row in body["results"]}
    assert len(by_id) == 7

    for emp in (ready, pre_joiner):
        row = by_id[str(emp.id)]
        assert row["created"] is True
        assert row["temporary_password"]
        assert row["email"] == emp.work_email.lower()
        assert row["welcome_email"]["status"] == "queued"
        login = await session.scalar(select(User).where(User.employee_id == emp.id))
        assert login is not None
        assert login.role is UserRole.EMPLOYEE
        assert login.must_change_password is True
        assert login.email == emp.work_email.lower()

    reasons = {key: row["reason"] for key, row in by_id.items() if not row["created"]}
    assert "left" in reasons[str(left.id)]
    assert "Already has a login" in reasons[str(has_login.employee_id)]
    assert "same work email" in reasons[str(twin.id)]
    assert "already signs in" in reasons[str(taken.id)]
    assert "No work email" in reasons[str(no_email.id)]
    assert all(by_id[key]["temporary_password"] is None for key in reasons)

    # Queued for the delivery job, not sent during the request.
    welcome = await session.scalar(
        select(Notification).where(
            Notification.event_key == WELCOME_EVENT,
            Notification.channel == Channel.EMAIL,
            Notification.recipient_user_id == by_id[str(ready.id)]["user_id"],
        )
    )
    assert welcome is not None
    assert welcome.status is NotificationStatus.QUEUED
    assert welcome.next_attempt_at is not None
    assert welcome.attempt_count == 0

    # One audited permission change per login, in the same transaction.
    audits = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant.id,
                AuditLog.action == AuditAction.PERMISSION_CHANGE,
                AuditLog.entity_type == "user",
            )
        )
    ).all()
    bulk = [row for row in audits if row.changes.get("bulk")]
    assert len(bulk) == 2


async def test_admin_rights_are_not_given_in_bulk(client, session, tenant):
    owner = await make_person(session, tenant, role=UserRole.ADMIN, name="Owner One")
    emp = await _employee(session, tenant, "Would Be Admin")
    response = await client.post(
        API, json={"employee_ids": [str(emp.id)], "role": "admin"}, headers=bearer(owner)
    )
    assert response.status_code == 400
    assert await session.scalar(select(User).where(User.employee_id == emp.id)) is None


async def test_without_welcome_emails_nothing_is_queued(client, session, tenant):
    owner = await make_person(session, tenant, role=UserRole.ADMIN, name="Owner One")
    emp = await _employee(session, tenant, "Quiet Start")
    response = await client.post(
        API,
        json={"employee_ids": [str(emp.id)], "role": "sub_admin", "send_welcome_email": False},
        headers=bearer(owner),
    )
    assert response.status_code == 200, response.text
    row = response.json()["results"][0]
    assert row["created"] is True
    assert row["welcome_email"] is None
    login = await session.scalar(select(User).where(User.employee_id == emp.id))
    assert login.role is UserRole.SUB_ADMIN
    queued = await session.scalar(
        select(Notification).where(Notification.recipient_user_id == login.id)
    )
    assert queued is None


async def test_only_an_admin_creates_logins_in_bulk(client, session, tenant):
    sub_admin = await make_person(session, tenant, role=UserRole.SUB_ADMIN, name="Hr Ops")
    emp = await _employee(session, tenant, "Not Yet")
    response = await client.post(
        API, json={"employee_ids": [str(emp.id)]}, headers=bearer(sub_admin)
    )
    assert response.status_code == 403


async def test_another_workspaces_employee_is_not_found(client, session, tenant, other_tenant):
    owner = await make_person(session, tenant, role=UserRole.ADMIN, name="Owner One")
    stranger = await _employee(session, other_tenant, "Other Company")
    response = await client.post(
        API, json={"employee_ids": [str(stranger.id)]}, headers=bearer(owner)
    )
    assert response.status_code == 200
    row = response.json()["results"][0]
    assert row["created"] is False
    assert row["reason"] == "Not found in this workspace."
    assert row["employee_name"] is None
    assert await session.scalar(select(User).where(User.employee_id == stranger.id)) is None
