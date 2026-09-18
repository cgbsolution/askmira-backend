"""Slice 12 gates that need a real database.

The headline one: **closed payroll rows are provably unchanged after arrear
processing**, hashed before and after. Everything else in this slice is built on
that promise, and a promise about immutability is worth exactly as much as the
test that checks it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.arrears import ArrearBatch, ArrearLine, ArrearStatus, RevisionType
from app.models.attendance import AttendancePeriodLock
from app.models.employee import Employee
from app.models.org import LegalEntity
from app.models.payroll import (
    CalculationType,
    ComponentType,
    EmployeeSalary,
    PayrollRuleSet,
    PayrollRun,
    PayrollRunStatus,
    Payslip,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
)
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.payroll import EmployeeSalaryCreate
from app.services.arrears import service as arrears_service
from app.services.payroll import service as salary_service
from tests.integration.conftest import requires_db

pytestmark = requires_db

MARCH = date(2026, 3, 1)


async def _structure(session: AsyncSession, tenant: Tenant) -> SalaryStructure:
    basic = SalaryComponent(
        tenant_id=tenant.id, code="BASIC", name="Basic",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.PERCENTAGE_OF,
        base_component_code="CTC", value=Decimal(40),
        counts_toward_pf=True, counts_toward_esi=True, display_order=1,
    )
    special = SalaryComponent(
        tenant_id=tenant.id, code="SPECIAL", name="Special allowance",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.BALANCE,
        counts_toward_esi=True, display_order=9,
    )
    session.add_all([basic, special])
    await session.flush()

    structure = SalaryStructure(
        tenant_id=tenant.id, name="Standard", effective_from=date(2026, 1, 1),
        applicable_grades=[], applicable_locations=[],
    )
    for index, item in enumerate((basic, special)):
        structure.components.append(
            SalaryStructureComponent(
                tenant_id=tenant.id, component_id=item.id, component=item,
                display_order=index,
            )
        )
    session.add(structure)
    await session.commit()
    await session.refresh(structure)
    return structure


async def _pf_rules(session: AsyncSession, tenant: Tenant, *, ceiling: int, effective: date):
    rule_set = PayrollRuleSet(
        tenant_id=tenant.id, rule_type="pf", jurisdiction="IN",
        effective_from=effective,
        rules={
            "wage_ceiling": ceiling, "employee_rate": 12, "employer_rate": 12,
            "apply_wage_ceiling": True,
        },
        approved_at=datetime.now(UTC),
    )
    session.add(rule_set)
    await session.commit()
    await session.refresh(rule_set)
    return rule_set


async def _closed_march(
    session: AsyncSession, tenant: Tenant, legal_entity: LegalEntity, employee: Employee
) -> tuple[PayrollRun, Payslip]:
    """A closed March run on the old salary, with its line items snapshotted."""
    run = PayrollRun(
        tenant_id=tenant.id, legal_entity_id=legal_entity.id,
        period_year=2026, period_month=3, effective_date=date(2026, 3, 31),
        status=PayrollRunStatus.PAID,
    )
    session.add(run)
    await session.flush()

    payslip = Payslip(
        tenant_id=tenant.id, run_id=run.id, employee_id=employee.id,
        working_days=Decimal(31), paid_days=Decimal(31), lop_days=Decimal(0),
        gross_earnings=Decimal("100000.00"), net_pay=Decimal("98200.00"),
        pf_wage_base=Decimal("40000.00"),
        pf_employee=Decimal("1800.00"), pf_employer=Decimal("1800.00"),
        line_items=[
            {"code": "BASIC", "name": "Basic", "type": "earning", "amount": "40000.00"},
            {"code": "SPECIAL", "name": "Special allowance", "type": "earning",
             "amount": "60000.00"},
            {"code": "PF_EMPLOYEE", "name": "Provident Fund (employee)",
             "type": "deduction", "amount": "1800.00"},
            {"code": "PF_EMPLOYER", "name": "Provident Fund (employer)",
             "type": "employer_contribution", "amount": "1800.00"},
        ],
    )
    session.add(payslip)

    session.add(
        AttendancePeriodLock(
            tenant_id=tenant.id,
            period_start=date(2026, 1, 1),
            period_end=date(2099, 12, 31),
            locked_at=datetime.now(UTC),
        )
    )
    await session.commit()
    await session.refresh(run)
    await session.refresh(payslip)
    return run, payslip


async def _new_salary(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, ctc: str
) -> EmployeeSalary:
    structure = await _structure(session, tenant)
    salary = await salary_service.assign_salary(
        session, hr_user, employee.id,
        EmployeeSalaryCreate(
            structure_id=structure.id, ctc_annual=Decimal(ctc), effective_from=MARCH
        ),
    )
    await session.commit()
    return salary


def _fingerprint(payslip: Payslip) -> str:
    """Everything on the payslip that a rewrite would move."""
    return hashlib.sha256(
        json.dumps(
            {
                "gross": str(payslip.gross_earnings),
                "net": str(payslip.net_pay),
                "pf_employee": str(payslip.pf_employee),
                "pf_employer": str(payslip.pf_employer),
                "pf_wage_base": str(payslip.pf_wage_base),
                "paid_days": str(payslip.paid_days),
                "line_items": payslip.line_items,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


# ═══════════════════════════════════════════════════════════════════════════
# Immutability
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_closed_payslip_is_byte_identical_after_arrear_processing(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """The checklist gate: hashed before and after.

    An auditor asking what was disbursed in March needs March's answer.
    """
    await _pf_rules(session, tenant, ceiling=15_000, effective=date(2026, 1, 1))
    run, payslip = await _closed_march(session, tenant, legal_entity, employee)
    before = _fingerprint(payslip)
    run_status_before = run.status

    salary = await _new_salary(session, tenant, hr_user, employee, "1500000")
    batch = await arrears_service.revise_salary(
        session, hr_user,
        legal_entity_id=legal_entity.id,
        revisions=[{"employee_id": str(employee.id), "new_salary_id": str(salary.id)}],
        effective_from=MARCH,
        revision_type=RevisionType.INCREMENT,
    )
    await session.commit()

    await session.refresh(payslip)
    await session.refresh(run)

    assert _fingerprint(payslip) == before, "the closed payslip moved"
    assert run.status is run_status_before
    assert batch.total_earnings_arrear > 0, "and the arrear was still computed"


async def test_the_arrear_is_new_rows_not_a_rewrite(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    await _pf_rules(session, tenant, ceiling=15_000, effective=date(2026, 1, 1))
    run, _ = await _closed_march(session, tenant, legal_entity, employee)
    salary = await _new_salary(session, tenant, hr_user, employee, "1500000")

    batch = await arrears_service.revise_salary(
        session, hr_user, legal_entity_id=legal_entity.id,
        revisions=[{"employee_id": str(employee.id), "new_salary_id": str(salary.id)}],
        effective_from=MARCH, revision_type=RevisionType.INCREMENT,
    )
    await session.commit()

    lines = (
        await session.scalars(select(ArrearLine).where(ArrearLine.batch_id == batch.id))
    ).all()
    assert lines, "arrears exist as their own rows"
    assert all(line.period == "2026-03" for line in lines)

    payslips = (
        await session.scalars(select(Payslip).where(Payslip.run_id == run.id))
    ).all()
    assert len(payslips) == 1, "no new payslip was written into the closed run"


# ═══════════════════════════════════════════════════════════════════════════
# The historical rule set
# ═══════════════════════════════════════════════════════════════════════════


async def test_recomputation_uses_the_rule_version_in_force_in_the_period(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """The checklist gate, end to end.

    The PF ceiling moves in July. Recomputing March must use March's ceiling —
    a March figure computed against July's could never have been deducted then.
    """
    march_rules = await _pf_rules(session, tenant, ceiling=15_000, effective=date(2026, 1, 1))
    march_rules.effective_to = date(2026, 6, 30)
    await _pf_rules(session, tenant, ceiling=21_000, effective=date(2026, 7, 1))
    await session.commit()

    await _closed_march(session, tenant, legal_entity, employee)
    salary = await _new_salary(session, tenant, hr_user, employee, "1500000")

    batch = await arrears_service.revise_salary(
        session, hr_user, legal_entity_id=legal_entity.id,
        revisions=[{"employee_id": str(employee.id), "new_salary_id": str(salary.id)}],
        effective_from=MARCH, revision_type=RevisionType.INCREMENT,
    )
    await session.commit()

    pf_line = await session.scalar(
        select(ArrearLine).where(
            ArrearLine.batch_id == batch.id, ArrearLine.component_code == "PF_EMPLOYEE"
        )
    )
    if pf_line is not None:
        assert pf_line.rule_set_id == march_rules.id, (
            "the arrear was computed against a rule version that was not in force in "
            "March"
        )
        # At March's 15,000 ceiling the PF is unchanged by the raise, so there
        # should be no PF arrear at all. At July's 21,000 ceiling there would be.
        assert Decimal(str(pf_line.difference)) == Decimal(0)


# ═══════════════════════════════════════════════════════════════════════════
# Guardrails
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_revision_before_the_date_of_joining_is_rejected(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """The checklist gate. A salary cannot apply before someone was employed."""
    employee.date_of_joining = date(2026, 2, 1)
    await session.commit()
    salary = await _new_salary(session, tenant, hr_user, employee, "1500000")

    with pytest.raises(HTTPException) as exc:
        await arrears_service.revise_salary(
            session, hr_user, legal_entity_id=legal_entity.id,
            revisions=[{"employee_id": str(employee.id), "new_salary_id": str(salary.id)}],
            effective_from=date(2026, 1, 1), revision_type=RevisionType.INCREMENT,
        )
    assert exc.value.status_code == 422
    assert "cannot apply before someone was employed" in exc.value.detail


async def test_a_revision_into_an_unlocked_period_is_rejected(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """An arrear recomputed against loss-of-pay days that are still moving
    would be wrong the moment someone regularises a punch."""
    employee.date_of_joining = date(2020, 1, 1)
    await session.commit()
    salary = await _new_salary(session, tenant, hr_user, employee, "1500000")

    with pytest.raises(HTTPException) as exc:
        await arrears_service.revise_salary(
            session, hr_user, legal_entity_id=legal_entity.id,
            revisions=[{"employee_id": str(employee.id), "new_salary_id": str(salary.id)}],
            effective_from=date(2024, 1, 1), revision_type=RevisionType.INCREMENT,
        )
    assert exc.value.status_code == 409
    assert "is not locked" in exc.value.detail


async def test_a_large_arrear_is_flagged_for_additional_approval(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    await _pf_rules(session, tenant, ceiling=15_000, effective=date(2026, 1, 1))
    await _closed_march(session, tenant, legal_entity, employee)
    salary = await _new_salary(session, tenant, hr_user, employee, "1500000")

    batch = await arrears_service.revise_salary(
        session, hr_user, legal_entity_id=legal_entity.id,
        revisions=[{"employee_id": str(employee.id), "new_salary_id": str(salary.id)}],
        effective_from=MARCH, revision_type=RevisionType.INCREMENT,
        threshold=Decimal(1_000),
    )
    await session.commit()
    assert batch.requires_additional_approval is True


# ═══════════════════════════════════════════════════════════════════════════
# Preview
# ═══════════════════════════════════════════════════════════════════════════


async def test_preview_writes_nothing(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """The checklist gate: total liability before anything exists."""
    await _pf_rules(session, tenant, ceiling=15_000, effective=date(2026, 1, 1))
    await _closed_march(session, tenant, legal_entity, employee)
    salary = await _new_salary(session, tenant, hr_user, employee, "1500000")

    _, summary = await arrears_service.preview_arrears(
        session, hr_user, legal_entity_id=legal_entity.id,
        employee_salaries={employee.id: salary}, effective_from=MARCH,
    )
    await session.commit()

    assert summary.earnings > 0
    batches = (await session.scalars(select(ArrearBatch))).all()
    assert batches == [], "preview created a batch"


async def test_preview_and_commit_agree(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """A preview that does not match what is committed is worse than no
    preview — it is a number someone approved that never existed."""
    await _pf_rules(session, tenant, ceiling=15_000, effective=date(2026, 1, 1))
    await _closed_march(session, tenant, legal_entity, employee)
    salary = await _new_salary(session, tenant, hr_user, employee, "1500000")

    _, summary = await arrears_service.preview_arrears(
        session, hr_user, legal_entity_id=legal_entity.id,
        employee_salaries={employee.id: salary}, effective_from=MARCH,
    )
    batch = await arrears_service.revise_salary(
        session, hr_user, legal_entity_id=legal_entity.id,
        revisions=[{"employee_id": str(employee.id), "new_salary_id": str(salary.id)}],
        effective_from=MARCH, revision_type=RevisionType.INCREMENT,
    )
    await session.commit()

    assert Decimal(str(batch.total_net_payable)) == summary.net_payable
    assert Decimal(str(batch.total_earnings_arrear)) == summary.earnings


# ═══════════════════════════════════════════════════════════════════════════
# Bulk
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_bulk_revision_produces_one_batch_not_one_per_employee(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """An increment cycle is one batch.

    A payroll lead asked to approve thirty separate batches will approve them
    without reading any.
    """
    await _pf_rules(session, tenant, ceiling=15_000, effective=date(2026, 1, 1))
    structure = await _structure(session, tenant)

    run = PayrollRun(
        tenant_id=tenant.id, legal_entity_id=legal_entity.id,
        period_year=2026, period_month=3, effective_date=date(2026, 3, 31),
        status=PayrollRunStatus.PAID,
    )
    session.add(run)
    session.add(
        AttendancePeriodLock(
            tenant_id=tenant.id, period_start=date(2026, 1, 1),
            period_end=date(2099, 12, 31), locked_at=datetime.now(UTC),
        )
    )
    await session.flush()

    revisions = []
    for index in range(30):
        person = Employee(
            tenant_id=tenant.id, employee_code=f"BULK{index:03d}",
            first_name="Bulk", last_name=str(index),
            work_email=f"bulk{index}@acme.test", legal_entity_id=legal_entity.id,
            date_of_joining=date(2020, 1, 1),
        )
        session.add(person)
        await session.flush()
        session.add(
            Payslip(
                tenant_id=tenant.id, run_id=run.id, employee_id=person.id,
                working_days=Decimal(31), paid_days=Decimal(31),
                gross_earnings=Decimal("100000.00"), net_pay=Decimal("98200.00"),
                line_items=[
                    {"code": "BASIC", "name": "Basic", "type": "earning",
                     "amount": "40000.00"},
                ],
            )
        )
        salary = await salary_service.assign_salary(
            session, hr_user, person.id,
            EmployeeSalaryCreate(
                structure_id=structure.id, ctc_annual=Decimal("1500000"),
                effective_from=MARCH,
            ),
        )
        revisions.append(
            {"employee_id": str(person.id), "new_salary_id": str(salary.id)}
        )
    await session.commit()

    batch = await arrears_service.revise_salary(
        session, hr_user, legal_entity_id=legal_entity.id, revisions=revisions,
        effective_from=MARCH, revision_type=RevisionType.INCREMENT,
    )
    await session.commit()

    batches = (await session.scalars(select(ArrearBatch))).all()
    assert len(batches) == 1
    assert batch.employee_count == 30

    lines = (
        await session.scalars(select(ArrearLine).where(ArrearLine.batch_id == batch.id))
    ).all()
    assert len({line.employee_id for line in lines}) == 30, "per-employee lines"


# ═══════════════════════════════════════════════════════════════════════════
# Approval and affected filings
# ═══════════════════════════════════════════════════════════════════════════


async def _checker(session: AsyncSession, tenant: Tenant) -> User:
    """A second payroll user.

    An arrear batch is approved by somebody other than the person who computed
    it — money leaving the company gets a second pair of eyes — so these tests
    need two people.
    """
    user = User(
        tenant_id=tenant.id, email="checker@acme.test", password_hash="not-a-real-hash",
        full_name="Ravi Checker", role=UserRole.HR_ADMIN, is_active=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def test_affected_filings_must_be_acknowledged_before_approval(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """The checklist gate: the warning surfaces to HR Ops.

    A revised ECR for a closed month is a decision with consequences —
    sometimes carrying the difference forward is right — and approving past it
    silently means nobody made that decision.
    """
    await _pf_rules(session, tenant, ceiling=21_000, effective=date(2026, 1, 1))
    await _closed_march(session, tenant, legal_entity, employee)
    salary = await _new_salary(session, tenant, hr_user, employee, "1500000")

    batch = await arrears_service.revise_salary(
        session, hr_user, legal_entity_id=legal_entity.id,
        revisions=[{"employee_id": str(employee.id), "new_salary_id": str(salary.id)}],
        effective_from=MARCH, revision_type=RevisionType.INCREMENT,
    )
    await session.commit()
    assert batch.affected_filings, "a PF movement changes the ECR"
    checker = await _checker(session, tenant)

    with pytest.raises(HTTPException) as exc:
        await arrears_service.approve_batch(session, checker, batch_id=batch.id)
    assert "already-filed return" in exc.value.detail

    approved = await arrears_service.approve_batch(
        session, checker, batch_id=batch.id, acknowledge_filings=True
    )
    await session.commit()
    assert approved.status is ArrearStatus.APPROVED


async def test_a_batch_with_no_filing_impact_approves_directly(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """At March's ceiling the PF does not move, so no filing is touched."""
    await _pf_rules(session, tenant, ceiling=15_000, effective=date(2026, 1, 1))
    await _closed_march(session, tenant, legal_entity, employee)
    salary = await _new_salary(session, tenant, hr_user, employee, "1500000")

    batch = await arrears_service.revise_salary(
        session, hr_user, legal_entity_id=legal_entity.id,
        revisions=[{"employee_id": str(employee.id), "new_salary_id": str(salary.id)}],
        effective_from=MARCH, revision_type=RevisionType.INCREMENT,
    )
    await session.commit()
    assert batch.affected_filings == []

    checker = await _checker(session, tenant)
    approved = await arrears_service.approve_batch(session, checker, batch_id=batch.id)
    await session.commit()
    assert approved.status is ArrearStatus.APPROVED


async def test_arrear_batches_are_tenant_isolated(
    session: AsyncSession, tenant: Tenant, hr_user: User, other_hr_user: User,
    legal_entity: LegalEntity, employee: Employee,
) -> None:
    await _pf_rules(session, tenant, ceiling=15_000, effective=date(2026, 1, 1))
    await _closed_march(session, tenant, legal_entity, employee)
    salary = await _new_salary(session, tenant, hr_user, employee, "1500000")
    batch = await arrears_service.revise_salary(
        session, hr_user, legal_entity_id=legal_entity.id,
        revisions=[{"employee_id": str(employee.id), "new_salary_id": str(salary.id)}],
        effective_from=MARCH, revision_type=RevisionType.INCREMENT,
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await arrears_service.approve_batch(session, other_hr_user, batch_id=batch.id)
    assert exc.value.status_code == 404
