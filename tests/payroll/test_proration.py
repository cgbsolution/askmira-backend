"""Joining and leaving part-way through a month.

Attendance writes no rows before someone joins or after they leave, so loss of
pay alone paid a 15th-of-the-month joiner for the whole month. Days outside
employment are unpaid, prorated exactly as loss of pay is, and reported apart
from it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models.payroll import CalculationType
from app.services.payroll.engine import PayrollInput, PayrollRuleError, compute_payroll
from app.services.payroll.run import days_outside, employment_window
from app.services.payroll.salary_structure import resolve_ctc
from tests.payroll.conftest import component, employee_salary_from, structure

SEPTEMBER = (date(2026, 9, 1), date(2026, 9, 30))
OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))


def salary():
    """Monthly BASIC 40,000 (the PF base) and SPECIAL 60,000: gross 1,00,000."""
    shape = structure(
        component("BASIC", calculation_type=CalculationType.PERCENTAGE_OF, base="CTC",
                  value="40", counts_toward_pf=True, display_order=1),
        component("SPECIAL", calculation_type=CalculationType.BALANCE, display_order=9),
    )
    return employee_salary_from(resolve_ctc(shape, Decimal("1200000")), Decimal("1200000"))


def compute(pf_rules, period, *, lop="0", not_employed="0"):
    start, end = period
    return compute_payroll(
        PayrollInput(
            employee_salary=salary(),
            period_start=start,
            period_end=end,
            working_days=Decimal((end - start).days + 1),
            lop_days=Decimal(lop),
            not_employed_days=Decimal(not_employed),
            rule_sets={"pf": pf_rules},
        )
    )


# ── The window ──────────────────────────────────────────────────────────────


def test_a_joiner_on_the_15th_is_outside_employment_for_the_first_14_days() -> None:
    window = employment_window(
        period_start=SEPTEMBER[0], period_end=SEPTEMBER[1],
        joined_on=date(2026, 9, 15), left_on=None,
    )
    assert window == (date(2026, 9, 15), date(2026, 9, 30))
    assert days_outside(*SEPTEMBER, window) == Decimal(14)


def test_a_leaver_on_the_10th_is_outside_employment_for_the_rest_of_the_month() -> None:
    window = employment_window(
        period_start=OCTOBER[0], period_end=OCTOBER[1],
        joined_on=date(2020, 1, 1), left_on=date(2026, 10, 10),
    )
    assert window == (date(2026, 10, 1), date(2026, 10, 10))
    assert days_outside(*OCTOBER, window) == Decimal(21)


def test_someone_employed_all_month_has_no_days_outside() -> None:
    window = employment_window(
        period_start=OCTOBER[0], period_end=OCTOBER[1],
        joined_on=date(2020, 1, 1), left_on=date(2027, 3, 31),
    )
    assert window == OCTOBER
    assert days_outside(*OCTOBER, window) == Decimal(0)


def test_joining_and_leaving_in_the_same_month() -> None:
    window = employment_window(
        period_start=OCTOBER[0], period_end=OCTOBER[1],
        joined_on=date(2026, 10, 5), left_on=date(2026, 10, 20),
    )
    assert window == (date(2026, 10, 5), date(2026, 10, 20))
    assert days_outside(*OCTOBER, window) == Decimal(15)


@pytest.mark.parametrize(
    ("joined", "left"),
    [(date(2026, 11, 2), None), (date(2020, 1, 1), date(2026, 9, 30))],
    ids=["joins after the period", "left before the period"],
)
def test_nobody_is_paid_for_a_period_they_were_not_employed_in(joined, left) -> None:
    assert (
        employment_window(
            period_start=OCTOBER[0], period_end=OCTOBER[1], joined_on=joined, left_on=left
        )
        is None
    )


# ── The engine ──────────────────────────────────────────────────────────────


def test_the_15th_joiner_is_paid_16_of_30_days(pf_rules) -> None:
    result = compute(pf_rules, SEPTEMBER, not_employed="14")
    assert result.paid_days == Decimal(16)
    assert result.not_employed_days == Decimal(14)
    assert result.lop_days == Decimal(0), "not being employed is not an absence"
    # 1,00,000 x 16/30.
    assert result.gross_earnings == Decimal("53333.33")
    basic = next(line for line in result.lines if line.code == "BASIC")
    assert basic.amount == Decimal("21333.33")
    assert "14 not employed" in basic.basis
    # PF on the prorated basic, capped at the ceiling: prorate first.
    assert result.pf_employee == Decimal("1800.00")


def test_the_10th_leaver_is_paid_10_of_31_days(pf_rules) -> None:
    result = compute(pf_rules, OCTOBER, not_employed="21")
    assert result.paid_days == Decimal(10)
    # 1,00,000 x 10/31.
    assert result.gross_earnings == Decimal("32258.07")
    basic = next(line for line in result.lines if line.code == "BASIC")
    # 40,000 x 10/31 = 12,903.23, under the PF ceiling: 12% of it, on wages in whole
    # rupees as the ECR reports them (12,903), to the nearest rupee.
    assert basic.amount == Decimal("12903.23")
    assert result.pf_employee == Decimal("1548.00")


def test_loss_of_pay_and_days_outside_employment_both_reduce_pay(pf_rules) -> None:
    result = compute(pf_rules, SEPTEMBER, lop="2", not_employed="14")
    assert result.paid_days == Decimal(14)
    assert result.lop_days == Decimal(2)
    assert result.not_employed_days == Decimal(14)
    basic = next(line for line in result.lines if line.code == "BASIC")
    assert "2 loss of pay" in basic.basis and "14 not employed" in basic.basis


def test_a_full_month_is_unchanged(pf_rules) -> None:
    result = compute(pf_rules, SEPTEMBER)
    assert result.gross_earnings == Decimal("100000.00")
    assert result.paid_days == Decimal(30)
    assert result.not_employed_days == Decimal(0)


def test_a_period_with_no_employed_day_is_refused(pf_rules) -> None:
    with pytest.raises(PayrollRuleError, match="not employed on any day"):
        compute(pf_rules, SEPTEMBER, not_employed="30")


def test_unpaid_days_cannot_exceed_the_period(pf_rules) -> None:
    with pytest.raises(PayrollRuleError, match="exceed working days"):
        compute(pf_rules, SEPTEMBER, lop="20", not_employed="14")


def test_negative_days_outside_employment_are_refused(pf_rules) -> None:
    with pytest.raises(PayrollRuleError, match="cannot be negative"):
        compute(pf_rules, SEPTEMBER, not_employed="-1")
