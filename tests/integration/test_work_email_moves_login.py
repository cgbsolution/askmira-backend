"""Changing an employee's work email moves their login with it.

Users & roles shows the login's email, and the person signs in with it. When
HR corrected a work email the login used to keep the old address, so the
person was told to sign in with an address that was no longer theirs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import AuditAction, AuditLog, PasswordResetToken, User, UserRole
from app.models.employee import Employee
from tests.integration import platform_support
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, make_person

pytestmark = requires_db

client = platform_support.client


async def _person_on_work_email(session, tenant, name, role=UserRole.EMPLOYEE):
    """A login whose email is their work email, as logins made from the directory are."""
    login = await make_person(session, tenant, role=role, name=name)
    emp = await session.get(Employee, login.employee_id)
    emp.work_email = login.email
    await session.commit()
    return login, emp


async def _edit(client, actor, emp, email):
    return await client.patch(
        f"/api/v1/employees/{emp.id}", json={"work_email": email}, headers=bearer(actor)
    )


async def test_login_follows_the_new_work_email(client, session, tenant):
    admin = await make_person(session, tenant, role=UserRole.ADMIN, name="Owner One")
    login, emp = await _person_on_work_email(session, tenant, "Lipika Prasad")
    old = login.email
    # A welcome link went to the old address and is still outstanding.
    session.add(
        PasswordResetToken(
            tenant_id=tenant.id,
            user_id=login.id,
            token_hash="a" * 64,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
    )
    login.is_email_verified = True
    await session.commit()

    response = await _edit(client, admin, emp, "Lipika.New@Artbox-Demo.com")
    assert response.status_code == 200, response.text
    assert response.json()["work_email"] == "Lipika.New@artbox-demo.com"

    await session.refresh(login)
    assert login.email == "lipika.new@artbox-demo.com"
    assert login.is_email_verified is False
    token = await session.scalar(
        select(PasswordResetToken).where(PasswordResetToken.user_id == login.id)
    )
    assert token.expires_at <= datetime.now(UTC)

    # Users & roles shows the new address.
    listed = await client.get("/api/v1/admin/users", headers=bearer(admin))
    assert listed.status_code == 200, listed.text
    emails = {row["email"] for row in listed.json()}
    assert "lipika.new@artbox-demo.com" in emails
    assert old not in emails

    moved = await session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "user",
            AuditLog.entity_id == login.id,
            AuditLog.action == AuditAction.PERMISSION_CHANGE,
        )
    )
    assert moved is not None
    assert moved.changes["email"] == {"from": old, "to": "lipika.new@artbox-demo.com"}

    # The welcome can be resent to the new address.
    login.must_change_password = True
    await session.commit()
    resent = await client.post(
        f"/api/v1/admin/users/{login.id}/welcome-email", headers=bearer(admin)
    )
    assert resent.status_code == 200, resent.text


async def test_a_login_kept_on_another_address_is_left_alone(client, session, tenant):
    admin = await make_person(session, tenant, role=UserRole.ADMIN, name="Owner One")
    # make_person gives the login an address other than the work email.
    login = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Personal Mail")
    emp = await session.get(Employee, login.employee_id)
    before = login.email

    response = await _edit(client, admin, emp, "corrected@acme-demo.com")
    assert response.status_code == 200, response.text
    await session.refresh(login)
    assert login.email == before


async def test_an_address_another_login_has_is_refused(client, session, tenant):
    admin = await make_person(session, tenant, role=UserRole.ADMIN, name="Owner One")
    other, _ = await _person_on_work_email(session, tenant, "Other Person")
    login, emp = await _person_on_work_email(session, tenant, "Moving Person")
    old = emp.work_email

    response = await _edit(client, admin, emp, other.email.upper())
    assert response.status_code == 409, response.text
    assert "already signs in" in response.json()["detail"]

    # Nothing changed: not the record, not the login.
    await session.refresh(emp)
    await session.refresh(login)
    assert emp.work_email == old
    assert login.email == old


async def test_a_sub_admin_cannot_move_a_login(client, session, tenant):
    sub = await make_person(session, tenant, role=UserRole.SUB_ADMIN, name="Hr Person")
    login, emp = await _person_on_work_email(
        session, tenant, "Top Admin", role=UserRole.ADMIN
    )
    old = login.email

    response = await _edit(client, sub, emp, "taken-over@acme-demo.com")
    assert response.status_code == 403, response.text
    assert "Only an admin" in response.json()["detail"]
    await session.refresh(login)
    await session.refresh(emp)
    assert login.email == old
    assert emp.work_email == old


async def test_a_sub_admin_can_still_edit_someone_without_a_login(client, session, tenant):
    sub = await make_person(session, tenant, role=UserRole.SUB_ADMIN, name="Hr Person")
    login, emp = await _person_on_work_email(session, tenant, "No Login Yet")
    login.employee_id = None
    await session.commit()

    response = await _edit(client, sub, emp, "fixed@acme-demo.com")
    assert response.status_code == 200, response.text


async def test_the_break_glass_designation_follows(client, session, tenant):
    admin = await make_person(session, tenant, role=UserRole.ADMIN, name="Owner One")
    login, emp = await _person_on_work_email(
        session, tenant, "Glass Admin", role=UserRole.ADMIN
    )
    tenant.settings = {**(tenant.settings or {}), "break_glass_email": login.email}
    await session.commit()

    response = await _edit(client, admin, emp, "glass.new@acme-demo.com")
    assert response.status_code == 200, response.text
    await session.refresh(tenant)
    assert tenant.settings["break_glass_email"] == "glass.new@acme-demo.com"
    assert await session.scalar(
        select(User.id).where(User.email == "glass.new@acme-demo.com")
    ) == login.id
