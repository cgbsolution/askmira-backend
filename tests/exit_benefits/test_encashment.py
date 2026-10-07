"""Slice 08 gate: leave encashment.

Balances are read, never recomputed — so these tests supply a balance and
assert what the policy does with it, which is the only thing this module is
responsible for.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.models.exit_benefits import EncashmentPolicy
from app.services.exit_benefits.encashment import EncashmentError, compute_encashment


def D(value: str | int) -> Decimal:  # noqa: N802 - a Decimal alias, used throughout
    return Decimal(str(value))


def policy(**overrides) -> EncashmentPolicy:
    base = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "leave_type_code": "EL",
        "encashable_on_exit": True,
        "encashable_in_service": True,
        "max_days_per_year": Decimal(15),
        "cap_days": Decimal(30),
        "min_balance_after_encashment": Decimal(5),
        "formula_basis": ["BASIC", "DA"],
        "days_per_month_divisor": 26,
        "exempt_on_retirement": False,
        "retirement_exemption_ceiling": None,
        "in_service_fully_taxable": True,
        "active": True,
    }
    base.update(overrides)
    return EncashmentPolicy(**base)


# ═══════════════════════════════════════════════════════════════════════════
# Policy caps
# ═══════════════════════════════════════════════════════════════════════════


def test_encashment_is_capped_by_the_available_balance() -> None:
    result = compute_encashment(
        policy=policy(),
        days_requested=D(20),
        available_balance=D(8),
        monthly_wages=D(52_000),
        is_exit=True,
    )
    assert result.days_approved == D(8)
    assert result.limiting_factor == "available_balance"


def test_encashment_is_capped_by_the_per_event_limit() -> None:
    """The checklist gate: capped by policy days."""
    result = compute_encashment(
        policy=policy(cap_days=Decimal(10), max_days_per_year=None),
        days_requested=D(40),
        available_balance=D(60),
        monthly_wages=D(52_000),
        is_exit=True,
    )
    assert result.days_approved == D(10)
    assert result.limiting_factor == "cap_days"


def test_encashment_is_capped_by_the_annual_limit() -> None:
    result = compute_encashment(
        policy=policy(max_days_per_year=Decimal(15), cap_days=None),
        days_requested=D(15),
        available_balance=D(60),
        monthly_wages=D(52_000),
        is_exit=False,
        days_already_encashed_this_year=D(10),
    )
    assert result.days_approved == D(5), "only 5 of the 15-day annual allowance is left"
    assert result.limiting_factor == "max_days_per_year"


def test_an_in_service_encashment_leaves_the_required_minimum_balance() -> None:
    result = compute_encashment(
        policy=policy(min_balance_after_encashment=Decimal(10)),
        days_requested=D(20),
        available_balance=D(25),
        monthly_wages=D(52_000),
        is_exit=False,
    )
    assert result.days_approved == D(15)
    assert result.limiting_factor == "min_balance_after_encashment"


def test_an_exit_encashment_ignores_the_minimum_balance() -> None:
    """There is no reason to retain a balance for someone who is leaving."""
    result = compute_encashment(
        policy=policy(min_balance_after_encashment=Decimal(10), cap_days=None,
                      max_days_per_year=None),
        days_requested=D(25),
        available_balance=D(25),
        monthly_wages=D(52_000),
        is_exit=True,
    )
    assert result.days_approved == D(25)


def test_an_exit_encashment_is_not_held_to_the_in_service_annual_limit() -> None:
    """Leave already encashed in service this year does not forfeit the rest at exit."""
    result = compute_encashment(
        policy=policy(max_days_per_year=Decimal(10), cap_days=Decimal(45)),
        days_requested=D(25),
        available_balance=D(25),
        monthly_wages=D(52_000),
        is_exit=True,
        days_already_encashed_this_year=D(10),
    )
    assert result.days_approved == D(25)
    assert result.limiting_factor is None


def test_nothing_encashable_returns_zero_with_an_explanation() -> None:
    result = compute_encashment(
        policy=policy(max_days_per_year=Decimal(15)),
        days_requested=D(5),
        available_balance=D(20),
        monthly_wages=D(52_000),
        is_exit=False,
        days_already_encashed_this_year=D(15),
    )
    assert result.days_approved == D(0)
    assert result.gross_amount == D(0)
    assert result.notes


# ═══════════════════════════════════════════════════════════════════════════
# What the policy forbids
# ═══════════════════════════════════════════════════════════════════════════


def test_a_type_not_encashable_in_service_is_refused_with_a_useful_message() -> None:
    with pytest.raises(EncashmentError, match="can be encashed at exit"):
        compute_encashment(
            policy=policy(encashable_in_service=False),
            days_requested=D(5),
            available_balance=D(20),
            monthly_wages=D(52_000),
            is_exit=False,
        )


def test_a_type_not_encashable_on_exit_is_refused() -> None:
    with pytest.raises(EncashmentError, match="not encashable on exit"):
        compute_encashment(
            policy=policy(encashable_on_exit=False),
            days_requested=D(5),
            available_balance=D(20),
            monthly_wages=D(52_000),
            is_exit=True,
        )


def test_an_unmapped_wage_basis_is_refused() -> None:
    with pytest.raises(EncashmentError, match="Compliance SME maps"):
        compute_encashment(
            policy=policy(formula_basis=[]),
            days_requested=D(5),
            available_balance=D(20),
            monthly_wages=D(52_000),
            is_exit=True,
        )


def test_a_float_is_refused() -> None:
    with pytest.raises(EncashmentError, match="must be Decimals"):
        compute_encashment(
            policy=policy(),
            days_requested=5.0,  # type: ignore[arg-type]
            available_balance=D(20),
            monthly_wages=D(52_000),
            is_exit=True,
        )


def test_zero_days_is_refused() -> None:
    with pytest.raises(EncashmentError, match="greater than zero"):
        compute_encashment(
            policy=policy(),
            days_requested=D(0),
            available_balance=D(20),
            monthly_wages=D(52_000),
            is_exit=True,
        )


# ═══════════════════════════════════════════════════════════════════════════
# The amount
# ═══════════════════════════════════════════════════════════════════════════


def test_the_daily_rate_uses_the_configured_divisor() -> None:
    at_26 = compute_encashment(
        policy=policy(days_per_month_divisor=26, cap_days=None, max_days_per_year=None),
        days_requested=D(10), available_balance=D(20),
        monthly_wages=D(52_000), is_exit=True,
    )
    at_30 = compute_encashment(
        policy=policy(days_per_month_divisor=30, cap_days=None, max_days_per_year=None),
        days_requested=D(10), available_balance=D(20),
        monthly_wages=D(52_000), is_exit=True,
    )

    assert at_26.daily_rate == D(2_000)
    assert at_30.daily_rate < at_26.daily_rate


def test_the_gross_is_days_times_the_daily_rate() -> None:
    result = compute_encashment(
        policy=policy(cap_days=None, max_days_per_year=None),
        days_requested=D(10), available_balance=D(20),
        monthly_wages=D(52_000), is_exit=True,
    )
    assert result.gross_amount == D(20_000)


# ═══════════════════════════════════════════════════════════════════════════
# Tax treatment — configured, not inferred
# ═══════════════════════════════════════════════════════════════════════════


def test_in_service_encashment_is_fully_taxable_by_default() -> None:
    result = compute_encashment(
        policy=policy(cap_days=None, max_days_per_year=None,
                      min_balance_after_encashment=Decimal(0)),
        days_requested=D(10), available_balance=D(20),
        monthly_wages=D(52_000), is_exit=False,
    )
    assert result.exempt_amount == D(0)
    assert result.taxable_amount == result.gross_amount


def test_retirement_encashment_is_exempt_up_to_the_configured_ceiling() -> None:
    result = compute_encashment(
        policy=policy(
            exempt_on_retirement=True,
            retirement_exemption_ceiling=Decimal(15_000),
            cap_days=None, max_days_per_year=None,
        ),
        days_requested=D(10), available_balance=D(20),
        monthly_wages=D(52_000), is_exit=True, is_retirement=True,
    )
    assert result.gross_amount == D(20_000)
    assert result.exempt_amount == D(15_000)
    assert result.taxable_amount == D(5_000)


def test_a_retirement_policy_with_no_ceiling_is_fully_exempt() -> None:
    """The government-employee treatment, which is a policy setting here."""
    result = compute_encashment(
        policy=policy(
            exempt_on_retirement=True, retirement_exemption_ceiling=None,
            cap_days=None, max_days_per_year=None,
        ),
        days_requested=D(10), available_balance=D(20),
        monthly_wages=D(52_000), is_exit=True, is_retirement=True,
    )
    assert result.taxable_amount == D(0)
    assert result.exempt_amount == result.gross_amount


def test_a_resignation_gets_no_retirement_exemption() -> None:
    """The exemption is for retirement, not for every exit."""
    result = compute_encashment(
        policy=policy(
            exempt_on_retirement=True, retirement_exemption_ceiling=Decimal(15_000),
            cap_days=None, max_days_per_year=None,
        ),
        days_requested=D(10), available_balance=D(20),
        monthly_wages=D(52_000), is_exit=True, is_retirement=False,
    )
    assert result.exempt_amount == D(0)
    assert result.taxable_amount == result.gross_amount


def test_the_exemption_ceiling_is_configuration() -> None:
    low = compute_encashment(
        policy=policy(exempt_on_retirement=True,
                      retirement_exemption_ceiling=Decimal(5_000),
                      cap_days=None, max_days_per_year=None),
        days_requested=D(10), available_balance=D(20),
        monthly_wages=D(52_000), is_exit=True, is_retirement=True,
    )
    high = compute_encashment(
        policy=policy(exempt_on_retirement=True,
                      retirement_exemption_ceiling=Decimal(18_000),
                      cap_days=None, max_days_per_year=None),
        days_requested=D(10), available_balance=D(20),
        monthly_wages=D(52_000), is_exit=True, is_retirement=True,
    )
    assert low.taxable_amount > high.taxable_amount


# ═══════════════════════════════════════════════════════════════════════════
# Working
# ═══════════════════════════════════════════════════════════════════════════


def test_the_working_explains_what_limited_the_request() -> None:
    import json

    result = compute_encashment(
        policy=policy(cap_days=Decimal(10)),
        days_requested=D(40), available_balance=D(60),
        monthly_wages=D(52_000), is_exit=True,
    )
    working = result.as_working()

    json.dumps(working)
    assert working["limiting_factor"] == "cap_days"
    assert working["notes"]


def test_computation_is_deterministic() -> None:
    args = {
        "policy": policy(),
        "days_requested": D(10),
        "available_balance": D(20),
        "monthly_wages": D(52_000),
        "is_exit": True,
    }
    assert compute_encashment(**args).as_working() == compute_encashment(**args).as_working()
