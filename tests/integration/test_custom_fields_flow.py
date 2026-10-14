"""Custom fields on the employee record: defined by an admin, checked on save,
hidden from the person when sensitive, and audited when they change."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.employee import Employee
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.custom_fields import CustomFieldCreate, CustomFieldUpdate
from app.schemas.employee import EmployeeUpdate
from app.services import custom_fields
from app.services import employee as employee_service
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db


async def _self_user(session: AsyncSession, tenant: Tenant, person: Employee) -> User:
    user = User(
        tenant_id=tenant.id, email="anita.self@acme-demo.com", password_hash="x",
        full_name="Anita Sharma", role=UserRole.EMPLOYEE, is_active=True,
        employee_id=person.id,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return await as_caller(session, user)


async def test_values_are_checked_against_their_definition(
    session: AsyncSession, hr_user: User, employee: Employee
) -> None:
    await custom_fields.create_definition(
        session, hr_user,
        CustomFieldCreate(key="shirt_size", label="T-shirt size", field_type="select",
                          options=["S", "M", "L"]),
        request=None,
    )
    await custom_fields.create_definition(
        session, hr_user,
        CustomFieldCreate(key="uan_seeded_on", label="UAN seeded on", field_type="date"),
        request=None,
    )

    with pytest.raises(HTTPException) as refused:
        await employee_service.update_employee(
            session, hr_user, employee.id, EmployeeUpdate(custom_fields={"shirt_size": "XXL"})
        )
    assert "T-shirt size must be one of" in refused.value.detail

    with pytest.raises(HTTPException) as refused:
        await employee_service.update_employee(
            session, hr_user, employee.id, EmployeeUpdate(custom_fields={"nickname": "Ani"})
        )
    assert "Unknown custom field" in refused.value.detail

    saved = await employee_service.update_employee(
        session, hr_user, employee.id,
        EmployeeUpdate(custom_fields={"shirt_size": "M", "uan_seeded_on": "2026-04-01"}),
    )
    assert saved.custom_fields == {"shirt_size": "M", "uan_seeded_on": "2026-04-01"}

    logged = (
        await session.scalars(
            select(AuditLog.summary).where(AuditLog.entity_type == "custom_field_definition")
        )
    ).all()
    assert any("T-shirt size" in s for s in logged)


async def test_a_required_field_blocks_a_save_without_it(
    session: AsyncSession, hr_user: User, employee: Employee
) -> None:
    await custom_fields.create_definition(
        session, hr_user,
        CustomFieldCreate(key="blood_group", label="Blood group", field_type="text",
                          is_required=True),
        request=None,
    )
    with pytest.raises(HTTPException) as refused:
        await employee_service.update_employee(
            session, hr_user, employee.id, EmployeeUpdate(custom_fields={})
        )
    assert "Blood group" in refused.value.detail


async def test_a_deactivated_field_keeps_its_value_but_takes_no_new_one(
    session: AsyncSession, hr_user: User, employee: Employee
) -> None:
    field = await custom_fields.create_definition(
        session, hr_user,
        CustomFieldCreate(key="locker", label="Locker number", field_type="number"),
        request=None,
    )
    await employee_service.update_employee(
        session, hr_user, employee.id, EmployeeUpdate(custom_fields={"locker": 42})
    )
    await custom_fields.update_definition(
        session, hr_user, field.id, CustomFieldUpdate(is_active=False), request=None
    )

    # Sent back unchanged: the save goes through and the value stays.
    saved = await employee_service.update_employee(
        session, hr_user, employee.id,
        EmployeeUpdate(phone="9800000001", custom_fields={"locker": 42}),
    )
    assert saved.custom_fields == {"locker": 42}

    with pytest.raises(HTTPException):
        await employee_service.update_employee(
            session, hr_user, employee.id, EmployeeUpdate(custom_fields={"locker": 7})
        )


async def test_a_sensitive_field_is_hidden_from_the_person_entirely(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await custom_fields.create_definition(
        session, hr_user,
        CustomFieldCreate(key="bgv_flag", label="BGV flag", field_type="text",
                          is_sensitive=True),
        request=None,
    )
    await custom_fields.create_definition(
        session, hr_user,
        CustomFieldCreate(key="desk", label="Desk", field_type="text"),
        request=None,
    )
    await employee_service.update_employee(
        session, hr_user, employee.id,
        EmployeeUpdate(custom_fields={"bgv_flag": "Address pending", "desk": "4B-12"}),
    )
    me = await _self_user(session, tenant, employee)

    listed = await custom_fields.list_definitions(session, me)
    assert [d.key for d in listed] == ["desk"]

    person = await employee_service.get_employee(session, me, employee.id)
    view = await employee_service.employee_view(session, me, person)
    assert view.custom_fields == {"desk": "4B-12"}


async def test_the_key_must_be_unique_and_the_type_needs_its_options(
    session: AsyncSession, hr_user: User
) -> None:
    await custom_fields.create_definition(
        session, hr_user,
        CustomFieldCreate(key="desk", label="Desk", field_type="text"),
        request=None,
    )
    with pytest.raises(HTTPException) as refused:
        await custom_fields.create_definition(
            session, hr_user,
            CustomFieldCreate(key="desk", label="Desk again", field_type="text"),
            request=None,
        )
    assert refused.value.status_code == 409

    with pytest.raises(ValueError):
        CustomFieldCreate(key="team", label="Team", field_type="select", options=[])
