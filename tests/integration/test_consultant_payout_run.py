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


def _timesheet_adjustment(
    tenant: Tenant, person: Employee, hr_user: User, *, adjustment_status: AdjustmentStatus
) -> PayrollAdjustment:
    return PayrollAdjustment(
        tenant_id=tenant.id,
        employee_id=person.id,
        kind=AdjustmentKind.EARNING,
        category=AdjustmentCategory.OTHER_EARNING,
        name="Hours, week of 03 Aug 2026",
        amount=Decimal("48000"),
        recurrence=AdjustmentRecurrence.ONE_TIME,
        start_year=2026,
        start_month=8,
        reference=f"timesheet:{uuid.uuid4()}",
        status=adjustment_status,
        reason="Approved timesheet, 40 hours.",
        created_by_user_id=hr_user.id,
    )


async def test_a_week_whose_payroll_adjustment_was_cancelled_can_be_paid_once(
    session, tenant, hr_user, legal_entity
) -> None:
    from app.services.payroll import adjustments

    await _setup(session, tenant, hr_user)
    person = await _consultant(session, tenant, legal_entity, "CON1")
    pending = _timesheet_adjustment(
        tenant, person, hr_user, adjustment_status=AdjustmentStatus.PENDING
    )
    completed = _timesheet_adjustment(
        tenant, person, hr_user, adjustment_status=AdjustmentStatus.COMPLETED
    )
    session.add_all([pending, completed])
    await session.flush()
    before_release = datetime(2026, 8, 10, tzinfo=UTC)
    in_flight = await _week(
        session,
        tenant,
        person,
        monday=date(2026, 8, 3),
        decided_at=before_release,
        payroll_adjustment_id=pending.id,
    )
    paid = await _week(
        session,
        tenant,
        person,
        monday=date(2026, 8, 10),
        decided_at=before_release,
        payroll_adjustment_id=completed.id,
    )
    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    run_id = uuid.UUID(run["id"])
    # Still pending in payroll: payroll may pay it, so a payout must not.
    with pytest.raises(HTTPException) as refused:
        await payouts.include_week(session, hr_user, run_id, in_flight.id)
    assert refused.value.status_code == 409

    checker = await _second_hr(session, tenant)
    await adjustments.cancel(session, checker, pending.id, note="Paid as a consultant payout")
    run = await payouts.include_week(session, hr_user, run_id, in_flight.id)
    assert [w["timesheet_id"] for w in _line(run, person)["weeks"]] == [str(in_flight.id)]
    # Once only: no second run takes it, and a week payroll paid stays refused.
    again = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    with pytest.raises(HTTPException) as twice:
        await payouts.include_week(session, hr_user, uuid.UUID(again["id"]), in_flight.id)
    assert twice.value.status_code == 409
    with pytest.raises(HTTPException) as refused:
        await payouts.include_week(session, hr_user, run_id, paid.id)
    assert refused.value.status_code == 409


async def test_an_approved_week_with_a_cancelled_adjustment_is_taken(
    session, tenant, hr_user, legal_entity
) -> None:
    await _setup(session, tenant, hr_user)
    person = await _consultant(session, tenant, legal_entity, "CON1")
    cancelled = _timesheet_adjustment(
        tenant, person, hr_user, adjustment_status=AdjustmentStatus.CANCELLED
    )
    session.add(cancelled)
    await session.flush()
    sheet = await _week(session, tenant, person, payroll_adjustment_id=cancelled.id)
    taken = await payouts.eligible_weeks(session, hr_user, legal_entity_id=None, cut_off=CUT_OFF)
    assert [week.id for week in taken] == [sheet.id]


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


# ── Approve, pay, statements, attention ─────────────────────────────────────


async def _confirmed() -> None:
    """Stands in for the step-up the route passes."""


async def _second_hr(session: AsyncSession, tenant: Tenant) -> User:
    from app.models.user import UserRole

    checker = User(
        tenant_id=tenant.id,
        email="checker@acme.test",
        password_hash="not-a-real-hash",
        full_name="Second Checker",
        role=UserRole.ADMIN,
        is_active=True,
    )
    session.add(checker)
    await session.commit()
    await session.refresh(checker)
    return checker


async def test_approved_run_is_locked_and_never_repriced(
    session, tenant, hr_user, legal_entity
) -> None:
    await _setup(session, tenant, hr_user)
    person = await _consultant(session, tenant, legal_entity, "CON1")
    await _week(session, tenant, person)
    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    checker = await _second_hr(session, tenant)
    run_id = uuid.UUID(run["id"])

    # The person who drafted it cannot approve it.
    with pytest.raises(HTTPException) as refused:
        await payouts.approve_run(session, hr_user, run_id, confirm=_confirmed)
    assert refused.value.status_code == 403

    approved = await payouts.approve_run(session, checker, run_id, confirm=_confirmed)
    assert approved["status"] == "approved"
    assert approved["approved_by_user_id"] == str(checker.id)

    session.add(
        HourlyRate(
            tenant_id=tenant.id,
            employee_id=person.id,
            rate=Decimal("5000"),
            effective_from=date(2026, 9, 1),
        )
    )
    await session.commit()
    with pytest.raises(HTTPException) as locked:
        await payouts.recalculate(session, hr_user, run_id)
    assert locked.value.status_code == 409
    again = await payouts.get_run(session, hr_user, run_id)
    assert _line(again, person)["gross"] == "48000.00"

    with pytest.raises(HTTPException) as blank:
        await payouts.mark_paid(session, hr_user, run_id, paid_on=PAY_DATE, reference=" ")
    assert blank.value.status_code == 422
    paid = await payouts.mark_paid(session, hr_user, run_id, paid_on=PAY_DATE, reference="UTR123")
    assert paid["status"] == "paid" and paid["payment_reference"] == "UTR123"
    with pytest.raises(HTTPException) as twice:
        await payouts.mark_paid(session, hr_user, run_id, paid_on=PAY_DATE, reference="UTR123")
    assert twice.value.status_code == 409


async def test_cannot_approve_with_problem_lines(session, tenant, hr_user, legal_entity) -> None:
    await _setup(session, tenant, hr_user)
    good = await _consultant(session, tenant, legal_entity, "CONB")
    held = await _consultant(session, tenant, legal_entity, "CONA", bank=False)
    for person in (good, held):
        await _week(session, tenant, person)
    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    checker = await _second_hr(session, tenant)
    run_id = uuid.UUID(run["id"])

    with pytest.raises(HTTPException) as refused:
        await payouts.approve_run(session, checker, run_id, confirm=_confirmed)
    assert refused.value.status_code == 409

    # Leaving the held line out clears the way.
    await payouts.exclude_line(
        session, hr_user, run_id, uuid.UUID(_line(run, held)["id"]), reason="Bank details pending"
    )
    approved = await payouts.approve_run(session, checker, run_id, confirm=_confirmed)
    assert approved["status"] == "approved"


async def test_cannot_approve_an_empty_run(session, tenant, hr_user, legal_entity) -> None:
    await _setup(session, tenant, hr_user)
    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    checker = await _second_hr(session, tenant)
    with pytest.raises(HTTPException) as refused:
        await payouts.approve_run(session, checker, uuid.UUID(run["id"]), confirm=_confirmed)
    assert refused.value.status_code == 409


async def test_statement_and_my_payouts(
    session,
    tenant,
    hr_user,
    legal_entity,
    client,  # noqa: F811
) -> None:
    from app.models.user import UserRole
    from app.services.consultants import statement
    from tests.integration.conftest import as_caller

    await _setup(session, tenant, hr_user)
    mine = await _consultant(session, tenant, legal_entity, "CON1")
    other = await _consultant(session, tenant, legal_entity, "CON2")
    for person in (mine, other):
        await _week(session, tenant, person)
    logins = []
    for person in (mine, other):
        login = User(
            tenant_id=tenant.id,
            email=f"{person.employee_code.lower()}@login.test",
            password_hash="not-a-real-hash",
            full_name=person.full_name,
            role=UserRole.EMPLOYEE,
            is_active=True,
            employee_id=person.id,
        )
        session.add(login)
        logins.append(login)
    await session.commit()
    mine_login, other_login = [await as_caller(session, login) for login in logins]
    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    run_id = uuid.UUID(run["id"])
    line_id = uuid.UUID(_line(run, mine)["id"])

    # Nothing to see while it is a draft.
    assert await payouts.my_payouts(session, mine_login) == []
    with pytest.raises(HTTPException) as draft:
        await statement.payout_statement_pdf(session, mine_login, line_id)
    assert draft.value.status_code == 404

    checker = await _second_hr(session, tenant)
    await payouts.approve_run(session, checker, run_id, confirm=_confirmed)

    listed = await payouts.my_payouts(session, mine_login)
    assert [row["line_id"] for row in listed] == [str(line_id)]
    assert listed[0]["net_payable"] == "48000.00"
    filename, data = await statement.payout_statement_pdf(session, mine_login, line_id)
    assert data.startswith(b"%PDF") and filename.endswith(".pdf")
    _, hr_data = await statement.payout_statement_pdf(session, hr_user, line_id)
    assert hr_data.startswith(b"%PDF")
    with pytest.raises(HTTPException) as foreign:
        await statement.payout_statement_pdf(session, other_login, line_id)
    assert foreign.value.status_code == 403
    assert [r["line_id"] for r in await payouts.my_payouts(session, other_login)] != [str(line_id)]
    # My pay is the caller's own only: HR opens other people's from Consultant payouts.
    hr_self_service = await client.get(
        f"/api/v1/payroll/my-payouts/{line_id}/pdf", headers=bearer(hr_user)
    )
    assert hr_self_service.status_code in (403, 404)
    hr_route = await client.get(
        f"/api/v1/consultant-payouts/lines/{line_id}/pdf", headers=bearer(hr_user)
    )
    assert hr_route.status_code == 200


async def test_attention_list_and_end_salaries(session, tenant, hr_user, legal_entity) -> None:
    from app.models.payroll import EmployeeSalary, SalaryStructure
    from app.services.consultants import attention

    await _setup(session, tenant, hr_user)
    person = await _consultant(session, tenant, legal_entity, "CON1", gst_amount=Decimal("1800"))
    await _consultant(session, tenant, legal_entity, "CON2", internal_approver_user_id=hr_user.id)
    # A record from before payouts: GST applies, the old fixed amount, no rate yet.
    await _consultant(
        session,
        tenant,
        legal_entity,
        "CON4",
        internal_approver_user_id=hr_user.id,
        gst_applicable=True,
        gstin="29ABCDE1234F1Z5",
        gst_amount=Decimal("1800"),
    )
    unrated = await _consultant(session, tenant, legal_entity, "CON3")
    rates = await session.scalars(select(HourlyRate).where(HourlyRate.employee_id == unrated.id))
    for rate in rates.all():
        await session.delete(rate)
    structure = SalaryStructure(
        tenant_id=tenant.id, name="Standard", effective_from=date(2026, 1, 1)
    )
    session.add(structure)
    await session.flush()
    salary = EmployeeSalary(
        tenant_id=tenant.id,
        employee_id=person.id,
        structure_id=structure.id,
        ctc_annual=Decimal("600000"),
        effective_from=date(2026, 4, 1),
    )
    session.add(salary)
    session.add(
        PayrollAdjustment(
            tenant_id=tenant.id,
            employee_id=person.id,
            kind=AdjustmentKind.EARNING,
            category=AdjustmentCategory.OTHER_EARNING,
            name="Hours, week of 07 Sep 2026",
            amount=Decimal("9600"),
            recurrence=AdjustmentRecurrence.ONE_TIME,
            start_year=2026,
            start_month=9,
            reference=f"timesheet:{uuid.uuid4()}",
            reason="Approved timesheet, 40 hours.",
            status=AdjustmentStatus.PENDING,
            created_by_user_id=hr_user.id,
        )
    )
    # Approved but not yet paid by a run: payroll would still pay it.
    session.add(
        PayrollAdjustment(
            tenant_id=tenant.id,
            employee_id=person.id,
            kind=AdjustmentKind.EARNING,
            category=AdjustmentCategory.OTHER_EARNING,
            name="Hours, week of 14 Sep 2026",
            amount=Decimal("4800"),
            recurrence=AdjustmentRecurrence.ONE_TIME,
            start_year=2026,
            start_month=9,
            reference=f"timesheet:{uuid.uuid4()}",
            reason="Approved timesheet, 4 hours.",
            status=AdjustmentStatus.APPROVED,
            created_by_user_id=hr_user.id,
        )
    )
    await session.commit()

    listed = {row["code"]: row for row in await attention.attention_list(session, hr_user)}
    assert listed["CON1"]["reasons"] == ["Has a current salary", "No timesheet approver"]
    # The old figure is a hint only while GST applies and no rate is set.
    assert listed["CON1"]["old_gst_amount"] is None
    assert listed["CON4"]["reasons"] == ["No GST rate"]
    assert listed["CON4"]["old_gst_amount"] == "1800.00"
    assert "No hourly rate" in listed["CON3"]["reasons"]
    assert "CON2" not in listed
    pending = await attention.pending_adjustments(session, hr_user)
    assert sorted((p["employee_code"], p["amount"]) for p in pending) == [
        ("CON1", "4800.00"),
        ("CON1", "9600.00"),
    ]

    ended = await attention.end_salaries(
        session, hr_user, [person.id], effective_to=date(2026, 9, 30)
    )
    assert ended == 1
    await session.refresh(salary)
    assert salary.effective_to == date(2026, 9, 30)
    # Ended in the past: the salary reason is gone.
    after = {row["code"]: row for row in await attention.attention_list(session, hr_user)}
    assert after["CON1"]["reasons"] == ["No timesheet approver"]


async def test_end_salaries_refuses_non_consultants(
    session, tenant, hr_user, legal_entity, employee
) -> None:
    from app.services.consultants import attention

    with pytest.raises(HTTPException) as refused:
        await attention.end_salaries(
            session, hr_user, [employee.id], effective_to=date(2026, 9, 30)
        )
    assert refused.value.status_code == 409


async def test_set_opening_upserts_and_needs_an_april_first(
    session, tenant, hr_user, legal_entity
) -> None:
    from app.services.consultants import attention

    person = await _consultant(session, tenant, legal_entity, "CON1")
    args = {
        "employee_id": person.id,
        "section": TdsSection.S194J_PROFESSIONAL,
        "fees_paid": Decimal("40000"),
        "taxed_base": Decimal("0"),
        "tds_deducted": Decimal("0"),
    }
    with pytest.raises(HTTPException) as refused:
        await attention.set_opening(session, hr_user, fy_start=date(2026, 5, 1), **args)
    assert refused.value.status_code == 422
    await attention.set_opening(session, hr_user, fy_start=date(2026, 4, 1), **args)
    args["fees_paid"] = Decimal("45000")
    await attention.set_opening(session, hr_user, fy_start=date(2026, 4, 1), **args)
    rows = (await session.scalars(select(ConsultantTdsOpening))).all()
    assert [row.fees_paid for row in rows] == [Decimal("45000.00")]


async def test_whoever_last_changed_the_figures_cannot_approve(
    session, tenant, hr_user, legal_entity
) -> None:
    await _setup(session, tenant, hr_user)
    person = await _consultant(session, tenant, legal_entity, "CON1")
    await _week(session, tenant, person)
    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    other = await _second_hr(session, tenant)
    run_id = uuid.UUID(run["id"])

    # The checker recalculates A's draft: the figures are now theirs, so they cannot sign.
    await payouts.recalculate(session, other, run_id)
    with pytest.raises(HTTPException) as refused:
        await payouts.approve_run(session, other, run_id, confirm=_confirmed)
    assert refused.value.status_code == 403
    # The drafter is no longer the last to change them, so now they can.
    approved = await payouts.approve_run(session, hr_user, run_id, confirm=_confirmed)
    assert approved["status"] == "approved"


async def test_approval_needs_scope_over_everybody_on_the_run(
    session, tenant, hr_user, legal_entity
) -> None:
    from app.core import hr_scope

    await _setup(session, tenant, hr_user)
    inside = await _consultant(session, tenant, legal_entity, "CON1")
    outside = await _consultant(session, tenant, legal_entity, "CON2")
    for person in (inside, outside):
        await _week(session, tenant, person)
    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    checker = await _second_hr(session, tenant)
    run_id = uuid.UUID(run["id"])

    hr_scope.set_scope(hr_scope.HrScope(employee_ids=(inside.id,)))
    try:
        with pytest.raises(HTTPException) as refused:
            await payouts.approve_run(session, checker, run_id, confirm=_confirmed)
        assert refused.value.status_code == 403
        assert "outside your department or location scope" in refused.value.detail
    finally:
        hr_scope.set_scope(None)
    assert (await _run_row(session, run_id)).status.value == "draft"


async def _run_row(session: AsyncSession, run_id: uuid.UUID):
    from app.models.consultant_payouts import ConsultantPayoutRun

    return await session.get(ConsultantPayoutRun, run_id)


# ── TDS larger than the payment ─────────────────────────────────────────────

SECOND_MONDAY = date(2026, 10, 5)
SECOND_CUT_OFF = date(2026, 10, 11)
SECOND_PAY_DATE = date(2026, 10, 20)


async def _untaxed_opening(session, tenant, person: Employee, fees: str) -> None:
    session.add(
        ConsultantTdsOpening(
            tenant_id=tenant.id,
            employee_id=person.id,
            fy_start=date(2026, 4, 1),
            section=TdsSection.S194J_PROFESSIONAL,
            fees_paid=Decimal(fees),
        )
    )
    await session.commit()


async def _gst_capped_consultant(session, tenant, legal_entity, code: str) -> Employee:
    """59000 paid untaxed, then 5005 + 18% GST (900.90): TDS on 64005 beats the 5905.90 paid."""
    person = await _consultant(
        session,
        tenant,
        legal_entity,
        code,
        rate="1001",
        gst_applicable=True,
        gst_rate=Decimal("18"),
        gstin="29ABCDE1234F1Z5",
    )
    await _untaxed_opening(session, tenant, person, "59000")
    await _week(session, tenant, person, hours_per_day="5", days=1)
    return person


async def _capped_first_run(session, tenant, hr_user, legal_entity, *, gst_code: str | None = None):
    """49000 paid untaxed this year, then 5000: 10% of 54000 is more than the 5000 paid."""
    await _setup(session, tenant, hr_user)
    person = await _consultant(session, tenant, legal_entity, "CON1", rate="1000")
    await _untaxed_opening(session, tenant, person, "49000")
    await _week(session, tenant, person, hours_per_day="5", days=1)
    if gst_code:
        await _gst_capped_consultant(session, tenant, legal_entity, gst_code)
    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    return person, run


def _assert_never_negative(run: dict) -> None:
    for line in run["lines"]:
        payable = Decimal(line["gross"]) + Decimal(line["gst_amount"])
        assert Decimal(line["net_payable"]) >= 0
        assert Decimal(line["tds_amount"]) <= payable
        assert Decimal(line["net_payable"]) == payable - Decimal(line["tds_amount"])
        assert Decimal(line["tds_due"]) == Decimal(line["tds_amount"]) + Decimal(
            line["tds_carried_out"]
        )


async def test_tds_above_payment_is_capped_and_carried(
    session, tenant, hr_user, legal_entity
) -> None:
    person, run = await _capped_first_run(session, tenant, hr_user, legal_entity)

    line = _line(run, person)
    assert line["gross"] == "5000.00"
    assert line["tds_base"] == "54000.00"
    assert line["tds_due"] == "5400.00"
    assert line["tds_amount"] == "5000.00"
    assert line["net_payable"] == "0.00"
    assert line["tds_carried_out"] == "400.00"
    assert "₹400.00 of TDS is carried to the next payout." in line["tds_note"]
    assert run["totals"]["tds_carried_out"] == "400.00"
    assert Decimal(line["net_payable"]) >= 0


async def test_carried_tds_is_taken_from_the_next_payout(
    session, tenant, hr_user, legal_entity, monkeypatch
) -> None:
    from app.models.user import UserRole
    from app.services.consultants import statement
    from app.services.documents import pdf
    from tests.integration.conftest import as_caller

    person, first = await _capped_first_run(session, tenant, hr_user, legal_entity)
    checker = await _second_hr(session, tenant)
    await payouts.approve_run(session, checker, uuid.UUID(first["id"]), confirm=_confirmed)
    await _week(session, tenant, person, monday=SECOND_MONDAY, hours_per_day="10", days=1)

    second = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=SECOND_CUT_OFF, pay_date=SECOND_PAY_DATE
    )

    line = _line(second, person)
    assert line["gross"] == "10000.00"
    # The earlier fees were taxed in the first run, so only this payout is: 1000, plus 400.
    assert line["tds_base"] == "10000.00"
    assert line["tds_due"] == "1400.00"
    assert line["tds_amount"] == "1400.00"
    assert line["net_payable"] == "8600.00"
    assert line["tds_carried_out"] == "0.00"
    assert "Includes ₹400.00 TDS carried from an earlier payout." in line["tds_note"]
    assert Decimal(line["net_payable"]) >= 0

    # The consultant sees what was deducted and what was carried.
    login = User(
        tenant_id=tenant.id,
        email="con1@login.test",
        password_hash="not-a-real-hash",
        full_name=person.full_name,
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=person.id,
    )
    session.add(login)
    await session.commit()
    mine = await payouts.my_payouts(session, await as_caller(session, login))
    assert [(row["tds_due"], row["tds_amount"], row["tds_carried_out"]) for row in mine] == [
        ("5400.00", "5000.00", "400.00")
    ]
    seen: list[str] = []
    real = pdf.render_pdf

    def capture(*, merged_html, **kwargs):
        seen.append(merged_html)
        return real(merged_html=merged_html, **kwargs)

    monkeypatch.setattr(pdf, "render_pdf", capture)
    await statement.payout_statement_pdf(session, hr_user, uuid.UUID(_line(first, person)["id"]))
    assert "TDS deducted" in seen[0] and "TDS carried to next payout" in seen[0]
    await statement.payout_statement_pdf(session, hr_user, uuid.UUID(line["id"]))
    assert "TDS deducted" in seen[1] and "TDS carried to next payout" not in seen[1]


async def test_net_is_never_negative(session, tenant, hr_user, legal_entity) -> None:
    # A capped line without GST, and one with GST whose payable has paise.
    person, first = await _capped_first_run(session, tenant, hr_user, legal_entity, gst_code="CON2")
    assert len(first["lines"]) == 2
    _assert_never_negative(first)
    checker = await _second_hr(session, tenant)
    await payouts.approve_run(session, checker, uuid.UUID(first["id"]), confirm=_confirmed)

    # The 400 carried is more than the whole next payout (250): it is capped again.
    await _week(session, tenant, person, monday=SECOND_MONDAY, hours_per_day="0.25", days=1)
    second = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=SECOND_CUT_OFF, pay_date=SECOND_PAY_DATE
    )
    _assert_never_negative(second)
    line = _line(second, person)
    assert line["gross"] == "250.00"
    assert line["tds_amount"] == "250.00"
    assert line["net_payable"] == "0.00"
    assert line["tds_carried_out"] == "175.00"  # 25 on this payout + 400 brought in - 250


async def test_capped_tds_stays_in_whole_rupees(session, tenant, hr_user, legal_entity) -> None:
    await _setup(session, tenant, hr_user)
    person = await _gst_capped_consultant(session, tenant, legal_entity, "CON2")
    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    line = _line(run, person)
    assert line["gross"] == "5005.00" and line["gst_amount"] == "900.90"
    # TDS is capped at the whole rupees of the 5905.90 payable; the paise are paid.
    assert line["tds_amount"] == "5905.00"
    assert line["net_payable"] == "0.90"
    assert line["tds_carried_out"].endswith(".00")
    assert Decimal(line["tds_carried_out"]) > 0
    _assert_never_negative(run)


async def test_backdated_run_cannot_take_a_carry_twice(
    session, tenant, hr_user, legal_entity
) -> None:
    person, first = await _capped_first_run(session, tenant, hr_user, legal_entity)
    checker = await _second_hr(session, tenant)
    await payouts.approve_run(session, checker, uuid.UUID(first["id"]), confirm=_confirmed)
    # Run A, paid 25 Oct, takes the 400 carried and is approved.
    await _week(session, tenant, person, monday=SECOND_MONDAY, hours_per_day="10", days=1)
    run_a = await payouts.create_run(
        session,
        hr_user,
        legal_entity_id=None,
        cut_off=SECOND_CUT_OFF,
        pay_date=date(2026, 10, 25),
    )
    assert _line(run_a, person)["tds_due"] == "1400.00"
    await payouts.approve_run(session, checker, uuid.UUID(run_a["id"]), confirm=_confirmed)

    # Run B is dated 20 Oct, before A: it would find the first run's 400 again.
    await _week(session, tenant, person, monday=date(2026, 10, 12), hours_per_day="10", days=1)
    run_b = await payouts.create_run(
        session,
        hr_user,
        legal_entity_id=None,
        cut_off=date(2026, 10, 18),
        pay_date=date(2026, 10, 20),
    )
    line = _line(run_b, person)
    assert line["excluded"] is True
    assert line["problems"] == [
        "Paid on 2026-10-25 in an approved run; this run's pay date is earlier. "
        "Change the pay date."
    ]
    with pytest.raises(HTTPException) as refused:
        await payouts.approve_run(session, checker, uuid.UUID(run_b["id"]), confirm=_confirmed)
    assert refused.value.status_code == 409
    assert (await _run_row(session, uuid.UUID(run_b["id"]))).status.value == "draft"


async def test_carried_tds_does_not_cross_financial_years(
    session, tenant, hr_user, legal_entity
) -> None:
    from app.models.consultant_payouts import PayoutRunStatus

    await _setup(session, tenant, hr_user)
    person = await _consultant(session, tenant, legal_entity, "CON1")
    last_year = await _earlier_line(
        session,
        tenant,
        person,
        run_status=PayoutRunStatus.PAID,
        pay_date=date(2026, 3, 15),
        gross="1000",
    )
    last_year.tds_carried_out = Decimal("400")
    await session.commit()
    await _week(session, tenant, person)

    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    line = _line(run, person)
    # 48000 is below this year's threshold, and last year's 400 is not brought in.
    assert line["tds_due"] == line["tds_amount"] == "0.00"
    assert line["tds_carried_out"] == "0.00"


# ── Approval refuses stale figures ──────────────────────────────────────────

STALE_MESSAGE = (
    "The figures have changed since this run was priced (another run was approved, a rate or "
    "opening changed, or a record was updated). Recalculate the run, check it, then approve."
)


async def test_approve_refuses_when_another_run_took_the_carry(
    session, tenant, hr_user, legal_entity
) -> None:
    person, first = await _capped_first_run(session, tenant, hr_user, legal_entity)
    checker = await _second_hr(session, tenant)
    await payouts.approve_run(session, checker, uuid.UUID(first["id"]), confirm=_confirmed)
    # Two drafts open at once: both bring in the 400 the first run could not take.
    await _week(session, tenant, person, monday=SECOND_MONDAY, hours_per_day="10", days=1)
    draft_a = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=SECOND_CUT_OFF, pay_date=SECOND_PAY_DATE
    )
    await _week(session, tenant, person, monday=date(2026, 10, 12), hours_per_day="10", days=1)
    draft_b = await payouts.create_run(
        session,
        hr_user,
        legal_entity_id=None,
        cut_off=date(2026, 10, 18),
        pay_date=date(2026, 10, 25),
    )
    assert _line(draft_a, person)["tds_due"] == _line(draft_b, person)["tds_due"] == "1400.00"
    b_id = uuid.UUID(draft_b["id"])

    await payouts.approve_run(session, checker, uuid.UUID(draft_a["id"]), confirm=_confirmed)
    with pytest.raises(HTTPException) as stale:
        await payouts.approve_run(session, checker, b_id, confirm=_confirmed)
    assert stale.value.status_code == 409
    assert stale.value.detail == STALE_MESSAGE
    unchanged = await payouts.get_run(session, hr_user, b_id)
    assert unchanged["status"] == "draft"
    assert _line(unchanged, person)["tds_due"] == "1400.00"

    line = _line(await payouts.recalculate(session, hr_user, b_id), person)
    assert line["tds_due"] == line["tds_amount"] == "1000.00"
    assert line["tds_carried_out"] == "0.00"
    approved = await payouts.approve_run(session, checker, b_id, confirm=_confirmed)
    assert approved["status"] == "approved"


async def test_approve_refuses_after_opening_changed(
    session, tenant, hr_user, legal_entity
) -> None:
    from app.services.consultants import attention

    await _setup(session, tenant, hr_user)
    person = await _consultant(session, tenant, legal_entity, "CON1")
    await _week(session, tenant, person)
    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    assert _line(run, person)["tds_amount"] == "0.00"
    run_id = uuid.UUID(run["id"])
    await attention.set_opening(
        session,
        hr_user,
        employee_id=person.id,
        fy_start=date(2026, 4, 1),
        section=TdsSection.S194J_PROFESSIONAL,
        fees_paid=Decimal("40000"),
        taxed_base=Decimal("0"),
        tds_deducted=Decimal("0"),
    )
    checker = await _second_hr(session, tenant)

    with pytest.raises(HTTPException) as stale:
        await payouts.approve_run(session, checker, run_id, confirm=_confirmed)
    assert stale.value.status_code == 409
    assert stale.value.detail == STALE_MESSAGE
    again = await payouts.get_run(session, hr_user, run_id)
    assert again["status"] == "draft"
    assert _line(again, person)["tds_amount"] == "0.00"
