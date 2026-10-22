"""A rule set is refused at entry when payroll could not read it as meant.

Shape only, never values: a misspelt key or a slab table that does not climb
would otherwise be approved and then charge everyone the first slab (or
nothing) without a word.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.services.payroll.rule_sets import refuse_unreadable, rule_problems

KA_PT = {
    "slabs": [
        {"up_to": 24999, "amount": 0},
        {"up_to": None, "amount": 200, "month_amounts": {"2": 300}},
    ]
}
MH_PT = {
    "slabs": [
        {"up_to": 7500, "amount": 0},
        {"up_to": 10000, "amount": 175},
        {"up_to": None, "amount": 200, "month_amounts": {"2": 300}},
    ],
    "slabs_by_gender": {
        "female": [
            {"up_to": 25000, "amount": 0},
            {"up_to": None, "amount": 200, "month_amounts": {"2": 300}},
        ]
    },
}
PF = {
    "wage_ceiling": 15000,
    "employee_rate": 12,
    "employer_rate": 12,
    "eps_wage_ceiling": 15000,
    "edli_wage_ceiling": 15000,
    "eps_rate": 8.33,
}
LWF = {
    "applicable": True,
    "deduction_months": [6, 12],
    "employee_amount": 25,
    "employer_amount": 75,
}


@pytest.mark.parametrize(
    ("rule_type", "rules"),
    [
        ("professional_tax", KA_PT),
        ("professional_tax", MH_PT),
        ("pf", PF),
        ("pf", {**PF, "contribution_rounding": "paise"}),
        ("esi", {"wage_threshold": 21000, "employee_rate": 0.75, "employer_rate": "3.25"}),
        ("labour_welfare_fund", LWF),
        ("labour_welfare_fund", {"applicable": False}),
        (
            "labour_welfare_fund",
            {
                "applicable": True,
                "deduction_months": [12],
                "wage_bands": [
                    {"up_to": 3000, "employee_amount": 6, "employer_amount": 18},
                    {"up_to": None, "employee_amount": 12, "employer_amount": 36},
                ],
            },
        ),
    ],
)
def test_rule_sets_payroll_reads_pass(rule_type: str, rules: dict) -> None:
    assert rule_problems(rule_type, rules) == []


def test_slabs_with_other_key_names_are_refused_not_read_as_one_open_slab() -> None:
    problems = rule_problems(
        "professional_tax", {"slabs": [{"min": "0", "max": "24999", "amount": "0"}]}
    )
    assert any("slab 1 has no up_to" in p for p in problems)
    assert any("slab 1 has max, min, which payroll does not read" in p for p in problems)


@pytest.mark.parametrize(
    ("slabs", "expected"),
    [
        ([{"up_to": 24999}, {"up_to": None, "amount": 200}], "slab 1 needs an amount"),
        ([{"up_to": 24999, "amount": "two hundred"}], "slab 1 needs an amount"),
        ([{"up_to": None, "amount": 0}, {"up_to": None, "amount": 200}], "no upper bound"),
        ([{"up_to": 20000, "amount": 0}, {"up_to": 10000, "amount": 1}], "must be above"),
        ([{"up_to": 24999, "amount": 0}, {"up_to": 99999, "amount": 200}], "needs up_to null"),
        ([{"up_to": None, "amount": 200, "month_amount": {"2": 300}}], "month_amount"),
        ([{"up_to": None, "amount": 200, "month_amounts": {"02": 300}}], "month_amounts maps"),
        ([{"up_to": None, "amount": 200, "month_amounts": {"Feb": 300}}], "month_amounts maps"),
        ([], "must be a list of slabs"),
        ({"up_to": None, "amount": 200}, "must be a list of slabs"),
    ],
)
def test_a_slab_table_payroll_would_misread_is_refused(slabs, expected: str) -> None:
    problems = rule_problems("professional_tax", {"slabs": slabs})
    assert any(expected in p for p in problems), problems


def test_womens_slabs_must_name_a_gender_payroll_knows() -> None:
    # "Female" would never match the employee's "female": the general slabs
    # would apply to everyone, silently.
    problems = rule_problems(
        "professional_tax", {**KA_PT, "slabs_by_gender": {"Female": KA_PT["slabs"]}}
    )
    assert any("'Female' is not one of" in p for p in problems)


@pytest.mark.parametrize(
    ("rule_type", "rules", "expected"),
    [
        ("pf", {**PF, "employee_rate": "12%"}, "employee_rate must be a number"),
        ("pf", {**PF, "employer_rate": 120}, "employer_rate is a percentage"),
        ("pf", {**PF, "wage_ceiling": True}, "wage_ceiling must be a number"),
        ("pf", {**PF, "contribution_rounding": "round"}, "contribution_rounding must be one of"),
        ("tds", {"regimes": {}, "cess_percent": 4, "tds_rounding": "ceil"}, "tds_rounding"),
        ("labour_welfare_fund", {"deduction_months": [6]}, "applicable must be true or false"),
        ("labour_welfare_fund", {**LWF, "deduction_months": ["6", "12"]}, "deduction_months"),
        ("labour_welfare_fund", {**LWF, "deduction_months": [13]}, "deduction_months"),
        (
            "labour_welfare_fund",
            {"applicable": True, "deduction_months": [6]},
            "needs employee_amount and employer_amount",
        ),
        (
            "labour_welfare_fund",
            {**LWF, "employee_amount": "twenty"},
            "employee_amount must be a number",
        ),
        (
            "labour_welfare_fund",
            {
                "applicable": True,
                "deduction_months": [12],
                "wage_bands": [{"up_to": 3000, "employee_amount": 6, "employer_amount": 18}],
            },
            "band 1 is the last and needs up_to null",
        ),
    ],
)
def test_values_payroll_cannot_read_are_refused(rule_type: str, rules: dict, expected: str) -> None:
    problems = rule_problems(rule_type, rules)
    assert any(expected in p for p in problems), problems


def test_refusal_names_what_is_wrong() -> None:
    with pytest.raises(HTTPException) as exc:
        refuse_unreadable("professional_tax", {"slabs": [{"max": 24999, "amount": 0}]})
    assert exc.value.status_code == 422
    assert exc.value.detail.startswith("Payroll cannot read this professional_tax rule set")
    assert "slab 1 has no up_to" in exc.value.detail

    with pytest.raises(HTTPException, match="needs employee_rate"):
        refuse_unreadable("pf", {"wage_ceiling": 15000})
