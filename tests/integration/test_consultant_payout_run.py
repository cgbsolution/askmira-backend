"""Consultant payout runs: approved weeks priced, GST added, TDS taken, weeks held once."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.models.audit import AuditLog
from app.models.consultant_payouts import ConsultantTdsOpening
from app.models.contractors import TdsSection
from app.models.disbursement import EmployeeBankAccount
from app.models.employee import ApproverRoute, Employee, EmploymentStatus, EmploymentType
from app.models.org import LegalEntity
from app.models.payroll import PayrollRuleSet
from app.models.payroll_inputs import (
    AdjustmentCategory,
    AdjustmentKind,
    AdjustmentRecurrence,
    AdjustmentStatus,
    PayrollAdjustment,
)
from app.models.tenant import Tenant
from app.models.timesheets import HourlyRate, Timesheet, TimesheetEntry, TimesheetStatus
from app.models.user import User
from app.services import timesheets
from app.services.consultants import payouts
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, client  # noqa: F401  (fixture)

pytestmark = requires_db

RULES = {
    "no_pan_rate": "20",
    "sections": {
        "194C": {
            "rate_individual": "1",
            "rate_other": "2",
            "single_threshold": "30000",
            "annual_threshold": "100000",
        },
        "194J_professional": {"rate": "10", "annual_threshold": "50000"},
        "194J_technical": {"rate": "2", "annual_threshold": "50000"},
    },
}

RELEASE = date(2026, 9, 1)
MONDAY = date(2026, 9, 7)
CUT_OFF = date(2026, 9, 30)
PAY_DATE = date(2026, 10, 5)
APPROVED_AT = datetime(2026, 9, 14, 6, 0, tzinfo=UTC)


async def _setup(session: AsyncSession, tenant: Tenant, hr_user: User) -> None:
    tenant.consultant_payouts_from = RELEASE
    session.add(
        PayrollRuleSet(
            tenant_id=tenant.id,
            rule_type="tds_non_salary",
            jurisdiction="IN",
            effective_from=date(2025, 4, 1),
            rules=RULES,
            source_reference="Test rules",
            approved_at=datetime(2025, 4, 1, tzinfo=UTC),
            approved_by_user_id=hr_user.id,
        )
    )
    await session.commit()


async def _consultant(
    session: AsyncSession,
    tenant: Tenant,
    entity: LegalEntity,
    code: str,
    *,
    rate: str = "1200",
    bank: bool = True,
    pan: str | None = "ABCDE1234F",
    **extra,
) -> Employee:
    person = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code.title(),
        work_email=f"{code.lower()}@acme-demo.com",
        legal_entity_id=entity.id,
        status=EmploymentStatus.ACTIVE,
        employment_type=EmploymentType.CONSULTANT,
        timesheet_approver_route=ApproverRoute.INTERNAL,
        tds_section=TdsSection.S194J_PROFESSIONAL,
        pan=pan,
        **extra,
    )
    session.add(person)
    await session.flush()
    session.add(
        HourlyRate(
            tenant_id=tenant.id,
            employee_id=person.id,
            rate=Decimal(rate),
            effective_from=date(2026, 1, 1),
        )
    )
    if bank:
        session.add(
            EmployeeBankAccount(
                tenant_id=tenant.id,
                employee_id=person.id,
                account_number=encrypt_secret("50100123456789", tenant.id),
                ifsc="HDFC0001234",
                account_holder_name=person.full_name,
                is_primary=True,
            )
        )
    await session.commit()
    return person


async def _week(
    session: AsyncSession,
    tenant: Tenant,
    person: Employee,
    *,
    monday: date = MONDAY,
    hours_per_day: str = "8",
    days: int = 5,
    decided_at: datetime = APPROVED_AT,
    payroll_adjustment_id: uuid.UUID | None = None,
) -> Timesheet:
    sheet = Timesheet(
        tenant_id=tenant.id,
        employee_id=person.id,
        week_start=monday,
        status=TimesheetStatus.APPROVED,
        decided_at=decided_at,
        approval_route="internal",
        payroll_adjustment_id=payroll_adjustment_id,
    )
    session.add(sheet)
    await session.flush()
    for offset in range(days):
        session.add(
            TimesheetEntry(
                tenant_id=tenant.id,
                timesheet_id=sheet.id,
                work_date=monday + timedelta(days=offset),
                hours=Decimal(hours_per_day),
            )
        )
    await session.commit()
    return sheet


def _line(run: dict, person: Employee) -> dict:
    return next(line for line in run["lines"] if line["employee_id"] == str(person.id))


async def test_run_pays_hours_times_rate_plus_gst_minus_tds(
    session, tenant, hr_user, legal_entity
) -> None:
    await _setup(session, tenant, hr_user)
    person = await _consultant(
        session,
        tenant,
        legal_entity,
        "CON1",
        gst_applicable=True,
        gst_rate=Decimal("18"),
        gstin="29ABCDE1234F1Z5",
    )
    session.add(
        ConsultantTdsOpening(
            tenant_id=tenant.id,
            employee_id=person.id,
            fy_start=date(2026, 4, 1),
            section=TdsSection.S194J_PROFESSIONAL,
            fees_paid=Decimal("40000"),
        )
    )
    await session.commit()
    sheet = await _week(session, tenant, person)

    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )

    assert run["status"] == "draft"
    line = _line(run, person)
    assert line["excluded"] is False and line["problems"] == []
    assert line["hours"] == "40.00"
    assert line["gross"] == "48000.00"
    assert line["gst_rate"] == "18.00"
    assert line["gst_amount"] == "8640.00"
    assert line["section"] == "194J_professional"
    # 40000 already paid this year + 48000 crosses 50000: all 88000 taxed at 10%.
    assert line["tds_base"] == "88000.00"
    assert line["tds_rate"] == "10.000"
    assert line["tds_amount"] == "8800.00"
    assert line["net_payable"] == "47840.00"
    assert [w["timesheet_id"] for w in line["weeks"]] == [str(sheet.id)]
    assert line["weeks"][0]["breakdown"][0] == {
        "work_date": "2026-09-07",
        "project_id": None,
        "hours": "8.00",
        "rate": "1200.00",
        "amount": "9600.00",
    }
    assert run["totals"]["net_payable"] == "47840.00"
    logged = await session.scalar(
        select(AuditLog).where(AuditLog.entity_type == "consultant_payout_run")
    )
    assert logged is not None


async def _earlier_line(
    session: AsyncSession,
    tenant: Tenant,
    person: Employee,
    *,
    run_status,
    pay_date: date = date(2026, 8, 1),
    gross: str,
    tds_base: str = "0",
    section: TdsSection = TdsSection.S194J_PROFESSIONAL,
    excluded: bool = False,
):
    from app.models.consultant_payouts import ConsultantPayoutLine, ConsultantPayoutRun

    run = ConsultantPayoutRun(
        tenant_id=tenant.id, cut_off=pay_date, pay_date=pay_date, status=run_status
    )
    session.add(run)
    await session.flush()
    line = ConsultantPayoutLine(
        tenant_id=tenant.id,
        run_id=run.id,
        employee_id=person.id,
        section=section,
        gross=Decimal(gross),
        tds_base=Decimal(tds_base),
        excluded=excluded,
    )
    session.add(line)
    await session.commit()
    return line


async def test_ytd_counts_only_approved_or_paid_runs_same_fy_and_section(
    session, tenant, hr_user, legal_entity
) -> None:
    from app.models.consultant_payouts import PayoutRunStatus

    await _setup(session, tenant, hr_user)
    person = await _consultant(session, tenant, legal_entity, "CON1")
    approved = await _earlier_line(
        session, tenant, person, run_status=PayoutRunStatus.APPROVED, gross="30000"
    )
    paid = await _earlier_line(
        session, tenant, person, run_status=PayoutRunStatus.PAID, gross="10000"
    )
    # None of these count towards this year's 194J (professional) total.
    await _earlier_line(session, tenant, person, run_status=PayoutRunStatus.DRAFT, gross="100000")
    await _earlier_line(
        session,
        tenant,
        person,
        run_status=PayoutRunStatus.APPROVED,
        gross="100000",
        excluded=True,
    )
    await _earlier_line(
        session,
        tenant,
        person,
        run_status=PayoutRunStatus.APPROVED,
        gross="100000",
        section=TdsSection.S194J_TECHNICAL,
    )
    await _earlier_line(
        session,
        tenant,
        person,
        run_status=PayoutRunStatus.PAID,
        pay_date=date(2026, 3, 15),  # the previous financial year
        gross="100000",
    )
    await _week(session, tenant, person)

    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    line = _line(run, person)
    # 40000 paid untaxed this year + 48000 crosses 50000: all 88000 taxed.
    assert line["tds_base"] == "88000.00"
    assert line["tds_amount"] == "8800.00"

    # Once those earlier fees were taxed, only this payout is.
    approved.tds_base = Decimal("30000")
    paid.tds_base = Decimal("10000")
    await session.commit()
    line = _line(await payouts.recalculate(session, hr_user, uuid.UUID(run["id"])), person)
    assert line["tds_base"] == "48000.00"
    assert line["tds_amount"] == "4800.00"


async def test_second_draft_does_not_take_held_weeks(
    session, tenant, hr_user, legal_entity
) -> None:
    await _setup(session, tenant, hr_user)
    person = await _consultant(session, tenant, legal_entity, "CON1")
    await _week(session, tenant, person)

    first = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    second = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    assert len(first["lines"]) == 1
    assert second["lines"] == []


async def test_pre_release_and_adjusted_weeks_are_not_eligible(
    session, tenant, hr_user, legal_entity
) -> None:
    await _setup(session, tenant, hr_user)
    person = await _consultant(session, tenant, legal_entity, "CON1")
    old = await _week(
        session,
        tenant,
        person,
        monday=date(2026, 8, 3),
        decided_at=datetime(2026, 8, 10, tzinfo=UTC),
    )
    adjustment = PayrollAdjustment(
        tenant_id=tenant.id,
        employee_id=person.id,
        kind=AdjustmentKind.EARNING,
        category=AdjustmentCategory.OTHER_EARNING,
        name="Hours, week of 14 Sep 2026",
        amount=Decimal("48000"),
        taxable=True,
        counts_as_wages=True,
        within_net_pay=False,
        recurrence=AdjustmentRecurrence.ONE_TIME,
        start_year=2026,
        start_month=9,
        status=AdjustmentStatus.PENDING,
        reason="Approved timesheet, 40 hours.",
        created_by_user_id=hr_user.id,
    )
    session.add(adjustment)
    await session.flush()
    adjusted = await _week(
        session, tenant, person, monday=date(2026, 9, 14), payroll_adjustment_id=adjustment.id
    )

    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    assert run["lines"] == []

    run = await payouts.include_week(session, hr_user, uuid.UUID(run["id"]), old.id)
    line = _line(run, person)
    assert [w["timesheet_id"] for w in line["weeks"]] == [str(old.id)]
    assert line["gross"] == "48000.00"

    with pytest.raises(HTTPException) as refused:
        await payouts.include_week(session, hr_user, uuid.UUID(run["id"]), adjusted.id)
    assert refused.value.status_code == 409


async def test_problems_hold_only_that_line(session, tenant, hr_user, legal_entity) -> None:
    await _setup(session, tenant, hr_user)
    a = await _consultant(session, tenant, legal_entity, "CONA", bank=False)
    b = await _consultant(session, tenant, legal_entity, "CONB")
    c = await _consultant(session, tenant, legal_entity, "CONC", pan=None, rate="1500")
    for person in (a, b, c):
        await _week(session, tenant, person)

    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )

    line_a, line_b, line_c = _line(run, a), _line(run, b), _line(run, c)
    assert line_a["excluded"] is True
    assert line_a["problems"] == ["No bank account"]
    assert len(line_a["weeks"]) == 1  # held, not released
    assert line_b["excluded"] is False and line_b["problems"] == []
    assert line_c["excluded"] is False and line_c["problems"] == []
    # 60000 crosses 50000; no PAN, so the higher rate.
    assert line_c["tds_rate"] == "20.000"
    assert line_c["tds_amount"] == "12000.00"
    assert "No PAN: TDS at 20%." in line_c["tds_note"]
    assert run["totals"]["lines_included"] == 2

    # Fixing the data and recalculating brings the line back.
    session.add(
        EmployeeBankAccount(
            tenant_id=tenant.id,
            employee_id=a.id,
            account_number=encrypt_secret("50100999988887", tenant.id),
            ifsc="HDFC0001234",
            account_holder_name=a.full_name,
            is_primary=True,
        )
    )
    await session.commit()
    run = await payouts.recalculate(session, hr_user, uuid.UUID(run["id"]))
    assert _line(run, a)["excluded"] is False
    assert _line(run, a)["problems"] == []


async def test_missing_rate_and_gstin_are_problems(session, tenant, hr_user, legal_entity) -> None:
    await _setup(session, tenant, hr_user)
    late = await _consultant(session, tenant, legal_entity, "CONL", gst_applicable=True)
    rate = await session.scalar(select(HourlyRate).where(HourlyRate.employee_id == late.id))
    rate.effective_from = date(2026, 9, 9)
    await session.commit()
    await _week(session, tenant, late)

    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    line = _line(run, late)
    assert line["excluded"] is True
    assert line["problems"] == [
        "No hourly rate on 2026-09-07",
        "No hourly rate on 2026-09-08",
        "GST applies but no GSTIN",
    ]


async def test_exclude_and_delete_release_weeks(session, tenant, hr_user, legal_entity) -> None:
    await _setup(session, tenant, hr_user)
    a = await _consultant(session, tenant, legal_entity, "CONA")
    b = await _consultant(session, tenant, legal_entity, "CONB")
    week_a = await _week(session, tenant, a)
    week_b = await _week(session, tenant, b)

    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    run_id = uuid.UUID(run["id"])
    assert (
        await payouts.eligible_weeks(session, hr_user, legal_entity_id=None, cut_off=CUT_OFF) == []
    )

    run = await payouts.exclude_line(
        session, hr_user, run_id, uuid.UUID(_line(run, a)["id"]), reason="Disputed hours"
    )
    excluded = _line(run, a)
    assert excluded["excluded"] is True
    assert excluded["excluded_reason"] == "Disputed hours"
    assert excluded["weeks"] == [] and excluded["net_payable"] == "0.00"
    free = await payouts.eligible_weeks(session, hr_user, legal_entity_id=None, cut_off=CUT_OFF)
    assert [w.id for w in free] == [week_a.id]

    # Recalculating never brings back a line HR took out.
    run = await payouts.recalculate(session, hr_user, run_id)
    assert _line(run, a)["excluded"] is True and _line(run, a)["weeks"] == []

    await payouts.delete_run(session, hr_user, run_id)
    free = await payouts.eligible_weeks(session, hr_user, legal_entity_id=None, cut_off=CUT_OFF)
    assert {w.id for w in free} == {week_a.id, week_b.id}
    with pytest.raises(HTTPException) as gone:
        await payouts.get_run(session, hr_user, run_id)
    assert gone.value.status_code == 404


async def test_legal_entity_and_cut_off_narrow_the_run(
    session, tenant, hr_user, legal_entity
) -> None:
    await _setup(session, tenant, hr_user)
    other = LegalEntity(tenant_id=tenant.id, name="Acme Labs Pvt Ltd", code="LABS")
    session.add(other)
    await session.commit()
    here = await _consultant(session, tenant, legal_entity, "CONH")
    there = await _consultant(session, tenant, other, "CONT")
    await _week(session, tenant, here)
    await _week(session, tenant, there)
    await _week(session, tenant, here, monday=date(2026, 9, 28))  # ends 4 Oct

    run = await payouts.create_run(
        session, hr_user, legal_entity_id=legal_entity.id, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    assert [line["employee_id"] for line in run["lines"]] == [str(here.id)]
    assert len(run["lines"][0]["weeks"]) == 1


async def test_reopen_refused_once_in_a_run(session, tenant, hr_user, legal_entity) -> None:
    await _setup(session, tenant, hr_user)
    person = await _consultant(session, tenant, legal_entity, "CON1")
    sheet = await _week(session, tenant, person)
    await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )

    with pytest.raises(HTTPException) as refused:
        await timesheets.reopen_week(session, hr_user, sheet.id)
    assert refused.value.status_code == 409
    assert "already in a consultant payout" in refused.value.detail


async def test_only_drafts_change(session, tenant, hr_user, legal_entity) -> None:
    from app.models.consultant_payouts import ConsultantPayoutRun, PayoutRunStatus

    await _setup(session, tenant, hr_user)
    person = await _consultant(session, tenant, legal_entity, "CON1")
    await _week(session, tenant, person)
    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    row = await session.get(ConsultantPayoutRun, uuid.UUID(run["id"]))
    row.status = PayoutRunStatus.APPROVED
    await session.commit()

    for attempt in (
        payouts.recalculate(session, hr_user, row.id),
        payouts.delete_run(session, hr_user, row.id),
        payouts.exclude_line(
            session, hr_user, row.id, uuid.UUID(run["lines"][0]["id"]), reason="No"
        ),
    ):
        with pytest.raises(HTTPException) as refused:
            await attempt
        assert refused.value.status_code == 409


async def test_payout_api(
    client,  # noqa: F811
    session,
    tenant,
    hr_user,
    legal_entity,
) -> None:
    await _setup(session, tenant, hr_user)
    person = await _consultant(session, tenant, legal_entity, "CON1")
    await _week(session, tenant, person)

    created = await client.post(
        "/api/v1/consultant-payouts",
        json={"cut_off": CUT_OFF.isoformat(), "pay_date": PAY_DATE.isoformat()},
        headers=bearer(hr_user),
    )
    assert created.status_code == 201, created.text
    run_id = created.json()["id"]
    listed = await client.get("/api/v1/consultant-payouts", headers=bearer(hr_user))
    assert listed.status_code == 200
    assert [r["id"] for r in listed.json()] == [run_id]
    assert listed.json()[0]["totals"]["gross"] == "48000.00"
    fetched = await client.get(f"/api/v1/consultant-payouts/{run_id}", headers=bearer(hr_user))
    assert fetched.json()["lines"][0]["gross"] == "48000.00"
    recalculated = await client.post(
        f"/api/v1/consultant-payouts/{run_id}/recalculate", headers=bearer(hr_user)
    )
    assert recalculated.status_code == 200
    deleted = await client.delete(f"/api/v1/consultant-payouts/{run_id}", headers=bearer(hr_user))
    assert deleted.status_code == 204
