"""Slice 08 gates that need a real database.

Three that cannot be asserted against pure functions: the accrual job is
idempotent, the liability report reconciles to the rupee, and the taxable
excess above the gratuity exemption provably reaches the TDS computation.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.employee import Employee, EmploymentStatus
from app.models.exit_benefits import (
    EncashmentPolicy,
    EncashmentStatus,
    ExitReason,
    GratuityAccrual,
    GratuityConfig,
    ServiceRounding,
)
from app.models.exit_workflow import ExitRecord, ExitStatus, ExitType
from app.models.leave import LeaveBalance, LeaveType
from app.models.payroll import (
    CalculationType,
    ComponentType,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.payroll import EmployeeSalaryCreate
from app.services.exit_benefits import service as exit_service
from app.services.exit_workflow import operations as exit_operations
from app.services.payroll import service as salary_service
from tests.integration.conftest import requires_db

pytestmark = requires_db

AS_OF = date(2026, 4, 1)


async def _config(session: AsyncSession, tenant: Tenant, *, approved=True, **overrides):
    """Gratuity configuration as the SME would set it. Fixture values."""
    base = {
        "eligibility_years": 5,
        "eligibility_days_in_final_year": None,
        "formula_basis": ["BASIC", "DA"],
        "days_per_year_divisor": 26,
        "days_per_year": Decimal(15),
        "max_exempt_amount": Decimal(2_000_000),
        "max_payable_amount": None,
        "service_rounding": ServiceRounding.NEAREST_YEAR_SIX_MONTH_RULE,
        "include_notice_period_in_service": True,
        "eligibility_waived_reasons": ["death", "disablement"],
    }
    base.update(overrides)
    config = GratuityConfig(
        tenant_id=tenant.id,
        approved_at=datetime.now(UTC) if approved else None,
        **base,
    )
    session.add(config)
    await session.commit()
    await session.refresh(config)
    return config


async def _salary_for(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, ctc="1200000"
):
    """BASIC and DA, so the configured wage basis has something to read."""
    existing = await session.scalar(
        select(SalaryStructure).where(SalaryStructure.tenant_id == tenant.id)
    )
    if existing is None:
        basic = SalaryComponent(
            tenant_id=tenant.id, code="BASIC", name="Basic",
            component_type=ComponentType.EARNING,
            calculation_type=CalculationType.PERCENTAGE_OF,
            base_component_code="CTC", value=Decimal(40), counts_toward_pf=True,
            display_order=1,
        )
        da = SalaryComponent(
            tenant_id=tenant.id, code="DA", name="Dearness allowance",
            component_type=ComponentType.EARNING,
            calculation_type=CalculationType.PERCENTAGE_OF,
            base_component_code="BASIC", value=Decimal(10), counts_toward_pf=True,
            display_order=2,
        )
        special = SalaryComponent(
            tenant_id=tenant.id, code="SPECIAL", name="Special allowance",
            component_type=ComponentType.EARNING,
            calculation_type=CalculationType.BALANCE, display_order=9,
        )
        session.add_all([basic, da, special])
        await session.flush()

        existing = SalaryStructure(
            tenant_id=tenant.id, name="Standard", effective_from=date(2010, 1, 1),
            applicable_grades=[], applicable_locations=[],
        )
        for index, component in enumerate((basic, da, special)):
            existing.components.append(
                SalaryStructureComponent(
                    tenant_id=tenant.id, component_id=component.id,
                    component=component, display_order=index,
                )
            )
        session.add(existing)
        await session.commit()

    await salary_service.assign_salary(
        session, hr_user, employee.id,
        EmployeeSalaryCreate(
            structure_id=existing.id,
            ctc_annual=Decimal(ctc),
            effective_from=date(2010, 1, 1),
        ),
    )
    return existing


# ═══════════════════════════════════════════════════════════════════════════
# Accrual idempotency — the headline gate
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_accrual_job_is_idempotent_across_two_runs(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate.

    Everything but `computed_at` must match, and there must be exactly one row
    per employee per date.
    """
    await _config(session, tenant)
    employee.date_of_joining = date(2015, 4, 1)
    await session.commit()
    await _salary_for(session, tenant, hr_user, employee)

    first = await exit_service.accrue_gratuity(session, hr_user, as_of=AS_OF)
    snapshot = [
        (a.employee_id, a.service_days, a.service_years, a.eligible_amount, a.is_eligible)
        for a in first
    ]

    second = await exit_service.accrue_gratuity(session, hr_user, as_of=AS_OF)
    assert [
        (a.employee_id, a.service_days, a.service_years, a.eligible_amount, a.is_eligible)
        for a in second
    ] == snapshot

    count = await session.scalar(
        select(func.count(GratuityAccrual.id)).where(GratuityAccrual.as_of_date == AS_OF)
    )
    assert count == len(first), "one row per employee per date, not duplicates"


async def test_a_third_run_still_changes_nothing(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _config(session, tenant)
    employee.date_of_joining = date(2015, 4, 1)
    await session.commit()
    await _salary_for(session, tenant, hr_user, employee)

    for _ in range(3):
        await exit_service.accrue_gratuity(session, hr_user, as_of=AS_OF)

    count = await session.scalar(select(func.count(GratuityAccrual.id)))
    assert count == 1


async def test_accruals_on_different_dates_are_separate_rows(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """Finance needs the series, not just the latest figure."""
    await _config(session, tenant)
    employee.date_of_joining = date(2015, 4, 1)
    await session.commit()
    await _salary_for(session, tenant, hr_user, employee)

    await exit_service.accrue_gratuity(session, hr_user, as_of=date(2026, 3, 1))
    await exit_service.accrue_gratuity(session, hr_user, as_of=date(2026, 4, 1))

    assert await session.scalar(select(func.count(GratuityAccrual.id))) == 2


async def test_an_employee_with_no_joining_date_is_skipped_not_fatal(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """One incomplete record must not stop the workforce's accrual."""
    await _config(session, tenant)
    employee.date_of_joining = date(2015, 4, 1)

    incomplete = Employee(
        tenant_id=tenant.id, employee_code="NODOJ", first_name="No",
        work_email="nodoj@acme.test", status=EmploymentStatus.ACTIVE,
        date_of_joining=None,
    )
    session.add(incomplete)
    await session.commit()
    await _salary_for(session, tenant, hr_user, employee)

    accruals = await exit_service.accrue_gratuity(session, hr_user, as_of=AS_OF)
    assert len(accruals) == 1
    assert accruals[0].employee_id == employee.id


async def test_accrual_refuses_an_unapproved_configuration(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The SME gate, enforced in code rather than in process."""
    await _config(session, tenant, approved=False)

    with pytest.raises(HTTPException) as exc:
        await exit_service.accrue_gratuity(session, hr_user, as_of=AS_OF)
    assert exc.value.status_code == 409
    assert "not been approved by a Compliance SME" in exc.value.detail


async def test_accrual_refuses_when_no_configuration_exists(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    with pytest.raises(HTTPException) as exc:
        await exit_service.accrue_gratuity(session, hr_user, as_of=AS_OF)
    assert "Compliance SME sets" in exc.value.detail


# ═══════════════════════════════════════════════════════════════════════════
# Liability report reconciliation
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_liability_report_reconciles_to_individual_accruals_to_the_rupee(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate.

    A report that does not tie out is worse than no report, because somebody
    books against it.
    """
    await _config(session, tenant)
    employee.date_of_joining = date(2012, 4, 1)
    await session.commit()
    await _salary_for(session, tenant, hr_user, employee)

    # A spread of tenures, so the bands are actually exercised.
    for index, (joined, ctc) in enumerate(
        [
            (date(2025, 6, 1), "600000"),   # under a year
            (date(2024, 1, 1), "900000"),   # 1-3
            (date(2022, 1, 1), "1200000"),  # 3-5
            (date(2018, 1, 1), "1800000"),  # 5-10
            (date(2010, 1, 1), "2400000"),  # 10+
        ]
    ):
        person = Employee(
            tenant_id=tenant.id, employee_code=f"TEN{index:03d}",
            first_name=f"Person{index}", work_email=f"t{index}@acme.test",
            status=EmploymentStatus.ACTIVE, date_of_joining=joined,
        )
        session.add(person)
        await session.flush()
        await _salary_for(session, tenant, hr_user, person, ctc=ctc)

    accruals = await exit_service.accrue_gratuity(session, hr_user, as_of=AS_OF)
    report = await exit_service.liability_report(session, hr_user, AS_OF)

    individual_total = sum(
        (Decimal(str(a.eligible_amount)) for a in accruals), start=Decimal(0)
    )

    assert Decimal(report["total_accrued_liability"]) == individual_total
    assert report["reconciles"] is True

    by_department = sum(
        Decimal(row["accrued_liability"]) for row in report["by_department"]
    )
    by_tenure = sum(Decimal(row["accrued_liability"]) for row in report["by_tenure_band"])
    assert by_department == individual_total
    assert by_tenure == individual_total


async def test_the_report_separates_eligible_from_not_yet_eligible(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """Two different questions: the provision, and what is payable today."""
    await _config(session, tenant)
    employee.date_of_joining = date(2010, 4, 1)  # long service
    await session.commit()
    await _salary_for(session, tenant, hr_user, employee)

    newcomer = Employee(
        tenant_id=tenant.id, employee_code="NEW001", first_name="New",
        work_email="new@acme.test", status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2025, 6, 1),
    )
    session.add(newcomer)
    await session.flush()
    await _salary_for(session, tenant, hr_user, newcomer, ctc="600000")

    await exit_service.accrue_gratuity(session, hr_user, as_of=AS_OF)
    report = await exit_service.liability_report(session, hr_user, AS_OF)

    assert Decimal(report["eligible_liability"]) > Decimal(0)
    total = Decimal(report["total_accrued_liability"])
    assert (
        Decimal(report["eligible_liability"])
        + Decimal(report["not_yet_eligible_liability"])
        == total
    )


async def test_the_report_groups_by_tenure_band(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _config(session, tenant)
    employee.date_of_joining = date(2010, 4, 1)
    await session.commit()
    await _salary_for(session, tenant, hr_user, employee)
    await exit_service.accrue_gratuity(session, hr_user, as_of=AS_OF)

    report = await exit_service.liability_report(session, hr_user, AS_OF)
    bands = {row["band"] for row in report["by_tenure_band"]}
    assert "10+ years" in bands


# ═══════════════════════════════════════════════════════════════════════════
# The taxable excess reaching TDS
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_taxable_excess_is_recorded_on_the_payout_for_tds(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate: the excess provably reaches the TDS computation.

    A gratuity paid without reaching TDS under-deducts, and the employee finds
    out at filing.
    """
    await _config(session, tenant, max_exempt_amount=Decimal(500_000))
    employee.date_of_joining = date(2005, 4, 1)  # ~21 years
    await session.commit()
    await _salary_for(session, tenant, hr_user, employee, ctc="6000000")

    payout = await exit_service.compute_exit_gratuity(
        session, hr_user, employee_id=employee.id,
        exit_date=date(2026, 4, 1), exit_reason=ExitReason.RETIREMENT,
    )

    assert Decimal(str(payout.gross_amount)) > Decimal(500_000)
    assert Decimal(str(payout.exempt_amount)) == Decimal(500_000)
    assert Decimal(str(payout.taxable_amount)) == Decimal(
        str(payout.gross_amount)
    ) - Decimal(500_000)
    assert Decimal(str(payout.taxable_amount)) > Decimal(0)

    # And it is on the record the TDS computation reads, not only in a log.
    assert payout.working["taxable_amount"] == str(payout.taxable_amount)


async def test_a_payout_below_the_ceiling_carries_no_taxable_amount(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _config(session, tenant, max_exempt_amount=Decimal(2_000_000))
    employee.date_of_joining = date(2018, 4, 1)
    await session.commit()
    await _salary_for(session, tenant, hr_user, employee)

    payout = await exit_service.compute_exit_gratuity(
        session, hr_user, employee_id=employee.id,
        exit_date=date(2026, 4, 1), exit_reason=ExitReason.RESIGNATION,
    )
    assert Decimal(str(payout.taxable_amount)) == Decimal(0)


async def test_a_payout_records_which_configuration_produced_it(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The exemption ceiling changes; a past payout must stay explainable."""
    config = await _config(session, tenant)
    employee.date_of_joining = date(2015, 4, 1)
    await session.commit()
    await _salary_for(session, tenant, hr_user, employee)

    payout = await exit_service.compute_exit_gratuity(
        session, hr_user, employee_id=employee.id,
        exit_date=date(2026, 4, 1), exit_reason=ExitReason.RESIGNATION,
    )
    assert payout.config_id == config.id


async def test_gratuity_cannot_be_paid_twice(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _config(session, tenant)
    employee.date_of_joining = date(2015, 4, 1)
    await session.commit()
    await _salary_for(session, tenant, hr_user, employee)

    payout = await exit_service.compute_exit_gratuity(
        session, hr_user, employee_id=employee.id,
        exit_date=date(2026, 4, 1), exit_reason=ExitReason.RESIGNATION,
    )
    # Mark it paid, as a payroll run would.
    from app.models.payroll import PayrollRun, PayrollRunType

    run = PayrollRun(
        tenant_id=tenant.id,
        legal_entity_id=(await session.scalar(select(Employee.legal_entity_id))),
        period_year=2026, period_month=4, effective_date=date(2026, 4, 30),
        run_type=PayrollRunType.FULL_AND_FINAL,
    )
    session.add(run)
    await session.flush()
    payout.paid_in_run_id = run.id
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await exit_service.compute_exit_gratuity(
            session, hr_user, employee_id=employee.id,
            exit_date=date(2026, 4, 1), exit_reason=ExitReason.RESIGNATION,
        )
    assert exc.value.status_code == 409
    assert "already been paid" in exc.value.detail


async def test_a_payout_with_no_matching_wage_components_names_the_basis(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The error tells the admin which components to map."""
    await _config(session, tenant, formula_basis=["BASIC_PLUS_DA_NONEXISTENT"])
    employee.date_of_joining = date(2015, 4, 1)
    await session.commit()
    await _salary_for(session, tenant, hr_user, employee)

    with pytest.raises(HTTPException) as exc:
        await exit_service.compute_exit_gratuity(
            session, hr_user, employee_id=employee.id,
            exit_date=date(2026, 4, 1), exit_reason=ExitReason.RESIGNATION,
        )
    assert "BASIC_PLUS_DA_NONEXISTENT" in exc.value.detail


# ═══════════════════════════════════════════════════════════════════════════
# Encashment reads the ledger
# ═══════════════════════════════════════════════════════════════════════════


async def _leave_balance(session, tenant, employee, code="EL", available="20"):
    leave_type = await session.scalar(
        select(LeaveType).where(LeaveType.tenant_id == tenant.id, LeaveType.code == code)
    )
    if leave_type is None:
        leave_type = LeaveType(
            tenant_id=tenant.id, name="Earned Leave", code=code, is_paid=True
        )
        session.add(leave_type)
        await session.flush()

    session.add(
        LeaveBalance(
            tenant_id=tenant.id, employee_id=employee.id, leave_type_id=leave_type.id,
            year=2026, opening_balance=Decimal(available), accrued=Decimal(0),
            used=Decimal(0), encashed=Decimal(0), carried_forward=Decimal(0),
        )
    )
    await session.commit()
    return leave_type


async def _encashment_policy(session, tenant, **overrides):
    base = {
        "leave_type_code": "EL",
        "encashable_on_exit": True,
        "encashable_in_service": True,
        "max_days_per_year": Decimal(15),
        "cap_days": None,
        "min_balance_after_encashment": Decimal(0),
        "formula_basis": ["BASIC", "DA"],
        "days_per_month_divisor": 26,
        "exempt_on_retirement": False,
        "retirement_exemption_ceiling": None,
        "in_service_fully_taxable": True,
        "active": True,
    }
    base.update(overrides)
    policy = EncashmentPolicy(tenant_id=tenant.id, **base)
    session.add(policy)
    await session.commit()
    return policy


async def test_encashment_reads_the_balance_from_the_leave_ledger(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """Read, never recomputed. Two computations is two places to disagree."""
    await _config(session, tenant)
    await _salary_for(session, tenant, hr_user, employee)
    await _leave_balance(session, tenant, employee, available="8")
    await _encashment_policy(session, tenant)

    record = await exit_service.request_encashment(
        session, hr_user, employee_id=employee.id, leave_type_code="EL",
        days=Decimal(20), financial_year="2026-27",
    )

    assert Decimal(str(record.days_approved)) == Decimal(8)
    assert record.working["limiting_factor"] == "available_balance"


async def test_used_and_encashed_days_reduce_the_available_balance(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _config(session, tenant)
    await _salary_for(session, tenant, hr_user, employee)

    leave_type = await _leave_balance(session, tenant, employee, available="30")
    balance = await session.scalar(
        select(LeaveBalance).where(LeaveBalance.leave_type_id == leave_type.id)
    )
    balance.used = Decimal(10)
    balance.encashed = Decimal(5)
    await session.commit()

    available = await exit_service.available_leave_balance(
        session, hr_user, employee.id, "EL", 2026
    )
    assert available == Decimal(15)


async def test_an_earlier_approved_encashment_counts_against_the_annual_cap(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _config(session, tenant)
    await _salary_for(session, tenant, hr_user, employee)
    await _leave_balance(session, tenant, employee, available="60")
    await _encashment_policy(session, tenant, max_days_per_year=Decimal(15))

    first = await exit_service.request_encashment(
        session, hr_user, employee_id=employee.id, leave_type_code="EL",
        days=Decimal(10), financial_year="2026-27",
    )
    await exit_service.decide_encashment(session, hr_user, first.id, approve=True)

    second = await exit_service.request_encashment(
        session, hr_user, employee_id=employee.id, leave_type_code="EL",
        days=Decimal(10), financial_year="2026-27",
    )
    assert Decimal(str(second.days_approved)) == Decimal(5)
    assert second.working["limiting_factor"] == "max_days_per_year"


async def test_an_exit_encashment_is_approved_without_a_separate_step(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """Full and Final computes it; asking someone to approve it again is noise."""
    await _config(session, tenant)
    await _salary_for(session, tenant, hr_user, employee)
    await _leave_balance(session, tenant, employee, available="20")
    await _encashment_policy(session, tenant)

    record = await exit_service.request_encashment(
        session, hr_user, employee_id=employee.id, leave_type_code="EL",
        days=Decimal(10), financial_year="2026-27", is_exit=True,
    )
    assert record.status is EncashmentStatus.APPROVED
    assert record.is_exit_encashment is True


async def test_the_whole_balance_is_encashed_at_exit_and_offered_to_the_settlement(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """At exit the accumulated balance is paid, up to the per-event cap.

    The in-service annual limit does not apply, and the figure reaches the full
    and final settlement as a prefill instead of being worked out by hand.
    """
    await _config(session, tenant)
    await _salary_for(session, tenant, hr_user, employee)
    await _leave_balance(session, tenant, employee, available="25")
    await _encashment_policy(session, tenant, max_days_per_year=Decimal(10),
                             cap_days=Decimal(45))
    record = ExitRecord(
        tenant_id=tenant.id, employee_id=employee.id, exit_type=ExitType.RESIGNATION,
        status=ExitStatus.SETTLEMENT_PENDING, resignation_date=date(2026, 7, 31),
        approved_last_working_day=date(2026, 8, 31), notice_period_days=30,
    )
    session.add(record)
    await session.commit()

    sources = await exit_operations.settlement_sources(session, hr_user, record.id)
    assert [(r["leave_type_code"], Decimal(r["days"])) for r in sources["encashable_leave"]] == [
        ("EL", Decimal(25))
    ]

    created = await exit_service.compute_exit_encashments(session, hr_user, exit_id=record.id)
    assert [Decimal(str(row.days_approved)) for row in created] == [Decimal(25)]
    assert created[0].is_exit_encashment and created[0].status is EncashmentStatus.APPROVED
    assert created[0].financial_year == "2026-27"

    sources = await exit_operations.settlement_sources(session, hr_user, record.id)
    assert sources["encashable_leave"] == []
    assert "leave_encashment" in {s["field"] for s in sources["suggestions"]}

    with pytest.raises(HTTPException) as again:
        await exit_service.compute_exit_encashments(session, hr_user, exit_id=record.id)
    assert again.value.status_code == 409


async def test_an_encashment_decision_is_audited(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _config(session, tenant)
    await _salary_for(session, tenant, hr_user, employee)
    await _leave_balance(session, tenant, employee, available="20")
    await _encashment_policy(session, tenant)

    record = await exit_service.request_encashment(
        session, hr_user, employee_id=employee.id, leave_type_code="EL",
        days=Decimal(5), financial_year="2026-27",
    )
    await exit_service.decide_encashment(
        session, hr_user, record.id, approve=True, note="Approved by manager."
    )

    entry = await session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "encashment_request", AuditLog.entity_id == record.id
        ).order_by(AuditLog.created_at.desc())
    )
    assert entry is not None
    assert entry.changes["note"] == "Approved by manager."


async def test_an_unknown_leave_type_is_a_clear_404(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _config(session, tenant)
    await _salary_for(session, tenant, hr_user, employee)

    with pytest.raises(HTTPException) as exc:
        await exit_service.request_encashment(
            session, hr_user, employee_id=employee.id, leave_type_code="NOPE",
            days=Decimal(5), financial_year="2026-27",
        )
    assert exc.value.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# Isolation
# ═══════════════════════════════════════════════════════════════════════════


async def test_one_tenant_cannot_read_anothers_accruals(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    other_hr_user: User,
    employee: Employee,
) -> None:
    await _config(session, tenant)
    employee.date_of_joining = date(2015, 4, 1)
    await session.commit()
    await _salary_for(session, tenant, hr_user, employee)
    await exit_service.accrue_gratuity(session, hr_user, as_of=AS_OF)

    report = await exit_service.liability_report(session, other_hr_user, AS_OF)
    assert report["employee_count"] == 0
    assert Decimal(report["total_accrued_liability"]) == Decimal(0)
