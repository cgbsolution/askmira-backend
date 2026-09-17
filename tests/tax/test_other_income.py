"""Taxable income from outside the salary structure reaches the projection.

Closes 08 §4 (gratuity/encashment), 12 §4 (arrears) and the slice 14 perquisite
and unclaimed-entitlement routing at the engine: whatever the gatherer finds is
added to gross exactly once and shown with its basis.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.tax.computation import ProjectionInput, project


def _inputs(**overrides) -> ProjectionInput:
    base = {
        "regime": "new",
        "gross_salary": Decimal("1200000"),
        "basic_plus_da": Decimal("480000"),
    }
    base.update(overrides)
    return ProjectionInput(**base)


def test_other_income_is_added_to_gross_and_raises_the_liability(rules) -> None:
    plain = project(_inputs(), rules)
    with_gratuity = project(
        _inputs(
            other_income=(
                ("GRATUITY_TAXABLE", "Gratuity above the exemption", Decimal("300000"),
                 "exit_benefits", "Gratuity of 2300000; 2000000 exempt."),
            )
        ),
        rules,
    )
    assert with_gratuity.gross_annual_income == plain.gross_annual_income + Decimal("300000")
    assert with_gratuity.annual_tax_liability > plain.annual_tax_liability
    assert with_gratuity.other_income[0]["code"] == "GRATUITY_TAXABLE"
    assert with_gratuity.other_income[0]["amount"] == "300000.00"
    assert any("outside the salary structure" in note for note in with_gratuity.notes)


def test_every_line_is_counted_once(rules) -> None:
    lines = (
        ("ARREARS", "Arrears", Decimal("10000"), "arrears", "basis"),
        ("LOAN_PERQUISITE", "Perquisite", Decimal("2500.50"), "loans", "basis"),
    )
    result = project(_inputs(other_income=lines), rules)
    assert result.gross_annual_income == Decimal("1212500.50")


def test_pending_amounts_are_shown_but_never_taxed(rules) -> None:
    plain = project(_inputs(), rules)
    pending = project(
        _inputs(
            pending_if_unclaimed=(
                ("REIMBURSEMENT_UNCLAIMED", "Unclaimed fuel", Decimal("24000"),
                 "reimbursements", "Taxable if still unclaimed at year end."),
            )
        ),
        rules,
    )
    assert pending.gross_annual_income == plain.gross_annual_income
    assert pending.annual_tax_liability == plain.annual_tax_liability
    working = pending.as_working()
    assert working["pending_if_unclaimed"][0]["amount"] == "24000.00"
    assert working["other_income"] == []


def test_gatherer_notes_reach_the_working(rules) -> None:
    result = project(_inputs(income_notes=("No approved loan-perquisite rule set.",)), rules)
    assert "No approved loan-perquisite rule set." in result.as_working()["notes"]
