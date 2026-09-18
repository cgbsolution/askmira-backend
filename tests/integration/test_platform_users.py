"""Logins, roles and modules: every escalation route is refused, every change audited."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.models import AuditAction, AuditLog, Tenant, User, UserRole
from tests.integration import platform_support
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, make_person

pytestmark = requires_db

client = platform_support.client

API = "/api/v1/admin"


async def _change(client, actor, target, role):
    return await client.patch(
        f"{API}/users/{target.id}/role",
        json={"role": role, "reason": "Testing the rules"},
        headers=bearer(actor),
    )


async def test_nobody_changes_their_own_role(client, session, tenant):
    owner = await make_person(session, tenant, role=UserRole.TENANT_ADMIN, name="Owner One")
    response = await _change(client, owner, owner, "hr_ops")
    assert response.status_code == 403
    assert "your own role" in response.json()["detail"]


async def test_nobody_grants_platform_admin(client, session, tenant):
    owner = await make_person(session, tenant, role=UserRole.TENANT_ADMIN, name="Owner One")
    target = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Emp One")
    response = await _change(client, owner, target, "platform_admin")
    assert response.status_code == 403

    created = await client.post(
        f"{API}/users",
        json={"email": "new.person@acme-demo.com", "full_name": "New Person",
              "role": "platform_admin"},
        headers=bearer(owner),
    )
    assert created.status_code == 403


async def test_only_a_tenant_admin_grants_or_changes_tenant_admin(client, session, tenant):
    hr_admin = await make_person(session, tenant, role=UserRole.HR_ADMIN, name="Hr Admin")
    owner = await make_person(session, tenant, role=UserRole.TENANT_ADMIN, name="Owner One")
    target = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Emp One")

    assert (await _change(client, hr_admin, target, "tenant_admin")).status_code == 403
    assert (await _change(client, hr_admin, owner, "employee")).status_code == 403
    deactivate = await client.patch(
        f"{API}/users/{owner.id}/active",
        json={"active": False, "reason": "Testing"},
        headers=bearer(hr_admin),
    )
    assert deactivate.status_code == 403

    # An HR admin may still manage ordinary roles.
    assert (await _change(client, hr_admin, target, "manager")).status_code == 200


async def test_an_employee_cannot_reach_user_administration(client, session, tenant):
    employee = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Emp One")
    other = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Emp Two")
    assert (await client.get(f"{API}/users", headers=bearer(employee))).status_code == 403
    assert (await _change(client, employee, other, "hr_admin")).status_code == 403


async def test_a_role_change_ends_sessions_and_is_audited(client, session, tenant):
    owner = await make_person(session, tenant, role=UserRole.TENANT_ADMIN, name="Owner One")
    target = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Emp One")
    old_token = bearer(target)

    response = await _change(client, owner, target, "hr_ops")
    assert response.status_code == 200
    assert response.json()["role"] == "hr_ops"

    await session.refresh(target)
    assert target.tokens_valid_from is not None
    # A token minted before the change no longer works.
    assert (await client.get("/api/v1/auth/me", headers=old_token)).status_code == 401

    entry = await session.scalar(
        select(AuditLog).where(
            AuditLog.entity_id == target.id, AuditLog.action == AuditAction.PERMISSION_CHANGE
        )
    )
    assert entry is not None
    assert entry.changes["role"] == {"from": "employee", "to": "hr_ops"}


async def test_another_tenants_user_is_not_found(client, session, tenant, other_tenant):
    owner = await make_person(session, tenant, role=UserRole.TENANT_ADMIN, name="Owner One")
    foreign = await make_person(session, other_tenant, role=UserRole.EMPLOYEE, name="Far Away")
    assert (await _change(client, owner, foreign, "manager")).status_code == 404


async def test_the_last_tenant_admin_cannot_be_removed(client, session, tenant):
    owner = await make_person(session, tenant, role=UserRole.TENANT_ADMIN, name="Owner One")
    other = await make_person(session, tenant, role=UserRole.TENANT_ADMIN, name="Owner Two")
    assert (await _change(client, owner, other, "hr_admin")).status_code == 200
    # A token minted in the same second as the demotion is refused; wait it out.
    await asyncio.sleep(1.1)
    assert (await _change(client, other, owner, "hr_admin")).status_code == 403
    # Now the only tenant admin, the owner cannot step down either.
    assert (await _change(client, owner, owner, "hr_admin")).status_code == 403
    deactivate = await client.patch(
        f"{API}/users/{owner.id}/active",
        json={"active": False, "reason": "Testing"},
        headers=bearer(owner),
    )
    assert deactivate.status_code == 403


async def test_creating_a_login_returns_a_one_time_password_only(client, session, tenant):
    owner = await make_person(session, tenant, role=UserRole.TENANT_ADMIN, name="Owner One")
    employee = await make_person(
        session, tenant, role=UserRole.EMPLOYEE, name="No Login", with_employee=True
    )
    # Detach the login so the employee record is free to link.
    employee_id = employee.employee_id
    employee.employee_id = None
    await session.commit()

    response = await client.post(
        f"{API}/users",
        json={
            "email": "linked.person@acme-demo.com",
            "full_name": "Linked Person",
            "role": "employee",
            "employee_id": str(employee_id),
        },
        headers=bearer(owner),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["temporary_password"]
    assert "password_hash" not in body["user"]

    listing = await client.get(f"{API}/users", headers=bearer(owner))
    assert all("password" not in key for row in listing.json() for key in row)
    created = await session.scalar(select(User).where(User.email == "linked.person@acme-demo.com"))
    assert created.employee_id == employee_id


async def test_module_toggle_is_enforced_and_audited(client, session, tenant: Tenant):
    owner = await make_person(session, tenant, role=UserRole.TENANT_ADMIN, name="Owner One")
    employee = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Emp One")

    assert (await client.get("/api/v1/leave/types", headers=bearer(employee))).status_code == 200
    response = await client.put(
        f"{API}/modules", json={"modules": {"leave": False}}, headers=bearer(owner)
    )
    assert response.status_code == 200
    state = {m["key"]: m["enabled"] for m in response.json()["modules"]}
    assert state["leave"] is False
    assert (await client.get("/api/v1/leave/types", headers=bearer(employee))).status_code == 403

    unknown = await client.put(
        f"{API}/modules", json={"modules": {"quantum": True}}, headers=bearer(owner)
    )
    assert unknown.status_code == 400

    entry = await session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "tenant", AuditLog.action == AuditAction.PERMISSION_CHANGE
        )
    )
    assert entry is not None and entry.changes["enabled_modules"] == {"leave": False}

    # Ordinary employees cannot toggle modules.
    denied = await client.put(
        f"{API}/modules", json={"modules": {"leave": True}}, headers=bearer(employee)
    )
    assert denied.status_code == 403
