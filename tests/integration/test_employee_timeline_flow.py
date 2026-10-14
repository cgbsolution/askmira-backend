"""One person's history across modules, and who may read which part of it."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.culture import Shoutout
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import LeaveRequest, LeaveRequestStatus, LeaveType
from app.models.lifecycle import LifecycleEvent, LifecycleEventType, LifecycleStatus
from app.models.org import LegalEntity
from app.models.payroll import EmployeeSalary, SalaryStructure
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services import employee_timeline
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db


async def _person(
    session: AsyncSession, tenant: Tenant, entity: LegalEntity, code: str, **extra
) -> tuple[Employee, User]:
    person = Employee(
        tenant_id=tenant.id, employee_code=code, first_name=code.title(),
        work_email=f"{code.lower()}@acme-demo.com", legal_entity_id=entity.id,
        status=EmploymentStatus.ACTIVE, date_of_joining=date(2025, 4, 1), **extra,
    )
    session.add(person)
    await session.flush()
    login = User(
        tenant_id=tenant.id, email=f"{code.lower()}.tl@acme-demo.com", password_hash="x",
        full_name=code.title(), role=UserRole.EMPLOYEE, is_active=True, employee_id=person.id,
    )
    session.add(login)
    await session.commit()
    return person, await as_caller(session, login)


async def test_the_timeline_gathers_modules_and_hides_pay_from_the_manager(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    lead, lead_login = await _person(session, tenant, legal_entity, "LEAD")
    dev, dev_login = await _person(session, tenant, legal_entity, "DEV", manager_id=lead.id)
    peer, peer_login = await _person(session, tenant, legal_entity, "PEER")
    lead_login = await as_caller(session, lead_login)

    leave_type = LeaveType(tenant_id=tenant.id, code="CL", name="Casual leave", is_paid=True)
    structure = SalaryStructure(
        tenant_id=tenant.id, name="Standard", effective_from=date(2025, 1, 1),
        applicable_grades=[], applicable_locations=[],
    )
    session.add_all([leave_type, structure])
    await session.flush()
    session.add_all([
        LeaveRequest(
            tenant_id=tenant.id, employee_id=dev.id, leave_type_id=leave_type.id,
            start_date=date(2026, 5, 4), end_date=date(2026, 5, 5), total_days=2,
            status=LeaveRequestStatus.APPROVED,
        ),
        EmployeeSalary(
            tenant_id=tenant.id, employee_id=dev.id, structure_id=structure.id,
            ctc_annual=Decimal("1800000"), effective_from=date(2026, 4, 1),
        ),
        LifecycleEvent(
            tenant_id=tenant.id, employee_id=dev.id, event_type=LifecycleEventType.GRADE_CHANGE,
            effective_from=date(2026, 7, 1), previous_values={"grade": "L3"},
            new_values={"grade": "L4"}, reason="Promotion", status=LifecycleStatus.EFFECTED,
        ),
        Shoutout(
            tenant_id=tenant.id, from_employee_id=lead.id, to_employee_id=dev.id,
            message="Great launch",
        ),
    ])
    await session.commit()

    everything = await employee_timeline.timeline(session, hr_user, dev.id)
    modules = {e["module"] for e in everything["events"]}
    assert {"leave", "pay", "lifecycle", "recognition"} <= modules
    assert everything["events"][0]["at"] >= everything["events"][-1]["at"]

    own = await employee_timeline.timeline(session, dev_login, dev.id)
    assert "pay" in {e["module"] for e in own["events"]}

    managed = await employee_timeline.timeline(session, lead_login, dev.id)
    seen = {e["module"] for e in managed["events"]}
    assert "pay" not in seen and "leave" in seen
    assert "pay" not in managed["modules"]

    with pytest.raises(HTTPException):
        await employee_timeline.timeline(session, peer_login, dev.id)

    only_leave = await employee_timeline.timeline(session, hr_user, dev.id, modules={"leave"})
    assert {e["module"] for e in only_leave["events"]} == {"leave"}
