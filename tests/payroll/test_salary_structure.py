"""Slice 01 gate: the CTC resolution engine.

These are the tests the prompt names as the gate, not an afterthought. Each one
maps to a line on the verification checklist.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.payroll import CalculationType, ComponentType, RoundingMode
from app.services.payroll.salary_structure import (
    SalaryStructureError,
    resolve_ctc,
    validate_structure,
)
from tests.payroll.conftest import component, structure

# ── The six structure shapes ────────────────────────────────────────────────
# Deliberately varied: flat, nested percentages, formula-driven, non-CTC
# reimbursements, and awkward CTCs that do not divide cleanly.


def shape_simple():
    """Basic as a percentage of CTC, everything else the balance."""
    return structure(
        component("BASIC", calculation_type=CalculationType.PERCENTAGE_OF, base="CTC",
                  value="40", counts_toward_pf=True, display_order=1),
        component("SPECIAL", calculation_type=CalculationType.BALANCE, display_order=9),
    )


def shape_nested_percentage():
    """HRA = 50% of BASIC, where BASIC = 40% of CTC — the nested case."""
    return structure(
        component("BASIC", calculation_type=CalculationType.PERCENTAGE_OF, base="CTC",
                  value="40", counts_toward_pf=True, display_order=1),
        component("HRA", calculation_type=CalculationType.PERCENTAGE_OF, base="BASIC",
                  value="50", display_order=2),
        component("SPECIAL", calculation_type=CalculationType.BALANCE, display_order=9),
    )


def shape_with_da():
    """Basic + DA, both in the PF wage base — the case the PF fix depends on."""
    return structure(
        component("BASIC", calculation_type=CalculationType.PERCENTAGE_OF, base="CTC",
                  value="35", counts_toward_pf=True, counts_toward_esi=True, display_order=1),
        component("DA", calculation_type=CalculationType.PERCENTAGE_OF, base="BASIC",
                  value="20", counts_toward_pf=True, counts_toward_esi=True, display_order=2),
        component("HRA", calculation_type=CalculationType.PERCENTAGE_OF, base="BASIC",
                  value="50", counts_toward_esi=True, display_order=3),
        component("SPECIAL", calculation_type=CalculationType.BALANCE, display_order=9),
    )


def shape_formula():
    """A formula component with min(), the shape used for capped allowances."""
    return structure(
        component("BASIC", calculation_type=CalculationType.PERCENTAGE_OF, base="CTC",
                  value="40", counts_toward_pf=True, display_order=1),
        component("LTA", calculation_type=CalculationType.FORMULA,
                  formula="min(BASIC * 0.1, 60000)", display_order=2),
        component("SPECIAL", calculation_type=CalculationType.BALANCE, display_order=9),
    )


def shape_fixed_and_employer():
    """A fixed allowance plus an employer contribution that is inside CTC."""
    return structure(
        component("BASIC", calculation_type=CalculationType.PERCENTAGE_OF, base="CTC",
                  value="40", counts_toward_pf=True, display_order=1),
        component("CONVEYANCE", calculation_type=CalculationType.FIXED, value="19200",
                  display_order=2),
        component("PF_ER", name="PF employer",
                  component_type=ComponentType.EMPLOYER_CONTRIBUTION,
                  calculation_type=CalculationType.FORMULA,
                  formula="min(BASIC * 0.12, 21600)", pro_rate_on_lop=False, display_order=3),
        component("SPECIAL", calculation_type=CalculationType.BALANCE, display_order=9),
    )


def shape_with_reimbursement():
    """A reimbursement outside CTC — must not consume the balance."""
    return structure(
        component("BASIC", calculation_type=CalculationType.PERCENTAGE_OF, base="CTC",
                  value="45", counts_toward_pf=True, display_order=1),
        component("FUEL", component_type=ComponentType.REIMBURSEMENT,
                  calculation_type=CalculationType.FIXED, value="24000",
                  part_of_ctc=False, taxable=False, display_order=2),
        component("SPECIAL", calculation_type=CalculationType.BALANCE, display_order=9),
    )


ALL_SHAPES = [
    ("simple", shape_simple),
    ("nested_percentage", shape_nested_percentage),
    ("basic_plus_da", shape_with_da),
    ("formula_with_cap", shape_formula),
    ("fixed_and_employer", shape_fixed_and_employer),
    ("with_reimbursement", shape_with_reimbursement),
]

# CTCs chosen to be awkward: two divide cleanly by 12, the rest do not, and
# several force a rounding residual on every percentage in the structure.
CTCS = ["1200000", "1250000", "987654", "1000001", "749999", "633337"]


@pytest.mark.parametrize("shape_name,builder", ALL_SHAPES)
@pytest.mark.parametrize("ctc", CTCS)
def test_resolution_sums_exactly_to_ctc(shape_name: str, builder, ctc: str) -> None:
    """The core guarantee, across all six shapes and six CTCs."""
    breakdown = resolve_ctc(builder(), Decimal(ctc))
    assert breakdown.annual_total_in_ctc == Decimal(ctc), (
        f"{shape_name} at CTC {ctc} summed to {breakdown.annual_total_in_ctc}"
    )


@pytest.mark.parametrize("shape_name,builder", ALL_SHAPES)
def test_no_component_is_negative(shape_name: str, builder) -> None:
    breakdown = resolve_ctc(builder(), Decimal("1200000"))
    assert all(c.annual_amount >= 0 for c in breakdown.components)


def test_nested_percentage_resolves_against_the_unrounded_parent() -> None:
    """HRA = 50% of BASIC where BASIC = 40% of CTC."""
    breakdown = resolve_ctc(shape_nested_percentage(), Decimal("1200000"))

    basic = breakdown.by_code("BASIC")
    hra = breakdown.by_code("HRA")
    assert basic is not None and hra is not None
    assert basic.annual_amount == Decimal("480000")
    assert hra.annual_amount == Decimal("240000")
    # And the balance takes the rest, exactly.
    assert breakdown.by_code("SPECIAL").annual_amount == Decimal("480000")


def test_reimbursement_outside_ctc_does_not_consume_the_balance() -> None:
    breakdown = resolve_ctc(shape_with_reimbursement(), Decimal("1000000"))

    fuel = breakdown.by_code("FUEL")
    assert fuel is not None
    assert fuel.annual_amount == Decimal("24000")
    assert not fuel.part_of_ctc
    # BASIC 450000 + SPECIAL 550000 = 1000000; FUEL sits outside.
    assert breakdown.annual_total_in_ctc == Decimal("1000000")
    assert breakdown.by_code("SPECIAL").annual_amount == Decimal("550000")


# ── Circular references ─────────────────────────────────────────────────────


def test_direct_cycle_is_rejected_with_the_cycle_named() -> None:
    """A stack overflow would be a bug; a named cycle is a usable error."""
    bad = structure(
        component("BASIC", calculation_type=CalculationType.PERCENTAGE_OF, base="HRA",
                  value="40", display_order=1),
        component("HRA", calculation_type=CalculationType.PERCENTAGE_OF, base="BASIC",
                  value="50", display_order=2),
        component("SPECIAL", calculation_type=CalculationType.BALANCE, display_order=9),
    )

    with pytest.raises(SalaryStructureError) as exc:
        validate_structure(bad)

    message = str(exc.value)
    assert "Circular reference" in message
    assert "BASIC" in message and "HRA" in message
    assert "->" in message, "the cycle chain itself must be in the message"


def test_indirect_cycle_through_a_formula_is_rejected() -> None:
    bad = structure(
        component("A", calculation_type=CalculationType.PERCENTAGE_OF, base="C", value="10",
                  display_order=1),
        component("B", calculation_type=CalculationType.FORMULA, formula="A * 2", display_order=2),
        component("C", calculation_type=CalculationType.FORMULA, formula="B + 100",
                  display_order=3),
        component("SPECIAL", calculation_type=CalculationType.BALANCE, display_order=9),
    )

    with pytest.raises(SalaryStructureError, match="Circular reference"):
        validate_structure(bad)


def test_self_reference_is_rejected() -> None:
    bad = structure(
        component("BASIC", calculation_type=CalculationType.FORMULA, formula="BASIC * 1.1",
                  display_order=1),
        component("SPECIAL", calculation_type=CalculationType.BALANCE, display_order=9),
    )

    with pytest.raises(SalaryStructureError, match="Circular reference"):
        validate_structure(bad)


# ── BALANCE cardinality ─────────────────────────────────────────────────────


def test_zero_balance_components_is_rejected_at_save() -> None:
    bad = structure(
        component("BASIC", calculation_type=CalculationType.PERCENTAGE_OF, base="CTC",
                  value="40", display_order=1),
        component("HRA", calculation_type=CalculationType.PERCENTAGE_OF, base="BASIC",
                  value="50", display_order=2),
    )

    with pytest.raises(SalaryStructureError, match="exactly one BALANCE"):
        validate_structure(bad)


def test_multiple_balance_components_is_rejected_at_save() -> None:
    bad = structure(
        component("BASIC", calculation_type=CalculationType.PERCENTAGE_OF, base="CTC",
                  value="40", display_order=1),
        component("SPECIAL", calculation_type=CalculationType.BALANCE, display_order=8),
        component("OTHER", calculation_type=CalculationType.BALANCE, display_order=9),
    )

    with pytest.raises(SalaryStructureError) as exc:
        validate_structure(bad)
    assert "only one BALANCE" in str(exc.value)
    assert "SPECIAL" in str(exc.value) and "OTHER" in str(exc.value)


def test_a_dangling_percentage_base_is_rejected() -> None:
    bad = structure(
        component("HRA", calculation_type=CalculationType.PERCENTAGE_OF, base="NOPE",
                  value="50", display_order=1),
        component("SPECIAL", calculation_type=CalculationType.BALANCE, display_order=9),
    )

    with pytest.raises(SalaryStructureError, match="NOPE"):
        validate_structure(bad)


def test_components_exceeding_ctc_are_rejected_rather_than_going_negative() -> None:
    over = structure(
        component("BASIC", calculation_type=CalculationType.FIXED, value="900000",
                  display_order=1),
        component("BONUS", calculation_type=CalculationType.FIXED, value="400000",
                  display_order=2),
        component("SPECIAL", calculation_type=CalculationType.BALANCE, display_order=9),
    )

    with pytest.raises(SalaryStructureError) as exc:
        resolve_ctc(over, Decimal("1000000"))
    assert "exceeds the CTC" in str(exc.value)


# ── Rounding ────────────────────────────────────────────────────────────────


def test_rounding_residual_lands_in_balance_and_the_total_still_matches() -> None:
    """A CTC that cannot divide cleanly is where rounding drift would show."""
    # 33.33% of 1000001 is 333333.4233 — rounds down, leaving a residual.
    shape = structure(
        component("BASIC", calculation_type=CalculationType.PERCENTAGE_OF, base="CTC",
                  value="33.33", display_order=1),
        component("HRA", calculation_type=CalculationType.PERCENTAGE_OF, base="BASIC",
                  value="66.67", display_order=2),
        component("SPECIAL", calculation_type=CalculationType.BALANCE, display_order=9),
    )
    breakdown = resolve_ctc(shape, Decimal("1000001"))

    assert breakdown.annual_total_in_ctc == Decimal("1000001")
    # Every non-balance component is a whole rupee at precision 0.
    for c in breakdown.components:
        if c.code != "SPECIAL":
            assert c.annual_amount == c.annual_amount.to_integral_value()


@pytest.mark.parametrize(
    "mode", [RoundingMode.HALF_UP, RoundingMode.HALF_EVEN, RoundingMode.CEILING, RoundingMode.FLOOR]
)
def test_every_rounding_mode_still_reconciles(mode: RoundingMode) -> None:
    """Whichever policy a tenant picks, the total must still equal CTC."""
    shape = shape_with_da()
    shape.rounding_mode = mode
    breakdown = resolve_ctc(shape, Decimal("987654"))
    assert breakdown.annual_total_in_ctc == Decimal("987654")


def test_paise_precision_reconciles() -> None:
    shape = shape_nested_percentage()
    shape.rounding_precision = 2
    breakdown = resolve_ctc(shape, Decimal("750000.50"))
    assert breakdown.annual_total_in_ctc == Decimal("750000.50")


def test_monthly_column_reconciles_to_a_twelfth_of_ctc() -> None:
    breakdown = resolve_ctc(shape_with_da(), Decimal("1200000"))
    assert breakdown.monthly_total_in_ctc == Decimal("100000")


# ── Decimal discipline ──────────────────────────────────────────────────────


def test_a_float_ctc_is_refused() -> None:
    """Accepting a float here would silently reintroduce binary drift."""
    with pytest.raises(SalaryStructureError, match="must be a Decimal"):
        resolve_ctc(shape_simple(), 1200000.0)  # type: ignore[arg-type]


def test_zero_or_negative_ctc_is_refused() -> None:
    with pytest.raises(SalaryStructureError, match="greater than zero"):
        resolve_ctc(shape_simple(), Decimal("0"))


def test_a_ctc_finer_than_the_structure_rounding_is_refused() -> None:
    """50 paise has nowhere to go in a whole-rupee structure.

    Refusing is the honest answer: the alternative is a BALANCE component
    carrying paise in a structure that rounds to rupees, or a silently lost
    50 paise. Both are worse than an error the admin can act on.
    """
    with pytest.raises(SalaryStructureError, match="finer than this structure"):
        resolve_ctc(shape_simple(), Decimal("750000.50"))

    # The same CTC resolves fine once the structure is told to keep paise.
    paise = shape_simple()
    paise.rounding_precision = 2
    assert resolve_ctc(paise, Decimal("750000.50")).annual_total_in_ctc == Decimal("750000.50")


def test_resolution_is_deterministic() -> None:
    """The same structure and CTC must always produce the same breakdown."""
    first = resolve_ctc(shape_with_da(), Decimal("1234567"))
    second = resolve_ctc(shape_with_da(), Decimal("1234567"))
    assert [(c.code, c.annual_amount) for c in first.components] == [
        (c.code, c.annual_amount) for c in second.components
    ]


# ── Materialisation immutability ────────────────────────────────────────────


def test_editing_a_structure_does_not_change_an_existing_materialised_salary() -> None:
    """The checklist item: prior rows stay byte-identical.

    Materialised amounts are the payroll engine's input. If a structure edit
    could reach back into them, every historical payroll run would become
    irreproducible the moment someone adjusted an HRA percentage.
    """
    from tests.payroll.conftest import employee_salary_from

    shape = shape_nested_percentage()
    salary = employee_salary_from(resolve_ctc(shape, Decimal("1200000")), Decimal("1200000"))
    before = [(c.component_code, c.annual_amount, c.monthly_amount) for c in salary.components]

    # Change HRA from 50% of BASIC to 60%, as an HR admin might.
    hra_link = next(link for link in shape.components if link.component.code == "HRA")
    hra_link.override_value = Decimal("60")

    # The structure now resolves differently...
    reresolved = resolve_ctc(shape, Decimal("1200000"))
    assert reresolved.by_code("HRA").annual_amount == Decimal("288000")

    # ...but the already-materialised salary is untouched.
    after = [(c.component_code, c.annual_amount, c.monthly_amount) for c in salary.components]
    assert after == before
    assert next(c for c in salary.components if c.component_code == "HRA").annual_amount == Decimal(
        "240000"
    )
