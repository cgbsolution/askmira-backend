"""Slice 09 gates that need a real database.

The pure engines are proved in `tests/payroll/`. These are the things only a
database can settle: that ESI coverage survives across months of a contribution
period, that a mid-period transfer actually reaches payroll as a flag, that
bonus reads a real year of payslips, and that a register renders from stored
records rather than from a recomputation.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import AttendancePeriodLock
from app.models.employee import Employee, EmployeeWorkLocation
from app.models.org import Location
from app.models.payroll import (
    BonusComputation,
    BonusStatus,
    CalculationType,
    ComponentType,
    EmployeePfElection,
    EsiCoveragePeriod,
    PayrollRuleSet,
    PayrollRun,
    PayrollRunStatus,
    PayrollRunType,
    Payslip,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.payroll import EmployeeSalaryCreate
from app.services.payroll import run as run_service
from app.services.payroll import service as salary_service
from app.services.payroll import statutory_service
from app.services.payroll.registers import to_csv
from tests.integration.conftest import requires_db

pytestmark = requires_db


ESI_RULES = {
    "wage_threshold": 21000,
    "employee_rate": "0.75",
    "employer_rate": "3.25",
    "contribution_periods": [
        {"start_month": 4, "end_month": 9, "label": "APR-SEP"},
        {"start_month": 10, "end_month": 3, "label": "OCT-MAR"},
    ],
}

BONUS_RULES = {
    "eligibility_wage_ceiling": 21000,
    "calculation_wage_ceiling": 7000,
    "minimum_percentage": "8.33",
    "maximum_percentage": "20",
    "minimum_service_days": 30,
}


async def _rules(
    session: AsyncSession,
    tenant: Tenant,
    rule_type: str,
    rules: dict,
    *,
    jurisdiction: str = "IN",
    approved: bool = True,
) -> PayrollRuleSet:
    """A rule set as the Compliance SME would supply it. Fixture values."""
    from datetime import UTC, datetime

    rule_set = PayrollRuleSet(
        tenant_id=tenant.id,
        rule_type=rule_type,
        jurisdiction=jurisdiction,
        effective_from=date(2025, 4, 1),
        rules=rules,
        approved_at=datetime.now(UTC) if approved else None,
    )
    session.add(rule_set)
    await session.commit()
    await session.refresh(rule_set)
    return rule_set


async def _run_with_payslip(
    session: AsyncSession,
    tenant: Tenant,
    employee: Employee,
    legal_entity_id,
    *,
    year: int,
    month: int,
    gross: str = "7000.00",
    paid_days: str = "30",
    working_days: str = "30",
    run_type: PayrollRunType = PayrollRunType.REGULAR,
    **payslip_kwargs,
) -> Payslip:
    import calendar

    run = PayrollRun(
        tenant_id=tenant.id,
        legal_entity_id=legal_entity_id,
        period_year=year,
        period_month=month,
        effective_date=date(year, month, calendar.monthrange(year, month)[1]),
        run_type=run_type,
        status=PayrollRunStatus.APPROVED,
    )
    session.add(run)
    await session.flush()

    payslip = Payslip(
        tenant_id=tenant.id,
        run_id=run.id,
        employee_id=employee.id,
        working_days=Decimal(working_days),
        paid_days=Decimal(paid_days),
        lop_days=Decimal(working_days) - Decimal(paid_days),
        gross_earnings=Decimal(gross),
        net_pay=Decimal(gross),
        **payslip_kwargs,
    )
    session.add(payslip)
    await session.commit()
    await session.refresh(payslip)
    return payslip


# ═══════════════════════════════════════════════════════════════════════════
# ESI coverage across a contribution period
# ═══════════════════════════════════════════════════════════════════════════


async def test_coverage_recorded_in_april_governs_july(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate, end to end.

    April records that this employee was contributing. July reads it back and
    keeps deducting even though wages are now above the ceiling — which is the
    whole reason the record exists.
    """
    esi = await _rules(session, tenant, "esi", ESI_RULES)

    await statutory_service.open_esi_coverage_period(
        session,
        hr_user,
        employee_id=employee.id,
        on_date=date(2025, 4, 1),
        esi_wage_base=Decimal("18000.00"),
        esi_rules=esi,
    )
    await session.commit()

    contributing, coverage = await statutory_service.esi_coverage_for(
        session, hr_user, employee_id=employee.id, on_date=date(2025, 7, 1), esi_rules=esi
    )
    assert contributing is True
    assert coverage is not None
    assert coverage.period_label == "2025-APR-SEP"


async def test_a_new_period_does_not_inherit_the_previous_one(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """October opens its own period, and April's answer does not carry over."""
    esi = await _rules(session, tenant, "esi", ESI_RULES)
    await statutory_service.open_esi_coverage_period(
        session,
        hr_user,
        employee_id=employee.id,
        on_date=date(2025, 4, 1),
        esi_wage_base=Decimal("18000.00"),
        esi_rules=esi,
    )
    await session.commit()

    contributing, _ = await statutory_service.esi_coverage_for(
        session, hr_user, employee_id=employee.id, on_date=date(2025, 10, 1), esi_rules=esi
    )
    assert contributing is None, "no record for OCT-MAR yet"


async def test_the_wrapping_period_belongs_to_the_year_it_started_in(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """October 2025 to March 2026 is one period, and February 2026 is in it."""
    esi = await _rules(session, tenant, "esi", ESI_RULES)
    await statutory_service.open_esi_coverage_period(
        session,
        hr_user,
        employee_id=employee.id,
        on_date=date(2025, 10, 1),
        esi_wage_base=Decimal("18000.00"),
        esi_rules=esi,
    )
    await session.commit()

    contributing, coverage = await statutory_service.esi_coverage_for(
        session, hr_user, employee_id=employee.id, on_date=date(2026, 2, 1), esi_rules=esi
    )
    assert contributing is True
    assert coverage is not None
    assert coverage.period_label == "2025-OCT-MAR"


async def test_opening_a_period_twice_keeps_the_first_answer(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """A recompute of April must not rewrite what May through September were
    already decided against."""
    esi = await _rules(session, tenant, "esi", ESI_RULES)
    await statutory_service.open_esi_coverage_period(
        session,
        hr_user,
        employee_id=employee.id,
        on_date=date(2025, 4, 1),
        esi_wage_base=Decimal("18000.00"),
        esi_rules=esi,
    )
    await session.commit()

    # A recompute after a backdated raise pushed April's wage above the ceiling.
    again = await statutory_service.open_esi_coverage_period(
        session,
        hr_user,
        employee_id=employee.id,
        on_date=date(2025, 4, 1),
        esi_wage_base=Decimal("30000.00"),
        esi_rules=esi,
    )
    await session.commit()

    assert again is not None
    assert again.was_contributing_at_start is True
    assert Decimal(str(again.opening_wage)) == Decimal("18000.00")

    count = len(
        (
            await session.scalars(
                select(EsiCoveragePeriod).where(EsiCoveragePeriod.employee_id == employee.id)
            )
        ).all()
    )
    assert count == 1


async def test_coverage_is_only_opened_on_the_first_month_of_a_period(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    esi = await _rules(session, tenant, "esi", ESI_RULES)
    result = await statutory_service.open_esi_coverage_period(
        session,
        hr_user,
        employee_id=employee.id,
        on_date=date(2025, 7, 1),
        esi_wage_base=Decimal("18000.00"),
        esi_rules=esi,
    )
    assert result is None


async def test_a_rule_set_without_periods_records_nothing(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """A tenant whose SME has not supplied contribution periods keeps the plain
    ceiling test, and no coverage rows accumulate."""
    esi = await _rules(session, tenant, "esi", {"wage_threshold": 21000})
    result = await statutory_service.open_esi_coverage_period(
        session,
        hr_user,
        employee_id=employee.id,
        on_date=date(2025, 4, 1),
        esi_wage_base=Decimal("18000.00"),
        esi_rules=esi,
    )
    assert result is None


async def test_coverage_is_tenant_isolated(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    other_hr_user: User,
    employee: Employee,
) -> None:
    esi = await _rules(session, tenant, "esi", ESI_RULES)
    await statutory_service.open_esi_coverage_period(
        session,
        hr_user,
        employee_id=employee.id,
        on_date=date(2025, 4, 1),
        esi_wage_base=Decimal("18000.00"),
        esi_rules=esi,
    )
    await session.commit()

    contributing, _ = await statutory_service.esi_coverage_for(
        session, other_hr_user, employee_id=employee.id, on_date=date(2025, 7, 1), esi_rules=esi
    )
    assert contributing is None, "another tenant must not see this coverage record"


# ═══════════════════════════════════════════════════════════════════════════
# The PF election
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_election_in_force_is_the_one_effective_on_the_date(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """A raise in VPF in October must not move September's payslip."""
    session.add_all(
        [
            EmployeePfElection(
                tenant_id=tenant.id,
                employee_id=employee.id,
                effective_from=date(2025, 4, 1),
                effective_to=date(2025, 9, 30),
                vpf_percentage=Decimal(5),
            ),
            EmployeePfElection(
                tenant_id=tenant.id,
                employee_id=employee.id,
                effective_from=date(2025, 10, 1),
                vpf_percentage=Decimal(15),
            ),
        ]
    )
    await session.commit()

    september = await statutory_service.active_pf_election(
        session, hr_user, employee.id, date(2025, 9, 30)
    )
    october = await statutory_service.active_pf_election(
        session, hr_user, employee.id, date(2025, 10, 1)
    )

    assert september is not None and september.vpf_percent == Decimal(5)
    assert october is not None and october.vpf_percent == Decimal(15)


async def test_no_election_returns_none_rather_than_a_default(
    session: AsyncSession, hr_user: User, employee: Employee
) -> None:
    assert (
        await statutory_service.active_pf_election(session, hr_user, employee.id, date(2025, 9, 30))
        is None
    )


# ═══════════════════════════════════════════════════════════════════════════
# Statutory bonus over a real year of payslips
# ═══════════════════════════════════════════════════════════════════════════


async def test_bonus_is_computed_from_the_years_payslips(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    await _rules(session, tenant, "bonus", BONUS_RULES)
    for month in (4, 5, 6):
        await _run_with_payslip(session, tenant, employee, legal_entity.id, year=2025, month=month)

    rows = await statutory_service.compute_bonus_for_year(
        session, hr_user, financial_year="2025-2026", legal_entity_id=legal_entity.id
    )
    await session.commit()

    assert len(rows) == 1
    bonus = rows[0]
    assert bonus.is_eligible is True
    assert Decimal(str(bonus.eligible_wage)) == Decimal("7000.00")
    assert Decimal(str(bonus.amount)) > 0
    assert bonus.working["notes"] is not None


async def test_unpaid_absence_reduces_the_bonus(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    """Days worked comes from the payslips, so loss of pay flows through."""
    await _rules(session, tenant, "bonus", BONUS_RULES)
    for month in (4, 5, 6):
        await _run_with_payslip(
            session, tenant, employee, legal_entity.id, year=2025, month=month, paid_days="15"
        )

    rows = await statutory_service.compute_bonus_for_year(
        session, hr_user, financial_year="2025-2026", legal_entity_id=legal_entity.id
    )
    await session.commit()

    # 45 days paid in a 365-day year, at the 7,000 calculation ceiling: the bonus
    # is a share of the year's, not three months standing in for twelve.
    full_year = Decimal("7000.00") * 12 * Decimal("8.33") / 100
    assert Decimal(str(rows[0].amount)) == (full_year * 45 / 365).quantize(Decimal("0.01"))
    assert Decimal(str(rows[0].total_working_days)) == 365


async def test_a_high_earner_is_recorded_as_ineligible_with_the_reason(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    """An ineligible employee is recorded, not skipped — "why did I get no
    bonus" needs an answer."""
    await _rules(session, tenant, "bonus", BONUS_RULES)
    await _run_with_payslip(
        session, tenant, employee, legal_entity.id, year=2025, month=4, gross="50000.00"
    )

    rows = await statutory_service.compute_bonus_for_year(
        session, hr_user, financial_year="2025-2026", legal_entity_id=legal_entity.id
    )
    await session.commit()

    assert rows[0].is_eligible is False
    assert "eligibility ceiling" in rows[0].ineligibility_reason


async def test_recomputing_overwrites_a_draft(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    await _rules(session, tenant, "bonus", BONUS_RULES)
    await _run_with_payslip(session, tenant, employee, legal_entity.id, year=2025, month=4)

    first = await statutory_service.compute_bonus_for_year(
        session, hr_user, financial_year="2025-2026", legal_entity_id=legal_entity.id
    )
    await session.commit()
    original = Decimal(str(first[0].amount))

    second = await statutory_service.compute_bonus_for_year(
        session,
        hr_user,
        financial_year="2025-2026",
        legal_entity_id=legal_entity.id,
        percentage=Decimal(20),
    )
    await session.commit()

    assert Decimal(str(second[0].amount)) > original
    rows = (
        await session.scalars(
            select(BonusComputation).where(BonusComputation.employee_id == employee.id)
        )
    ).all()
    assert len(rows) == 1, "the unique constraint keeps one record per employee per year"


async def test_a_paid_bonus_is_never_recomputed(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    """A bonus already handed over is a fact. A later configuration change does
    not retroactively alter it."""
    await _rules(session, tenant, "bonus", BONUS_RULES)
    await _run_with_payslip(session, tenant, employee, legal_entity.id, year=2025, month=4)

    rows = await statutory_service.compute_bonus_for_year(
        session, hr_user, financial_year="2025-2026", legal_entity_id=legal_entity.id
    )
    await session.commit()
    rows[0].status = BonusStatus.PAID
    paid_amount = Decimal(str(rows[0].amount))
    await session.commit()

    again = await statutory_service.compute_bonus_for_year(
        session,
        hr_user,
        financial_year="2025-2026",
        legal_entity_id=legal_entity.id,
        percentage=Decimal(20),
    )
    await session.commit()

    assert Decimal(str(again[0].amount)) == paid_amount
    assert again[0].status is BonusStatus.PAID


async def test_an_unapproved_bonus_rule_set_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    await _rules(session, tenant, "bonus", BONUS_RULES, approved=False)
    await _run_with_payslip(session, tenant, employee, legal_entity.id, year=2025, month=4)

    with pytest.raises(HTTPException) as exc:
        await statutory_service.compute_bonus_for_year(
            session, hr_user, financial_year="2025-2026", legal_entity_id=legal_entity.id
        )
    assert "not been approved" in exc.value.detail


async def test_a_missing_bonus_rule_set_names_what_is_needed(
    session: AsyncSession, hr_user: User, legal_entity
) -> None:
    with pytest.raises(HTTPException) as exc:
        await statutory_service.compute_bonus_for_year(
            session, hr_user, financial_year="2025-2026", legal_entity_id=legal_entity.id
        )
    assert "Compliance SME supplies and approves it" in exc.value.detail


async def test_a_malformed_financial_year_is_refused(
    session: AsyncSession, hr_user: User, legal_entity
) -> None:
    with pytest.raises(HTTPException, match="consecutive years"):
        await statutory_service.compute_bonus_for_year(
            session, hr_user, financial_year="2025-2027", legal_entity_id=legal_entity.id
        )


# ═══════════════════════════════════════════════════════════════════════════
# Multi-state Professional Tax, from a real transfer
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_recorded_transfer_makes_a_mid_period_state_change_visible(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    """The checklist gate at the data layer.

    Without the posting series, payroll reads the current location and applies
    Maharashtra to the whole of June. With it, the change is visible.
    """
    from app.services.payroll.statutory_service import work_state_for_period

    pune = Location(
        tenant_id=tenant.id,
        legal_entity_id=legal_entity.id,
        name="Pune",
        code="PNQ",
        city="Pune",
        state_code="MH",
    )
    session.add(pune)
    await session.flush()

    session.add_all(
        [
            EmployeeWorkLocation(
                tenant_id=tenant.id,
                employee_id=employee.id,
                location_id=employee.location_id,
                effective_from=date(2024, 1, 1),
                effective_to=date(2025, 6, 14),
            ),
            EmployeeWorkLocation(
                tenant_id=tenant.id,
                employee_id=employee.id,
                location_id=pune.id,
                effective_from=date(2025, 6, 15),
            ),
        ]
    )
    await session.commit()

    resolution = await work_state_for_period(
        session, hr_user, employee, date(2025, 6, 1), date(2025, 6, 30)
    )
    assert resolution.needs_review is True
    assert resolution.state_code is None
    assert "KA" in resolution.reason and "MH" in resolution.reason


async def test_an_employee_with_no_transfers_resolves_from_their_location(
    session: AsyncSession, hr_user: User, employee: Employee
) -> None:
    from app.services.payroll.statutory_service import work_state_for_period

    resolution = await work_state_for_period(
        session, hr_user, employee, date(2025, 6, 1), date(2025, 6, 30)
    )
    assert resolution.state_code == "KA"
    assert resolution.needs_review is False


async def test_an_employee_with_no_location_at_all_is_flagged(
    session: AsyncSession, hr_user: User, employee: Employee
) -> None:
    from app.services.payroll.statutory_service import work_state_for_period

    employee.location_id = None
    await session.commit()

    resolution = await work_state_for_period(
        session, hr_user, employee, date(2025, 6, 1), date(2025, 6, 30)
    )
    assert resolution.needs_review is True
    assert "no work location" in resolution.reason


# ═══════════════════════════════════════════════════════════════════════════
# Registers, rendered from stored records
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_lwf_register_lists_only_the_deduction_month(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    """November has no LWF row because nothing was deducted, and December does."""
    november = await _run_with_payslip(
        session, tenant, employee, legal_entity.id, year=2025, month=11
    )
    december = await _run_with_payslip(
        session,
        tenant,
        employee,
        legal_entity.id,
        year=2025,
        month=12,
        lwf_employee=Decimal(20),
        lwf_employer=Decimal(40),
    )

    empty = await statutory_service.build_register(
        session, hr_user, code="LWF_REGISTER", run_id=november.run_id
    )
    filled = await statutory_service.build_register(
        session, hr_user, code="LWF_REGISTER", run_id=december.run_id
    )

    assert empty.row_count == 0
    assert filled.row_count == 1
    assert filled.totals["total_contribution"] == Decimal(60)


async def test_the_pf_register_shows_voluntary_separately(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    payslip = await _run_with_payslip(
        session,
        tenant,
        employee,
        legal_entity.id,
        year=2025,
        month=4,
        pf_wage_base=Decimal("40000.00"),
        pf_employee=Decimal("1800.00"),
        pf_employer=Decimal("1800.00"),
        vpf=Decimal("4000.00"),
    )
    rendered = await statutory_service.build_register(
        session, hr_user, code="PF_REGISTER", run_id=payslip.run_id
    )

    assert rendered.totals["employee_contribution"] == Decimal("1800.00")
    assert rendered.totals["voluntary_contribution"] == Decimal("4000.00")
    assert "Voluntary contribution" in rendered.headings


async def test_the_register_reads_the_payslip_rather_than_recomputing(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    """A register that recomputes can disagree with what was actually paid.

    The payslip here carries a figure no current configuration would produce;
    the register still reports it, because that is what was paid.
    """
    payslip = await _run_with_payslip(
        session,
        tenant,
        employee,
        legal_entity.id,
        year=2025,
        month=12,
        lwf_employee=Decimal("13.00"),
        lwf_employer=Decimal("27.00"),
    )
    rendered = await statutory_service.build_register(
        session, hr_user, code="LWF_REGISTER", run_id=payslip.run_id
    )
    assert rendered.totals["employee_contribution"] == Decimal("13.00")


async def test_the_bonus_register_excludes_the_ineligible_but_names_them(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    """Form C is a register of bonus *due*. Someone with none due has no row,
    but they do not silently vanish."""
    await _rules(session, tenant, "bonus", BONUS_RULES)
    await _run_with_payslip(
        session, tenant, employee, legal_entity.id, year=2025, month=4, gross="50000.00"
    )
    await statutory_service.compute_bonus_for_year(
        session, hr_user, financial_year="2025-2026", legal_entity_id=legal_entity.id
    )
    await session.commit()

    rendered = await statutory_service.build_register(
        session, hr_user, code="BONUS_FORM_C", financial_year="2025-2026"
    )
    assert rendered.row_count == 0
    assert rendered.excluded == [employee.full_name]


async def test_the_bonus_register_csv_carries_the_prescribed_headings(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    await _rules(session, tenant, "bonus", BONUS_RULES)
    await _run_with_payslip(session, tenant, employee, legal_entity.id, year=2025, month=4)
    await statutory_service.compute_bonus_for_year(
        session, hr_user, financial_year="2025-2026", legal_entity_id=legal_entity.id
    )
    await session.commit()

    rendered = await statutory_service.build_register(
        session, hr_user, code="BONUS_FORM_C", financial_year="2025-2026"
    )
    output = to_csv(rendered)

    assert "Payment of Bonus Rules, 1975 - Form C" in output
    assert "Number of days worked in the year" in output
    assert "Accounting year: 2025-2026" in output
    assert employee.full_name in output


async def test_an_unknown_register_lists_what_is_available(
    session: AsyncSession, hr_user: User
) -> None:
    with pytest.raises(HTTPException) as exc:
        await statutory_service.build_register(session, hr_user, code="FORM_XYZ")
    assert "Available:" in exc.value.detail
    assert "BONUS_FORM_C" in exc.value.detail


async def test_registers_are_tenant_isolated(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    other_hr_user: User,
    employee: Employee,
    legal_entity,
) -> None:
    payslip = await _run_with_payslip(
        session,
        tenant,
        employee,
        legal_entity.id,
        year=2025,
        month=12,
        lwf_employee=Decimal(20),
        lwf_employer=Decimal(40),
    )
    with pytest.raises(HTTPException) as exc:
        await statutory_service.build_register(
            session, other_hr_user, code="LWF_REGISTER", run_id=payslip.run_id
        )
    assert exc.value.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# Bonus as its own disbursement
# ═══════════════════════════════════════════════════════════════════════════


async def test_bonus_is_paid_as_its_own_disbursement_without_a_payroll_run(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    """The usual case: a separate payment before a festival, not a payslip line."""
    await _rules(session, tenant, "bonus", BONUS_RULES)
    await _run_with_payslip(session, tenant, employee, legal_entity.id, year=2025, month=4)
    await statutory_service.compute_bonus_for_year(
        session, hr_user, financial_year="2025-2026", legal_entity_id=legal_entity.id
    )
    await session.commit()

    await statutory_service.approve_bonus(session, hr_user, financial_year="2025-2026")
    await session.commit()

    paid = await statutory_service.disburse_bonus(
        session, hr_user, financial_year="2025-2026", paid_on=date(2025, 10, 15)
    )
    await session.commit()

    assert paid[0].status is BonusStatus.PAID
    assert paid[0].paid_on == date(2025, 10, 15)
    assert paid[0].paid_in_run_id is None


async def test_a_bonus_can_ride_a_payroll_run_when_the_employer_chooses(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    await _rules(session, tenant, "bonus", BONUS_RULES)
    payslip = await _run_with_payslip(
        session, tenant, employee, legal_entity.id, year=2025, month=4
    )
    await statutory_service.compute_bonus_for_year(
        session, hr_user, financial_year="2025-2026", legal_entity_id=legal_entity.id
    )
    await statutory_service.approve_bonus(session, hr_user, financial_year="2025-2026")
    await session.commit()

    paid = await statutory_service.disburse_bonus(
        session,
        hr_user,
        financial_year="2025-2026",
        paid_on=date(2025, 10, 15),
        run_id=payslip.run_id,
    )
    await session.commit()
    assert paid[0].paid_in_run_id == payslip.run_id


async def test_an_unapproved_bonus_cannot_be_paid(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    await _rules(session, tenant, "bonus", BONUS_RULES)
    await _run_with_payslip(session, tenant, employee, legal_entity.id, year=2025, month=4)
    await statutory_service.compute_bonus_for_year(
        session, hr_user, financial_year="2025-2026", legal_entity_id=legal_entity.id
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await statutory_service.disburse_bonus(
            session, hr_user, financial_year="2025-2026", paid_on=date(2025, 10, 15)
        )
    assert "Approve the computation first" in exc.value.detail


async def test_the_register_shows_the_payment_date_once_paid(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    """Form C has an "amount actually paid" and a "date on which paid" column,
    and both stay blank until the money moves."""
    await _rules(session, tenant, "bonus", BONUS_RULES)
    await _run_with_payslip(session, tenant, employee, legal_entity.id, year=2025, month=4)
    await statutory_service.compute_bonus_for_year(
        session, hr_user, financial_year="2025-2026", legal_entity_id=legal_entity.id
    )
    await session.commit()

    before = await statutory_service.build_register(
        session, hr_user, code="BONUS_FORM_C", financial_year="2025-2026"
    )
    assert before.totals["amount_paid"] == Decimal(0)

    await statutory_service.approve_bonus(session, hr_user, financial_year="2025-2026")
    await statutory_service.disburse_bonus(
        session, hr_user, financial_year="2025-2026", paid_on=date(2025, 10, 15)
    )
    await session.commit()

    after = await statutory_service.build_register(
        session, hr_user, code="BONUS_FORM_C", financial_year="2025-2026"
    )
    assert after.totals["amount_paid"] == after.totals["bonus_payable"]
    assert "2025-10-15" in to_csv(after)


async def test_the_bonus_wage_basis_is_configurable(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    """ "Salary or wage" is not the same as gross.

    With BASIC and DA named as the basis, an employee on a 20,000 gross made up
    of a 6,000 BASIC has their bonus computed on 6,000 — below the calculation
    ceiling — rather than on the gross.
    """
    await _rules(session, tenant, "bonus", {**BONUS_RULES, "wage_components": ["BASIC", "DA"]})
    await _run_with_payslip(
        session,
        tenant,
        employee,
        legal_entity.id,
        year=2025,
        month=4,
        gross="20000.00",
        line_items=[
            {"code": "BASIC", "type": "earning", "amount": "6000.00"},
            {"code": "HRA", "type": "earning", "amount": "3000.00"},
            {"code": "SPECIAL", "type": "earning", "amount": "11000.00"},
        ],
    )

    rows = await statutory_service.compute_bonus_for_year(
        session, hr_user, financial_year="2025-2026", legal_entity_id=legal_entity.id
    )
    await session.commit()

    assert rows[0].is_eligible is True
    assert Decimal(str(rows[0].eligible_wage)) == Decimal("6000.00")


# ═══════════════════════════════════════════════════════════════════════════
# The whole run pipeline, with the new heads wired in
# ═══════════════════════════════════════════════════════════════════════════
#
# The engines above are proved in isolation and the services against a
# database. This is the part in between: that `compute_run` actually resolves
# a state's LWF rule set, reads the employee's PF election, opens their ESI
# coverage period, and puts the result on the payslip. Wiring is where this
# kind of change goes wrong.


async def _structure(session: AsyncSession, tenant: Tenant) -> SalaryStructure:
    basic = SalaryComponent(
        tenant_id=tenant.id,
        code="BASIC",
        name="Basic",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.PERCENTAGE_OF,
        base_component_code="CTC",
        value=Decimal(40),
        counts_toward_pf=True,
        counts_toward_esi=True,
        display_order=1,
    )
    special = SalaryComponent(
        tenant_id=tenant.id,
        code="SPECIAL",
        name="Special allowance",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.BALANCE,
        counts_toward_esi=True,
        display_order=9,
    )
    session.add_all([basic, special])
    await session.flush()

    structure = SalaryStructure(
        tenant_id=tenant.id,
        name="Standard 2026",
        effective_from=date(2026, 4, 1),
        applicable_grades=[],
        applicable_locations=[],
    )
    for index, component in enumerate((basic, special)):
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


async def _ready_to_run(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    *,
    year: int,
    month: int,
    ctc: str = "1200000",
) -> None:
    """Salary assigned, attendance locked, PF and LWF rules approved."""
    import calendar
    from datetime import UTC, datetime

    structure = await _structure(session, tenant)
    await salary_service.assign_salary(
        session,
        hr_user,
        employee.id,
        EmployeeSalaryCreate(
            structure_id=structure.id,
            ctc_annual=Decimal(ctc),
            effective_from=date(2026, 4, 1),
        ),
    )

    session.add(
        AttendancePeriodLock(
            tenant_id=tenant.id,
            period_start=date(year, month, 1),
            period_end=date(year, month, calendar.monthrange(year, month)[1]),
            locked_by_user_id=hr_user.id,
            locked_at=datetime.now(UTC),
        )
    )
    await _rules(
        session,
        tenant,
        "pf",
        {
            "wage_ceiling": 15000,
            "employee_rate": 12,
            "employer_rate": 12,
            "apply_wage_ceiling": True,
        },
    )
    await _rules(
        session,
        tenant,
        "labour_welfare_fund",
        {
            "applicable": True,
            "deduction_months": [12],
            "employee_amount": 20,
            "employer_amount": 40,
        },
        jurisdiction="KA",
    )
    await session.commit()


async def test_a_december_run_carries_lwf_and_vpf_onto_the_payslip(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    """End to end: the state's LWF rule set is resolved by the employee's
    location, and their VPF election is read and applied."""
    await _ready_to_run(session, tenant, hr_user, employee, year=2026, month=12)
    session.add(
        EmployeePfElection(
            tenant_id=tenant.id,
            employee_id=employee.id,
            effective_from=date(2026, 4, 1),
            vpf_fixed_amount=Decimal("2500.00"),
        )
    )
    await session.commit()

    run = await run_service.create_run(
        session,
        hr_user,
        legal_entity_id=legal_entity.id,
        period_year=2026,
        period_month=12,
    )
    await run_service.compute_run(session, hr_user, run.id)
    await session.commit()

    payslip = await session.scalar(select(Payslip).where(Payslip.run_id == run.id))
    assert payslip is not None
    assert Decimal(str(payslip.lwf_employee)) == Decimal("20.00")
    assert Decimal(str(payslip.lwf_employer)) == Decimal("40.00")
    assert Decimal(str(payslip.vpf)) == Decimal("2500.00")
    assert payslip.review_flags == []

    codes = {line["code"] for line in payslip.line_items}
    assert {"LWF_EMPLOYEE", "LWF_EMPLOYER", "VPF"} <= codes


async def test_a_november_run_carries_no_lwf(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    """The same tenant, the same state, a month the state does not collect."""
    await _ready_to_run(session, tenant, hr_user, employee, year=2026, month=11)

    run = await run_service.create_run(
        session,
        hr_user,
        legal_entity_id=legal_entity.id,
        period_year=2026,
        period_month=11,
    )
    await run_service.compute_run(session, hr_user, run.id)
    await session.commit()

    payslip = await session.scalar(select(Payslip).where(Payslip.run_id == run.id))
    assert payslip is not None
    assert Decimal(str(payslip.lwf_employee)) == Decimal("0.00")


async def test_a_run_opens_the_esi_contribution_period(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    """April's run records coverage, so July's run has something to read."""
    await _ready_to_run(session, tenant, hr_user, employee, year=2026, month=4, ctc="240000")
    await _rules(session, tenant, "esi", ESI_RULES)

    run = await run_service.create_run(
        session,
        hr_user,
        legal_entity_id=legal_entity.id,
        period_year=2026,
        period_month=4,
    )
    await run_service.compute_run(session, hr_user, run.id)
    await session.commit()

    coverage = await session.scalar(
        select(EsiCoveragePeriod).where(EsiCoveragePeriod.employee_id == employee.id)
    )
    assert coverage is not None
    assert coverage.period_label == "2026-APR-SEP"
    assert coverage.was_contributing_at_start is True


async def test_a_mid_period_transfer_flags_the_run_and_blocks_approval(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    """The checklist gate, end to end and at its consequence.

    The run still computes — everyone else gets paid — but the transferred
    employee's Professional Tax is left undeducted and flagged, and the run
    cannot be signed off until someone settles which state applies.
    """
    await _ready_to_run(session, tenant, hr_user, employee, year=2026, month=12)
    await _rules(
        session,
        tenant,
        "professional_tax",
        {"slabs": [{"up_to": None, "amount": 200}]},
        jurisdiction="KA",
    )

    pune = Location(
        tenant_id=tenant.id,
        legal_entity_id=legal_entity.id,
        name="Pune",
        code="PNQ",
        city="Pune",
        state_code="MH",
    )
    session.add(pune)
    await session.flush()
    session.add_all(
        [
            EmployeeWorkLocation(
                tenant_id=tenant.id,
                employee_id=employee.id,
                location_id=employee.location_id,
                effective_from=date(2026, 1, 1),
                effective_to=date(2026, 12, 14),
            ),
            EmployeeWorkLocation(
                tenant_id=tenant.id,
                employee_id=employee.id,
                location_id=pune.id,
                effective_from=date(2026, 12, 15),
            ),
        ]
    )
    await session.commit()

    run = await run_service.create_run(
        session,
        hr_user,
        legal_entity_id=legal_entity.id,
        period_year=2026,
        period_month=12,
    )
    computed = await run_service.compute_run(session, hr_user, run.id)
    await session.commit()

    assert computed.review_flags, "the transfer should have been flagged"
    assert "will not guess" in computed.review_flags[0]

    payslip = await session.scalar(select(Payslip).where(Payslip.run_id == run.id))
    assert payslip is not None
    assert Decimal(str(payslip.professional_tax)) == Decimal("0.00")

    # And it cannot be acknowledged away the way a variance can.
    with pytest.raises(HTTPException) as exc:
        await run_service.approve_run(session, hr_user, run.id, acknowledge_variances=True)
    assert exc.value.status_code == 409
    assert "cannot be acknowledged away" in exc.value.detail


async def test_a_clean_run_approves(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    """The counterpart: no flags, so nothing blocks sign-off."""
    from app.models.user import UserRole

    await _ready_to_run(session, tenant, hr_user, employee, year=2026, month=12)
    # A run without an income tax rule set is flagged, rightly; a clean run has one.
    await _rules(
        session,
        tenant,
        "tds",
        {
            "regimes": {
                "new": {
                    "slabs": [{"up_to": None, "rate": 10}],
                    "standard_deduction": 0,
                    "deductions_allowed": [],
                }
            },
            "cess_percent": 0,
        },
    )
    # Maker-checker: whoever computes the run is not who approves it.
    checker = User(
        tenant_id=tenant.id,
        email="checker@acme.test",
        password_hash="not-a-real-hash",
        full_name="Payroll Checker",
        role=UserRole.ADMIN,
        is_active=True,
    )
    session.add(checker)
    await session.commit()

    run = await run_service.create_run(
        session,
        hr_user,
        legal_entity_id=legal_entity.id,
        period_year=2026,
        period_month=12,
    )
    await run_service.compute_run(session, hr_user, run.id)
    await session.commit()

    approved = await run_service.approve_run(session, checker, run.id)
    await session.commit()
    assert approved.status is PayrollRunStatus.APPROVED


async def test_bonus_counts_approved_regular_months_once_over_the_whole_year(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, legal_entity
) -> None:
    """A full year earns a full year's bonus; a top-up payslip adds no month, and a
    run nobody approved adds nothing."""
    await _rules(session, tenant, "bonus", BONUS_RULES)
    for year, month in [(2025, m) for m in range(4, 13)] + [(2026, m) for m in (1, 2, 3)]:
        import calendar

        days = calendar.monthrange(year, month)[1]
        await _run_with_payslip(
            session, tenant, employee, legal_entity.id, year=year, month=month,
            paid_days=str(days), working_days=str(days),
        )
    top_up = await _run_with_payslip(
        session, tenant, employee, legal_entity.id, year=2025, month=10, gross="0.00",
        is_supplementary=True, run_type=PayrollRunType.OFF_CYCLE,
    )
    draft = await _run_with_payslip(
        session, tenant, employee, legal_entity.id, year=2025, month=11, gross="3000.00",
        run_type=PayrollRunType.OFF_CYCLE,
    )
    draft_run = await session.get(PayrollRun, draft.run_id)
    draft_run.status = PayrollRunStatus.PENDING_REVIEW
    await session.commit()
    del top_up

    rows = await statutory_service.compute_bonus_for_year(
        session, hr_user, financial_year="2025-2026", legal_entity_id=legal_entity.id
    )
    await session.commit()

    bonus = rows[0]
    assert Decimal(str(bonus.days_worked)) == 365
    assert Decimal(str(bonus.total_working_days)) == 365
    assert Decimal(str(bonus.eligible_wage)) == Decimal("7000.00")
    assert Decimal(str(bonus.amount)) == Decimal("6997.20")  # 7,000 x 12 x 8.33%
