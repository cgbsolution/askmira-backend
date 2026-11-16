"""Consultant fields saved through the employee service."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.contractors import ContractorKind, TdsSection
from app.models.employee import ApproverRoute, Employee, EmploymentType
from app.models.user import User, UserRole
from app.schemas.employee import EmployeeCreate, EmployeeUpdate
from app.services import employee as employee_service
from tests.integration.conftest import as_caller, requires_db
from tests.integration.platform_support import make_person

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


# ── The rest of the consultant rules, through the service ───────────────────

NINE = (
    "gst_rate",
    "gstin",
    "tds_section",
    "payee_kind",
    "contract_end_date",
    "timesheet_approver_route",
    "customer_contact_name",
    "customer_contact_email",
    "internal_approver_user_id",
)


async def test_a_consultant_saved_bare_gets_the_defaults(session, hr_user) -> None:
    caller = await as_caller(session, hr_user)
    created = await employee_service.create_employee(session, caller, _consultant("D1"))
    assert created.tds_section is TdsSection.S194J_PROFESSIONAL
    assert created.payee_kind is ContractorKind.INDIVIDUAL
    assert created.gst_rate is None  # no GST, so no rate

    with_gst = await employee_service.create_employee(
        session, caller, _consultant("D2", gst_applicable=True, gstin="29ABCDE1234F1Z5")
    )
    assert with_gst.gst_rate == Decimal("18")


async def test_becoming_a_consultant_gets_the_defaults(session, hr_user) -> None:
    caller = await as_caller(session, hr_user)
    person = await employee_service.create_employee(
        session,
        caller,
        EmployeeCreate(employee_code="D3", first_name="Pat", work_email="d3@acme.example.com"),
    )
    updated = await employee_service.update_employee(
        session,
        caller,
        person.id,
        EmployeeUpdate(
            employment_type=EmploymentType.CONSULTANT,
            gst_applicable=True,
            gstin="29ABCDE1234F1Z5",
        ),
    )
    assert updated.tds_section is TdsSection.S194J_PROFESSIONAL
    assert updated.payee_kind is ContractorKind.INDIVIDUAL
    assert updated.gst_rate == Decimal("18")


async def test_legacy_gst_consultant_can_be_edited_without_gstin(session, tenant, hr_user) -> None:
    caller = await as_caller(session, hr_user)
    legacy = Employee(
        tenant_id=tenant.id,
        employee_code="L1",
        first_name="Old",
        work_email="l1@acme.example.com",
        employment_type=EmploymentType.CONSULTANT,
        gst_applicable=True,
    )
    session.add(legacy)
    await session.commit()

    edited = await employee_service.update_employee(
        session, caller, legacy.id, EmployeeUpdate(phone="9999999999")
    )
    assert edited.phone == "9999999999"
    assert edited.gst_rate is None and edited.gstin is None

    with pytest.raises(HTTPException) as refused:
        await employee_service.update_employee(
            session, caller, legacy.id, EmployeeUpdate(gst_rate=Decimal("18"))
        )
    assert refused.value.status_code == 422
    with pytest.raises(HTTPException) as refused:
        await employee_service.update_employee(
            session, caller, legacy.id, EmployeeUpdate(gst_applicable=True)
        )
    assert refused.value.status_code == 422


async def test_leaving_consultant_clears_the_consultant_fields(session, hr_user) -> None:
    caller = await as_caller(session, hr_user)
    created = await employee_service.create_employee(
        session,
        caller,
        _consultant(
            "X1",
            gst_applicable=True,
            gstin="29ABCDE1234F1Z5",
            contract_end_date=date(2027, 3, 31),
            internal_approver_user_id=hr_user.id,
        ),
    )
    assert created.gstin is not None
    updated = await employee_service.update_employee(
        session,
        caller,
        created.id,
        EmployeeUpdate(employment_type=EmploymentType.FULL_TIME),
    )
    assert all(getattr(updated, key) is None for key in NINE)
    assert updated.gst_applicable is False


async def test_turning_gst_off_clears_rate_and_gstin(session, hr_user) -> None:
    caller = await as_caller(session, hr_user)
    created = await employee_service.create_employee(
        session, caller, _consultant("G1", gst_applicable=True, gstin="29ABCDE1234F1Z5")
    )
    updated = await employee_service.update_employee(
        session, caller, created.id, EmployeeUpdate(gst_applicable=False)
    )
    assert updated.gst_rate is None and updated.gstin is None
    assert updated.tds_section is TdsSection.S194J_PROFESSIONAL


async def test_only_hr_sees_the_consultant_fields(session, tenant, hr_user) -> None:
    caller = await as_caller(session, hr_user)
    manager = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Mona Lisa")
    manager_employee = await session.get(Employee, manager.employee_id)
    created = await employee_service.create_employee(
        session,
        caller,
        _consultant(
            "V1",
            gst_applicable=True,
            gstin="29ABCDE1234F1Z5",
            contract_end_date=date(2027, 3, 31),
            customer_contact_name="Asha",
            customer_contact_email="asha@client.example.com",
            internal_approver_user_id=hr_user.id,
            manager_id=manager_employee.id,
        ),
    )

    seen_by_hr = await employee_service.employee_view(session, caller, created)
    assert all(getattr(seen_by_hr, key) is not None for key in NINE)

    seen_by_manager = await employee_service.employee_view(
        session, await as_caller(session, manager), created
    )
    assert all(getattr(seen_by_manager, key) is None for key in NINE)
