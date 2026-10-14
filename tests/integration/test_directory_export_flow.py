"""The dotted-line manager and the directory export."""

from __future__ import annotations

import csv
import io

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditAction, AuditLog
from app.models.employee import Employee, EmploymentStatus
from app.models.org import LegalEntity
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.employee import EmployeeUpdate
from app.services import employee as employee_service
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db


async def _person(
    session: AsyncSession, tenant: Tenant, entity: LegalEntity, code: str, **extra
) -> Employee:
    person = Employee(
        tenant_id=tenant.id, employee_code=code, first_name=code.title(),
        work_email=f"{code.lower()}@acme-demo.com", legal_entity_id=entity.id,
        status=EmploymentStatus.ACTIVE, **extra,
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


async def test_a_dotted_line_manager_is_kept_and_exported_with_the_line(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    lead = await _person(session, tenant, legal_entity, "LEAD", designation="Engineering lead")
    pm = await _person(session, tenant, legal_entity, "PM", designation="Product manager")
    dev = await _person(
        session, tenant, legal_entity, "DEV", designation="=HYPERLINK(1)", manager_id=lead.id
    )

    with pytest.raises(HTTPException):
        await employee_service.update_employee(
            session, hr_user, dev.id, EmployeeUpdate(dotted_line_manager_id=dev.id)
        )
    saved = await employee_service.update_employee(
        session, hr_user, dev.id, EmployeeUpdate(dotted_line_manager_id=pm.id)
    )
    assert saved.dotted_line_manager_id == pm.id

    content = await employee_service.directory_csv(session, hr_user)
    rows = {row["Employee code"]: row for row in csv.DictReader(io.StringIO(content))}
    assert rows["DEV"]["Manager"] == "Lead"
    assert rows["DEV"]["Manager code"] == "LEAD"
    assert rows["DEV"]["Dotted-line manager"] == "Pm"
    assert rows["LEAD"]["Direct reports"] == "1"
    # A title that would run as a formula is written as text.
    assert rows["DEV"]["Designation"] == "'=HYPERLINK(1)"

    exported = await session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "employee_directory", AuditLog.action == AuditAction.EXPORT
        )
    )
    assert exported is not None


async def test_only_hr_exports_the_directory(
    session: AsyncSession, tenant: Tenant, legal_entity: LegalEntity
) -> None:
    person = await _person(session, tenant, legal_entity, "EMP")
    user = User(
        tenant_id=tenant.id, email="emp@acme-demo.com", password_hash="x", full_name="Emp",
        role=UserRole.EMPLOYEE, is_active=True, employee_id=person.id,
    )
    session.add(user)
    await session.commit()
    await as_caller(session, user)
    with pytest.raises(HTTPException) as refused:
        await employee_service.directory_csv(session, user)
    assert refused.value.status_code == 403
