"""Consultant fields saved through the employee service."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.models.employee import ApproverRoute, EmploymentType
from app.models.user import User, UserRole
from app.schemas.employee import EmployeeCreate, EmployeeUpdate
from app.services import employee as employee_service
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db


def _consultant(code: str, **extra) -> EmployeeCreate:
    return EmployeeCreate(
        employee_code=code,
        first_name="Con",
        work_email=f"{code.lower()}-{uuid.uuid4().hex[:6]}@acme.example.com",
        employment_type=EmploymentType.CONSULTANT,
        timesheet_approver_route=ApproverRoute.INTERNAL,
        **extra,
    )


async def test_internal_approver_must_be_in_this_workspace(
    session, tenant, hr_user, other_hr_user
) -> None:
    caller = await as_caller(session, hr_user)

    with pytest.raises(HTTPException) as refused:
        await employee_service.create_employee(
            session, caller, _consultant("C1", internal_approver_user_id=other_hr_user.id)
        )
    assert refused.value.status_code == 422
    assert "active login in this workspace" in refused.value.detail

    inactive = User(
        tenant_id=tenant.id,
        email="gone@acme.example.com",
        password_hash="x",
        full_name="Gone",
        role=UserRole.ADMIN,
        is_active=False,
    )
    session.add(inactive)
    await session.commit()
    with pytest.raises(HTTPException) as refused:
        await employee_service.create_employee(
            session, caller, _consultant("C2", internal_approver_user_id=inactive.id)
        )
    assert refused.value.status_code == 422

    created = await employee_service.create_employee(
        session, caller, _consultant("C3", internal_approver_user_id=hr_user.id)
    )
    assert created.internal_approver_user_id == hr_user.id

    plain = await employee_service.create_employee(session, caller, _consultant("C4"))
    with pytest.raises(HTTPException) as refused:
        await employee_service.update_employee(
            session,
            caller,
            plain.id,
            EmployeeUpdate(internal_approver_user_id=other_hr_user.id),
        )
    assert refused.value.status_code == 422
