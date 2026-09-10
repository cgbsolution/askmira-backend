"""Slice 14 gate: EMI recovery respects the minimum net-pay floor.

The obvious implementation — the EMI is due, so deduct it — is how an employee
who took two weeks of unpaid leave receives nothing at all in a month they
needed to eat. The loan is not more urgent than the salary.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.loans.recovery import RecoveryError, plan_recovery


def D(value: str | int) -> Decimal:  # noqa: N802 - a Decimal alias, used throughout
    return Decimal(str(value))


def plan(**overrides):
    base = {
        "emi": D(10_000),
        "net_pay_before_recovery": D(50_000),
        "minimum_net_pay": D(15_000),
        "outstanding_principal": D(100_000),
    }
    base.update(overrides)
    return plan_recovery(**base)


# ═══════════════════════════════════════════════════════════════════════════
# The floor — the gate
# ═══════════════════════════════════════════════════════════════════════════


def test_a_comfortable_month_recovers_in_full() -> None:
    result = plan()
    assert result.amount == D(10_000)
    assert result.is_full_emi is True
    assert result.is_skipped is False
    assert result.resulting_net == D(40_000)


def test_a_recovery_that_would_breach_the_floor_is_skipped_and_carried_forward() -> None:
    """The checklist gate.

    A month of unpaid leave leaves 20,000. Deducting the full 10,000 would take
    the employee below the 15,000 floor, so nothing is recovered.
    """
    result = plan(net_pay_before_recovery=D(20_000))

    assert result.amount == D(0)
    assert result.is_skipped is True
    assert result.carried_forward == D(10_000)
    assert result.resulting_net == D(20_000)
    assert "not more urgent than the salary" in result.reason or "not enabled" in result.reason


def test_net_pay_already_at_the_floor_recovers_nothing() -> None:
    result = plan(net_pay_before_recovery=D(15_000))
    assert result.amount == D(0)
    assert result.is_skipped is True
    assert "not more urgent than the salary" in result.reason


def test_net_pay_below_the_floor_recovers_nothing() -> None:
    """A month where LOP has already taken pay under the floor."""
    result = plan(net_pay_before_recovery=D(8_000))
    assert result.amount == D(0)
    assert result.resulting_net == D(8_000), "the deduction did not make it worse"


def test_a_recovery_landing_exactly_on_the_floor_is_allowed() -> None:
    """The boundary. The floor is a floor, not an exclusive bound."""
    result = plan(net_pay_before_recovery=D(25_000))
    assert result.amount == D(10_000)
    assert result.resulting_net == D(15_000)
    assert result.is_skipped is False


def test_one_rupee_past_the_floor_is_skipped() -> None:
    result = plan(net_pay_before_recovery=D("24999.99"))
    assert result.is_skipped is True


def test_the_floor_is_configuration() -> None:
    """What counts as a liveable minimum is a policy decision, not a constant."""
    tight = plan(net_pay_before_recovery=D(20_000), minimum_net_pay=D(15_000))
    loose = plan(net_pay_before_recovery=D(20_000), minimum_net_pay=D(5_000))
    assert tight.is_skipped is True
    assert loose.is_skipped is False


# ═══════════════════════════════════════════════════════════════════════════
# Partial recovery, when configured
# ═══════════════════════════════════════════════════════════════════════════


def test_partial_recovery_is_off_by_default_and_says_why() -> None:
    """A part-payment nobody recorded as partial is how a ledger stops
    reconciling."""
    result = plan(net_pay_before_recovery=D(20_000))
    assert result.is_skipped is True
    assert "partial recovery is not enabled" in result.reason


def test_partial_recovery_takes_what_fits_and_carries_the_rest() -> None:
    result = plan(net_pay_before_recovery=D(20_000), allow_partial=True)

    assert result.amount == D(5_000), "the headroom above the floor"
    assert result.carried_forward == D(5_000)
    assert result.is_skipped is False
    assert result.is_full_emi is False
    assert result.resulting_net == D(15_000)


def test_partial_recovery_still_respects_a_zero_headroom() -> None:
    """Enabling partial recovery does not let it breach the floor."""
    result = plan(net_pay_before_recovery=D(15_000), allow_partial=True)
    assert result.amount == D(0)
    assert result.is_skipped is True


# ═══════════════════════════════════════════════════════════════════════════
# Carry-forward
# ═══════════════════════════════════════════════════════════════════════════


def test_a_backlog_is_attempted_alongside_this_months_instalment() -> None:
    """So a good month clears it, rather than the backlog growing forever."""
    result = plan(already_carried_forward=D(10_000))

    assert result.amount == D(20_000)
    assert result.is_full_emi is True
    assert any("carried forward from earlier" in note for note in result.notes)


def test_a_backlog_too_large_for_this_month_is_carried_on() -> None:
    result = plan(net_pay_before_recovery=D(22_000), already_carried_forward=D(10_000))
    assert result.is_skipped is True
    assert result.carried_forward == D(20_000), "both months carry on"


def test_recovery_never_exceeds_what_is_owed() -> None:
    """Even with a backlog, the last recovery is what is left."""
    result = plan(already_carried_forward=D(10_000), outstanding_principal=D(12_000))

    assert result.amount == D(12_000)
    assert any("Capped at the outstanding principal" in note for note in result.notes)


def test_the_final_recovery_closes_the_loan_exactly() -> None:
    result = plan(emi=D(10_000), outstanding_principal=D(3_500))
    assert result.amount == D(3_500)
    assert result.resulting_net == D(46_500)


# ═══════════════════════════════════════════════════════════════════════════
# The reason, always
# ═══════════════════════════════════════════════════════════════════════════


def test_every_decision_carries_a_reason() -> None:
    """It is what the payslip and the borrower get told. "Why was nothing
    deducted in July" is a question that will be asked."""
    for case in (
        plan(),
        plan(net_pay_before_recovery=D(20_000)),
        plan(net_pay_before_recovery=D(20_000), allow_partial=True),
        plan(net_pay_before_recovery=D(10_000)),
    ):
        assert case.reason.strip()


def test_the_decision_is_json_serialisable() -> None:
    import json

    json.dumps(plan().as_dict())


# ═══════════════════════════════════════════════════════════════════════════
# What is refused
# ═══════════════════════════════════════════════════════════════════════════


def test_a_float_is_refused() -> None:
    with pytest.raises(RecoveryError, match="must be a Decimal"):
        plan(emi=10000.0)


def test_a_negative_figure_is_refused() -> None:
    with pytest.raises(RecoveryError, match="cannot be negative"):
        plan(minimum_net_pay=D(-1))


def test_a_zero_emi_is_refused() -> None:
    with pytest.raises(RecoveryError, match="no instalment to recover"):
        plan(emi=D(0))
