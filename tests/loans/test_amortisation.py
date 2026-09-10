"""Slice 14 gate: the amortisation ledger reconciles to the principal exactly.

Not approximately. Every EMI is a rounded figure, and rounding sixty of them
independently leaves a remainder — where that remainder goes decides whether the
loan closes cleanly or leaves someone owing four rupees forever.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.loans.amortisation import (
    AmortisationError,
    PrepaymentMode,
    build_schedule,
    concessional_loan_perquisite,
    recompute_after_prepayment,
)


def D(value: str | int) -> Decimal:  # noqa: N802 - a Decimal alias, used throughout
    return Decimal(str(value))


def schedule(**overrides):
    base = {
        "principal": D(120_000),
        "annual_rate": D(12),
        "tenure_months": 12,
    }
    base.update(overrides)
    return build_schedule(**base)


# ═══════════════════════════════════════════════════════════════════════════
# Reconciliation — the gate
# ═══════════════════════════════════════════════════════════════════════════


def test_the_principal_components_sum_to_exactly_the_principal() -> None:
    """The checklist gate."""
    result = schedule()
    assert result.total_principal == D(120_000)
    assert result.reconciles is True


@pytest.mark.parametrize(
    ("principal", "rate", "tenure"),
    [
        ("100000", "10", 12),
        ("250000", "8.5", 36),
        ("75000", "14.75", 24),
        ("1000000", "9.25", 60),
        ("33333", "11.11", 7),
        ("1", "12", 3),
    ],
)
def test_the_ledger_reconciles_across_awkward_numbers(
    principal: str, rate: str, tenure: int
) -> None:
    """Rates and principals chosen to produce rounding remainders."""
    result = schedule(principal=D(principal), annual_rate=D(rate), tenure_months=tenure)
    assert result.total_principal == D(principal), (
        f"{result.total_principal} of principal recovered against a loan of {principal}"
    )


def test_the_final_instalment_absorbs_the_remainder() -> None:
    """Which is why it is a slightly odd number, exactly as on a real loan
    statement."""
    result = schedule(principal=D(100_000), annual_rate=D("13.37"), tenure_months=17)
    last = result.instalments[-1]

    assert last.closing_balance == D(0)
    assert last.emi != result.emi, "the last instalment is what is left, not the EMI"


def test_the_balance_reaches_zero_and_never_goes_below() -> None:
    result = schedule(principal=D(250_000), annual_rate=D("8.5"), tenure_months=36)
    balances = [i.closing_balance for i in result.instalments]

    assert balances[-1] == D(0)
    assert all(b >= 0 for b in balances)
    assert balances == sorted(balances, reverse=True), "the balance only falls"


def test_an_interest_free_loan_reconciles_and_charges_nothing() -> None:
    result = schedule(annual_rate=D(0), interest_type="interest_free")
    assert result.total_principal == D(120_000)
    assert result.total_interest == D(0)
    assert result.emi == D(10_000)


# ═══════════════════════════════════════════════════════════════════════════
# Interest types
# ═══════════════════════════════════════════════════════════════════════════


def test_simple_interest_costs_more_than_reducing_at_the_same_rate() -> None:
    """It is computed once on the full principal, and the module says so."""
    reducing = schedule(interest_type="reducing")
    simple = schedule(interest_type="simple")

    assert simple.total_interest > reducing.total_interest
    assert any("effective cost is higher" in note for note in simple.notes)
    assert simple.reconciles


def test_reducing_balance_interest_falls_each_month() -> None:
    result = schedule()
    interests = [i.interest for i in result.instalments]
    assert interests[0] > interests[-1]


def test_simple_interest_is_the_same_every_month() -> None:
    result = schedule(interest_type="simple")
    interests = [i.interest for i in result.instalments]
    assert len(set(interests)) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Moratorium
# ═══════════════════════════════════════════════════════════════════════════


def test_a_moratorium_recovers_no_principal_and_still_reconciles() -> None:
    result = schedule(tenure_months=15, moratorium_months=3)
    holiday = result.instalments[:3]

    assert all(i.is_moratorium for i in holiday)
    assert all(i.principal == D(0) for i in holiday)
    assert result.reconciles


def test_interest_may_or_may_not_accrue_during_a_moratorium() -> None:
    """Both arrangements exist, and assuming one silently changes what the
    borrower owes."""
    accruing = schedule(
        tenure_months=15, moratorium_months=3, accrue_interest_during_moratorium=True
    )
    free = schedule(
        tenure_months=15, moratorium_months=3, accrue_interest_during_moratorium=False
    )

    assert accruing.instalments[0].interest > D(0)
    assert free.instalments[0].interest == D(0)
    assert accruing.total_interest > free.total_interest


def test_a_moratorium_covering_the_whole_tenure_is_refused() -> None:
    with pytest.raises(AmortisationError, match="nothing would ever be recovered"):
        schedule(tenure_months=6, moratorium_months=6)


# ═══════════════════════════════════════════════════════════════════════════
# Prepayment
# ═══════════════════════════════════════════════════════════════════════════


def test_reduce_emi_keeps_the_tenure_and_lowers_the_instalment() -> None:
    """The checklist gate, first half."""
    result = recompute_after_prepayment(
        outstanding_principal=D(100_000),
        annual_rate=D(12),
        remaining_months=10,
        prepayment=D(30_000),
        mode=PrepaymentMode.REDUCE_EMI,
        current_emi=D(10_600),
    )
    assert result.tenure_months == 10
    assert result.emi < D(10_600)
    assert result.total_principal == D(70_000)
    assert any("tenure stays" in note for note in result.notes)


def test_reduce_tenure_keeps_the_instalment_and_shortens_the_loan() -> None:
    """The checklist gate, second half."""
    result = recompute_after_prepayment(
        outstanding_principal=D(100_000),
        annual_rate=D(12),
        remaining_months=10,
        prepayment=D(30_000),
        mode=PrepaymentMode.REDUCE_TENURE,
        current_emi=D(10_600),
    )
    assert result.tenure_months < 10
    assert result.total_principal == D(70_000)
    assert any("tenure falls" in note for note in result.notes)


def test_the_two_modes_produce_genuinely_different_schedules() -> None:
    """Picking one for the borrower is how a product ends up arguing about
    what they asked for."""
    common = {
        "outstanding_principal": D(100_000),
        "annual_rate": D(12),
        "remaining_months": 10,
        "prepayment": D(30_000),
        "current_emi": D(10_600),
    }
    emi_mode = recompute_after_prepayment(**common, mode=PrepaymentMode.REDUCE_EMI)
    tenure_mode = recompute_after_prepayment(**common, mode=PrepaymentMode.REDUCE_TENURE)

    assert emi_mode.emi != tenure_mode.emi
    assert emi_mode.tenure_months != tenure_mode.tenure_months
    # Shortening saves interest.
    assert tenure_mode.total_interest < emi_mode.total_interest


def test_a_prepayment_clearing_the_loan_says_to_close_it() -> None:
    with pytest.raises(AmortisationError, match="Close the account"):
        recompute_after_prepayment(
            outstanding_principal=D(50_000), annual_rate=D(12), remaining_months=5,
            prepayment=D(50_000), mode=PrepaymentMode.REDUCE_EMI, current_emi=D(10_000),
        )


def test_a_prepayment_beyond_the_balance_is_refused() -> None:
    with pytest.raises(AmortisationError, match="exceeds the outstanding principal"):
        recompute_after_prepayment(
            outstanding_principal=D(50_000), annual_rate=D(12), remaining_months=5,
            prepayment=D(60_000), mode=PrepaymentMode.REDUCE_EMI, current_emi=D(10_000),
        )


def test_an_unknown_prepayment_mode_is_refused_with_the_options() -> None:
    with pytest.raises(AmortisationError, match="reduce_emi"):
        recompute_after_prepayment(
            outstanding_principal=D(50_000), annual_rate=D(12), remaining_months=5,
            prepayment=D(10_000), mode="whatever", current_emi=D(10_000),
        )


def test_an_emi_too_small_to_cover_interest_is_refused() -> None:
    """Otherwise the loan never repays and the loop runs forever."""
    with pytest.raises(AmortisationError, match="never be repaid"):
        recompute_after_prepayment(
            outstanding_principal=D(1_000_000), annual_rate=D(24), remaining_months=60,
            prepayment=D(1_000), mode=PrepaymentMode.REDUCE_TENURE, current_emi=D(100),
        )


# ═══════════════════════════════════════════════════════════════════════════
# What is refused
# ═══════════════════════════════════════════════════════════════════════════


def test_a_float_is_refused() -> None:
    with pytest.raises(AmortisationError, match="must be a Decimal"):
        schedule(principal=120000.0)


def test_a_zero_principal_is_refused() -> None:
    with pytest.raises(AmortisationError, match="loan of zero"):
        schedule(principal=D(0))


def test_a_zero_tenure_is_refused() -> None:
    with pytest.raises(AmortisationError, match="at least one month"):
        schedule(tenure_months=0)


def test_the_schedule_is_json_serialisable() -> None:
    import json

    json.dumps(schedule().as_dict())


# ═══════════════════════════════════════════════════════════════════════════
# The concessional-loan perquisite
# ═══════════════════════════════════════════════════════════════════════════

# Fixture values, not an assertion about any real rate or threshold.
LOAN_RULES = {"notional_rate": "10", "exempt_threshold": 20_000}


def test_an_interest_free_loan_above_the_threshold_is_a_perquisite() -> None:
    """The checklist gate: the perquisite value reaches TDS."""
    result = concessional_loan_perquisite(
        outstanding_by_month=[D(120_000)] * 12,
        actual_interest_charged=D(0),
        config=LOAN_RULES,
    )
    assert result.is_perquisite is True
    assert result.notional_interest == D(12_000), "10% of 120,000 for a year"
    assert result.perquisite_value == D(12_000)
    assert "taxable perquisite" in result.reason


def test_a_small_loan_is_exempt_regardless_of_rate() -> None:
    result = concessional_loan_perquisite(
        outstanding_by_month=[D(15_000)] * 12,
        actual_interest_charged=D(0),
        config=LOAN_RULES,
    )
    assert result.is_perquisite is False
    assert result.perquisite_value == D(0)
    assert "within the exempt threshold" in result.reason


def test_the_threshold_is_checked_against_the_largest_balance() -> None:
    """A loan that was once large was once a benefit, so an average would let
    a big loan repaid quickly escape entirely."""
    result = concessional_loan_perquisite(
        outstanding_by_month=[D(100_000), D(5_000), D(0)],
        actual_interest_charged=D(0),
        config=LOAN_RULES,
    )
    assert result.is_perquisite is True


def test_a_loan_at_or_above_the_notional_rate_is_not_concessional() -> None:
    result = concessional_loan_perquisite(
        outstanding_by_month=[D(120_000)] * 12,
        actual_interest_charged=D(15_000),
        config=LOAN_RULES,
    )
    assert result.is_perquisite is False
    assert result.perquisite_value == D(0)
    assert "not concessional" in result.reason


def test_a_partly_concessional_loan_is_valued_at_the_difference() -> None:
    result = concessional_loan_perquisite(
        outstanding_by_month=[D(120_000)] * 12,
        actual_interest_charged=D(5_000),
        config=LOAN_RULES,
    )
    assert result.perquisite_value == D(7_000)


def test_a_falling_balance_carries_less_benefit() -> None:
    """A loan being repaid is worth less than one that is not."""
    steady = concessional_loan_perquisite(
        outstanding_by_month=[D(120_000)] * 12,
        actual_interest_charged=D(0), config=LOAN_RULES,
    )
    repaying = concessional_loan_perquisite(
        outstanding_by_month=[D(120_000) - D(10_000) * i for i in range(12)],
        actual_interest_charged=D(0), config=LOAN_RULES,
    )
    assert repaying.perquisite_value < steady.perquisite_value


@pytest.mark.parametrize("missing", ["notional_rate", "exempt_threshold"])
def test_the_notional_rate_and_threshold_must_be_supplied(missing: str) -> None:
    """Neither is assumed — both change, and a change should not be a release."""
    config = {key: value for key, value in LOAN_RULES.items() if key != missing}
    with pytest.raises(AmortisationError, match=missing):
        concessional_loan_perquisite(
            outstanding_by_month=[D(120_000)], actual_interest_charged=D(0), config=config
        )


def test_no_balances_is_refused() -> None:
    with pytest.raises(AmortisationError, match="nothing to value"):
        concessional_loan_perquisite(
            outstanding_by_month=[], actual_interest_charged=D(0), config=LOAN_RULES
        )


def test_the_perquisite_result_is_json_serialisable() -> None:
    import json

    json.dumps(
        concessional_loan_perquisite(
            outstanding_by_month=[D(120_000)] * 12,
            actual_interest_charged=D(0),
            config=LOAN_RULES,
        ).as_dict()
    )
