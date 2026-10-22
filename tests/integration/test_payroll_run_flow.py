"""A payroll run end to end, against a real database.

Salary assigned → rule sets signed off by someone other than their author →
attendance locked → compute (by the maker) → variances acknowledged → approve
(by a different person) → inputs consumed exactly once → publish → the employee
reads their own payslip, and nobody else's.
"""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import AttendancePeriodLock
from app.models.audit import AuditLog
from app.models.employee import Employee, EmploymentStatus
from app.models.loans import (
    ClaimStatus,
    InterestType,
    LoanAccount,
    LoanRepayment,
    LoanScheme,
    LoanStatus,
    LoanType,
    ReimbursementClaim,
    ReimbursementComponent,
)
from app.models.org import LegalEntity, Location
from app.models.payroll import (
    BonusComputation,
    BonusStatus,
    CalculationType,
    ComponentType,
    PayrollRuleSet,
    PayrollRunStatus,
    PayrollRunType,
    Payslip,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
)
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.payroll import EmployeeSalaryCreate
from app.schemas.payroll_run import RuleSetCreate
from app.services.payroll import payslips as payslip_service
from app.services.payroll import rule_sets as rule_set_service
from app.services.payroll import run as run_service
from app.services.payroll import service as salary_service
from app.services.payroll import statutory_service
from app.services.tax import service as tax_service
from tests.integration.conftest import requires_db

pytestmark = requires_db

# Test fixtures, deliberately synthetic — not an authority on Indian rates.
PF_RULES = {
    "wage_ceiling": 15000,
    "employee_rate": 12,
    "employer_rate": 12,
    "eps_wage_ceiling": 15000,
    "edli_wage_ceiling": 15000,
    "eps_rate": 8.33,
}
TDS_RULES = {
    "regimes": {
        "new": {
            "slabs": [{"up_to": None, "rate": 10}],
            "standard_deduction": 0,
            "deductions_allowed": [],
        }
    },
    "cess_percent": 0,
    "financial_year_start_month": 4,
}


async def _person(
    session: AsyncSession,
    tenant: Tenant,
    entity: LegalEntity,
    location: Location,
    *,
    code: str,
    role: UserRole,
) -> tuple[Employee, User]:
    employee = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code.title(),
        last_name="Test",
        work_email=f"{code.lower()}@acme.test",
        legal_entity_id=entity.id,
        location_id=location.id,
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2020, 1, 1),
    )
    session.add(employee)
    await session.flush()
    user = User(
        tenant_id=tenant.id,
        email=f"{code.lower()}@acme.test",
        password_hash="not-a-real-hash",
        full_name=f"{code.title()} Test",
        role=role,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(user)
    await session.commit()
    await session.refresh(employee)
    await session.refresh(user)
    return employee, user


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
        display_order=1,
    )
    special = SalaryComponent(
        tenant_id=tenant.id,
        code="SPECIAL",
        name="Special allowance",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.BALANCE,
        display_order=9,
    )
    session.add_all([basic, special])
    await session.flush()
    structure = SalaryStructure(
        tenant_id=tenant.id,
        name="Standard",
        effective_from=date(2026, 4, 1),
        applicable_grades=[],
        applicable_locations=[],
    )
    for index, item in enumerate((basic, special)):
        structure.components.append(
            SalaryStructureComponent(
                tenant_id=tenant.id, component_id=item.id, component=item, display_order=index
            )
        )
    session.add(structure)
    await session.commit()
    await session.refresh(structure)
    return structure


def _lock(tenant: Tenant, user: User, year: int, month: int) -> AttendancePeriodLock:
    return AttendancePeriodLock(
        tenant_id=tenant.id,
        period_start=date(year, month, 1),
        period_end=date(year, month, calendar.monthrange(year, month)[1]),
        locked_by_user_id=user.id,
        locked_at=datetime.now(UTC),
    )


@pytest.fixture
async def org(session: AsyncSession, tenant: Tenant, legal_entity: LegalEntity, location: Location):
    _, maker = await _person(
        session, tenant, legal_entity, location, code="MAKER", role=UserRole.ADMIN
    )
    _, checker = await _person(
        session, tenant, legal_entity, location, code="CHECKER", role=UserRole.SUB_ADMIN
    )
    meera_emp, meera = await _person(
        session, tenant, legal_entity, location, code="MEERA", role=UserRole.EMPLOYEE
    )
    ravi_emp, ravi = await _person(
        session, tenant, legal_entity, location, code="RAVI", role=UserRole.EMPLOYEE
    )
    structure = await _structure(session, tenant)
    for person in (maker, checker):
        await salary_service.assign_salary(
            session,
            maker,
            person.employee_id,
            EmployeeSalaryCreate(
                structure_id=structure.id,
                ctc_annual=Decimal("600000"),
                effective_from=date(2026, 4, 1),
            ),
        )
    for person in (meera_emp, ravi_emp):
        await salary_service.assign_salary(
            session,
            maker,
            person.id,
            EmployeeSalaryCreate(
                structure_id=structure.id,
                ctc_annual=Decimal("1200000"),
                effective_from=date(2026, 4, 1),
            ),
        )
    return {
        "tenant": tenant,
        "entity": legal_entity,
        "maker": maker,
        "checker": checker,
        "meera": meera,
        "meera_emp": meera_emp,
        "ravi": ravi,
        "ravi_emp": ravi_emp,
        "structure": structure,
    }


async def _approved_rules(session: AsyncSession, org, *, tds: bool = True) -> None:
    """Entered by the maker, attested by the checker — through the service."""
    kinds = [("pf", PF_RULES)] + ([("tds", TDS_RULES)] if tds else [])
    for rule_type, rules in kinds:
        created = await rule_set_service.create_rule_set(
            session,
            org["maker"],
            RuleSetCreate(
                rule_type=rule_type,
                jurisdiction="IN",
                effective_from=date(2026, 4, 1),
                rules=rules,
                source_reference="Test fixture, not a statutory source",
            ),
        )
        await rule_set_service.approve_rule_set(session, org["checker"], created.id, attest=True)


async def _open_run(session: AsyncSession, org, month: int):
    return await run_service.create_run(
        session,
        org["maker"],
        legal_entity_id=org["entity"].id,
        period_year=2026,
        period_month=month,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Rule-set sign-off
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_author_of_a_rule_set_cannot_approve_it(session: AsyncSession, org) -> None:
    created = await rule_set_service.create_rule_set(
        session,
        org["maker"],
        RuleSetCreate(
            rule_type="pf",
            jurisdiction="IN",
            effective_from=date(2026, 4, 1),
            rules=PF_RULES,
            source_reference="fixture",
        ),
    )
    assert created.is_approved is False
    assert created.caller_may_approve is False

    with pytest.raises(HTTPException) as exc:
        await rule_set_service.approve_rule_set(session, org["maker"], created.id, attest=True)
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        await rule_set_service.approve_rule_set(session, org["checker"], created.id, attest=False)
    assert exc.value.status_code == 422

    approved = await rule_set_service.approve_rule_set(
        session, org["checker"], created.id, attest=True
    )
    assert approved.is_approved and approved.approved_by_user_id == org["checker"].id


async def test_a_rule_set_missing_an_engine_key_is_refused(session: AsyncSession, org) -> None:
    with pytest.raises(HTTPException) as exc:
        await rule_set_service.create_rule_set(
            session,
            org["maker"],
            RuleSetCreate(
                rule_type="pf",
                jurisdiction="IN",
                effective_from=date(2026, 4, 1),
                rules={"wage_ceiling": 1},
                source_reference="fixture",
            ),
        )
    assert exc.value.status_code == 422
    assert "employee_rate" in exc.value.detail


async def test_a_slab_table_payroll_would_misread_is_refused_at_entry(
    session: AsyncSession, org
) -> None:
    with pytest.raises(HTTPException) as exc:
        await rule_set_service.create_rule_set(
            session,
            org["maker"],
            RuleSetCreate(
                rule_type="professional_tax",
                jurisdiction="KA",
                effective_from=date(2026, 4, 1),
                rules={"slabs": [{"max": 24999, "amount": 0}, {"amount": 200}]},
                source_reference="fixture",
            ),
        )
    assert exc.value.status_code == 422
    assert "slab 1 has no up_to" in exc.value.detail
    assert (await session.scalar(select(func.count()).select_from(PayrollRuleSet))) == 0


async def test_approving_a_new_version_closes_the_old_one_and_leaves_its_rules(
    session: AsyncSession, org
) -> None:
    await _approved_rules(session, org, tds=False)
    newer = await rule_set_service.create_rule_set(
        session,
        org["maker"],
        RuleSetCreate(
            rule_type="pf",
            jurisdiction="IN",
            effective_from=date(2026, 10, 1),
            rules={**PF_RULES, "wage_ceiling": 99999},
            source_reference="fixture v2",
        ),
    )
    assert newer.version == 2
    await rule_set_service.approve_rule_set(session, org["checker"], newer.id, attest=True)

    rows = (
        await session.scalars(
            select(PayrollRuleSet)
            .where(PayrollRuleSet.rule_type == "pf")
            .order_by(PayrollRuleSet.effective_from)
        )
    ).all()
    assert rows[0].effective_to == date(2026, 9, 30)
    assert rows[0].rules == PF_RULES, "an older version's rules never change"
    assert rows[1].effective_to is None


# ═══════════════════════════════════════════════════════════════════════════
# The run, end to end
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_run_end_to_end(session: AsyncSession, org, other_hr_user: User) -> None:
    await _approved_rules(session, org)
    run = await _open_run(session, org, 4)

    # Attendance not locked: refused rather than assuming zero loss of pay.
    with pytest.raises(HTTPException) as exc:
        await run_service.compute_run(session, org["maker"], run.id)
    assert exc.value.status_code == 409
    assert "not been finalised" in exc.value.detail

    session.add(_lock(org["tenant"], org["maker"], 2026, 4))
    await session.commit()

    computed = await run_service.compute_run(session, org["maker"], run.id)
    assert computed.status is PayrollRunStatus.PENDING_REVIEW
    assert computed.employee_count == 4
    assert computed.review_flags == []
    assert computed.computed_by_user_id == org["maker"].id

    meera_slip = await session.scalar(
        select(Payslip).where(Payslip.run_id == run.id, Payslip.employee_id == org["meera_emp"].id)
    )
    # 1,200,000 projected at the fixture's flat 10%, over twelve months.
    assert Decimal(str(meera_slip.tds)) == Decimal("10000.00")
    assert meera_slip.tds_working["financial_year"] == "2026-27"
    assert Decimal(str(meera_slip.net_pay)) == Decimal("88200.00")

    # Maker-checker.
    with pytest.raises(HTTPException) as exc:
        await run_service.approve_run(session, org["maker"], run.id)
    assert exc.value.status_code == 403

    approved = await run_service.approve_run(session, org["checker"], run.id)
    assert approved.status is PayrollRunStatus.APPROVED
    assert approved.approved_by_user_id == org["checker"].id

    # Immutable once approved.
    with pytest.raises(HTTPException) as exc:
        await run_service.compute_run(session, org["maker"], run.id)
    assert exc.value.status_code == 409

    # Nothing is visible to the employee before publication.
    assert await payslip_service.my_payslips(session, org["meera"]) == []

    published = await run_service.publish_run(session, org["checker"], run.id)
    assert published.published_at is not None

    mine = await payslip_service.my_payslips(session, org["meera"])
    assert [slip.id for slip in mine] == [meera_slip.id]
    detail = await payslip_service.my_payslip(session, org["meera"], meera_slip.id)
    assert detail.net_pay == Decimal("88200.00")
    assert {rs.rule_type for rs in detail.rule_sets} == {"pf", "tds"}
    assert detail.tds_working is not None

    # Another employee cannot read it: a 404, not a 403.
    with pytest.raises(HTTPException) as exc:
        await payslip_service.my_payslip(session, org["ravi"], meera_slip.id)
    assert exc.value.status_code == 404
    # Nor can another tenant's HR, by run or by payslip.
    with pytest.raises(HTTPException) as exc:
        await payslip_service.hr_payslip(session, other_hr_user, run.id, meera_slip.id)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await run_service.get_run(session, other_hr_user, run.id)
    assert exc.value.status_code == 404

    history = await payslip_service.my_salary_history(session, org["meera"])
    assert len(history) == 1 and history[0].employee_id == org["meera_emp"].id

    paid = await run_service.mark_paid(session, org["checker"], run.id)
    assert paid.status is PayrollRunStatus.PAID

    steps = [
        entry.changes.get("step")
        for entry in (
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.entity_type == "payroll_run", AuditLog.entity_id == run.id)
                .order_by(AuditLog.created_at)
            )
        ).all()
    ]
    assert [s for s in steps if s] == ["compute", "approve", "publish", "paid"]


async def test_a_leaver_whose_exit_was_processed_is_still_paid_to_their_last_day(
    session: AsyncSession, org
) -> None:
    """Exit processed on the last day, payroll computed after: the days are owed.

    Status alone would leave them out of their final month's pay entirely.
    """
    await _approved_rules(session, org, tds=False)
    session.add(_lock(org["tenant"], org["maker"], 2026, 4))
    ravi = org["ravi_emp"]
    ravi.status = EmploymentStatus.EXITED
    ravi.date_of_exit = date(2026, 4, 15)
    await session.commit()

    run = await _open_run(session, org, 4)
    computed = await run_service.compute_run(session, org["maker"], run.id)
    assert computed.employee_count == 4

    slip = await session.scalar(
        select(Payslip).where(Payslip.run_id == run.id, Payslip.employee_id == ravi.id)
    )
    assert slip is not None
    assert Decimal(str(slip.not_employed_days)) == Decimal(15)
    # 1,200,000 a year is 100,000 a month; 15 of April's 30 days.
    assert Decimal(str(slip.gross_earnings)) == Decimal("50000.00")

    # Someone who left before the period is not in it at all.
    may = await _open_run(session, org, 5)
    session.add(_lock(org["tenant"], org["maker"], 2026, 5))
    await session.commit()
    computed = await run_service.compute_run(session, org["maker"], may.id)
    assert computed.employee_count == 3


async def test_variances_must_each_be_acknowledged_and_inputs_paid_once(
    session: AsyncSession, org
) -> None:
    await _approved_rules(session, org)
    session.add(_lock(org["tenant"], org["maker"], 2026, 4))
    session.add(_lock(org["tenant"], org["maker"], 2026, 5))
    await session.commit()

    april = await _open_run(session, org, 4)
    await run_service.compute_run(session, org["maker"], april.id)
    await run_service.approve_run(session, org["checker"], april.id)

    # May: a raise for Meera (a variance), a reimbursement and a loan for Ravi.
    await salary_service.assign_salary(
        session,
        org["maker"],
        org["meera_emp"].id,
        EmployeeSalaryCreate(
            structure_id=org["structure"].id,
            ctc_annual=Decimal("1800000"),
            effective_from=date(2026, 5, 1),
            revision_reason="Promotion",
        ),
    )
    tenant_id = org["tenant"].id
    session.add(
        ReimbursementComponent(
            tenant_id=tenant_id, code="FUEL", name="Fuel", annual_entitlement=Decimal("24000")
        )
    )
    claim = ReimbursementClaim(
        tenant_id=tenant_id,
        employee_id=org["ravi_emp"].id,
        component_code="FUEL",
        financial_year="2026-27",
        amount=Decimal("3000"),
        accepted_amount=Decimal("2500"),
        status=ClaimStatus.PARTIAL,
    )
    scheme = LoanScheme(
        tenant_id=tenant_id,
        name="Salary advance",
        loan_type=LoanType.SALARY_ADVANCE,
        interest_type=InterestType.INTEREST_FREE,
        max_tenure_months=12,
        minimum_net_pay=Decimal("10000"),
    )
    session.add_all([claim, scheme])
    await session.flush()
    loan = LoanAccount(
        tenant_id=tenant_id,
        employee_id=org["ravi_emp"].id,
        scheme_id=scheme.id,
        status=LoanStatus.ACTIVE,
        principal=Decimal("12000"),
        interest_type=InterestType.INTEREST_FREE,
        tenure_months=12,
        emi_amount=Decimal("1000"),
        outstanding_principal=Decimal("12000"),
    )
    session.add(loan)
    await session.commit()

    may = await _open_run(session, org, 5)
    computed = await run_service.compute_run(session, org["maker"], may.id)
    assert [flag["employee_code"] for flag in computed.variance_flags] == ["MEERA"]

    ravi_slip = await session.scalar(
        select(Payslip).where(Payslip.run_id == may.id, Payslip.employee_id == org["ravi_emp"].id)
    )
    codes = {line["code"]: line for line in ravi_slip.input_lines}
    assert codes["REIMB_FUEL"]["amount"] == "2500.00"
    assert codes["LOAN_EMI"]["amount"] == "1000.00"
    # 98,200 before TDS; TDS 10,000; +2,500 reimbursement; -1,000 EMI.
    assert Decimal(str(ravi_slip.net_pay)) == Decimal("89700.00")
    assert Decimal(str(ravi_slip.gross_earnings)) == Decimal("100000.00")
    # Nothing is consumed at compute.
    await session.refresh(claim)
    assert claim.paid_in_run_id is None

    with pytest.raises(HTTPException) as exc:
        await run_service.approve_run(session, org["checker"], may.id)
    assert exc.value.status_code == 409
    assert "acknowledge" in exc.value.detail.lower()

    acknowledged = await run_service.acknowledge_variance(
        session, org["checker"], may.id, org["meera_emp"].id, note="Promotion effective May"
    )
    assert acknowledged.variance_flags[0]["acknowledged"] is True
    assert acknowledged.variance_flags[0]["note"] == "Promotion effective May"

    # Somebody else paid the claim between compute and approval: refuse, and
    # leave everything else unmarked. The refusal rolls back, which expires every
    # loaded object, so the loan id is read before it rather than lazily after.
    loan_id = loan.id
    claim.paid_in_run_id = april.id
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await run_service.approve_run(session, org["checker"], may.id)
    assert exc.value.status_code == 409
    assert "Recompute" in exc.value.detail
    assert (
        await session.scalar(
            select(LoanRepayment.id).where(LoanRepayment.loan_account_id == loan_id)
        )
        is None
    )

    # The refusal rolled the approval back, which expires everything loaded.
    for loaded in (
        org["maker"],
        org["checker"],
        org["meera_emp"],
        org["ravi_emp"],
        claim,
        loan,
        april,
        may,
    ):
        await session.refresh(loaded)

    # Put it back, recompute, re-acknowledge, approve: consumed exactly once.
    claim.paid_in_run_id = None
    await session.commit()
    await run_service.compute_run(session, org["maker"], may.id)
    await run_service.acknowledge_variance(
        session, org["checker"], may.id, org["meera_emp"].id, note="Promotion effective May"
    )
    approved = await run_service.approve_run(session, org["checker"], may.id)
    assert approved.status is PayrollRunStatus.APPROVED

    await session.refresh(claim)
    await session.refresh(loan)
    assert claim.paid_in_run_id == may.id
    repayment = await session.scalar(
        select(LoanRepayment).where(LoanRepayment.loan_account_id == loan.id)
    )
    assert repayment.payroll_run_id == may.id
    assert repayment.period == "2026-05"
    assert Decimal(str(loan.outstanding_principal)) == Decimal("11000.00")

    # May's TDS re-levels on April's actual deduction.
    meera_may = await session.scalar(
        select(Payslip).where(Payslip.run_id == may.id, Payslip.employee_id == org["meera_emp"].id)
    )
    # April 100,000 + May 150,000 + 10 x 150,000 = 1,750,000 → 175,000;
    # 10,000 already deducted, 165,000 over 11 months.
    assert Decimal(str(meera_may.tds)) == Decimal("15000.00")


async def test_no_approved_tds_rule_set_blocks_approval(session: AsyncSession, org) -> None:
    await _approved_rules(session, org, tds=False)
    session.add(_lock(org["tenant"], org["maker"], 2026, 4))
    await session.commit()
    run = await _open_run(session, org, 4)
    computed = await run_service.compute_run(session, org["maker"], run.id)
    assert any("TDS" in flag for flag in computed.review_flags)

    with pytest.raises(HTTPException) as exc:
        await run_service.approve_run(session, org["checker"], run.id)
    assert exc.value.status_code == 409
    assert "cannot be acknowledged away" in exc.value.detail


async def test_an_unapproved_rule_set_fails_the_computation(session: AsyncSession, org) -> None:
    await rule_set_service.create_rule_set(
        session,
        org["maker"],
        RuleSetCreate(
            rule_type="pf",
            jurisdiction="IN",
            effective_from=date(2026, 4, 1),
            rules=PF_RULES,
            source_reference="fixture",
        ),
    )
    session.add(_lock(org["tenant"], org["maker"], 2026, 4))
    await session.commit()
    run = await _open_run(session, org, 4)
    with pytest.raises(HTTPException) as exc:
        await run_service.compute_run(session, org["maker"], run.id)
    assert exc.value.status_code == 422
    assert "approved" in exc.value.detail
    refreshed = await run_service.get_run(session, org["maker"], run.id)
    assert refreshed.status is PayrollRunStatus.FAILED


async def test_a_scheduled_statutory_bonus_rides_the_run(session: AsyncSession, org) -> None:
    await _approved_rules(session, org)
    bonus_rules = PayrollRuleSet(
        tenant_id=org["tenant"].id,
        rule_type="bonus",
        jurisdiction="IN",
        version=1,
        effective_from=date(2025, 4, 1),
        rules={"note": "fixture"},
        approved_at=datetime.now(UTC),
    )
    session.add(bonus_rules)
    await session.flush()
    bonus = BonusComputation(
        tenant_id=org["tenant"].id,
        employee_id=org["ravi_emp"].id,
        financial_year="2025-2026",
        rule_set_id=bonus_rules.id,
        is_eligible=True,
        eligible_wage=Decimal("7000"),
        days_worked=Decimal(300),
        total_working_days=Decimal(300),
        percentage=Decimal("8.33"),
        amount=Decimal("7000.00"),
        status=BonusStatus.APPROVED,
    )
    session.add(bonus)
    session.add(_lock(org["tenant"], org["maker"], 2026, 4))
    await session.commit()

    run = await _open_run(session, org, 4)
    scheduled = await statutory_service.disburse_bonus(
        session,
        org["maker"],
        financial_year="2025-2026",
        paid_on=date(2026, 4, 30),
        run_id=run.id,
    )
    await session.commit()
    assert scheduled[0].status is BonusStatus.APPROVED
    assert scheduled[0].paid_in_run_id == run.id

    await run_service.compute_run(session, org["maker"], run.id)
    slip = await session.scalar(
        select(Payslip).where(Payslip.run_id == run.id, Payslip.employee_id == org["ravi_emp"].id)
    )
    bonus_line = next(line for line in slip.input_lines if line["code"] == "STATUTORY_BONUS")
    assert bonus_line["amount"] == "7000.00"
    # Taxed in full this month: 10% of 7,000 on top of the recurring 10,000.
    assert Decimal(str(slip.tds)) == Decimal("10700.00")

    await run_service.approve_run(session, org["checker"], run.id)
    await session.refresh(bonus)
    assert bonus.status is BonusStatus.PAID
    assert bonus.paid_on is not None


# ═══════════════════════════════════════════════════════════════════════════
# Cancelling a run opened in error
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_computed_run_can_be_cancelled_and_its_month_opened_again(
    session: AsyncSession, org
) -> None:
    await _approved_rules(session, org)
    session.add(_lock(org["tenant"], org["maker"], 2026, 4))
    await session.commit()
    run = await _open_run(session, org, 4)
    computed = await run_service.compute_run(session, org["maker"], run.id)
    assert computed.employee_count == 4

    with pytest.raises(HTTPException) as exc:
        await run_service.cancel_run(session, org["maker"], run.id, reason="   ")
    assert exc.value.status_code == 422

    cancelled = await run_service.cancel_run(
        session, org["maker"], run.id, reason="Opened for the wrong legal entity"
    )
    assert cancelled.status is PayrollRunStatus.CANCELLED
    assert cancelled.cancel_reason == "Opened for the wrong legal entity"
    assert cancelled.cancelled_by_user_id == org["maker"].id
    assert cancelled.employee_count == 0
    left = (await session.scalars(select(Payslip).where(Payslip.run_id == run.id))).all()
    assert left == [], "a cancelled run keeps no payslips"
    audit_rows = (await session.scalars(select(AuditLog).where(AuditLog.entity_id == run.id))).all()
    assert any((row.changes or {}).get("step") == "cancel" for row in audit_rows)

    # The month is free again, and a cancelled run cannot be revived.
    again = await _open_run(session, org, 4)
    assert again.id != run.id
    with pytest.raises(HTTPException) as exc:
        await run_service.compute_run(session, org["maker"], run.id)
    assert exc.value.status_code == 409


async def test_an_approved_run_cannot_be_cancelled(session: AsyncSession, org) -> None:
    await _approved_rules(session, org)
    session.add(_lock(org["tenant"], org["maker"], 2026, 4))
    await session.commit()
    run = await _open_run(session, org, 4)
    await run_service.compute_run(session, org["maker"], run.id)
    await run_service.approve_run(session, org["checker"], run.id)

    with pytest.raises(HTTPException) as exc:
        await run_service.cancel_run(session, org["maker"], run.id, reason="Too late")
    assert exc.value.status_code == 409
    assert "arrears or off-cycle" in exc.value.detail


async def test_an_off_cycle_run_stands_on_the_live_regular_run_not_a_cancelled_one(
    session: AsyncSession, org
) -> None:
    await _approved_rules(session, org)
    session.add(_lock(org["tenant"], org["maker"], 2026, 4))
    await session.commit()
    wrong = await _open_run(session, org, 4)
    await run_service.cancel_run(session, org["maker"], wrong.id, reason="Opened in error")
    regular = await _open_run(session, org, 4)
    await run_service.compute_run(session, org["maker"], regular.id)
    await run_service.approve_run(session, org["checker"], regular.id)

    off_cycle = await run_service.create_run(
        session,
        org["maker"],
        legal_entity_id=org["entity"].id,
        period_year=2026,
        period_month=4,
        run_type=PayrollRunType.OFF_CYCLE,
        employee_ids=[org["meera_emp"].id],
        reason="Late incentive",
    )
    assert off_cycle.run_type is PayrollRunType.OFF_CYCLE


# ═══════════════════════════════════════════════════════════════════════════
# The employee's projection reads the year as payroll pays it
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_projection_reads_paid_months_and_the_salary_in_force_after_them(
    session: AsyncSession, org
) -> None:
    """Two months paid at 1 L, a revision to 1.5 L from August: 16 L, not 12 L."""
    await _approved_rules(session, org)
    for month in (4, 5):
        session.add(_lock(org["tenant"], org["maker"], 2026, month))
        await session.commit()
        run = await _open_run(session, org, month)
        await run_service.compute_run(session, org["maker"], run.id)
        await run_service.approve_run(session, org["checker"], run.id)
    await salary_service.assign_salary(
        session,
        org["maker"],
        org["meera_emp"].id,
        EmployeeSalaryCreate(
            structure_id=org["structure"].id,
            ctc_annual=Decimal("1800000"),
            effective_from=date(2026, 8, 1),
        ),
    )
    await session.commit()

    projection = await tax_service.live_preview(
        session, org["meera"], org["meera_emp"].id, "2026-27"
    )
    # April and May as paid, June and July at 1 L, August to March at 1.5 L.
    assert projection.gross_annual_income == Decimal("1600000.00")


async def test_a_mid_year_joiner_is_projected_from_the_day_they_joined(
    session: AsyncSession, org
) -> None:
    await _approved_rules(session, org)
    employee = Employee(
        tenant_id=org["tenant"].id,
        employee_code="SANA",
        first_name="Sana",
        last_name="Test",
        work_email="sana@acme.test",
        legal_entity_id=org["entity"].id,
        location_id=org["meera_emp"].location_id,
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2026, 10, 19),
    )
    session.add(employee)
    await session.commit()
    await salary_service.assign_salary(
        session,
        org["maker"],
        employee.id,
        EmployeeSalaryCreate(
            structure_id=org["structure"].id,
            ctc_annual=Decimal("1200000"),
            effective_from=date(2026, 10, 19),
        ),
    )
    await session.commit()

    projection = await tax_service.live_preview(session, org["maker"], employee.id, "2026-27")
    # 13 of October's 31 days, then November to March: about 5.42 L, not 12 L or nothing.
    expected = Decimal("100000") * 13 / 31 + Decimal("100000") * 5
    assert abs(projection.gross_annual_income - expected) < Decimal("1")
