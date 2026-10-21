"""Ad-hoc adjustments, overtime, shift allowances and off-cycle runs, end to end.

Keyed in by one person, approved by another, paid by a run, recorded against
that run so nothing is paid twice; overtime from attendance to a priced payslip
line; and an off-cycle run that tops up a month the regular run already paid
without paying the structure, PF or the month's TDS share a second time.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import AttendanceDay, AttendanceStatus, Shift
from app.models.payroll import PayrollRunStatus, PayrollRunType, Payslip
from app.models.payroll_inputs import (
    AdjustmentCategory,
    AdjustmentRecurrence,
    AdjustmentStatus,
    OvertimeEntry,
    OvertimeStatus,
    PayrollAdjustmentApplication,
)
from app.schemas.payroll_inputs import AdjustmentCreate, OvertimePolicyCreate
from app.services.attendance import overtime as overtime_service
from app.services.filings import service as filings_service
from app.services.payroll import adjustments as adjustment_service
from app.services.payroll import run as run_service
from tests.integration.conftest import requires_db
from tests.integration.test_payroll_run_flow import (  # noqa: F401 - fixture
    _approved_rules,
    _lock,
    _open_run,
    org,
)

pytestmark = requires_db


def _incentive(employee_id: uuid.UUID, amount: str, month: int = 4) -> AdjustmentCreate:
    return AdjustmentCreate(
        employee_id=employee_id,
        category=AdjustmentCategory.INCENTIVE,
        name="Q1 sales incentive",
        amount=Decimal(amount),
        start_year=2026,
        start_month=month,
        reason="Q1 target met",
    )


async def _approve_month(session: AsyncSession, org, month: int):
    session.add(_lock(org["tenant"], org["maker"], 2026, month))
    await session.commit()
    run = await _open_run(session, org, month)
    await run_service.compute_run(session, org["maker"], run.id)
    return await run_service.approve_run(session, org["checker"], run.id)


async def _slip(session: AsyncSession, run_id, employee_id) -> Payslip:
    return await session.scalar(
        select(Payslip).where(Payslip.run_id == run_id, Payslip.employee_id == employee_id)
    )


# ═══════════════════════════════════════════════════════════════════════════
# Adjustments
# ═══════════════════════════════════════════════════════════════════════════


async def test_an_adjustment_is_maker_checker_and_paid_once(session: AsyncSession, org) -> None:
    await _approved_rules(session, org)
    created = await adjustment_service.create(
        session, org["checker"], _incentive(org["meera_emp"].id, "5000")
    )
    assert created.status is AdjustmentStatus.PENDING

    # A sub-admin cannot approve, and nobody approves what they keyed in.
    with pytest.raises(HTTPException) as exc:
        await adjustment_service.approve(session, org["checker"], created.id)
    assert exc.value.status_code == 403
    approved = await adjustment_service.approve(session, org["maker"], created.id)
    assert approved.status is AdjustmentStatus.APPROVED

    run = await _approve_month(session, org, 4)
    slip = await _slip(session, run.id, org["meera_emp"].id)
    lines = [line for line in slip.input_lines if line["code"] == "ADJ_INCENTIVE"]
    assert len(lines) == 1
    assert Decimal(lines[0]["amount"]) == Decimal("5000.00")
    assert Decimal(lines[0]["taxable_amount"]) == Decimal("5000.00")

    after = await adjustment_service.get(session, org["maker"], created.id)
    assert after.status is AdjustmentStatus.COMPLETED
    assert [a.period_month for a in after.applications] == [4]

    # The next month's run does not pay it again.
    may = await _approve_month(session, org, 5)
    may_slip = await _slip(session, may.id, org["meera_emp"].id)
    assert not [line for line in may_slip.input_lines if line["code"] == "ADJ_INCENTIVE"]


async def test_nobody_keys_in_an_adjustment_to_their_own_pay(session: AsyncSession, org) -> None:
    with pytest.raises(HTTPException) as exc:
        await adjustment_service.create(
            session, org["maker"], _incentive(org["maker"].employee_id, "1000")
        )
    assert exc.value.status_code == 403


async def test_a_capped_monthly_recovery_stops_at_its_cap(session: AsyncSession, org) -> None:
    await _approved_rules(session, org)
    created = await adjustment_service.create(
        session,
        org["checker"],
        AdjustmentCreate(
            employee_id=org["ravi_emp"].id,
            category=AdjustmentCategory.COURT_ATTACHMENT,
            name="Attachment order",
            amount=Decimal("500"),
            recurrence=AdjustmentRecurrence.MONTHLY,
            start_year=2026,
            start_month=4,
            total_cap=Decimal("900"),
            reference="OS 114/2026, City Civil Court",
            reason="Decree for recovery",
        ),
    )
    await adjustment_service.approve(session, org["maker"], created.id)

    april = await _approve_month(session, org, 4)
    april_line = next(
        line
        for line in (await _slip(session, april.id, org["ravi_emp"].id)).input_lines
        if line["code"] == "ADJ_COURT_ATTACHMENT"
    )
    assert april_line["kind"] == "recovery"
    assert Decimal(april_line["amount"]) == Decimal("500.00")

    may = await _approve_month(session, org, 5)
    may_line = next(
        line
        for line in (await _slip(session, may.id, org["ravi_emp"].id)).input_lines
        if line["code"] == "ADJ_COURT_ATTACHMENT"
    )
    assert Decimal(may_line["amount"]) == Decimal("400.00"), "only what is left of the cap"

    after = await adjustment_service.get(session, org["maker"], created.id)
    assert after.status is AdjustmentStatus.COMPLETED
    assert after.amount_applied == Decimal("900.00")


async def test_a_monthly_court_attachment_needs_its_order_and_its_total(
    session: AsyncSession, org
) -> None:
    payload = AdjustmentCreate(
        employee_id=org["ravi_emp"].id,
        category=AdjustmentCategory.COURT_ATTACHMENT,
        name="Attachment",
        amount=Decimal("500"),
        recurrence=AdjustmentRecurrence.MONTHLY,
        start_year=2026,
        start_month=4,
        reason="Decree",
    )
    with pytest.raises(HTTPException) as exc:
        await adjustment_service.create(session, org["checker"], payload)
    assert exc.value.status_code == 422 and "reference" in exc.value.detail


# ═══════════════════════════════════════════════════════════════════════════
# Off-cycle runs
# ═══════════════════════════════════════════════════════════════════════════


async def test_an_off_cycle_run_tops_up_the_month_without_paying_it_twice(
    session: AsyncSession, org
) -> None:
    await _approved_rules(session, org)
    session.add(_lock(org["tenant"], org["maker"], 2026, 4))
    await session.commit()
    regular = await _open_run(session, org, 4)

    # Not before the regular run is approved, and never without a reason or people.
    with pytest.raises(HTTPException) as exc:
        await run_service.create_run(
            session,
            org["maker"],
            legal_entity_id=org["entity"].id,
            period_year=2026,
            period_month=4,
            run_type=PayrollRunType.OFF_CYCLE,
            employee_ids=[org["meera_emp"].id],
            reason="Late incentive",
        )
    assert exc.value.status_code == 409

    await run_service.compute_run(session, org["maker"], regular.id)
    await run_service.approve_run(session, org["checker"], regular.id)
    regular_slip = await _slip(session, regular.id, org["meera_emp"].id)

    with pytest.raises(HTTPException) as exc:
        await run_service.create_run(
            session,
            org["maker"],
            legal_entity_id=org["entity"].id,
            period_year=2026,
            period_month=4,
            run_type=PayrollRunType.OFF_CYCLE,
            reason="Late incentive",
        )
    assert exc.value.status_code == 422

    created = await adjustment_service.create(
        session, org["checker"], _incentive(org["meera_emp"].id, "10000")
    )
    await adjustment_service.approve(session, org["maker"], created.id)

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
    assert off_cycle.included_sources == ["payroll_adjustments", "overtime"]
    computed = await run_service.compute_run(session, org["maker"], off_cycle.id)
    assert computed.employee_count == 1

    top_up = await _slip(session, off_cycle.id, org["meera_emp"].id)
    assert top_up.is_supplementary is True
    assert Decimal(str(top_up.gross_earnings)) == Decimal("0.00")
    assert Decimal(str(top_up.pf_employee)) == Decimal("0.00"), "PF is not taken twice"
    # The fixture's flat 10%: only the incentive's tax, not another month's share.
    assert Decimal(str(top_up.tds)) == Decimal("1000.00")
    assert Decimal(str(top_up.net_pay)) == Decimal("9000.00")

    approved = await run_service.approve_run(session, org["checker"], off_cycle.id)
    assert approved.status is PayrollRunStatus.APPROVED
    assert (await adjustment_service.get(session, org["maker"], created.id)).status is (
        AdjustmentStatus.COMPLETED
    )

    # The month's returns see one line per person, adding both payslips.
    rows = await filings_service._payslips(session, org["maker"], regular)
    meera = next(pay for pay, employee in rows if employee.id == org["meera_emp"].id)
    assert meera.tds == Decimal(str(regular_slip.tds)) + Decimal("1000.00")
    assert meera.pf_employee == Decimal(str(regular_slip.pf_employee))
    assert meera.paid_days == Decimal(str(regular_slip.paid_days))


async def test_an_off_cycle_run_with_nothing_due_says_so(session: AsyncSession, org) -> None:
    await _approved_rules(session, org)
    await _approve_month(session, org, 4)
    off_cycle = await run_service.create_run(
        session,
        org["maker"],
        legal_entity_id=org["entity"].id,
        period_year=2026,
        period_month=4,
        run_type=PayrollRunType.BONUS,
        employee_ids=[org["ravi_emp"].id],
        reason="Festival bonus",
    )
    with pytest.raises(HTTPException) as exc:
        await run_service.compute_run(session, org["maker"], off_cycle.id)
    assert exc.value.status_code == 422
    assert "Nothing is due" in exc.value.detail


async def test_full_and_final_is_not_a_payroll_run(session: AsyncSession, org) -> None:
    with pytest.raises(HTTPException) as exc:
        await run_service.create_run(
            session,
            org["maker"],
            legal_entity_id=org["entity"].id,
            period_year=2026,
            period_month=4,
            run_type=PayrollRunType.FULL_AND_FINAL,
        )
    assert exc.value.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════
# Overtime and shift allowances
# ═══════════════════════════════════════════════════════════════════════════


def _day(org, employee_id, day: date, *, overtime: int, shift_id=None) -> AttendanceDay:
    return AttendanceDay(
        tenant_id=org["tenant"].id,
        employee_id=employee_id,
        attendance_date=day,
        shift_id=shift_id,
        worked_minutes=480 + overtime,
        status=AttendanceStatus.PRESENT,
        payable_fraction=Decimal(1),
        overtime_minutes=overtime,
        computed_at=datetime.now(UTC),
    )


async def test_overtime_from_attendance_to_the_payslip(session: AsyncSession, org) -> None:
    await _approved_rules(session, org)
    await overtime_service.create_policy(
        session,
        org["maker"],
        OvertimePolicyCreate(
            name="Plant overtime",
            rate_multiplier=Decimal("2.00"),
            wage_component_codes=["BASIC"],
            days_divisor=26,
            hours_per_day=Decimal("8.00"),
            minimum_minutes=30,
            rounding_minutes=30,
            effective_from=date(2026, 4, 1),
        ),
    )
    shift = Shift(
        tenant_id=org["tenant"].id,
        code="NIGHT",
        name="Night",
        start_time=time(22, 0),
        end_time=time(6, 0),
        is_night_shift=True,
        weekly_off_days=[7],
        allowance_per_day=Decimal("150.00"),
        allowance_name="Night shift allowance",
    )
    session.add(shift)
    await session.flush()
    session.add_all(
        [
            _day(org, org["meera_emp"].id, date(2026, 4, 7), overtime=95),
            _day(org, org["meera_emp"].id, date(2026, 4, 8), overtime=20),  # below the minimum
            _day(org, org["ravi_emp"].id, date(2026, 4, 9), overtime=0, shift_id=shift.id),
            _day(org, org["ravi_emp"].id, date(2026, 4, 10), overtime=0, shift_id=shift.id),
        ]
    )
    await session.commit()

    result = await overtime_service.sync(
        session, org["maker"], start=date(2026, 4, 1), end=date(2026, 4, 30)
    )
    assert result.created == 1, "20 minutes is below the policy's minimum"

    entry = await session.scalar(select(OvertimeEntry))
    assert entry.eligible_minutes == 90  # 95 rounded down to the 30-minute block

    # The employee who worked it cannot approve it.
    with pytest.raises(HTTPException) as exc:
        await overtime_service.decide(session, org["meera"], entry.id, approve_it=True)
    assert exc.value.status_code == 403

    decided = await overtime_service.decide(session, org["maker"], entry.id, approve_it=True)
    # BASIC 40,000 / 26 / 8 = 192.3077 an hour; double rate; 1.5 hours.
    assert decided.amount == Decimal("576.92")
    assert decided.status is OvertimeStatus.APPROVED

    run = await _approve_month(session, org, 4)
    meera_slip = await _slip(session, run.id, org["meera_emp"].id)
    overtime_lines = [line for line in meera_slip.input_lines if line["code"] == "OVERTIME"]
    assert [Decimal(line["amount"]) for line in overtime_lines] == [Decimal("576.92")]
    assert overtime_lines[0]["counts_as_wages"] is True

    ravi_slip = await _slip(session, run.id, org["ravi_emp"].id)
    allowance = [line for line in ravi_slip.input_lines if line["code"] == "SHIFT_ALLOWANCE"]
    assert [Decimal(line["amount"]) for line in allowance] == [Decimal("300.00")]

    await session.refresh(entry)
    assert entry.status is OvertimeStatus.PAID and entry.paid_in_run_id == run.id

    # Paid once: May's run pays neither again.
    may = await _approve_month(session, org, 5)
    may_slip = await _slip(session, may.id, org["meera_emp"].id)
    assert not [line for line in may_slip.input_lines if line["code"] == "OVERTIME"]
    assert (await session.scalar(select(PayrollAdjustmentApplication.id).limit(1))) is None


# ═══════════════════════════════════════════════════════════════════════════
# The legal ceiling on deductions
# ═══════════════════════════════════════════════════════════════════════════


async def test_deductions_past_the_legal_ceiling_need_an_acknowledgement(
    session: AsyncSession, org
) -> None:
    from app.schemas.payroll_run import RuleSetCreate
    from app.services.payroll import rule_sets as rule_set_service

    await _approved_rules(session, org)
    ceiling = await rule_set_service.create_rule_set(
        session,
        org["maker"],
        RuleSetCreate(
            rule_type="wage_deductions",
            jurisdiction="IN",
            effective_from=date(2026, 4, 1),
            rules={"max_deduction_percent": "50"},
            source_reference="Test fixture, not a statutory source",
        ),
    )
    await rule_set_service.approve_rule_set(session, org["checker"], ceiling.id, attest=True)

    attachment = await adjustment_service.create(
        session,
        org["checker"],
        AdjustmentCreate(
            employee_id=org["ravi_emp"].id,
            category=AdjustmentCategory.COURT_ATTACHMENT,
            name="Attachment order",
            amount=Decimal("60000"),
            within_net_pay=False,
            start_year=2026,
            start_month=4,
            reference="OS 9/2026, City Civil Court",
            reason="Decree for recovery",
        ),
    )
    await adjustment_service.approve(session, org["maker"], attachment.id)

    session.add(_lock(org["tenant"], org["maker"], 2026, 4))
    await session.commit()
    run = await _open_run(session, org, 4)
    run = await run_service.compute_run(session, org["maker"], run.id)

    flags = [f for f in run.variance_flags if f["employee_id"] == str(org["ravi_emp"].id)]
    assert len(flags) == 1
    assert flags[0]["change_percent"] is None
    assert flags[0]["deduction_ceiling"]["ceiling_percent"] == "50"
    assert Decimal(flags[0]["deduction_ceiling"]["share_percent"]) > Decimal(50)

    with pytest.raises(HTTPException) as exc:
        await run_service.approve_run(session, org["checker"], run.id)
    assert exc.value.status_code == 409

    await run_service.acknowledge_variance(
        session,
        org["checker"],
        run.id,
        org["ravi_emp"].id,
        note="Court order served; the decree overrides the ceiling for this month.",
    )
    approved = await run_service.approve_run(session, org["checker"], run.id)
    assert approved.status is PayrollRunStatus.APPROVED


async def test_a_week_past_48_hours_is_weekly_overtime(session: AsyncSession, org) -> None:
    """Six 9-hour days on a 9-hour shift: no day runs over, the week does (54 > 48)."""
    await overtime_service.create_policy(
        session,
        org["maker"],
        OvertimePolicyCreate(
            name="Factory",
            rate_multiplier=Decimal("2.00"),
            wage_component_codes=["BASIC"],
            days_divisor=26,
            hours_per_day=Decimal("8.00"),
            weekly_threshold_minutes=2880,
            week_starts_on=6,
            effective_from=date(2026, 4, 1),
        ),
    )
    for offset in range(6):  # Monday 6 to Saturday 11 April; Sunday 5 is off
        session.add(
            AttendanceDay(
                tenant_id=org["tenant"].id,
                employee_id=org["meera_emp"].id,
                attendance_date=date(2026, 4, 6 + offset),
                worked_minutes=540,
                status=AttendanceStatus.PRESENT,
                payable_fraction=Decimal(1),
                overtime_minutes=0,
                computed_at=datetime.now(UTC),
            )
        )
    await session.commit()

    result = await overtime_service.sync(
        session, org["maker"], start=date(2026, 4, 1), end=date(2026, 4, 30)
    )
    assert result.created == 1
    entry = await session.scalar(select(OvertimeEntry))
    assert entry.kind == "weekly"
    assert entry.attendance_date == date(2026, 4, 11)
    assert entry.eligible_minutes == 360

    # A second sync finds nothing new to do.
    again = await overtime_service.sync(
        session, org["maker"], start=date(2026, 4, 1), end=date(2026, 4, 30)
    )
    assert (again.created, again.updated, again.removed) == (0, 0, 0)

    decided = await overtime_service.decide(session, org["checker"], entry.id, approve_it=True)
    assert decided.status is OvertimeStatus.APPROVED
    assert "weekly limit of 48 h" in decided.basis
