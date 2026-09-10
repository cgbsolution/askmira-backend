"""A restricted arithmetic evaluator for salary component formulas.

Formulas are tenant-authored configuration. They reach this module from the
database, which means an evaluator built on `eval` would be remote code
execution with extra steps. This walks a parsed AST instead and permits exactly
one shape of expression: arithmetic over component codes and numeric literals,
plus `min`, `max` and `round`.

Everything else — attribute access, subscripting, calls to anything not on the
allow-list, comprehensions, lambdas, names that are not component codes — is
rejected at parse time with a message an HR admin can act on.

All arithmetic is `Decimal`. There is no float in this file, and there must not
be: a rounding drift of one paisa across a payroll run is a real dispute.
"""

from __future__ import annotations

import ast
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Final

__all__ = ["FormulaError", "evaluate_formula", "referenced_components", "validate_formula"]


class FormulaError(ValueError):
    """A formula is malformed, references something unknown, or is unsafe."""


# Functions a formula may call. `round` here is Decimal-aware, not the builtin.
_ALLOWED_FUNCTIONS: Final = {"min", "max", "round"}

_ALLOWED_NODES: Final = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Call,
)

# A formula this long is a configuration mistake, and parsing is not free.
_MAX_LENGTH: Final = 512


def _parse(formula: str) -> ast.Expression:
    if not formula or not formula.strip():
        raise FormulaError("Formula is empty")
    if len(formula) > _MAX_LENGTH:
        raise FormulaError(f"Formula exceeds {_MAX_LENGTH} characters")

    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"Formula is not valid arithmetic: {exc.msg}") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise FormulaError(
                f"'{type(node).__name__}' is not allowed in a formula. "
                "Use arithmetic over component codes, with min, max or round."
            )
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCTIONS:
                name = getattr(node.func, "id", "expression")
                raise FormulaError(
                    f"'{name}' is not a permitted function. "
                    f"Allowed: {', '.join(sorted(_ALLOWED_FUNCTIONS))}."
                )
            if node.keywords:
                raise FormulaError("Formula functions do not take keyword arguments")
        if isinstance(node, ast.Constant) and not isinstance(node.value, int | float):
            raise FormulaError("Only numeric literals are allowed in a formula")

    return tree


def referenced_components(formula: str) -> set[str]:
    """Component codes a formula depends on.

    Used to build the dependency graph before resolution, so ordering and cycle
    detection happen without evaluating anything.
    """
    tree = _parse(formula)
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id not in _ALLOWED_FUNCTIONS
    }


def validate_formula(formula: str, known_codes: set[str]) -> set[str]:
    """Parse a formula and confirm every name it uses is a known component.

    Returns the referenced codes. Raises `FormulaError` naming the unknown
    codes, so the error tells the admin which line to fix.
    """
    referenced = referenced_components(formula)
    unknown = referenced - known_codes
    if unknown:
        raise FormulaError(
            f"Formula references unknown component(s): {', '.join(sorted(unknown))}"
        )
    return referenced


def evaluate_formula(formula: str, values: dict[str, Decimal]) -> Decimal:
    """Evaluate a formula against already-resolved component amounts.

    Args:
        formula: e.g. `"BASIC * 0.5"` or `"min(BASIC * 0.12, 1800)"`.
        values: resolved amounts keyed by component code. Every name the
            formula references must be present — resolution order is the
            caller's responsibility (see `salary_structure.py`).

    Raises:
        FormulaError: on a malformed or unsafe formula, an unresolved name, or
            a division by zero.
    """
    tree = _parse(formula)
    return _eval_node(tree.body, values)


def _eval_node(node: ast.AST, values: dict[str, Decimal]) -> Decimal:
    if isinstance(node, ast.Constant):
        # str(...) rather than Decimal(float) — Decimal(0.1) is not 0.1.
        return Decimal(str(node.value))

    if isinstance(node, ast.Name):
        if node.id not in values:
            raise FormulaError(
                f"Component '{node.id}' has no value at the point this formula is evaluated. "
                "Check the dependency order."
            )
        return values[node.id]

    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, values)
        return -operand if isinstance(node.op, ast.USub) else operand

    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, values)
        right = _eval_node(node.right, values)
        try:
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                if right == 0:
                    raise FormulaError("Formula divides by zero")
                return left / right
            if isinstance(node.op, ast.Pow):
                # Guarded: a large exponent is a denial-of-service, not a
                # salary calculation.
                if not (-10 <= right <= 10):
                    raise FormulaError("Formula exponent must be between -10 and 10")
                return left**right
        except (InvalidOperation, DivisionByZero, OverflowError) as exc:
            raise FormulaError(f"Formula arithmetic failed: {exc}") from exc

    if isinstance(node, ast.Call):
        name = node.func.id  # type: ignore[union-attr]  — checked in _parse
        args = [_eval_node(arg, values) for arg in node.args]
        if name == "min":
            if not args:
                raise FormulaError("min() needs at least one argument")
            return min(args)
        if name == "max":
            if not args:
                raise FormulaError("max() needs at least one argument")
            return max(args)
        if name == "round":
            if len(args) not in (1, 2):
                raise FormulaError("round() takes one or two arguments")
            places = int(args[1]) if len(args) == 2 else 0
            return args[0].quantize(Decimal(1).scaleb(-places))

    raise FormulaError(f"'{type(node).__name__}' is not allowed in a formula")
