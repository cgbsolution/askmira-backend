"""The formula evaluator is a security boundary, not a convenience.

Formulas are tenant-authored strings loaded from the database. If this
evaluator can be talked into executing anything, a tenant admin has remote code
execution on the payroll service. These tests are the guard on that.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.payroll.formula import (
    FormulaError,
    evaluate_formula,
    referenced_components,
    validate_formula,
)

VALUES = {"BASIC": Decimal("40000"), "HRA": Decimal("20000"), "CTC": Decimal("1200000")}


# ── Arithmetic ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "formula,expected",
    [
        ("BASIC", "40000"),
        ("BASIC * 0.5", "20000"),
        ("BASIC + HRA", "60000"),
        ("BASIC - HRA", "20000"),
        ("CTC / 12", "100000"),
        ("BASIC * 0.12", "4800"),
        ("(BASIC + HRA) * 0.1", "6000"),
        ("-BASIC + CTC / 12", "60000"),
        ("min(BASIC * 0.12, 1800)", "1800"),
        ("max(BASIC * 0.01, 500)", "500"),
        ("round(BASIC / 3)", "13333"),
        ("round(BASIC / 3, 2)", "13333.33"),
        ("BASIC * 2 ** 2", "160000"),
    ],
)
def test_arithmetic(formula: str, expected: str) -> None:
    assert evaluate_formula(formula, VALUES) == Decimal(expected)


def test_result_is_always_decimal() -> None:
    """A float leaking out here would defeat the whole point."""
    result = evaluate_formula("BASIC * 0.1", VALUES)
    assert isinstance(result, Decimal)


def test_decimal_literals_do_not_inherit_binary_float_error() -> None:
    """0.1 parsed through float is not 0.1. Through str(...) it is."""
    assert evaluate_formula("100 * 0.1", {}) == Decimal("10.0")
    assert evaluate_formula("0.1 + 0.2", {}) == Decimal("0.3")


# ── Rejection: unsafe constructs ────────────────────────────────────────────


@pytest.mark.parametrize(
    "formula",
    [
        "__import__('os').system('echo pwned')",
        "().__class__.__bases__[0].__subclasses__()",
        "open('/etc/passwd').read()",
        "eval('1+1')",
        "exec('x=1')",
        "BASIC.__class__",
        "[x for x in range(10)]",
        "lambda: 1",
        "BASIC if BASIC else HRA",
        "{'a': 1}",
        "BASIC[0]",
        "globals()",
        "print('hi')",
        "BASIC; HRA",
        "'string'",
        "f'{BASIC}'",
    ],
)
def test_unsafe_formulas_are_rejected(formula: str) -> None:
    with pytest.raises(FormulaError):
        evaluate_formula(formula, VALUES)


def test_only_allowlisted_functions_are_callable() -> None:
    with pytest.raises(FormulaError, match="not a permitted function"):
        evaluate_formula("sum(BASIC)", VALUES)


def test_keyword_arguments_are_rejected() -> None:
    with pytest.raises(FormulaError, match="keyword arguments"):
        evaluate_formula("round(BASIC, ndigits=2)", VALUES)


def test_an_absurd_exponent_is_rejected() -> None:
    """`2 ** 999999999` is a denial of service, not a salary calculation."""
    with pytest.raises(FormulaError, match="exponent"):
        evaluate_formula("2 ** 999999999", {})


def test_division_by_zero_is_a_clear_error_not_a_crash() -> None:
    with pytest.raises(FormulaError, match="divides by zero"):
        evaluate_formula("BASIC / 0", VALUES)


def test_an_over_long_formula_is_rejected() -> None:
    with pytest.raises(FormulaError, match="exceeds"):
        evaluate_formula("BASIC + " * 200 + "1", VALUES)


def test_an_empty_formula_is_rejected() -> None:
    with pytest.raises(FormulaError, match="empty"):
        evaluate_formula("   ", VALUES)


def test_malformed_syntax_is_reported_readably() -> None:
    with pytest.raises(FormulaError, match="not valid arithmetic"):
        evaluate_formula("BASIC * * 2", VALUES)


# ── Dependency extraction ───────────────────────────────────────────────────


def test_referenced_components_ignores_function_names() -> None:
    assert referenced_components("min(BASIC * 0.12, HRA)") == {"BASIC", "HRA"}
    assert "min" not in referenced_components("min(BASIC, 1)")


def test_referenced_components_finds_nested_names() -> None:
    assert referenced_components("(BASIC + HRA) * CTC / 12") == {"BASIC", "HRA", "CTC"}


def test_validate_formula_names_the_unknown_codes() -> None:
    with pytest.raises(FormulaError) as exc:
        validate_formula("BASIC + NOPE + ALSO_NOPE", {"BASIC"})
    message = str(exc.value)
    assert "NOPE" in message and "ALSO_NOPE" in message


def test_an_unresolved_name_at_evaluation_names_the_component() -> None:
    with pytest.raises(FormulaError, match="MISSING"):
        evaluate_formula("BASIC + MISSING", VALUES)
