"""Slice 10 gates that need a real database.

The serialisers and validators are proved in `tests/filings/`. These are the
things only a database settles: that a return refuses to be built from an
unapproved run, that challan mapping is enforced end to end, that Q4 pulls
Annexure II from the year's tax snapshots, and that an acknowledgement closes
the loop exactly once.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee
from app.models.filings import FilingRecord, FilingStatus, ReturnType, TdsChallan, TdsChallanMapping
from app.models.org import LegalEntity
from app.models.payroll import PayrollRuleSet, PayrollRun, PayrollRunStatus, Payslip
from app.models.tax import TaxComputationSnapshot, TaxRegime
from app.models.tenant import Tenant
from app.models.user import User
from app.services.filings import service as filing_service
from tests.integration.conftest import requires_db

pytestmark = requires_db


PF_RULES = {
    "wage_ceiling": 15_000,
    "eps_wage_ceiling": 15_000,
    "edli_wage_ceiling": 15_000,
    "employee_rate": 12,
    "employer_rate": 12,
    "eps_rate": "8.33",
}


async def _pf_rules(session: AsyncSession, tenant: Tenant) -> PayrollRuleSet:
    rule_set = PayrollRuleSet(
        tenant_id=tenant.id,
        rule_type="pf",
        jurisdiction="IN",
        effective_from=date(2026, 4, 1),
        rules=PF_RULES,
        approved_at=datetime.now(UTC),
    )
    session.add(rule_set)
    await session.commit()
    return rule_set


async def _run(
    session: AsyncSession,
    tenant: Tenant,
    legal_entity_id,
    *,
    year: int = 2026,
    month: int = 6,
    status: PayrollRunStatus = PayrollRunStatus.APPROVED,
) -> PayrollRun:
    import calendar

    run = PayrollRun(
        tenant_id=tenant.id,
        legal_entity_id=legal_entity_id,
        period_year=year,
        period_month=month,
        effective_date=date(year, month, calendar.monthrange(year, month)[1]),
        status=status,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def _payslip(
    session: AsyncSession, tenant: Tenant, run: PayrollRun, employee: Employee, **overrides
) -> Payslip:
    base = {
        "working_days": Decimal(30),
        "paid_days": Decimal(30),
        "lop_days": Decimal(0),
        "gross_earnings": Decimal("50000.00"),
        "net_pay": Decimal("48200.00"),
        "pf_wage_base": Decimal("20000.00"),
        "pf_employee": Decimal("1800.00"),
        "pf_employer": Decimal("1800.00"),
    }
    base.update(overrides)
    payslip = Payslip(tenant_id=tenant.id, run_id=run.id, employee_id=employee.id, **base)
    session.add(payslip)
    await session.commit()
    await session.refresh(payslip)
    return payslip


async def _identify(session: AsyncSession, employee: Employee, **fields) -> None:
    for key, value in fields.items():
        setattr(employee, key, value)
    await session.commit()


# ═══════════════════════════════════════════════════════════════════════════
# The run gate
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_return_cannot_be_built_from_an_unapproved_run(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """Filing draft figures is filing numbers nobody has signed off."""
    await _pf_rules(session, tenant)
    await _run(session, tenant, legal_entity.id, status=PayrollRunStatus.PENDING_REVIEW)

    with pytest.raises(HTTPException) as exc:
        await filing_service.generate_ecr(
            session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=6
        )
    assert exc.value.status_code == 409
    assert "approved run" in exc.value.detail


async def test_a_missing_pf_rule_set_refuses_rather_than_assuming_ceilings(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    run = await _run(session, tenant, legal_entity.id)
    await _payslip(session, tenant, run, employee)

    with pytest.raises(HTTPException) as exc:
        await filing_service.generate_ecr(
            session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=6
        )
    assert "None of them is assumed" in exc.value.detail


# ═══════════════════════════════════════════════════════════════════════════
# PF ECR
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_clean_ecr_generates_and_records(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    await _pf_rules(session, tenant)
    await _identify(session, employee, uan="100123456789", first_name="Anita", last_name="Sharma")
    run = await _run(session, tenant, legal_entity.id)
    await _payslip(session, tenant, run, employee)

    generated = await filing_service.generate_ecr(
        session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=6
    )
    await session.commit()

    assert generated.record.status is FilingStatus.GENERATED
    assert generated.record.record_count == 1
    assert generated.record.content_hash
    assert generated.content.startswith("100123456789|Anita Sharma|")
    assert generated.content.endswith("\r\n")


async def test_a_blank_uan_blocks_the_ecr_and_records_the_fix_list(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """The checklist gate, end to end.

    No file is produced, and the blocked attempt is recorded — "we tried to
    file and could not, here is why" is what a missed-deadline conversation
    needs, and losing it on a page refresh helps nobody.
    """
    await _pf_rules(session, tenant)
    await _identify(session, employee, uan=None)
    run = await _run(session, tenant, legal_entity.id)
    await _payslip(session, tenant, run, employee)

    with pytest.raises(filing_service.FilingBlockedError) as exc:
        await filing_service.generate_ecr(
            session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=6
        )
    await session.commit()

    assert exc.value.record.status is FilingStatus.BLOCKED
    assert exc.value.record.content_hash is None
    assert exc.value.record.record_count == 0
    codes = {e["code"] for e in exc.value.record.validation_exceptions}
    assert "UAN_MISSING" in codes
    assert employee.employee_code in exc.value.record.validation_exceptions[0]["subject"]


async def test_an_employee_with_no_pf_is_left_off_the_ecr(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """A contractor, or someone excluded. Not a defect, and not on the return."""
    await _pf_rules(session, tenant)
    await _identify(session, employee, uan=None)
    run = await _run(session, tenant, legal_entity.id)
    await _payslip(
        session, tenant, run, employee,
        pf_employee=Decimal(0), pf_employer=Decimal(0), pf_wage_base=Decimal(0),
    )

    generated = await filing_service.generate_ecr(
        session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=6
    )
    await session.commit()
    assert generated.record.record_count == 0, "no UAN, but also no PF, so no exception"


async def test_ncp_days_reach_the_ecr_from_attendance(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """The checklist gate: NCP derives from LOP, not from a zero."""
    await _pf_rules(session, tenant)
    await _identify(session, employee, uan="100123456789", first_name="Anita", last_name="Sharma")
    run = await _run(session, tenant, legal_entity.id)
    await _payslip(session, tenant, run, employee, lop_days=Decimal(3), paid_days=Decimal(27))

    generated = await filing_service.generate_ecr(
        session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=6
    )
    await session.commit()
    assert generated.content.strip().split("|")[9] == "3"


async def test_ecr_generation_is_deterministic(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """Two runs, identical bytes — which is what makes the recorded hash a
    check rather than a decoration."""
    await _pf_rules(session, tenant)
    await _identify(session, employee, uan="100123456789", first_name="Anita", last_name="Sharma")
    run = await _run(session, tenant, legal_entity.id)
    await _payslip(session, tenant, run, employee)

    first = await filing_service.generate_ecr(
        session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=6
    )
    await session.commit()
    second = await filing_service.generate_ecr(
        session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=6
    )
    await session.commit()

    assert first.content == second.content
    assert first.record.content_hash == second.record.content_hash
    assert first.record.id != second.record.id, "a regeneration is a new record"


# ═══════════════════════════════════════════════════════════════════════════
# ESIC
# ═══════════════════════════════════════════════════════════════════════════


async def test_esic_lists_only_contributing_employees(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    await _identify(session, employee, esi_number="3100123456")
    run = await _run(session, tenant, legal_entity.id)
    await _payslip(
        session, tenant, run, employee,
        esi_wage_base=Decimal("18000.00"),
        esi_employee=Decimal("135.00"),
        esi_employer=Decimal("585.00"),
    )

    generated = await filing_service.generate_esic(
        session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=6
    )
    await session.commit()

    assert generated.record.record_count == 1
    assert "3100123456" in generated.content


async def test_a_missing_insurance_number_blocks_the_esic_return(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    run = await _run(session, tenant, legal_entity.id)
    await _payslip(
        session, tenant, run, employee,
        esi_wage_base=Decimal("18000.00"),
        esi_employee=Decimal("135.00"),
        esi_employer=Decimal("585.00"),
    )

    with pytest.raises(filing_service.FilingBlockedError) as exc:
        await filing_service.generate_esic(
            session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=6
        )
    await session.commit()
    assert "IP_MISSING" in {e["code"] for e in exc.value.result.as_list()}


# ═══════════════════════════════════════════════════════════════════════════
# Professional Tax — one challan per state
# ═══════════════════════════════════════════════════════════════════════════


async def test_pt_generates_one_challan_per_state(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee, location,
) -> None:
    from app.models.org import Location

    pune = Location(
        tenant_id=tenant.id, legal_entity_id=legal_entity.id, name="Pune",
        code="PNQ", city="Pune", state_code="MH",
    )
    session.add(pune)
    await session.flush()

    second = Employee(
        tenant_id=tenant.id, employee_code="ACM002", first_name="Rahul", last_name="Iyer",
        work_email="rahul@acme.test", legal_entity_id=legal_entity.id, location_id=pune.id,
    )
    session.add(second)
    legal_entity.pt_registrations = {"KA": "PT/BLR/12345", "MH": "PT/PNQ/67890"}
    await session.commit()

    run = await _run(session, tenant, legal_entity.id)
    await _payslip(session, tenant, run, employee, professional_tax=Decimal("200.00"))
    await _payslip(session, tenant, run, second, professional_tax=Decimal("175.00"))

    produced = await filing_service.generate_pt_challans(
        session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=6
    )
    await session.commit()

    assert len(produced) == 2, "two states, two challans — not one file with a state column"
    states = {item.record.jurisdiction for item in produced}
    assert states == {"KA", "MH"}
    for item in produced:
        assert item.record.record_count == 1


async def test_a_state_with_no_registration_blocks_its_challan(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee, location,
) -> None:
    run = await _run(session, tenant, legal_entity.id)
    await _payslip(session, tenant, run, employee, professional_tax=Decimal("200.00"))

    with pytest.raises(filing_service.FilingBlockedError) as exc:
        await filing_service.generate_pt_challans(
            session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=6
        )
    await session.commit()
    assert "PT_REGISTRATION_MISSING" in {e["code"] for e in exc.value.result.as_list()}
    assert exc.value.record.jurisdiction == "KA"


# ═══════════════════════════════════════════════════════════════════════════
# TDS 24Q and challan mapping
# ═══════════════════════════════════════════════════════════════════════════


async def _challan(
    session: AsyncSession, tenant: Tenant, legal_entity_id, *, quarter: int = 1, amount="45000.00"
) -> TdsChallan:
    challan = TdsChallan(
        tenant_id=tenant.id,
        legal_entity_id=legal_entity_id,
        bsr_code="0510308",
        deposit_date=date(2026, 7, 7),
        challan_serial="00123",
        financial_year="2026-2027",
        quarter=quarter,
        tds_amount=Decimal(amount),
        total_amount=Decimal(amount),
    )
    session.add(challan)
    await session.commit()
    await session.refresh(challan)
    return challan


async def test_an_unmapped_deduction_blocks_the_24q(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """The checklist gate: challan mapping is enforced before generation.

    The return could not be filed anyway; the difference is finding out here
    rather than from a portal a day later, on a deadline.
    """
    legal_entity.tan = "BLRA12345B"
    await _identify(session, employee, pan="ABCDE1234F")
    run = await _run(session, tenant, legal_entity.id, month=6)
    await _payslip(session, tenant, run, employee, tds=Decimal("45000.00"))

    with pytest.raises(filing_service.FilingBlockedError) as exc:
        await filing_service.generate_tds_24q(
            session, hr_user, legal_entity_id=legal_entity.id,
            financial_year="2026-2027", quarter=1,
        )
    await session.commit()

    codes = {e["code"] for e in exc.value.result.as_list()}
    assert "CHALLAN_UNMAPPED" in codes
    assert employee.employee_code in exc.value.result.blocking[0].subject


async def test_a_mapped_deduction_generates_the_24q(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    legal_entity.tan = "BLRA12345B"
    legal_entity.pan = "AAACA1234A"
    await _identify(session, employee, pan="ABCDE1234F", first_name="Anita", last_name="Sharma")
    run = await _run(session, tenant, legal_entity.id, month=6)
    payslip = await _payslip(session, tenant, run, employee, tds=Decimal("45000.00"))
    challan = await _challan(session, tenant, legal_entity.id)

    session.add(
        TdsChallanMapping(
            tenant_id=tenant.id,
            challan_id=challan.id,
            employee_id=employee.id,
            payslip_id=payslip.id,
            tds_amount=Decimal("45000.00"),
            deduction_date=date(2026, 6, 30),
        )
    )
    await session.commit()

    generated = await filing_service.generate_tds_24q(
        session, hr_user, legal_entity_id=legal_entity.id,
        financial_year="2026-2027", quarter=1,
    )
    await session.commit()

    assert generated.record.status is FilingStatus.GENERATED
    assert generated.content.startswith("FH^24Q^BLRA12345B^2026-2027^Q1\r\n")
    assert "\r\nCD^1^0510308^07072026^00123^" in generated.content
    assert "\r\nDD^1^1^ABCDE1234F^" in generated.content
    assert "SD^" not in generated.content, "Q1 carries no Annexure II"


async def test_a_missing_pan_blocks_the_24q(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    legal_entity.tan = "BLRA12345B"
    await _identify(session, employee, pan=None)
    run = await _run(session, tenant, legal_entity.id, month=6)
    payslip = await _payslip(session, tenant, run, employee, tds=Decimal("45000.00"))
    challan = await _challan(session, tenant, legal_entity.id)
    session.add(
        TdsChallanMapping(
            tenant_id=tenant.id, challan_id=challan.id, employee_id=employee.id,
            payslip_id=payslip.id, tds_amount=Decimal("45000.00"),
            deduction_date=date(2026, 6, 30),
        )
    )
    await session.commit()

    with pytest.raises(filing_service.FilingBlockedError) as exc:
        await filing_service.generate_tds_24q(
            session, hr_user, legal_entity_id=legal_entity.id,
            financial_year="2026-2027", quarter=1,
        )
    await session.commit()
    assert "PAN_MISSING" in {e["code"] for e in exc.value.result.as_list()}


async def test_q4_pulls_annexure_ii_from_the_years_tax_snapshots(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """Where slice 07's declarations reach a statutory return."""
    legal_entity.tan = "BLRA12345B"
    legal_entity.pan = "AAACA1234A"
    await _identify(session, employee, pan="ABCDE1234F", first_name="Anita", last_name="Sharma")

    run = await _run(session, tenant, legal_entity.id, year=2027, month=1)
    payslip = await _payslip(session, tenant, run, employee, tds=Decimal("45000.00"))
    challan = await _challan(session, tenant, legal_entity.id, quarter=4)
    session.add(
        TdsChallanMapping(
            tenant_id=tenant.id, challan_id=challan.id, employee_id=employee.id,
            payslip_id=payslip.id, tds_amount=Decimal("45000.00"),
            deduction_date=date(2027, 1, 31),
        )
    )
    session.add(
        TaxComputationSnapshot(
            tenant_id=tenant.id,
            employee_id=employee.id,
            financial_year="2026-2027",
            regime=TaxRegime.NEW,
            computed_for_month=1,
            gross_annual_income=Decimal("1200000.00"),
            total_exemptions=Decimal(0),
            total_deductions=Decimal(0),
            taxable_income=Decimal("1125000.00"),
            annual_tax_liability=Decimal("90000.00"),
            tds_already_deducted=Decimal("45000.00"),
            monthly_tds=Decimal("7500.00"),
            working={"standard_deduction": "75000.00"},
        )
    )
    await session.commit()

    generated = await filing_service.generate_tds_24q(
        session, hr_user, legal_entity_id=legal_entity.id,
        financial_year="2026-2027", quarter=4,
    )
    await session.commit()

    assert "\r\nSD^ABCDE1234F^Anita Sharma^new^" in generated.content
    assert "75000.00" in generated.content, "the standard deduction from the snapshot"


async def test_q4_without_a_tax_snapshot_blocks(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """An incomplete Q4 return is not filed and then corrected — it is refused."""
    legal_entity.tan = "BLRA12345B"
    await _identify(session, employee, pan="ABCDE1234F")
    run = await _run(session, tenant, legal_entity.id, year=2027, month=1)
    payslip = await _payslip(session, tenant, run, employee, tds=Decimal("45000.00"))
    challan = await _challan(session, tenant, legal_entity.id, quarter=4)
    session.add(
        TdsChallanMapping(
            tenant_id=tenant.id, challan_id=challan.id, employee_id=employee.id,
            payslip_id=payslip.id, tds_amount=Decimal("45000.00"),
            deduction_date=date(2027, 1, 31),
        )
    )
    await session.commit()

    with pytest.raises(filing_service.FilingBlockedError) as exc:
        await filing_service.generate_tds_24q(
            session, hr_user, legal_entity_id=legal_entity.id,
            financial_year="2026-2027", quarter=4,
        )
    await session.commit()
    assert "ANNEXURE_II_MISSING" in {e["code"] for e in exc.value.result.as_list()}


async def test_the_quarter_maps_to_financial_year_months_not_calendar_ones(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """April-June is Q1. A payslip from January belongs to Q4, not Q1."""
    assert filing_service.quarter_months(1) == (4, 5, 6)
    assert filing_service.quarter_months(4) == (1, 2, 3)

    legal_entity.tan = "BLRA12345B"
    await _identify(session, employee, pan="ABCDE1234F")
    january = await _run(session, tenant, legal_entity.id, year=2027, month=1)
    await _payslip(session, tenant, january, employee, tds=Decimal("45000.00"))
    await session.commit()

    # Q1 sees nothing, because January is not in April-June.
    generated = await filing_service.generate_tds_24q(
        session, hr_user, legal_entity_id=legal_entity.id,
        financial_year="2026-2027", quarter=1,
    )
    await session.commit()
    assert generated.record.record_count == 0


# ═══════════════════════════════════════════════════════════════════════════
# The filing record
# ═══════════════════════════════════════════════════════════════════════════


async def test_an_acknowledgement_closes_the_loop(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    await _pf_rules(session, tenant)
    await _identify(session, employee, uan="100123456789", first_name="Anita", last_name="Sharma")
    run = await _run(session, tenant, legal_entity.id)
    await _payslip(session, tenant, run, employee)

    generated = await filing_service.generate_ecr(
        session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=6
    )
    await session.commit()

    record = await filing_service.record_acknowledgement(
        session, hr_user, record_id=generated.record.id,
        acknowledgement_number="TRRN2026060012345",
    )
    await session.commit()

    assert record.status is FilingStatus.FILED
    assert record.acknowledgement_number == "TRRN2026060012345"
    assert record.filed_at is not None
    assert record.filed_by_user_id == hr_user.id


async def test_a_return_cannot_be_acknowledged_twice(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """A correction is a fresh filing, not an edit of the record."""
    await _pf_rules(session, tenant)
    await _identify(session, employee, uan="100123456789", first_name="Anita", last_name="Sharma")
    run = await _run(session, tenant, legal_entity.id)
    await _payslip(session, tenant, run, employee)
    generated = await filing_service.generate_ecr(
        session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=6
    )
    await filing_service.record_acknowledgement(
        session, hr_user, record_id=generated.record.id, acknowledgement_number="TRRN1"
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await filing_service.record_acknowledgement(
            session, hr_user, record_id=generated.record.id, acknowledgement_number="TRRN2"
        )
    assert exc.value.status_code == 409
    assert "already acknowledged" in exc.value.detail


async def test_a_blocked_return_cannot_be_acknowledged(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """There was never a file, so there was never an upload."""
    await _pf_rules(session, tenant)
    await _identify(session, employee, uan=None)
    run = await _run(session, tenant, legal_entity.id)
    await _payslip(session, tenant, run, employee)

    with pytest.raises(filing_service.FilingBlockedError) as exc:
        await filing_service.generate_ecr(
            session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=6
        )
    await session.commit()

    with pytest.raises(HTTPException) as ack_exc:
        await filing_service.record_acknowledgement(
            session, hr_user, record_id=exc.value.record.id, acknowledgement_number="TRRN1"
        )
    assert "nothing that could have been filed" in ack_exc.value.detail


async def test_filing_records_are_tenant_isolated(
    session: AsyncSession, tenant: Tenant, hr_user: User, other_hr_user: User,
    legal_entity: LegalEntity, employee: Employee,
) -> None:
    await _pf_rules(session, tenant)
    await _identify(session, employee, uan="100123456789", first_name="Anita", last_name="Sharma")
    run = await _run(session, tenant, legal_entity.id)
    await _payslip(session, tenant, run, employee)
    generated = await filing_service.generate_ecr(
        session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=6
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await filing_service.record_acknowledgement(
            session, other_hr_user, record_id=generated.record.id,
            acknowledgement_number="TRRN1",
        )
    assert exc.value.status_code == 404


async def test_a_regeneration_leaves_the_earlier_record_intact(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """"Which file did we actually upload in July" is what an inspection asks."""
    await _pf_rules(session, tenant)
    await _identify(session, employee, uan="100123456789", first_name="Anita", last_name="Sharma")
    run = await _run(session, tenant, legal_entity.id)
    await _payslip(session, tenant, run, employee)

    for _ in range(3):
        await filing_service.generate_ecr(
            session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=6
        )
        await session.commit()

    records = (
        await session.scalars(
            select(FilingRecord).where(FilingRecord.return_type == ReturnType.PF_ECR)
        )
    ).all()
    assert len(records) == 3, "history accumulates; it does not rewrite itself"
