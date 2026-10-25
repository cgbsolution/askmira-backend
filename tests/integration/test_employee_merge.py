"""Folding a duplicate employee record into the real one.

The workspace's admin has a bare record with their login; the old system's
import brought the same person in again under their own code. The merge
moves the login and everyone reporting to the bare record onto the imported
one, deletes the bare one and audits it; a reference that cannot move stops
it, and two different people are never merged.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.models import AuditLog, Employee, EmploymentStatus
from app.models.employee import EmployeeProfile
from app.models.user import User, UserRole
from app.services import employee_merge
from tests.integration.conftest import requires_db
from tests.integration.platform_support import make_person

pytestmark = requires_db


async def _imported(session, tenant, email: str, code: str = "CGB-007") -> Employee:
    employee = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name="Deepak",
        last_name="Rawat",
        work_email=email.upper(),
        pan="ABCPR1234F",
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2020, 1, 6),
    )
    session.add(employee)
    await session.commit()
    return employee


async def test_the_bare_record_folds_into_the_imported_one(session, tenant):
    admin = await make_person(session, tenant, role=UserRole.ADMIN, name="Deepak Rawat")
    bare = await session.get(Employee, admin.employee_id)
    report_to_bare = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Ravi Kumar")
    ravi = await session.get(Employee, report_to_bare.employee_id)
    ravi.manager_id = bare.id
    kept = await _imported(session, tenant, bare.work_email)
    kept.manager_id = bare.id  # would become a loop onto itself
    await session.commit()

    groups = await employee_merge.duplicates(session, tenant.id)
    assert [[e.employee_code for e in g] for g in groups] == [[bare.employee_code, "CGB-007"]]

    bare_id, bare_code, kept_id, ravi_id = bare.id, bare.employee_code, kept.id, ravi.id
    admin_id, tenant_id = admin.id, tenant.id
    report = await employee_merge.merge(
        session, tenant.id, keep_code="cgb-007", drop_code=bare_code
    )
    await session.commit()
    assert report.moved["users.employee_id"] == 1
    assert report.moved["employees.manager_id"] == 1

    session.expire_all()
    assert await session.get(Employee, bare_id) is None
    login = await session.get(User, admin_id)
    assert login.employee_id == kept_id
    assert (await session.get(Employee, ravi_id)).manager_id == kept_id
    kept = await session.get(Employee, kept_id)
    assert (kept.manager_id, kept.pan) == (None, "ABCPR1234F")
    trail = await session.scalar(select(AuditLog).where(AuditLog.entity_id == bare_id))
    assert trail.changes["merged_into"] == "CGB-007"
    assert await employee_merge.duplicates(session, tenant_id) == []


async def test_a_reference_that_cannot_move_stops_the_merge(session, tenant):
    admin = await make_person(session, tenant, role=UserRole.ADMIN, name="Deepak Rawat")
    bare = await session.get(Employee, admin.employee_id)
    kept = await _imported(session, tenant, bare.work_email)
    # One profile per person: both have one, so the bare one's cannot move.
    session.add(EmployeeProfile(tenant_id=tenant.id, employee_id=bare.id))
    session.add(EmployeeProfile(tenant_id=tenant.id, employee_id=kept.id))
    await session.commit()

    bare_id, bare_code, tenant_id, admin_id = bare.id, bare.employee_code, tenant.id, admin.id
    with pytest.raises(employee_merge.MergeError, match="employee_profiles.employee_id"):
        await employee_merge.merge(session, tenant_id, keep_code="CGB-007", drop_code=bare_code)
    await session.rollback()
    assert await session.get(Employee, bare_id) is not None
    assert (await session.get(User, admin_id)).employee_id == bare_id


async def test_two_different_people_are_never_merged(session, tenant):
    admin = await make_person(session, tenant, role=UserRole.ADMIN, name="Deepak Rawat")
    bare = await session.get(Employee, admin.employee_id)
    await _imported(session, tenant, "someone.else@example.com")
    with pytest.raises(employee_merge.MergeError, match="different emails"):
        await employee_merge.merge(
            session, tenant.id, keep_code="CGB-007", drop_code=bare.employee_code
        )
