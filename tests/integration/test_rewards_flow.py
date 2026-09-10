"""Slice 33 — points liability and reward tax against a real database.

The checklist gates:

- points ledger has no update or delete path
- movement report reconciles opening to closing exactly
- historical points not revalued by a rate change
- perquisite flows into TDS at the right threshold
- tax treatment reviewed by a tax advisor, not inferred
- Artbox reconciliation runs monthly and surfaces gaps

And the prompt's own test list: the balance derived across a complex sequence,
expiry reducing the liability without deleting anything, and budget exhaustion
enforcing per configuration.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.employee import Employee, EmploymentStatus
from app.models.rewards import (
    BudgetScopeEnum,
    EnforcementModeEnum,
    FulfilmentRecord,
    PointsBudget,
    PointsLedgerEntry,
    PointsTransactionTypeEnum,
    ReconciliationRun,
    ReconciliationStatusEnum,
    RewardEvent,
    RewardInvoiceLine,
    TaxTreatmentEnum,
)
from app.services.rewards import service as rewards
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

D = Decimal
TODAY = date(2026, 9, 7)


# ── Helpers ─────────────────────────────────────────────────────────────────


async def valued(session, hr_user, points_per_rupee="2", effective=date(2026, 1, 1)):
    return await rewards.set_valuation(
        session,
        hr_user,
        points_per_rupee=D(points_per_rupee),
        effective_from=effective,
    )


async def make_employees(session, tenant, legal_entity, location, n, prefix="R"):
    people = []
    for i in range(n):
        person = Employee(
            tenant_id=tenant.id,
            employee_code=f"{prefix}{i:03d}",
            first_name=f"Person{i}",
            last_name="Test",
            work_email=f"{prefix.lower()}{i}@acme.test",
            legal_entity_id=legal_entity.id,
            location_id=location.id,
            status=EmploymentStatus.ACTIVE,
            date_of_joining=date(2024, 1, 1),
        )
        session.add(person)
        people.append(person)
    await session.commit()
    for p in people:
        await session.refresh(p)
    return people


# ══════════════════════════════════════════════════════════════════════════════
# Gate: the ledger is insert-only
# ══════════════════════════════════════════════════════════════════════════════


async def test_an_award_writes_one_immutable_row(session, hr_user, employee):
    await valued(session, hr_user)
    out = await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=1000, awarded_on=date(2026, 3, 1)
    )
    assert out["monetary_value"] == "500.00"
    assert out["balance_after"] == 1000

    rows = list(
        (
            await session.execute(
                select(PointsLedgerEntry).where(
                    PointsLedgerEntry.employee_id == employee.id
                )
            )
        ).scalars()
    )
    assert len(rows) == 1
    assert rows[0].transaction_type is PointsTransactionTypeEnum.AWARDED


async def test_a_reversal_is_a_new_row_not_a_deletion(session, hr_user, employee):
    """A row that disappears leaves somebody who had 500 points and now has
    none, with nothing on their statement to explain it."""
    await valued(session, hr_user)
    award = await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=500, awarded_on=date(2026, 3, 1)
    )
    await rewards.reverse_entry(
        session,
        hr_user,
        entry_id=uuid.UUID(award["entry_id"]),
        reason="Awarded to the wrong person.",
        occurred_on=date(2026, 3, 5),
    )

    count = (
        await session.execute(
            select(func.count())
            .select_from(PointsLedgerEntry)
            .where(PointsLedgerEntry.employee_id == employee.id)
        )
    ).scalar()
    assert count == 2

    statement = await rewards.employee_statement(
        session,
        hr_user,
        employee_id=employee.id,
        financial_year="2026-27",
        as_of=TODAY,
    )
    assert statement["balance_points"] == 0
    assert {r["type"] for r in statement["rows"]} == {"awarded", "reversed"}


async def test_a_reversal_needs_a_reason(session, hr_user, employee):
    await valued(session, hr_user)
    award = await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=500, awarded_on=date(2026, 3, 1)
    )
    with pytest.raises(HTTPException) as exc:
        await rewards.reverse_entry(
            session,
            hr_user,
            entry_id=uuid.UUID(award["entry_id"]),
            reason="   ",
            occurred_on=TODAY,
        )
    assert "needs a reason" in exc.value.detail


async def test_an_award_cannot_be_reversed_twice(session, hr_user, employee):
    await valued(session, hr_user)
    award = await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=500, awarded_on=date(2026, 3, 1)
    )
    await rewards.reverse_entry(
        session,
        hr_user,
        entry_id=uuid.UUID(award["entry_id"]),
        reason="Wrong person.",
        occurred_on=date(2026, 3, 5),
    )
    with pytest.raises(HTTPException) as exc:
        await rewards.reverse_entry(
            session,
            hr_user,
            entry_id=uuid.UUID(award["entry_id"]),
            reason="Again.",
            occurred_on=date(2026, 3, 6),
        )
    assert "already been reversed" in exc.value.detail


# ══════════════════════════════════════════════════════════════════════════════
# Gate: balance derived across a complex sequence
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_balance_survives_a_complex_sequence(session, hr_user, employee):
    await valued(session, hr_user)
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=1000, awarded_on=date(2026, 3, 1)
    )
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=600, awarded_on=date(2026, 4, 1)
    )
    await rewards.redeem_points(
        session,
        hr_user,
        employee_id=employee.id,
        points=400,
        requested_on=date(2026, 5, 1),
        item_description="Headphones",
    )
    second = await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=200, awarded_on=date(2026, 6, 1)
    )
    await rewards.reverse_entry(
        session,
        hr_user,
        entry_id=uuid.UUID(second["entry_id"]),
        reason="Duplicate.",
        occurred_on=date(2026, 6, 2),
    )

    report = await rewards.liability_report(session, hr_user, as_of=TODAY)
    assert report["liability"]["balance_points"] == 1200
    assert report["chain"]["intact"] is True


async def test_the_chain_check_is_reported_beside_the_liability(
    session, hr_user, employee
):
    await valued(session, hr_user)
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=100, awarded_on=date(2026, 3, 1)
    )
    report = await rewards.liability_report(session, hr_user, as_of=TODAY)
    assert report["chain"]["intact"] is True
    assert report["chain"]["derived_balance"] == 100


# ══════════════════════════════════════════════════════════════════════════════
# Gate: a rate change does not revalue issued points
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_revaluation_does_not_restate_issued_points(
    session, hr_user, employee
):
    """A tenant that awards at Rs 0.50 and revalues to Rs 1.00 has not doubled
    what it owes on points already issued."""
    await valued(session, hr_user, "2", date(2026, 1, 1))
    await rewards.award_points(
        session,
        hr_user,
        employee_id=employee.id,
        points=10000,
        awarded_on=date(2026, 3, 1),
    )
    before = await rewards.liability_report(session, hr_user, as_of=TODAY)
    assert before["liability"]["rupees"] == "5000.00"

    change = await valued(session, hr_user, "1", date(2026, 6, 1))
    assert change["impact"]["liability_after_change"] == "5000.00"
    assert change["impact"]["hypothetical_if_revalued"] == "10000.00"

    after = await rewards.liability_report(session, hr_user, as_of=TODAY)
    assert after["liability"]["rupees"] == "5000.00"


async def test_a_later_award_uses_the_new_rate(session, hr_user, employee):
    await valued(session, hr_user, "2", date(2026, 1, 1))
    await valued(session, hr_user, "1", date(2026, 6, 1))
    out = await rewards.award_points(
        session,
        hr_user,
        employee_id=employee.id,
        points=1000,
        awarded_on=date(2026, 7, 1),
    )
    assert out["monetary_value"] == "1000.00"


async def test_the_liability_mixes_both_rates_correctly(session, hr_user, employee):
    await valued(session, hr_user, "2", date(2026, 1, 1))
    await valued(session, hr_user, "1", date(2026, 6, 1))
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=1000, awarded_on=date(2026, 3, 1)
    )
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=1000, awarded_on=date(2026, 7, 1)
    )
    report = await rewards.liability_report(session, hr_user, as_of=TODAY)
    assert report["liability"]["rupees"] == "1500.00"


async def test_a_redemption_consumes_the_oldest_lot(session, hr_user, employee):
    await valued(session, hr_user, "2", date(2026, 1, 1))
    await valued(session, hr_user, "1", date(2026, 6, 1))
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=1000, awarded_on=date(2026, 3, 1)
    )
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=1000, awarded_on=date(2026, 7, 1)
    )
    out = await rewards.redeem_points(
        session,
        hr_user,
        employee_id=employee.id,
        points=1000,
        requested_on=date(2026, 8, 1),
    )
    assert out["rupee_value"] == "500.00"

    report = await rewards.liability_report(session, hr_user, as_of=TODAY)
    assert report["liability"]["rupees"] == "1000.00"


async def test_awarding_with_no_valuation_is_refused(session, hr_user, employee):
    with pytest.raises(HTTPException) as exc:
        await rewards.award_points(
            session,
            hr_user,
            employee_id=employee.id,
            points=100,
            awarded_on=date(2026, 3, 1),
        )
    assert "unquantified liability" in exc.value.detail


async def test_a_redemption_cannot_overdraw(session, hr_user, employee):
    await valued(session, hr_user)
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=100, awarded_on=date(2026, 3, 1)
    )
    with pytest.raises(HTTPException) as exc:
        await rewards.redeem_points(
            session, hr_user, employee_id=employee.id, points=500, requested_on=TODAY
        )
    assert "negative balance" in exc.value.detail


# ══════════════════════════════════════════════════════════════════════════════
# Gate: the movement report reconciles
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_movement_report_reconciles_opening_to_closing(
    session, hr_user, employee
):
    await valued(session, hr_user)
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=1000, awarded_on=date(2026, 3, 1)
    )
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=500, awarded_on=date(2026, 5, 1)
    )
    await rewards.redeem_points(
        session,
        hr_user,
        employee_id=employee.id,
        points=300,
        requested_on=date(2026, 5, 15),
    )

    report = await rewards.movement(
        session, hr_user, period_start=date(2026, 4, 1), period_end=date(2026, 9, 30)
    )
    assert report["opening"] == 1000
    assert report["awarded"] == 500
    assert report["redeemed"] == 300
    assert report["closing"] == 1200
    assert report["reconciles"] is True


# ══════════════════════════════════════════════════════════════════════════════
# Expiry
# ══════════════════════════════════════════════════════════════════════════════


async def test_an_expiry_policy_needs_a_disclosure(session, hr_user):
    with pytest.raises(HTTPException) as exc:
        await rewards.set_expiry_policy(
            session,
            hr_user,
            enabled=True,
            expire_after_days=365,
            notify_days_before=30,
            disclosure="   ",
        )
    assert "shown at the point of award" in exc.value.detail


async def test_the_disclosure_appears_at_the_point_of_award(
    session, hr_user, employee
):
    """Not buried in a policy document."""
    await valued(session, hr_user)
    await rewards.set_expiry_policy(
        session,
        hr_user,
        enabled=True,
        expire_after_days=365,
        notify_days_before=30,
        disclosure="Points expire twelve months after award.",
    )
    out = await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=100, awarded_on=TODAY
    )
    assert out["expiry_disclosure"] == "Points expire twelve months after award."


async def test_expiry_reduces_the_liability_and_deletes_nothing(
    session, hr_user, employee
):
    await valued(session, hr_user)
    await rewards.set_expiry_policy(
        session,
        hr_user,
        enabled=True,
        expire_after_days=90,
        notify_days_before=30,
        disclosure="Points expire ninety days after award.",
    )
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=1000, awarded_on=date(2026, 1, 1)
    )
    before = await rewards.liability_report(session, hr_user, as_of=TODAY)
    assert before["liability"]["balance_points"] == 1000

    result = await rewards.expire_points(session, hr_user, as_of=TODAY)
    assert result["points_expired"] == 1000

    after = await rewards.liability_report(session, hr_user, as_of=TODAY)
    assert after["liability"]["balance_points"] == 0
    assert after["liability"]["rupees"] == "0.00"

    rows = list(
        (
            await session.execute(
                select(PointsLedgerEntry).where(
                    PointsLedgerEntry.employee_id == employee.id
                )
            )
        ).scalars()
    )
    assert len(rows) == 2
    assert any(
        r.transaction_type is PointsTransactionTypeEnum.EXPIRED for r in rows
    )


async def test_expiry_does_nothing_without_a_policy(session, hr_user, employee):
    """The award is old enough to have expired under any sane policy. Without
    one configured, nothing expires."""
    await valued(session, hr_user)
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=1000, awarded_on=date(2026, 1, 1)
    )
    result = await rewards.expire_points(session, hr_user, as_of=TODAY)
    assert result["points_expired"] == 0
    assert "No expiry policy is enabled" in result["note"]


# ══════════════════════════════════════════════════════════════════════════════
# Gate: budget exhaustion enforces per configuration
# ══════════════════════════════════════════════════════════════════════════════


async def budget(session, tenant, scope, key, allocated, consumed, mode):
    row = PointsBudget(
        tenant_id=tenant.id,
        scope=scope,
        scope_key=key,
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        allocated_points=allocated,
        consumed_points=consumed,
        enforcement=mode,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def test_a_hard_stop_budget_refuses_the_award(
    session, hr_user, tenant, employee
):
    await valued(session, hr_user)
    await budget(
        session,
        tenant,
        BudgetScopeEnum.MANAGER,
        str(hr_user.id),
        1000,
        950,
        EnforcementModeEnum.HARD_STOP,
    )
    with pytest.raises(HTTPException) as exc:
        await rewards.award_points(
            session, hr_user, employee_id=employee.id, points=200, awarded_on=TODAY
        )
    assert exc.value.status_code == 409
    assert "hard stop" in exc.value.detail


async def test_a_soft_warning_permits_and_records_the_overspend(
    session, hr_user, tenant, employee
):
    await valued(session, hr_user)
    row = await budget(
        session,
        tenant,
        BudgetScopeEnum.MANAGER,
        str(hr_user.id),
        1000,
        950,
        EnforcementModeEnum.SOFT_WARNING,
    )
    out = await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=200, awarded_on=TODAY
    )
    assert out["budget"]["permitted"] is True
    assert out["budget"]["overspend_points"] == 150

    await session.refresh(row)
    assert row.consumed_points == 1150
    assert row.overspend_points == 150


async def test_budget_consumption_is_recorded_on_a_permitted_award(
    session, hr_user, tenant, employee
):
    await valued(session, hr_user)
    row = await budget(
        session,
        tenant,
        BudgetScopeEnum.MANAGER,
        str(hr_user.id),
        1000,
        0,
        EnforcementModeEnum.HARD_STOP,
    )
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=250, awarded_on=TODAY
    )
    await session.refresh(row)
    assert row.consumed_points == 250


async def test_budget_utilisation_reports_rupees(session, hr_user, tenant):
    await valued(session, hr_user)
    await budget(
        session,
        tenant,
        BudgetScopeEnum.COMPANY,
        str(tenant.id),
        10000,
        4000,
        EnforcementModeEnum.SOFT_WARNING,
    )
    report = await rewards.budget_utilisation(session, hr_user, as_of=TODAY)
    assert report["budgets"][0]["consumed_rupees"] == "2000.00"
    assert report["budgets"][0]["utilisation"] == 0.4


# ══════════════════════════════════════════════════════════════════════════════
# Gate: perquisite at the threshold, and only from a reviewed rule
# ══════════════════════════════════════════════════════════════════════════════


async def test_payroll_refuses_a_perquisite_from_an_unreviewed_rule(
    session, hr_user, employee
):
    """This is the gate on the whole tax section."""
    await valued(session, hr_user, "1", date(2026, 1, 1))
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=9000, awarded_on=date(2026, 5, 1)
    )
    await rewards.redeem_points(
        session,
        hr_user,
        employee_id=employee.id,
        points=9000,
        requested_on=date(2026, 5, 15),
        item_description="Tablet",
    )
    with pytest.raises(HTTPException) as exc:
        await rewards.perquisites(
            session,
            hr_user,
            employee_id=employee.id,
            financial_year="2026-27",
            as_of=TODAY,
        )
    assert "reviewed by a tax advisor" in exc.value.detail


async def test_a_reviewed_rule_produces_a_perquisite_at_the_threshold(
    session, hr_user, employee
):
    await valued(session, hr_user, "1", date(2026, 1, 1))
    await rewards.set_tax_rule(
        session,
        hr_user,
        reward_type="merchandise",
        treatment=TaxTreatmentEnum.GIFT_RULE,
        exemption_threshold_per_year=D("5000"),
        effective_from=date(2020, 4, 1),
        reviewed=True,
        reviewed_by="Advisor LLP",
        reviewed_on=date(2026, 4, 1),
    )
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=9000, awarded_on=date(2026, 5, 1)
    )
    await rewards.redeem_points(
        session,
        hr_user,
        employee_id=employee.id,
        points=9000,
        requested_on=date(2026, 5, 15),
    )

    out = await rewards.perquisites(
        session,
        hr_user,
        employee_id=employee.id,
        financial_year="2026-27",
        as_of=TODAY,
    )
    assessment = out["assessments"][0]
    assert assessment["taxable_value"] == "9000.00"
    assert assessment["reviewed"] is True
    assert out["payroll"]["handed_over"] is False


async def test_below_the_threshold_nothing_is_taxable(session, hr_user, employee):
    await valued(session, hr_user, "1", date(2026, 1, 1))
    await rewards.set_tax_rule(
        session,
        hr_user,
        reward_type="merchandise",
        treatment=TaxTreatmentEnum.GIFT_RULE,
        exemption_threshold_per_year=D("5000"),
        effective_from=date(2020, 4, 1),
        reviewed=True,
        reviewed_by="Advisor LLP",
    )
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=3000, awarded_on=date(2026, 5, 1)
    )
    await rewards.redeem_points(
        session,
        hr_user,
        employee_id=employee.id,
        points=3000,
        requested_on=date(2026, 5, 15),
    )
    out = await rewards.perquisites(
        session,
        hr_user,
        employee_id=employee.id,
        financial_year="2026-27",
        as_of=TODAY,
    )
    assert out["assessments"][0]["taxable_value"] == "0.00"


async def test_a_rule_marked_reviewed_needs_a_named_reviewer(session, hr_user):
    with pytest.raises(HTTPException) as exc:
        await rewards.set_tax_rule(
            session,
            hr_user,
            reward_type="merchandise",
            treatment=TaxTreatmentEnum.EXEMPT,
            exemption_threshold_per_year=D("0"),
            effective_from=date(2020, 4, 1),
            reviewed=True,
        )
    assert "name who reviewed it" in exc.value.detail


async def test_the_perquisite_is_not_handed_to_payroll(session, hr_user, employee):
    """A crossed threshold is visible and unprocessed rather than silently
    applied to a payslip."""
    await valued(session, hr_user, "1", date(2026, 1, 1))
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=9000, awarded_on=date(2026, 5, 1)
    )
    await rewards.redeem_points(
        session,
        hr_user,
        employee_id=employee.id,
        points=9000,
        requested_on=date(2026, 5, 15),
    )
    row = (
        await session.execute(
            select(RewardEvent).where(RewardEvent.employee_id == employee.id)
        )
    ).scalar_one()
    assert row.payroll_reference is None
    assert row.financial_year == "2026-27"


async def test_the_employee_statement_shows_the_tax_position(
    session, hr_user, employee
):
    """Before the payslip rather than in it."""
    await valued(session, hr_user, "1", date(2026, 1, 1))
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=6000, awarded_on=date(2026, 5, 1)
    )
    await rewards.redeem_points(
        session,
        hr_user,
        employee_id=employee.id,
        points=6000,
        requested_on=date(2026, 5, 15),
        item_description="Coffee machine",
    )
    statement = await rewards.employee_statement(
        session,
        hr_user,
        employee_id=employee.id,
        financial_year="2026-27",
        as_of=TODAY,
    )
    assert statement["tax"]["total_value"] == "6000.00"
    assert statement["tax"]["any_provisional"] is True
    assert "provisional" in statement["tax"]["note"]


# ══════════════════════════════════════════════════════════════════════════════
# Gate: reconciliation surfaces a seeded gap
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_clean_month_is_recorded_as_clean(session, hr_user, employee, tenant):
    await valued(session, hr_user, "1", date(2026, 1, 1))
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=500, awarded_on=date(2026, 9, 1)
    )
    redemption = await rewards.redeem_points(
        session,
        hr_user,
        employee_id=employee.id,
        points=500,
        requested_on=date(2026, 9, 2),
    )
    session.add(
        FulfilmentRecord(
            tenant_id=tenant.id,
            redemption_id=uuid.UUID(redemption["redemption_id"]),
            order_id="ORD-1",
            status="fulfilled",
            fulfilled_on=date(2026, 9, 5),
            imported_on=date(2026, 9, 30),
        )
    )
    session.add(
        RewardInvoiceLine(
            tenant_id=tenant.id,
            order_id="ORD-1",
            amount=D("500.00"),
            invoiced_on=date(2026, 9, 28),
        )
    )
    await session.commit()

    report = await rewards.run_reconciliation(
        session,
        hr_user,
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        period_label="2026-09",
    )
    assert report["clean"] is True

    run = (
        await session.execute(select(ReconciliationRun))
    ).scalar_one()
    assert run.status is ReconciliationStatusEnum.CLEAN


async def test_a_seeded_fulfilment_gap_is_surfaced(session, hr_user, employee):
    """The employee has paid in points and has nothing to show for it."""
    await valued(session, hr_user, "1", date(2026, 1, 1))
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=500, awarded_on=date(2026, 9, 1)
    )
    await rewards.redeem_points(
        session,
        hr_user,
        employee_id=employee.id,
        points=500,
        requested_on=date(2026, 9, 2),
    )

    report = await rewards.run_reconciliation(
        session,
        hr_user,
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        period_label="2026-09",
    )
    assert report["clean"] is False
    assert report["gaps"][0]["kind"] == "unfulfilled_redemption"
    assert report["gaps"][0]["employee_id"] == str(employee.id)


async def test_a_billing_error_and_a_fulfilment_gap_do_not_cancel(
    session, hr_user, employee, tenant
):
    await valued(session, hr_user, "1", date(2026, 1, 1))
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=500, awarded_on=date(2026, 9, 1)
    )
    await rewards.redeem_points(
        session,
        hr_user,
        employee_id=employee.id,
        points=500,
        requested_on=date(2026, 9, 2),
    )
    session.add(
        FulfilmentRecord(
            tenant_id=tenant.id,
            redemption_id=None,
            external_redemption_ref="THEIRS-99",
            order_id="ORD-9",
            status="fulfilled",
            fulfilled_on=date(2026, 9, 5),
            imported_on=date(2026, 9, 30),
        )
    )
    session.add(
        RewardInvoiceLine(
            tenant_id=tenant.id,
            order_id="ORD-9",
            amount=D("500.00"),
            invoiced_on=date(2026, 9, 28),
        )
    )
    await session.commit()

    report = await rewards.run_reconciliation(
        session,
        hr_user,
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        period_label="2026-09",
    )
    kinds = set(report["gaps_by_kind"])
    assert "unfulfilled_redemption" in kinds
    assert "orphan_fulfilment" in kinds


async def test_a_month_cannot_be_reconciled_twice(session, hr_user):
    await rewards.run_reconciliation(
        session,
        hr_user,
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        period_label="2026-09",
    )
    with pytest.raises(HTTPException) as exc:
        await rewards.run_reconciliation(
            session,
            hr_user,
            period_start=date(2026, 9, 1),
            period_end=date(2026, 9, 30),
            period_label="2026-09",
        )
    assert "already been reconciled" in exc.value.detail


# ══════════════════════════════════════════════════════════════════════════════
# Liability reporting
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_liability_splits_by_department(
    session, hr_user, tenant, legal_entity, location
):
    await valued(session, hr_user, "1", date(2026, 1, 1))
    people = await make_employees(session, tenant, legal_entity, location, 2, "DPT")
    for person in people:
        await rewards.award_points(
            session,
            hr_user,
            employee_id=person.id,
            points=1000,
            awarded_on=date(2026, 3, 1),
        )
    report = await rewards.liability_report(session, hr_user, as_of=TODAY)
    assert report["by_group"]["total_rupees"] == "2000.00"


async def test_the_ageing_report_buckets_by_award_age(session, hr_user, employee):
    await valued(session, hr_user, "1", date(2026, 1, 1))
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=1000, awarded_on=date(2026, 3, 1)
    )
    report = await rewards.liability_report(session, hr_user, as_of=TODAY)
    assert report["ageing"]["buckets"]["181-365"]["points"] == 1000


async def test_a_split_finer_than_cost_centre_is_refused(session, hr_user):
    with pytest.raises(HTTPException) as exc:
        await rewards.liability_report(
            session, hr_user, as_of=TODAY, group_by="employee_id"
        )
    assert "allocated to individuals" in exc.value.detail


# ══════════════════════════════════════════════════════════════════════════════
# Tenant isolation
# ══════════════════════════════════════════════════════════════════════════════


async def test_another_tenant_sees_none_of_this_liability(
    session, hr_user, other_hr_user, employee
):
    await valued(session, hr_user, "1", date(2026, 1, 1))
    await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=1000, awarded_on=date(2026, 3, 1)
    )
    report = await rewards.liability_report(session, other_hr_user, as_of=TODAY)
    assert report["liability"]["balance_points"] == 0
    assert report["liability"]["rupees"] == "0.00"


async def test_another_tenant_cannot_reverse_an_entry(
    session, hr_user, other_hr_user, employee
):
    await valued(session, hr_user, "1", date(2026, 1, 1))
    award = await rewards.award_points(
        session, hr_user, employee_id=employee.id, points=500, awarded_on=date(2026, 3, 1)
    )
    with pytest.raises(HTTPException) as exc:
        await rewards.reverse_entry(
            session,
            other_hr_user,
            entry_id=uuid.UUID(award["entry_id"]),
            reason="Not mine.",
            occurred_on=TODAY,
        )
    assert exc.value.status_code == 404
