"""Grades, pay bands, and where each person's pay sits in their band."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee, EmploymentStatus
from app.models.org import LegalEntity
from app.models.payroll import EmployeeSalary, SalaryStructure
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.employee import EmployeeUpdate
from app.schemas.pay_grades import PayGradeCreate, PayGradeUpdate
from app.services import employee as employee_service
from app.services.payroll import grades
from tests.integration.conftest import requires_db

pytestmark = requires_db


async def _person(
    session: AsyncSession, tenant: Tenant, entity: LegalEntity, code: str, grade: str | None
) -> Employee:
    person = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code.title(),
        work_email=f"{code.lower()}@acme-demo.com",
        legal_entity_id=entity.id,
        status=EmploymentStatus.ACTIVE,
        grade=grade,
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


async def test_positioning_reads_each_ctc_against_the_band(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    l4 = await grades.create_grade(
        session,
        hr_user,
        PayGradeCreate(
            code="L4",
            name="Senior engineer",
            level=4,
            band_min=Decimal("1500000"),
            band_mid=Decimal("2000000"),
            band_max=Decimal("2500000"),
        ),
        request=None,
    )
    structure = SalaryStructure(
        tenant_id=tenant.id,
        name="Standard",
        effective_from=date(2025, 1, 1),
        applicable_grades=[],
        applicable_locations=[],
    )
    session.add(structure)
    await session.flush()
    low = await _person(session, tenant, legal_entity, "LOW", "L4")
    mid = await _person(session, tenant, legal_entity, "MID", "L4")
    none = await _person(session, tenant, legal_entity, "NONE", None)
    for person, ctc in ((low, "1200000"), (mid, "2200000"), (none, "900000")):
        session.add(
            EmployeeSalary(
                tenant_id=tenant.id,
                employee_id=person.id,
                structure_id=structure.id,
                ctc_annual=Decimal(ctc),
                effective_from=date(2026, 4, 1),
            )
        )
    await session.commit()

    report = await grades.positioning(session, hr_user, on=date(2026, 10, 1))
    by_code = {p["employee_code"]: p for p in report["people"]}
    assert by_code["LOW"]["position"] == "below"
    assert by_code["MID"]["position"] == "within"
    assert by_code["MID"]["compa_ratio"] == "1.10"
    assert by_code["NONE"]["position"] == "no_grade"
    assert report["summary"]["below"] == 1

    listed = await grades.list_grades(session, hr_user)
    assert [(g.code, count) for g, count in listed] == [("L4", 2)]

    with pytest.raises(HTTPException):
        await grades.update_grade(
            session, hr_user, l4.id, PayGradeUpdate(band_min=Decimal("3000000")), request=None
        )


async def test_a_record_can_only_carry_a_defined_grade_once_grades_exist(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    person = await _person(session, tenant, legal_entity, "P1", None)
    # No grades defined yet: free text is still accepted.
    await employee_service.update_employee(session, hr_user, person.id, EmployeeUpdate(grade="X9"))
    await grades.create_grade(
        session,
        hr_user,
        PayGradeCreate(
            code="L2",
            name="Engineer",
            level=2,
            band_min=Decimal("600000"),
            band_mid=Decimal("800000"),
            band_max=Decimal("1000000"),
        ),
        request=None,
    )
    with pytest.raises(HTTPException) as refused:
        await employee_service.update_employee(
            session, hr_user, person.id, EmployeeUpdate(grade="L9")
        )
    assert "not one of this workspace's grades" in refused.value.detail
    saved = await employee_service.update_employee(
        session, hr_user, person.id, EmployeeUpdate(grade="L2")
    )
    assert saved.grade == "L2"
