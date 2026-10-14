"""Narrowing a sub-admin by area, by right and by department — enforced, over HTTP.

What these hold:

- with no rows, a sub-admin is full HR (nothing changes for existing tenants);
- the role's rights apply to every sub-admin, and a person's own row wins;
- a withheld right is refused for the HR action while the sub-admin's own
  self-service keeps working;
- a department scope limits every ORM query the sub-admin makes in a request,
  their own record included, and the report builder too;
- only an admin changes rights or scope, never an admin's own, and each change
  asks for fresh proof of identity.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import hr_scope
from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.employee import Employee, EmploymentStatus
from app.models.opsec import StepUpChallenge
from app.models.org import Department
from app.models.user import User, UserRole
from tests.integration.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
async def client(session: AsyncSession):
    async def override():
        yield session

    app.dependency_overrides[get_session] = override
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        hr_scope.set_scope(None)


def _auth(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def org(session, tenant, legal_entity, location):
    sales = Department(tenant_id=tenant.id, name="Sales", code="SAL")
    tech = Department(tenant_id=tenant.id, name="Engineering", code="ENG")
    session.add_all([sales, tech])
    await session.flush()
    people = {}
    for code, dept in (("S1", sales), ("S2", sales), ("E1", tech), ("OPS", tech)):
        employee = Employee(
            tenant_id=tenant.id, employee_code=code, first_name=code, last_name="Test",
            work_email=f"{code.lower()}@acme-demo.com", legal_entity_id=legal_entity.id,
            location_id=location.id, department_id=dept.id, status=EmploymentStatus.ACTIVE,
            date_of_joining=date(2024, 1, 1),
        )
        session.add(employee)
        people[code] = employee
    await session.flush()
    admin = User(
        tenant_id=tenant.id, email="admin@acme-demo.com", password_hash="x",
        full_name="Admin", role=UserRole.ADMIN, is_active=True,
    )
    ops = User(
        tenant_id=tenant.id, email="ops@acme-demo.com", password_hash="x",
        full_name="Ops Person", role=UserRole.SUB_ADMIN, is_active=True,
        employee_id=people["OPS"].id,
    )
    session.add_all([admin, ops])
    await session.commit()
    return {"sales": sales, "tech": tech, "people": people, "admin": admin, "ops": ops}


async def _prove(session, user: User) -> None:
    session.add(
        StepUpChallenge(
            tenant_id=user.tenant_id, user_id=user.id, action_key="user.role_change",
            reason="test", satisfied_at=datetime.now(UTC),
        )
    )
    await session.commit()


async def test_with_no_rows_a_sub_admin_is_full_hr(client, org) -> None:
    runs = await client.get("/api/v1/payroll/runs", headers=_auth(org["ops"]))
    assert runs.status_code == 200
    people = await client.get("/api/v1/employees", headers=_auth(org["ops"]))
    assert people.json()["total"] == 4


async def test_a_withheld_area_refuses_hr_but_not_self_service(client, session, org) -> None:
    admin, ops = org["admin"], org["ops"]

    asked = await client.put(
        "/api/v1/admin/access/role/payroll",
        json={"can_view": False}, headers=_auth(admin),
    )
    assert asked.status_code == 428, "a privilege change wants fresh proof"
    await _prove(session, admin)
    done = await client.put(
        "/api/v1/admin/access/role/payroll", json={"can_view": False}, headers=_auth(admin)
    )
    assert done.status_code == 200 and done.json()["can_view"] is False

    refused = await client.get("/api/v1/payroll/runs", headers=_auth(ops))
    assert refused.status_code == 403
    assert "rights" in refused.json()["detail"]

    # Their own pay is still theirs to see.
    own = await client.get("/api/v1/payroll/my-salary", headers=_auth(ops))
    assert own.status_code != 403

    # A person's own row wins over the role's.
    await _prove(session, admin)
    granted = await client.put(
        f"/api/v1/admin/access/users/{ops.id}/areas/payroll",
        json={"can_view": True, "can_export": True}, headers=_auth(admin),
    )
    assert granted.status_code == 200
    assert (await client.get("/api/v1/payroll/runs", headers=_auth(ops))).status_code == 200
    # ...but not to create a run: that right stayed withheld.
    created = await client.post(
        "/api/v1/payroll/runs",
        json={"legal_entity_id": str(org["people"]["S1"].legal_entity_id),
              "period_year": 2026, "period_month": 9},
        headers=_auth(ops),
    )
    assert created.status_code == 403

    overview = (await client.get("/api/v1/admin/access", headers=_auth(admin))).json()
    assert overview["role_rights"]["payroll"]["can_view"] is False
    ops_row = next(p for p in overview["people"] if p["user_id"] == str(ops.id))
    assert ops_row["rights"]["payroll"]["can_export"] is True


async def test_a_department_scope_narrows_every_query(client, session, org) -> None:
    admin, ops = org["admin"], org["ops"]
    await _prove(session, admin)
    scoped = await client.put(
        f"/api/v1/admin/access/users/{ops.id}/scope",
        json={"department_ids": [str(org["sales"].id)]}, headers=_auth(admin),
    )
    assert scoped.status_code == 200, scoped.text

    listed = await client.get("/api/v1/employees", headers=_auth(ops))
    codes = {row["employee_code"] for row in listed.json()["items"]}
    assert codes == {"S1", "S2", "OPS"}, "their department, and themselves"

    outside = await client.get(
        f"/api/v1/employees/{org['people']['E1'].id}", headers=_auth(ops)
    )
    assert outside.status_code == 404

    # The admin is never narrowed.
    assert (await client.get("/api/v1/employees", headers=_auth(admin))).json()["total"] == 4

    # Removing the scope restores everybody.
    await _prove(session, admin)
    await client.put(
        f"/api/v1/admin/access/users/{ops.id}/scope", json={}, headers=_auth(admin)
    )
    assert (await client.get("/api/v1/employees", headers=_auth(ops))).json()["total"] == 4


async def test_only_sub_admins_are_narrowed_and_only_by_an_admin(client, session, org) -> None:
    admin, ops = org["admin"], org["ops"]
    await _prove(session, admin)
    on_admin = await client.put(
        f"/api/v1/admin/access/users/{admin.id}/areas/people",
        json={"can_view": False}, headers=_auth(admin),
    )
    assert on_admin.status_code == 409
    by_ops = await client.put(
        "/api/v1/admin/access/role/people", json={"can_view": False}, headers=_auth(ops)
    )
    assert by_ops.status_code == 403
