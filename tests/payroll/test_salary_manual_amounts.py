"""Salary master: amounts typed for one employee, resolved by the CTC engine.

A typed amount replaces the structure's rule for that component, anything
calculated from it follows, and BALANCE still makes the breakdown sum to the
CTC exactly.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.payroll import CalculationType, ComponentType
from app.services.payroll.salary_structure import SalaryStructureError, resolve_ctc
from tests.payroll.conftest import component, structure


def _standard():
    return structure(
        component(
            "BASIC",
            calculation_type=CalculationType.PERCENTAGE_OF,
            base="CTC",
            value="40",
            counts_toward_pf=True,
            display_order=1,
        ),
        component(
            "HRA",
            calculation_type=CalculationType.PERCENTAGE_OF,
            base="BASIC",
            value="50",
            display_order=2,
        ),
        component("SPECIAL", calculation_type=CalculationType.BALANCE, display_order=9),
    )


def _amounts(breakdown) -> dict[str, Decimal]:
    return {c.code: c.annual_amount for c in breakdown.components}


def test_without_typed_amounts_nothing_is_marked_manual() -> None:
    breakdown = resolve_ctc(_standard(), Decimal("1200000"))
    assert not any(c.is_manual for c in breakdown.components)


def test_a_typed_basic_moves_hra_and_balance_keeps_the_ctc_exact() -> None:
    breakdown = resolve_ctc(_standard(), Decimal("1200000"), manual={"BASIC": Decimal("360000")})
    amounts = _amounts(breakdown)
    assert amounts["BASIC"] == Decimal("360000")
    assert amounts["HRA"] == Decimal("180000")  # still 50% of the typed basic
    assert amounts["SPECIAL"] == Decimal("660000")
    assert breakdown.annual_total_in_ctc == Decimal("1200000")
    assert breakdown.by_code("BASIC").is_manual
    assert not breakdown.by_code("HRA").is_manual


def test_a_typed_dependent_stops_following_its_base() -> None:
    breakdown = resolve_ctc(_standard(), Decimal("1200000"), manual={"HRA": Decimal("100000")})
    amounts = _amounts(breakdown)
    assert amounts["BASIC"] == Decimal("480000")
    assert amounts["HRA"] == Decimal("100000")
    assert amounts["SPECIAL"] == Decimal("620000")


def test_a_wage_type_outside_the_structure_joins_and_balance_absorbs_it() -> None:
    telephone = component("TELEPHONE", name="Telephone allowance", display_order=5)
    breakdown = resolve_ctc(
        _standard(),
        Decimal("1200000"),
        manual={"TELEPHONE": Decimal("12000")},
        extras=[telephone],
    )
    amounts = _amounts(breakdown)
    assert amounts["TELEPHONE"] == Decimal("12000")
    # 12,00,000 - 4,80,000 basic - 2,40,000 HRA - 12,000 telephone
    assert amounts["SPECIAL"] == Decimal("468000")
    assert breakdown.annual_total_in_ctc == Decimal("1200000")
    assert breakdown.by_code("TELEPHONE").monthly_amount == Decimal("1000")


def test_an_extra_outside_ctc_is_paid_on_top() -> None:
    fuel = component(
        "FUEL",
        name="Fuel reimbursement",
        component_type=ComponentType.REIMBURSEMENT,
        part_of_ctc=False,
    )
    breakdown = resolve_ctc(
        _standard(), Decimal("1200000"), manual={"FUEL": Decimal("24000")}, extras=[fuel]
    )
    assert _amounts(breakdown)["SPECIAL"] == Decimal("480000")  # untouched by the extra
    assert breakdown.annual_total_in_ctc == Decimal("1200000")
    assert breakdown.by_code("FUEL").annual_amount == Decimal("24000")


def test_the_balance_component_cannot_be_typed() -> None:
    with pytest.raises(SalaryStructureError, match="balance of the CTC"):
        resolve_ctc(_standard(), Decimal("1200000"), manual={"SPECIAL": Decimal("1")})


def test_typed_amounts_beyond_the_ctc_are_refused() -> None:
    with pytest.raises(SalaryStructureError, match="exceeds the CTC"):
        resolve_ctc(_standard(), Decimal("1200000"), manual={"BASIC": Decimal("1000000")})


def test_an_unknown_code_is_refused() -> None:
    with pytest.raises(SalaryStructureError, match="not a wage type"):
        resolve_ctc(_standard(), Decimal("1200000"), manual={"NOPE": Decimal("1")})


def test_an_extra_without_an_amount_is_refused() -> None:
    with pytest.raises(SalaryStructureError, match="needs an amount"):
        resolve_ctc(_standard(), Decimal("1200000"), extras=[component("TELEPHONE")])


def test_an_amount_finer_than_the_rounding_is_refused() -> None:
    with pytest.raises(SalaryStructureError, match="finer than"):
        resolve_ctc(_standard(), Decimal("1200000"), manual={"BASIC": Decimal("360000.50")})


def test_a_typed_zero_removes_the_component_from_the_pay() -> None:
    breakdown = resolve_ctc(_standard(), Decimal("1200000"), manual={"HRA": Decimal("0")})
    assert _amounts(breakdown)["HRA"] == Decimal("0")
    assert _amounts(breakdown)["SPECIAL"] == Decimal("720000")
