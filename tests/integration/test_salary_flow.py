"""Slice 01 gates that need a real database, plus tenant isolation.

The audit-entry count and the effective-dated series cannot be asserted against
pure functions — they are about what the database ends up holding.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.employee import Employee
from app.models.payroll import (
    CalculationType,
    ComponentType,
    EmployeeSalary,
    EmployeeSalaryComponent,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.payroll import EmployeeSalaryCreate
from app.services.payroll import service as salary_service
from tests.integration.conftest import requires_db

pytestmark = requires_db


async def _standard_structure(session: AsyncSession, tenant: Tenant) -> SalaryStructure:
    """BASIC 40% of CTC, HRA 50% of BASIC, SPECIAL takes the balance."""
    basic = SalaryComponent(
        tenant_id=tenant.id,
        code="BASIC",
        name="Basic",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.PERCENTAGE_OF,
        base_component_code="CTC",
        value=Decimal(40),
        counts_toward_pf=True,
        display_order=1,
    )
    hra = SalaryComponent(
        tenant_id=tenant.id,
        code="HRA",
        name="House rent allowance",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.PERCENTAGE_OF,
        base_component_code="BASIC",
        value=Decimal(50),
        display_order=2,
    )
    special = SalaryComponent(
        tenant_id=tenant.id,
        code="SPECIAL",
        name="Special allowance",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.BALANCE,
        display_order=9,
    )
    session.add_all([basic, hra, special])
    await session.flush()

    structure = SalaryStructure(
        tenant_id=tenant.id,
        name="Standard 2026",
        effective_from=date(2026, 4, 1),
        applicable_grades=[],
        applicable_locations=[],
    )
    for index, component in enumerate((basic, hra, special)):
        structure.components.append(
            SalaryStructureComponent(
                tenant_id=tenant.id,
                component_id=component.id,
                component=component,
                display_order=index,
            )
        )
    session.add(structure)
    await session.commit()
    await session.refresh(structure)
    return structure


# ═══════════════════════════════════════════════════════════════════════════
# The audit gate
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_salary_assignment_writes_exactly_one_audit_entry(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate: exactly one, with the old and new CTC on it."""
    structure = await _standard_structure(session, tenant)

    await salary_service.assign_salary(
        session,
        hr_user,
        employee.id,
        EmployeeSalaryCreate(
            structure_id=structure.id,
            ctc_annual=Decimal("1200000"),
            effective_from=date(2026, 4, 1),
        ),
    )

    entries = (
        await session.scalars(
            select(AuditLog).where(AuditLog.entity_type == "employee_salary")
        )
    ).all()

    assert len(entries) == 1
    entry = entries[0]
    assert entry.actor_user_id == hr_user.id
    assert entry.changes["new_ctc"] == "1200000.00"
    assert entry.changes["old_ctc"] is None


async def test_a_revision_audits_the_old_and_new_ctc(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    structure = await _standard_structure(session, tenant)

    await salary_service.assign_salary(
        session,
        hr_user,
        employee.id,
        EmployeeSalaryCreate(
            structure_id=structure.id,
            ctc_annual=Decimal("1200000"),
            effective_from=date(2026, 4, 1),
        ),
    )
    await salary_service.assign_salary(
        session,
        hr_user,
        employee.id,
        EmployeeSalaryCreate(
            structure_id=structure.id,
            ctc_annual=Decimal("1500000"),
            effective_from=date(2026, 10, 1),
            revision_reason="Annual increment",
        ),
    )

    entries = (
        await session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "employee_salary")
            .order_by(AuditLog.created_at)
        )
    ).all()

    assert len(entries) == 2
    assert entries[1].changes["old_ctc"] == "1200000.00"
    assert entries[1].changes["new_ctc"] == "1500000.00"
    assert entries[1].changes["reason"] == "Annual increment"


# ═══════════════════════════════════════════════════════════════════════════
# The effective-dated series
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_revision_closes_the_previous_row_rather_than_updating_it(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    structure = await _standard_structure(session, tenant)

    await salary_service.assign_salary(
        session,
        hr_user,
        employee.id,
        EmployeeSalaryCreate(
            structure_id=structure.id,
            ctc_annual=Decimal("1200000"),
            effective_from=date(2026, 4, 1),
        ),
    )
    await salary_service.assign_salary(
        session,
        hr_user,
        employee.id,
        EmployeeSalaryCreate(
            structure_id=structure.id,
            ctc_annual=Decimal("1500000"),
            effective_from=date(2026, 10, 1),
        ),
    )

    history = await salary_service.salary_history(session, hr_user, employee.id)

    assert len(history) == 2, "a revision inserts; it never updates"
    assert history[0].effective_from == date(2026, 10, 1)
    assert history[0].effective_to is None
    assert history[1].effective_from == date(2026, 4, 1)
    assert history[1].effective_to == date(2026, 9, 30), "the prior row is closed the day before"


async def test_the_salary_in_force_resolves_by_date_not_by_today(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """A historical payroll run must resolve the salary that applied then."""
    structure = await _standard_structure(session, tenant)

    for effective, ctc in (
        (date(2026, 4, 1), "1200000"),
        (date(2026, 10, 1), "1500000"),
    ):
        await salary_service.assign_salary(
            session,
            hr_user,
            employee.id,
            EmployeeSalaryCreate(
                structure_id=structure.id,
                ctc_annual=Decimal(ctc),
                effective_from=effective,
            ),
        )

    june = await salary_service.active_salary_on(
        session, hr_user, employee.id, date(2026, 6, 30)
    )
    december = await salary_service.active_salary_on(
        session, hr_user, employee.id, date(2026, 12, 31)
    )

    assert Decimal(str(june.ctc_annual)) == Decimal("1200000")
    assert Decimal(str(december.ctc_annual)) == Decimal("1500000")


async def test_a_backdated_revision_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """Backdating is an arrears run, not an edit to the series."""
    structure = await _standard_structure(session, tenant)

    await salary_service.assign_salary(
        session,
        hr_user,
        employee.id,
        EmployeeSalaryCreate(
            structure_id=structure.id,
            ctc_annual=Decimal("1200000"),
            effective_from=date(2026, 10, 1),
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await salary_service.assign_salary(
            session,
            hr_user,
            employee.id,
            EmployeeSalaryCreate(
                structure_id=structure.id,
                ctc_annual=Decimal("1500000"),
                effective_from=date(2026, 5, 1),
            ),
        )
    assert exc.value.status_code == 422
    assert "arrears" in exc.value.detail


async def test_materialised_components_sum_to_the_ctc_in_the_database(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    structure = await _standard_structure(session, tenant)
    salary = await salary_service.assign_salary(
        session,
        hr_user,
        employee.id,
        EmployeeSalaryCreate(
            structure_id=structure.id,
            ctc_annual=Decimal("987654"),
            effective_from=date(2026, 4, 1),
        ),
    )

    total = await session.scalar(
        select(func.sum(EmployeeSalaryComponent.annual_amount)).where(
            EmployeeSalaryComponent.employee_salary_id == salary.id,
            EmployeeSalaryComponent.part_of_ctc.is_(True),
        )
    )
    assert Decimal(str(total)) == Decimal("987654")


# ═══════════════════════════════════════════════════════════════════════════
# Tenant isolation — the cross-cutting standing gate
# ═══════════════════════════════════════════════════════════════════════════


async def test_one_tenant_cannot_read_anothers_salary_structures(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    other_hr_user: User,
) -> None:
    """A query without the right workspace context returns zero rows.

    Not an error, not a filtered subset — zero.
    """
    await _standard_structure(session, tenant)

    mine = await salary_service.list_structures(session, hr_user)
    theirs = await salary_service.list_structures(session, other_hr_user)

    assert len(mine) == 1
    assert theirs == []


async def test_fetching_another_tenants_structure_by_id_is_a_404_not_a_403(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    other_hr_user: User,
) -> None:
    """A 403 would confirm the id exists somewhere. A 404 tells them nothing."""
    structure = await _standard_structure(session, tenant)

    with pytest.raises(HTTPException) as exc:
        await salary_service.get_structure(session, other_hr_user, structure.id)

    assert exc.value.status_code == 404


async def test_one_tenant_cannot_read_anothers_salary_history(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    other_hr_user: User,
    employee: Employee,
) -> None:
    structure = await _standard_structure(session, tenant)
    await salary_service.assign_salary(
        session,
        hr_user,
        employee.id,
        EmployeeSalaryCreate(
            structure_id=structure.id,
            ctc_annual=Decimal("1200000"),
            effective_from=date(2026, 4, 1),
        ),
    )

    # Same employee id, wrong workspace.
    assert await salary_service.salary_history(session, other_hr_user, employee.id) == []
    assert (
        await salary_service.active_salary_on(
            session, other_hr_user, employee.id, date(2026, 6, 1)
        )
        is None
    )


async def test_every_employee_salary_row_carries_a_tenant_id(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """A null tenant id would be a row visible to nobody, or to everybody."""
    structure = await _standard_structure(session, tenant)
    await salary_service.assign_salary(
        session,
        hr_user,
        employee.id,
        EmployeeSalaryCreate(
            structure_id=structure.id,
            ctc_annual=Decimal("1200000"),
            effective_from=date(2026, 4, 1),
        ),
    )

    orphans = await session.scalar(
        select(func.count(EmployeeSalary.id)).where(EmployeeSalary.tenant_id.is_(None))
    )
    assert orphans == 0

    component_orphans = await session.scalar(
        select(func.count(EmployeeSalaryComponent.id)).where(
            EmployeeSalaryComponent.tenant_id != tenant.id
        )
    )
    assert component_orphans == 0
