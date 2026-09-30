"""Slice 31 — expense policy, travel and advances against a real database.

The checklist gates:

- policy feedback appears at capture, not at approval
- per-trip aggregation correct across multiple claims
- unsettled advance genuinely blocks new advances
- currency conversion date convention configurable and recorded

And the prompt's own test list: all three violation actions, per-day and
per-trip aggregation, per-diem with no receipt, settlement against the trip with
the balance handed to payroll, and both amounts stored on a foreign claim.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.employee import Employee, EmploymentStatus
from app.models.expenses import (
    AdvanceSettlement,
    AdvanceStatusEnum,
    CityTierEnum,
    ClaimKindEnum,
    ConversionConventionEnum,
    ExpenseClaim,
    ExpenseClaimStatusEnum,
    ExpensePolicyViolation,
    LimitTypeEnum,
    PolicyVerdictEnum,
    SettlementOutcomeEnum,
    ViolationActionEnum,
)
from app.services.expenses import service as expenses
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

D = Decimal
TODAY = date(2026, 9, 7)


# ── Helpers ─────────────────────────────────────────────────────────────────


def hotel_rule(action: ViolationActionEnum, **kw) -> dict:
    spec = {
        "category_key": "hotel",
        "city_tier": CityTierEnum.A,
        "limit_type": LimitTypeEnum.PER_CLAIM,
        "limit_amount": D("8000"),
        "violation_action": action,
        "requires_receipt": True,
        "receipt_threshold": D("0"),
    }
    spec.update(kw)
    return spec


async def make_policy(session, user, rules: list[dict]):
    return await expenses.create_policy(
        session,
        user,
        name=f"Travel {uuid.uuid4().hex[:6]}",
        effective_from=date(2026, 4, 1),
        rules=rules,
    )


async def capture(session, user, employee, **kw):
    body = {
        "employee_id": employee.id,
        "category_key": "hotel",
        "city_tier": CityTierEnum.A,
        "amount": D("5000"),
        "incurred_on": TODAY,
        "has_receipt": True,
    }
    body.update(kw)
    return await expenses.capture_claim(session, user, **body)


# ══════════════════════════════════════════════════════════════════════════════
# Gate: policy feedback at capture, not at approval
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_verdict_comes_back_in_the_capture_response(
    session, hr_user, employee
):
    """The whole point of the slice.

    An employee who learns three weeks later that their claim was out of policy
    stops claiming properly, and finance loses the only signal it had about
    where its limits are wrong.
    """
    await make_policy(session, hr_user, [hotel_rule(ViolationActionEnum.FLAG)])

    out = await capture(session, hr_user, employee, amount=D("9500"))

    assert out["verdict"]["kind"] == "flagged"
    assert out["verdict"]["limit_amount"] == "8000.00"
    assert out["verdict"]["overage"] == "1500.00"
    assert out["may_submit"] is True
    # And it is on the row, not recomputed later.
    claim = (
        await session.execute(
            select(ExpenseClaim).where(ExpenseClaim.id == uuid.UUID(out["claim_id"]))
        )
    ).scalar_one()
    assert claim.policy_verdict is PolicyVerdictEnum.FLAGGED
    assert claim.evaluated_at is not None
    assert claim.status is ExpenseClaimStatusEnum.DRAFT


async def test_a_verdict_survives_the_limit_being_tightened_afterwards(
    session, hr_user, employee
):
    """A March claim must keep reading as having been judged against March.

    Recomputing at submission would turn a claim the employee was told was fine
    into a breach the approver never saw.
    """
    await make_policy(session, hr_user, [hotel_rule(ViolationActionEnum.FLAG)])
    out = await capture(session, hr_user, employee, amount=D("7000"))
    assert out["verdict"]["kind"] == "within_policy"

    # A much tighter policy, effective earlier, is added afterwards.
    await expenses.create_policy(
        session,
        hr_user,
        name="Austerity",
        effective_from=date(2026, 9, 1),
        rules=[hotel_rule(ViolationActionEnum.BLOCK, limit_amount=D("1000"))],
    )

    claim = await expenses.submit_claim(
        session, hr_user, claim_id=uuid.UUID(out["claim_id"])
    )
    assert claim.status is ExpenseClaimStatusEnum.SUBMITTED
    assert claim.policy_verdict is PolicyVerdictEnum.WITHIN_POLICY


# ══════════════════════════════════════════════════════════════════════════════
# Gate: all three violation actions
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_blocked_claim_cannot_be_submitted(session, hr_user, employee):
    await make_policy(session, hr_user, [hotel_rule(ViolationActionEnum.BLOCK)])
    out = await capture(session, hr_user, employee, amount=D("12000"))

    assert out["verdict"]["kind"] == "blocked"
    assert out["may_submit"] is False

    with pytest.raises(HTTPException) as exc:
        await expenses.submit_claim(
            session, hr_user, claim_id=uuid.UUID(out["claim_id"])
        )
    assert exc.value.status_code == 400
    assert "cannot be submitted" in exc.value.detail


async def test_a_blocked_claim_is_still_stored(session, hr_user, employee):
    """Throwing it away discards the strongest signal finance has about where a
    limit is wrong."""
    await make_policy(session, hr_user, [hotel_rule(ViolationActionEnum.BLOCK)])
    out = await capture(session, hr_user, employee, amount=D("12000"))

    claim = (
        await session.execute(
            select(ExpenseClaim).where(ExpenseClaim.id == uuid.UUID(out["claim_id"]))
        )
    ).scalar_one()
    assert claim.status is ExpenseClaimStatusEnum.DRAFT
    violation = (
        await session.execute(
            select(ExpensePolicyViolation).where(
                ExpensePolicyViolation.claim_id == claim.id
            )
        )
    ).scalar_one()
    assert violation.violation_action is ViolationActionEnum.BLOCK
    assert violation.overage == D("4000.00")


async def test_a_flagged_claim_submits_and_the_approver_sees_the_flag(
    session, hr_user, employee
):
    await make_policy(session, hr_user, [hotel_rule(ViolationActionEnum.FLAG)])
    out = await capture(session, hr_user, employee, amount=D("12000"))

    claim = await expenses.submit_claim(
        session, hr_user, claim_id=uuid.UUID(out["claim_id"])
    )
    assert claim.status is ExpenseClaimStatusEnum.SUBMITTED
    assert claim.flagged_for_approver is True


async def test_require_justification_needs_a_written_reason(
    session, hr_user, employee
):
    await make_policy(
        session, hr_user, [hotel_rule(ViolationActionEnum.REQUIRE_JUSTIFICATION)]
    )
    out = await capture(session, hr_user, employee, amount=D("12000"))
    assert out["verdict"]["kind"] == "needs_justification"
    assert out["may_submit"] is False

    with pytest.raises(HTTPException) as exc:
        await expenses.submit_claim(
            session, hr_user, claim_id=uuid.UUID(out["claim_id"])
        )
    assert "written reason" in exc.value.detail

    claim = await expenses.submit_claim(
        session,
        hr_user,
        claim_id=uuid.UUID(out["claim_id"]),
        justification="Conference hotel; the only room left in the block.",
    )
    assert claim.status is ExpenseClaimStatusEnum.SUBMITTED
    assert "Conference hotel" in claim.justification


async def test_whitespace_does_not_satisfy_a_justification(
    session, hr_user, employee
):
    await make_policy(
        session, hr_user, [hotel_rule(ViolationActionEnum.REQUIRE_JUSTIFICATION)]
    )
    out = await capture(session, hr_user, employee, amount=D("12000"))

    with pytest.raises(HTTPException):
        await expenses.submit_claim(
            session,
            hr_user,
            claim_id=uuid.UUID(out["claim_id"]),
            justification="    ",
        )


# ══════════════════════════════════════════════════════════════════════════════
# Gate: per-trip and per-day aggregation across claims
# ══════════════════════════════════════════════════════════════════════════════


async def test_per_trip_aggregation_across_several_claims(
    session, hr_user, employee
):
    """Four ₹3,000 nights on one trip breach a ₹10,000 trip limit.

    Every claim passes on its own, which is exactly the bug.
    """
    await make_policy(
        session,
        hr_user,
        [
            hotel_rule(
                ViolationActionEnum.FLAG,
                limit_type=LimitTypeEnum.PER_TRIP,
                limit_amount=D("10000"),
            )
        ],
    )
    trip = await expenses.create_travel_request(
        session,
        hr_user,
        employee_id=employee.id,
        purpose="Client workshop",
        from_date=date(2026, 9, 1),
        to_date=date(2026, 9, 5),
    )
    trip = await expenses.decide_travel_request(
        session, hr_user, travel_request_id=trip.id, approve=True
    )

    verdicts = []
    for day in (1, 2, 3, 4):
        out = await capture(
            session,
            hr_user,
            employee,
            amount=D("3000"),
            incurred_on=date(2026, 9, day),
            travel_request_id=trip.id,
        )
        verdicts.append(out["verdict"])

    assert [v["kind"] for v in verdicts[:3]] == ["within_policy"] * 3
    assert verdicts[3]["kind"] == "flagged"
    assert verdicts[3]["already_claimed"] == "9000.00"
    assert verdicts[3]["overage"] == "2000.00"


async def test_another_trips_claims_do_not_consume_this_trips_limit(
    session, hr_user, employee
):
    await make_policy(
        session,
        hr_user,
        [
            hotel_rule(
                ViolationActionEnum.FLAG,
                limit_type=LimitTypeEnum.PER_TRIP,
                limit_amount=D("10000"),
            )
        ],
    )
    trips = []
    for name in ("Trip one", "Trip two"):
        t = await expenses.create_travel_request(
            session,
            hr_user,
            employee_id=employee.id,
            purpose=name,
            from_date=date(2026, 9, 1),
            to_date=date(2026, 9, 5),
        )
        trips.append(
            await expenses.decide_travel_request(
                session, hr_user, travel_request_id=t.id, approve=True
            )
        )

    for _ in range(3):
        await capture(
            session,
            hr_user,
            employee,
            amount=D("3000"),
            travel_request_id=trips[0].id,
        )

    out = await capture(
        session, hr_user, employee, amount=D("3000"), travel_request_id=trips[1].id
    )
    assert out["verdict"]["already_claimed"] == "0.00"
    assert out["verdict"]["kind"] == "within_policy"


async def test_per_day_aggregation_counts_only_that_day(
    session, hr_user, employee
):
    await make_policy(
        session,
        hr_user,
        [
            hotel_rule(
                ViolationActionEnum.FLAG,
                category_key="meals",
                limit_type=LimitTypeEnum.PER_DAY,
                limit_amount=D("1200"),
            )
        ],
    )
    await capture(
        session,
        hr_user,
        employee,
        category_key="meals",
        amount=D("800"),
        incurred_on=date(2026, 9, 3),
    )
    other_day = await capture(
        session,
        hr_user,
        employee,
        category_key="meals",
        amount=D("800"),
        incurred_on=date(2026, 9, 4),
    )
    assert other_day["verdict"]["already_claimed"] == "0.00"

    same_day = await capture(
        session,
        hr_user,
        employee,
        category_key="meals",
        amount=D("800"),
        incurred_on=date(2026, 9, 3),
    )
    assert same_day["verdict"]["already_claimed"] == "800.00"
    assert same_day["verdict"]["overage"] == "400.00"


async def test_a_rejected_claim_stops_consuming_the_limit(
    session, hr_user, employee
):
    """A claim the company refused is not spend.

    Counting it would consume a limit the employee never used.
    """
    await make_policy(
        session,
        hr_user,
        [
            hotel_rule(
                ViolationActionEnum.FLAG,
                limit_type=LimitTypeEnum.PER_MONTH,
                limit_amount=D("10000"),
            )
        ],
    )
    first = await capture(session, hr_user, employee, amount=D("6000"))
    await expenses.submit_claim(session, hr_user, claim_id=uuid.UUID(first["claim_id"]))
    await expenses.decide_claim(
        session, hr_user, claim_id=uuid.UUID(first["claim_id"]), approve=False
    )

    out = await capture(session, hr_user, employee, amount=D("6000"))
    assert out["verdict"]["already_claimed"] == "0.00"
    assert out["verdict"]["kind"] == "within_policy"


# ══════════════════════════════════════════════════════════════════════════════
# Gate: a per-diem needs no receipt
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_per_diem_claim_needs_no_receipt(
    session, hr_user, employee, tenant
):
    employee.grade = "M3"
    await session.commit()

    await expenses.set_per_diem_rate(
        session,
        hr_user,
        city_tier=CityTierEnum.B,
        grade="M3",
        amount=D("1500"),
        effective_from=date(2026, 4, 1),
    )
    await make_policy(
        session,
        hr_user,
        [
            hotel_rule(
                ViolationActionEnum.BLOCK,
                category_key="per_diem",
                city_tier=CityTierEnum.B,
                limit_amount=D("10000"),
                requires_receipt=True,
                is_per_diem=True,
            )
        ],
    )

    out = await capture(
        session,
        hr_user,
        employee,
        category_key="per_diem",
        city_tier=CityTierEnum.B,
        kind=ClaimKindEnum.PER_DIEM,
        per_diem_days=3,
        amount=D("1"),
        has_receipt=False,
    )
    assert out["verdict"]["kind"] == "within_policy"
    assert out["amount"] == "4500.00"
    assert out["may_submit"] is True


async def test_a_receipted_claim_without_a_receipt_stops(
    session, hr_user, employee
):
    await make_policy(
        session,
        hr_user,
        [hotel_rule(ViolationActionEnum.FLAG, receipt_threshold=D("300"))],
    )
    out = await capture(session, hr_user, employee, amount=D("5000"), has_receipt=False)
    assert out["verdict"]["kind"] == "receipt_required"
    assert out["may_submit"] is False

    small = await capture(
        session,
        hr_user,
        employee,
        amount=D("120"),
        has_receipt=False,
    )
    assert small["verdict"]["kind"] == "within_policy"


# ══════════════════════════════════════════════════════════════════════════════
# Mileage
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_mileage_claim_is_valued_and_flagged_not_refused(
    session, hr_user, employee
):
    await expenses.set_mileage_rate(
        session,
        hr_user,
        vehicle_type="car",
        rate_per_km=D("12"),
        effective_from=date(2026, 4, 1),
    )
    await make_policy(
        session,
        hr_user,
        [
            hotel_rule(
                ViolationActionEnum.FLAG,
                category_key="mileage",
                limit_amount=D("5000"),
                requires_receipt=False,
            )
        ],
    )
    out = await capture(
        session,
        hr_user,
        employee,
        category_key="mileage",
        kind=ClaimKindEnum.MILEAGE,
        amount=D("1"),
        distance_km=D("140"),
        route_km=D("110"),
        vehicle_type="car",
        has_receipt=False,
    )
    assert out["amount"] == "1680.00"
    assert out["mileage"]["exceeds_tolerance"] is True
    assert out["may_submit"] is True

    claim = (
        await session.execute(
            select(ExpenseClaim).where(ExpenseClaim.id == uuid.UUID(out["claim_id"]))
        )
    ).scalar_one()
    assert claim.exceeds_route_tolerance is True


async def test_a_march_claim_keeps_the_march_mileage_rate(
    session, hr_user, employee
):
    await expenses.set_mileage_rate(
        session,
        hr_user,
        vehicle_type="car",
        rate_per_km=D("10"),
        effective_from=date(2026, 3, 1),
    )
    await expenses.set_mileage_rate(
        session,
        hr_user,
        vehicle_type="car",
        rate_per_km=D("12"),
        effective_from=date(2026, 4, 1),
    )
    out = await capture(
        session,
        hr_user,
        employee,
        category_key="mileage",
        kind=ClaimKindEnum.MILEAGE,
        amount=D("1"),
        incurred_on=date(2026, 3, 20),
        distance_km=D("100"),
        vehicle_type="car",
        has_receipt=False,
    )
    assert out["amount"] == "1000.00"


# ══════════════════════════════════════════════════════════════════════════════
# Gate: currency convention configurable and recorded
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_foreign_claim_stores_both_amounts_and_the_convention(
    session, hr_user, employee
):
    await expenses.record_exchange_rate(
        session,
        hr_user,
        from_currency="EUR",
        rate=D("94.20"),
        quote_date=date(2026, 9, 3),
        source="RBI reference rate",
    )
    out = await capture(
        session,
        hr_user,
        employee,
        amount=D("80"),
        currency="EUR",
        incurred_on=date(2026, 9, 3),
    )
    assert out["original_amount"] == "80.00"
    assert out["original_currency"] == "EUR"
    assert out["amount"] == "7536.00"

    claim = (
        await session.execute(
            select(ExpenseClaim).where(ExpenseClaim.id == uuid.UUID(out["claim_id"]))
        )
    ).scalar_one()
    assert claim.exchange_rate == D("94.200000")
    assert claim.exchange_rate_date == date(2026, 9, 3)
    assert claim.conversion_convention is ConversionConventionEnum.TRANSACTION_DATE
    assert claim.exchange_rate_source == "RBI reference rate"


async def test_the_settlement_convention_uses_a_different_date(
    session, hr_user, employee
):
    """Both conventions are in use, so it is a setting rather than an
    assumption — and the difference on a moving rupee is real money."""
    await expenses.set_settings(
        session,
        hr_user,
        conversion_convention=ConversionConventionEnum.SETTLEMENT_DATE,
    )
    for quote_date, rate in (
        (date(2026, 9, 3), D("94.20")),
        (date(2026, 9, 20), D("97.50")),
    ):
        await expenses.record_exchange_rate(
            session,
            hr_user,
            from_currency="EUR",
            rate=rate,
            quote_date=quote_date,
            source="RBI reference rate",
        )

    out = await capture(
        session,
        hr_user,
        employee,
        amount=D("80"),
        currency="EUR",
        incurred_on=date(2026, 9, 3),
        settlement_date=date(2026, 9, 20),
    )
    assert out["amount"] == "7800.00"
    assert out["conversion"]["rate_date"] == "2026-09-20"
    assert out["conversion"]["convention"] == "settlement_date"


async def test_a_settlement_convention_with_no_settlement_date_refuses(
    session, hr_user, employee
):
    await expenses.set_settings(
        session,
        hr_user,
        conversion_convention=ConversionConventionEnum.SETTLEMENT_DATE,
    )
    with pytest.raises(HTTPException) as exc:
        await capture(
            session,
            hr_user,
            employee,
            amount=D("80"),
            currency="EUR",
            incurred_on=date(2026, 9, 3),
        )
    assert "convention nobody chose" in exc.value.detail


async def test_a_missing_rate_refuses_rather_than_guessing(
    session, hr_user, employee
):
    with pytest.raises(HTTPException) as exc:
        await capture(
            session,
            hr_user,
            employee,
            amount=D("80"),
            currency="EUR",
            incurred_on=date(2026, 9, 3),
        )
    assert "no source stands behind" in exc.value.detail


# ══════════════════════════════════════════════════════════════════════════════
# Gate: an unsettled advance blocks a new one
# ══════════════════════════════════════════════════════════════════════════════


async def approved_trip(session, user, employee, purpose="Client visit"):
    trip = await expenses.create_travel_request(
        session,
        user,
        employee_id=employee.id,
        purpose=purpose,
        from_date=date(2026, 3, 2),
        to_date=date(2026, 3, 6),
        advance_requested=D("40000"),
    )
    return await expenses.decide_travel_request(
        session, user, travel_request_id=trip.id, approve=True
    )


async def test_an_unsettled_advance_blocks_the_next_one(
    session, hr_user, employee
):
    """The control finance actually asks for.

    ₹40,000 in March with no claims, another advance in June, and the company is
    ₹80,000 out to one person with nothing against either amount.
    """
    first = await approved_trip(session, hr_user, employee, "March trip")
    await expenses.issue_advance(
        session,
        hr_user,
        travel_request_id=first.id,
        amount=D("40000"),
        issued_on=date(2026, 3, 1),
    )

    second = await approved_trip(session, hr_user, employee, "June trip")
    with pytest.raises(HTTPException) as exc:
        await expenses.issue_advance(
            session,
            hr_user,
            travel_request_id=second.id,
            amount=D("25000"),
            issued_on=date(2026, 6, 1),
        )
    assert exc.value.status_code == 409
    assert "2026-03-01" in exc.value.detail
    assert str(first.id) in exc.value.detail
    assert "Submit the claims" in exc.value.detail


async def test_a_recent_advance_does_not_block(session, hr_user, employee):
    first = await approved_trip(session, hr_user, employee, "Last week")
    await expenses.issue_advance(
        session,
        hr_user,
        travel_request_id=first.id,
        amount=D("40000"),
        issued_on=date(2026, 3, 1),
    )
    second = await approved_trip(session, hr_user, employee, "This week")
    row = await expenses.issue_advance(
        session,
        hr_user,
        travel_request_id=second.id,
        amount=D("10000"),
        issued_on=date(2026, 3, 20),
    )
    assert row.status is AdvanceStatusEnum.ISSUED


async def test_the_block_threshold_is_configurable(session, hr_user, employee):
    await expenses.set_settings(session, hr_user, advance_block_after_days=5)
    first = await approved_trip(session, hr_user, employee, "Early March")
    await expenses.issue_advance(
        session,
        hr_user,
        travel_request_id=first.id,
        amount=D("40000"),
        issued_on=date(2026, 3, 1),
    )
    second = await approved_trip(session, hr_user, employee, "Mid March")
    with pytest.raises(HTTPException) as exc:
        await expenses.issue_advance(
            session,
            hr_user,
            travel_request_id=second.id,
            amount=D("10000"),
            issued_on=date(2026, 3, 20),
        )
    assert exc.value.status_code == 409


async def test_an_advance_needs_an_approved_trip(session, hr_user, employee):
    trip = await expenses.create_travel_request(
        session,
        hr_user,
        employee_id=employee.id,
        purpose="Unapproved",
        from_date=date(2026, 3, 2),
        to_date=date(2026, 3, 6),
    )
    with pytest.raises(HTTPException) as exc:
        await expenses.issue_advance(
            session,
            hr_user,
            travel_request_id=trip.id,
            amount=D("10000"),
            issued_on=date(2026, 3, 1),
        )
    assert "money out with nothing authorising it" in exc.value.detail


# ══════════════════════════════════════════════════════════════════════════════
# Settlement
# ══════════════════════════════════════════════════════════════════════════════


async def test_an_under_spent_advance_leaves_a_recovery_for_payroll(
    session, hr_user, employee
):
    """Recorded, not netted. Nothing in payroll picks it up yet, so the balance
    stays visible and unrecovered rather than being quietly written off."""
    await make_policy(
        session, hr_user, [hotel_rule(ViolationActionEnum.FLAG, limit_amount=D("50000"))]
    )
    trip = await approved_trip(session, hr_user, employee)
    advance = await expenses.issue_advance(
        session,
        hr_user,
        travel_request_id=trip.id,
        amount=D("40000"),
        issued_on=date(2026, 3, 1),
    )

    out = await capture(
        session,
        hr_user,
        employee,
        amount=D("31500"),
        incurred_on=date(2026, 3, 3),
        travel_request_id=trip.id,
    )
    await expenses.submit_claim(session, hr_user, claim_id=uuid.UUID(out["claim_id"]))
    await expenses.decide_claim(
        session, hr_user, claim_id=uuid.UUID(out["claim_id"]), approve=True
    )

    result = await expenses.settle_advance(
        session, hr_user, advance_id=advance.id, settled_on=date(2026, 3, 15)
    )
    assert result["outcome"] == "recovery_due_from_employee"
    assert result["recovery_amount"] == "8500.00"
    assert result["payroll_recovery"]["required"] is True
    assert result["payroll_recovery"]["reference"] is None

    row = (
        await session.execute(
            select(AdvanceSettlement).where(AdvanceSettlement.advance_id == advance.id)
        )
    ).scalar_one()
    assert row.outcome is SettlementOutcomeEnum.RECOVERY_DUE_FROM_EMPLOYEE
    assert row.payroll_recovery_ref is None


async def test_an_over_spent_advance_refunds_the_employee(
    session, hr_user, employee
):
    await make_policy(
        session, hr_user, [hotel_rule(ViolationActionEnum.FLAG, limit_amount=D("50000"))]
    )
    trip = await approved_trip(session, hr_user, employee)
    advance = await expenses.issue_advance(
        session,
        hr_user,
        travel_request_id=trip.id,
        amount=D("40000"),
        issued_on=date(2026, 3, 1),
    )
    out = await capture(
        session,
        hr_user,
        employee,
        amount=D("44000"),
        incurred_on=date(2026, 3, 3),
        travel_request_id=trip.id,
    )
    await expenses.submit_claim(session, hr_user, claim_id=uuid.UUID(out["claim_id"]))
    await expenses.decide_claim(
        session, hr_user, claim_id=uuid.UUID(out["claim_id"]), approve=True
    )

    result = await expenses.settle_advance(
        session, hr_user, advance_id=advance.id, settled_on=date(2026, 3, 15)
    )
    assert result["outcome"] == "refund_due_to_employee"
    assert result["refund_amount"] == "4000.00"
    assert result["payroll_recovery"] is None


async def test_another_trips_claims_do_not_settle_this_advance(
    session, hr_user, employee
):
    """The service selects claims by the advance's own trip, so a claim on
    another trip cannot reach the settlement at all."""
    await make_policy(
        session, hr_user, [hotel_rule(ViolationActionEnum.FLAG, limit_amount=D("50000"))]
    )
    trip = await approved_trip(session, hr_user, employee, "The funded trip")
    other = await approved_trip(session, hr_user, employee, "Somewhere else")
    advance = await expenses.issue_advance(
        session,
        hr_user,
        travel_request_id=trip.id,
        amount=D("40000"),
        issued_on=date(2026, 3, 1),
    )

    out = await capture(
        session,
        hr_user,
        employee,
        amount=D("40000"),
        incurred_on=date(2026, 3, 3),
        travel_request_id=other.id,
    )
    await expenses.submit_claim(session, hr_user, claim_id=uuid.UUID(out["claim_id"]))
    await expenses.decide_claim(
        session, hr_user, claim_id=uuid.UUID(out["claim_id"]), approve=True
    )

    result = await expenses.settle_advance(
        session, hr_user, advance_id=advance.id, settled_on=date(2026, 3, 15)
    )
    assert result["claim_count"] == 0
    assert result["outcome"] == "recovery_due_from_employee"
    assert result["recovery_amount"] == "40000.00"


async def test_a_settled_advance_stops_blocking(session, hr_user, employee):
    await make_policy(
        session, hr_user, [hotel_rule(ViolationActionEnum.FLAG, limit_amount=D("50000"))]
    )
    trip = await approved_trip(session, hr_user, employee, "March trip")
    advance = await expenses.issue_advance(
        session,
        hr_user,
        travel_request_id=trip.id,
        amount=D("40000"),
        issued_on=date(2026, 3, 1),
    )
    out = await capture(
        session,
        hr_user,
        employee,
        amount=D("40000"),
        incurred_on=date(2026, 3, 3),
        travel_request_id=trip.id,
    )
    await expenses.submit_claim(session, hr_user, claim_id=uuid.UUID(out["claim_id"]))
    await expenses.decide_claim(
        session, hr_user, claim_id=uuid.UUID(out["claim_id"]), approve=True
    )
    await expenses.settle_advance(
        session, hr_user, advance_id=advance.id, settled_on=date(2026, 3, 15)
    )

    second = await approved_trip(session, hr_user, employee, "June trip")
    row = await expenses.issue_advance(
        session,
        hr_user,
        travel_request_id=second.id,
        amount=D("25000"),
        issued_on=date(2026, 6, 1),
    )
    assert row.status is AdvanceStatusEnum.ISSUED


async def test_an_advance_cannot_be_settled_twice(session, hr_user, employee):
    trip = await approved_trip(session, hr_user, employee)
    advance = await expenses.issue_advance(
        session,
        hr_user,
        travel_request_id=trip.id,
        amount=D("40000"),
        issued_on=date(2026, 3, 1),
    )
    await expenses.settle_advance(
        session, hr_user, advance_id=advance.id, settled_on=date(2026, 3, 15)
    )
    with pytest.raises(HTTPException) as exc:
        await expenses.settle_advance(
            session, hr_user, advance_id=advance.id, settled_on=date(2026, 3, 20)
        )
    assert "twice" in exc.value.detail


async def test_the_ageing_report_buckets_outstanding_advances(
    session, hr_user, employee
):
    trip = await approved_trip(session, hr_user, employee)
    await expenses.issue_advance(
        session,
        hr_user,
        travel_request_id=trip.id,
        amount=D("40000"),
        issued_on=date(2026, 3, 1),
    )
    report = await expenses.advance_ageing(session, hr_user, as_of=TODAY)
    assert report["buckets"]["over_90"]["count"] == 1
    assert report["blocking_count"] == 1
    assert report["total_outstanding"] == "40000.00"


# ══════════════════════════════════════════════════════════════════════════════
# Violation rates
# ══════════════════════════════════════════════════════════════════════════════


async def test_violation_rates_group_by_scope(session, hr_user, employee):
    await make_policy(session, hr_user, [hotel_rule(ViolationActionEnum.FLAG)])
    for _ in range(8):
        await capture(session, hr_user, employee, amount=D("2000"))
    for _ in range(4):
        await capture(session, hr_user, employee, amount=D("12000"))

    report = await expenses.violation_rates(session, hr_user, scope="employee")
    group = next(g for g in report["groups"] if g["key"] == str(employee.id))
    assert group["total_claims"] == 12
    assert group["violations"] == 4
    assert group["rate"] == pytest.approx(0.333, abs=0.001)


async def test_an_unknown_scope_is_refused(session, hr_user):
    with pytest.raises(HTTPException):
        await expenses.violation_rates(session, hr_user, scope="astrology")


# ══════════════════════════════════════════════════════════════════════════════
# Tenant isolation
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_claim_is_invisible_to_another_tenant(
    session, hr_user, other_hr_user, employee
):
    await make_policy(session, hr_user, [hotel_rule(ViolationActionEnum.FLAG)])
    out = await capture(session, hr_user, employee, amount=D("5000"))

    with pytest.raises(HTTPException) as exc:
        await expenses.submit_claim(
            session, other_hr_user, claim_id=uuid.UUID(out["claim_id"])
        )
    assert exc.value.status_code == 404


async def test_an_advance_is_invisible_to_another_tenant(
    session, hr_user, other_hr_user, employee
):
    trip = await approved_trip(session, hr_user, employee)
    advance = await expenses.issue_advance(
        session,
        hr_user,
        travel_request_id=trip.id,
        amount=D("40000"),
        issued_on=date(2026, 3, 1),
    )
    with pytest.raises(HTTPException) as exc:
        await expenses.settle_advance(
            session, other_hr_user, advance_id=advance.id, settled_on=TODAY
        )
    assert exc.value.status_code == 404


async def test_another_tenants_advances_do_not_block_this_ones_employee(
    session, hr_user, other_hr_user, other_tenant, employee, legal_entity, location
):
    """Ageing is scoped, so one tenant's overdue advance cannot stop another
    tenant issuing one."""
    trip = await approved_trip(session, hr_user, employee)
    await expenses.issue_advance(
        session,
        hr_user,
        travel_request_id=trip.id,
        amount=D("40000"),
        issued_on=date(2026, 3, 1),
    )

    stranger = Employee(
        tenant_id=other_tenant.id,
        employee_code="GLX001",
        first_name="Sam",
        last_name="Okafor",
        work_email="sam@globex.test",
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2026, 1, 1),
    )
    session.add(stranger)
    await session.commit()

    report = await expenses.advance_ageing(session, other_hr_user, as_of=TODAY)
    assert report["blocking_count"] == 0
    assert report["total_outstanding"] == "0.00"


# ══════════════════════════════════════════════════════════════════════════════
# Posture
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_posture_says_how_payroll_pays_and_recovers():
    """The posture states what actually happens to the money: approved claims
    are paid in the next payroll run, and an under-spent advance is recovered
    there. It must not claim otherwise in either direction."""
    posture = expenses.expense_posture()
    enforced = " ".join(posture["enforced"]).lower()
    not_built = " ".join(posture["not_built"]).lower()
    assert "paid at cost, untaxed, in the next regular payroll run" in enforced
    assert "under-spent advance is recovered in payroll" in enforced
    assert "nothing in payroll picks up" not in not_built
    assert "no receipt ocr" in not_built
    assert "no exchange rate feed" in not_built


async def test_a_claim_on_someone_elses_trip_is_refused(
    session, hr_user, employee, tenant, legal_entity, location
):
    """It would settle their advance."""
    colleague = Employee(
        tenant_id=tenant.id,
        employee_code="ACM002",
        first_name="Ravi",
        last_name="Nair",
        work_email="ravi@acme.test",
        legal_entity_id=legal_entity.id,
        location_id=location.id,
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2026, 1, 1),
    )
    session.add(colleague)
    await session.commit()

    trip = await approved_trip(session, hr_user, colleague)
    with pytest.raises(HTTPException) as exc:
        await capture(
            session, hr_user, employee, amount=D("1000"), travel_request_id=trip.id
        )
    assert "settle their advance" in exc.value.detail
