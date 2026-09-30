"""Form 16 Part B, from a year of approved payslips.

What these hold: the year's pay is summed from what payslips recorded, not
from a salary record; one-off pay and perquisites land under the right head;
section 16 deductions are kept apart from Chapter VI-A; and the lines
reconcile to the computation's taxable income or nothing is produced.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.services.tax.computation import ProjectionInput, TaxRuleError, project
from app.services.tax.form16 import (
    SlipFacts,
    aggregate_year_pay,
    form16_html,
    part_b,
    year_projection_input,
)


def slip(month: int, *, taxable="100000", tds="5000", pt="200", inputs=(), hra="20000"):
    year = 2025 if month >= 4 else 2026
    return SlipFacts(
        period_year=year,
        period_month=month,
        gross_earnings=Decimal("100000"),
        pf_wage_base=Decimal("40000"),
        professional_tax=Decimal(pt),
        tds=Decimal(tds),
        line_items=[
            {"code": "BASIC", "type": "earning", "amount": "40000"},
            {"code": "HRA", "type": "earning", "amount": hra},
        ],
        input_lines=list(inputs),
        tds_working={"projection": {"this_month_taxable_pay": taxable}},
    )


YEAR = [slip(m) for m in (4, 5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3)]


def test_the_year_is_summed_from_the_payslips() -> None:
    bonus = {"kind": "earning", "taxable_amount": "30000", "amount": "30000"}
    perk = {"kind": "perquisite", "taxable_amount": "5000", "amount": "5000"}
    claim = {"kind": "reimbursement", "taxable_amount": "0", "amount": "4000"}
    skipped = {"kind": "earning", "taxable_amount": "999", "amount": "999", "skipped": True}
    slips = [*YEAR[:-1], slip(3, inputs=(bonus, perk, claim, skipped))]
    pay = aggregate_year_pay(slips)
    assert pay.months == 12
    assert pay.first_period == (2025, 4) and pay.last_period == (2026, 3)
    assert pay.salary == Decimal("1230000.00"), "structure pay plus the taxable bonus"
    assert pay.perquisites == Decimal("5000.00")
    assert pay.basic_plus_da == Decimal("480000.00")
    assert pay.hra == Decimal("240000.00")
    assert pay.professional_tax == Decimal("2400.00")
    assert pay.tds_deducted == Decimal("60000.00")


def test_a_payslip_from_before_tds_falls_back_to_its_gross() -> None:
    old = replace(slip(4), tds_working=None)
    assert aggregate_year_pay([old]).salary == Decimal("100000.00")


def test_part_b_reconciles_under_the_new_regime(rules) -> None:
    pay = aggregate_year_pay(YEAR)
    base = ProjectionInput(regime="new", gross_salary=Decimal(0), basic_plus_da=Decimal(0))
    inputs = year_projection_input(base, pay)
    computation = project(inputs, rules)
    statement = part_b(pay=pay, inputs=inputs, computation=computation)
    lines = {line["key"]: Decimal(line["amount"]) for line in statement["lines"]}

    assert lines["1a"] == Decimal("1200000.00")
    assert lines["2"] == Decimal("0.00"), "no HRA exemption in the new regime"
    assert lines["4a"] == Decimal("75000.00")
    assert lines["4c"] == Decimal("0.00"), "professional tax is not deductible here"
    assert lines["12"] == computation.taxable_income
    assert lines["17"] == computation.annual_tax_liability
    assert statement["tds"]["deducted_by_this_employer"] == "60000.00"
    assert Decimal(statement["tds"]["balance"]) == computation.annual_tax_liability - Decimal(
        "60000"
    )


def test_part_b_splits_section_16_from_chapter_via_under_the_old_regime(rules) -> None:
    pay = aggregate_year_pay(YEAR)
    base = ProjectionInput(
        regime="old",
        gross_salary=Decimal(0),
        basic_plus_da=Decimal(0),
        declarations=(("80C", Decimal("150000"), Decimal("150000")),),
        annual_rent=Decimal("300000"),
        is_metro=True,
    )
    inputs = year_projection_input(base, pay)
    computation = project(inputs, rules)
    statement = part_b(pay=pay, inputs=inputs, computation=computation)
    lines = {line["key"]: Decimal(line["amount"]) for line in statement["lines"]}

    assert lines["2"] == computation.total_exemptions > 0, "the HRA exemption"
    assert lines["4c"] == Decimal("2400.00"), "professional tax under section 16(iii)"
    assert [row["section"] for row in statement["chapter_via"]] == ["80C"]
    assert lines["12"] == computation.taxable_income
    assert statement["hra"] is not None


def test_income_paid_through_payroll_is_not_counted_twice(rules) -> None:
    arrears = {"kind": "earning", "taxable_amount": "30000", "amount": "30000"}
    pay = aggregate_year_pay([*YEAR[:-1], slip(3, inputs=(arrears,))])
    base = ProjectionInput(
        regime="new",
        gross_salary=Decimal(0),
        basic_plus_da=Decimal(0),
        other_income=(
            ("ARREARS", "Salary arrears received", Decimal("30000"), "arrears", "b"),
            ("GRATUITY_TAXABLE", "Gratuity above the exemption", Decimal("10000"),
             "exit_benefits", "b"),
        ),
    )
    inputs = year_projection_input(base, pay)
    assert [line[0] for line in inputs.other_income] == ["GRATUITY_TAXABLE"]
    statement = part_b(pay=pay, inputs=inputs, computation=project(inputs, rules))
    lines = {line["key"]: Decimal(line["amount"]) for line in statement["lines"]}
    assert lines["1a"] == Decimal("1230000.00")
    assert lines["1c"] == Decimal("10000.00"), "gratuity is profits in lieu of salary"


def test_part_b_that_does_not_reconcile_is_refused(rules) -> None:
    pay = aggregate_year_pay(YEAR)
    inputs = year_projection_input(
        ProjectionInput(regime="new", gross_salary=Decimal(0), basic_plus_da=Decimal(0)), pay
    )
    computation = project(inputs, rules)
    computation.taxable_income = computation.taxable_income + Decimal(1)
    with pytest.raises(TaxRuleError, match="does not reconcile"):
        part_b(pay=pay, inputs=inputs, computation=computation)


def test_the_page_escapes_and_labels_itself_honestly(rules) -> None:
    pay = aggregate_year_pay(YEAR)
    inputs = year_projection_input(
        ProjectionInput(regime="new", gross_salary=Decimal(0), basic_plus_da=Decimal(0)), pay
    )
    statement = {
        "financial_year": "2025-26",
        "assessment_year": "2026-27",
        "period_from": "2025-04-01",
        "period_to": "2026-03-31",
        "employer": {"name": "Acme <b>", "address": None, "pan": None, "tan": "BLRA12345B"},
        "employee": {"name": "Meera", "code": "E1", "pan": None, "designation": None},
        **part_b(pay=pay, inputs=inputs, computation=project(inputs, rules)),
    }
    page = form16_html(statement)
    assert "Part B" in page and "unsigned copy" in page
    assert "TRACES" in page
    assert "Acme &lt;b&gt;" in page
