"""The CTC template calculator: a CTC split by percentages, slabs and limits.

Figures are checked against the HRMS HR moved from (factoHR): its Salary Master
for CTC 51,666.67 a month shows Basic 3,09,996 / gratuity 14,904 / monthly gross
5,81,688 a year, and its salary sheet rows split the CTC the same way.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.payroll import ComponentType as T
from app.models.payroll import WageGroup as G
from app.services.payroll.ctc_template import (
    EmployerRow,
    Line,
    Template,
    TemplateError,
    calculate,
    describe,
    parse_rule,
    rule_json,
    validate,
)


def line(code, rule, *, kind=T.EARNING, group=G.REGULAR_EARNING, pf=False, esi=False, ctc=True,
         order=0):  # fmt: skip
    return Line(code, code, kind, group, parse_rule(rule, code), ctc, pf, esi, order)


PF = EmployerRow(
    "EMPLOYER_PF",
    "EMPLOYER PF",
    parse_rule({"method": "percent", "value": "13", "of": "PF_WAGES", "ceiling": "15000"}, "PF"),
)
ESI = EmployerRow(
    "EMPLOYER_ESI",
    "EMPLOYER ESI",
    parse_rule(
        {
            "method": "percent",
            "value": "3.25",
            "of": "ESI_WAGES",
            "when": {"on": "ESI_WAGES", "op": "up_to", "amount": "21000"},
            "rounding": "up",
        },
        "ESI",
    ),
)


def standard(gratuity="4.81", *, employer=(PF, ESI), extra=()) -> Template:
    return Template(
        lines=(
            line("GRATUITY", {"method": "percent", "value": gratuity, "of": "BASIC"},
                 kind=T.EMPLOYER_CONTRIBUTION, group=G.CTC_INPUT, order=5),
            line("BASIC", {"method": "percent", "value": "50", "of": "CTC"}, pf=True, esi=True,
                 order=10),
            line("HRA", {"method": "percent", "value": "40", "of": "BASIC"}, esi=True, order=40),
            line("SPECIAL", {"method": "balance"}, esi=True, order=50),
            *extra,
        ),
        employer=employer,
    )  # fmt: skip


def amounts(result) -> dict[str, Decimal]:
    return {item.code: item.monthly for item in result.lines}


# ── Matching the old HRMS ───────────────────────────────────────────────────


def test_the_salary_master_screenshot_is_reproduced() -> None:
    """CTC 51,666.67 a month: Basic 25,833, gratuity 1,242 (15/26 of a month's basic
    a year), employer PF 1,950, monthly gross 48,474 (5,81,688 a year)."""
    result = calculate(standard(gratuity="4.8077"), Decimal("51666.67"))
    got = amounts(result)
    assert got["BASIC"] == Decimal("25833")
    assert got["GRATUITY"] == Decimal("1242")
    assert got["HRA"] == Decimal("10333")
    assert result.employer[0].monthly == Decimal("1950")
    assert result.gross_monthly == Decimal("48474")
    assert result.gross_monthly * 12 == Decimal("581688")
    # The 67 paise rounding cannot place are reported, never lost or invented.
    assert result.unallocated_monthly == Decimal("0.67")


def test_a_yearly_ctc_from_the_salary_sheet_splits_to_its_figures() -> None:
    """Sheet row AB008: CTC 12,00,000 a year, Basic 6,00,000, HRA 2,40,000,
    SPECIAL 3,36,600, gross 11,76,600 (the 23,400 left is employer PF at 13%)."""
    result = calculate(standard(employer=(PF,), gratuity="0"), Decimal("1200000") / 12)
    got = amounts(result)
    assert got["BASIC"] * 12 == Decimal("600000")
    assert got["HRA"] * 12 == Decimal("240000")
    assert got["SPECIAL"] * 12 == Decimal("336600")
    assert result.gross_monthly * 12 == Decimal("1176600")
    assert result.employer[0].monthly * 12 == Decimal("23400")
    assert result.unallocated_monthly == 0


def test_a_percentage_of_a_line_reads_its_rounded_amount() -> None:
    """Sheet row AB001: CTC 68,617 → Basic 34,309 (34,308.5 rounded), and HRA is
    40% of 34,309 = 13,724, not 40% of 34,308.5 = 13,723."""
    got = amounts(calculate(standard(employer=(PF,)), Decimal("68617")))
    assert got["BASIC"] == Decimal("34309")
    assert got["HRA"] == Decimal("13724")


# ── The statutory limits ────────────────────────────────────────────────────


def test_employer_pf_is_capped_at_the_wage_ceiling() -> None:
    high = calculate(standard(), Decimal("100000"))
    low = calculate(standard(), Decimal("20000"))
    assert high.employer[0].monthly == Decimal("1950")  # 13% of 15,000, not of 50,000
    assert low.employer[0].monthly == Decimal("1300")  # 13% of a 10,000 basic


def test_employer_esi_applies_only_up_to_the_wage_limit_and_fits_inside_the_ctc() -> None:
    covered = calculate(standard(), Decimal("20000"))
    esi = covered.employer[1]
    assert esi.applied
    # 3.25% of ESI wages, rounded up to the rupee, and the CTC still adds up exactly.
    assert esi.monthly == (covered.esi_wages * Decimal("0.0325")).quantize(
        Decimal(1), rounding="ROUND_CEILING"
    )
    assert covered.allocated_monthly == Decimal("20000")

    above = calculate(standard(), Decimal("60000"))
    assert not above.employer[1].applied
    assert above.employer[1].monthly == 0
    assert "above ₹21,000" in (above.employer[1].note or "")


def test_a_ctc_just_above_the_esi_limit_is_decided_on_wages_without_esi() -> None:
    """Gross before ESI is above the limit, so ESI is not taken out of it."""
    result = calculate(standard(employer=(ESI,), gratuity="0"), Decimal("21100"))
    assert not result.employer[0].applied
    assert result.gross_monthly == Decimal("21100")


def test_professional_tax_by_slab_starts_above_an_amount() -> None:
    pt = line(
        "PT_MANUAL",
        {
            "method": "slab",
            "of": "GROSS",
            "slabs": [{"up_to": "15000", "amount": "0"}, {"up_to": None, "amount": "200"}],
        },
        kind=T.DEDUCTION,
        group=G.STATUTORY_DEDUCTION,
        ctc=False,
        order=340,
    )
    above = amounts(calculate(standard(extra=(pt,)), Decimal("40000")))
    below = amounts(calculate(standard(extra=(pt,)), Decimal("12000")))
    assert above["PT_MANUAL"] == Decimal("200")
    assert below["PT_MANUAL"] == Decimal("0")


def test_a_line_can_apply_only_above_an_amount() -> None:
    bonus = line(
        "BONUS",
        {
            "method": "fixed",
            "value": "1000",
            "when": {"on": "CTC", "op": "above", "amount": "50000"},
        },
        group=G.VARIABLE_EARNING,
        order=110,
    )
    assert amounts(calculate(standard(extra=(bonus,)), Decimal("60000")))["BONUS"] == Decimal(
        "1000"
    )
    small = calculate(standard(extra=(bonus,)), Decimal("40000"))
    assert amounts(small)["BONUS"] == 0
    assert not small.by_code("BONUS").applied


def test_a_fixed_yearly_amount_is_a_twelfth_a_month() -> None:
    conveyance = line("CONVEYANCE", {"method": "fixed", "value": "19200", "basis": "annual"})
    got = amounts(calculate(standard(extra=(conveyance,)), Decimal("50000")))
    assert got["CONVEYANCE"] == Decimal("1600")


def test_the_lines_and_employer_contributions_never_pass_the_ctc() -> None:
    for ctc in ("8000", "15000.50", "20999", "21000", "33333.33", "125000", "999999.99"):
        result = calculate(standard(), Decimal(ctc))
        assert result.allocated_monthly <= Decimal(ctc)
        assert 0 <= result.unallocated_monthly < 1


def test_paise_are_kept_when_the_structure_rounds_to_paise() -> None:
    template = Template(lines=standard().lines, employer=(PF,), precision=2)
    result = calculate(template, Decimal("51666.67"))
    assert amounts(result)["BASIC"] == Decimal("25833.34")  # 25,833.335 to the paisa
    assert result.unallocated_monthly == 0


# ── Refusals ────────────────────────────────────────────────────────────────


def test_lines_taking_more_than_the_ctc_are_refused() -> None:
    template = Template(
        lines=(
            line("BASIC", {"method": "percent", "value": "80", "of": "CTC"}),
            line("HRA", {"method": "percent", "value": "40", "of": "CTC"}),
            line("SPECIAL", {"method": "balance"}),
        )
    )
    with pytest.raises(TemplateError, match="more than the CTC"):
        calculate(template, Decimal("50000"))


def test_a_line_that_flips_on_and_off_forever_is_refused() -> None:
    """An allowance outside the CTC paid only while gross is up to 20,000 pushes
    the gross above 20,000, which takes it away again."""
    flip = line(
        "TOPUP",
        {
            "method": "fixed",
            "value": "5000",
            "when": {"on": "GROSS", "op": "up_to", "amount": "20000"},
        },
        ctc=False,
    )
    template = Template(lines=(line("SPECIAL", {"method": "balance"}), flip))
    with pytest.raises(TemplateError, match="switching a line on and off"):
        calculate(template, Decimal("18000"))


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ((line("BASIC", {"method": "percent", "value": "50", "of": "CTC"}),), "balance of the CTC"),
        (
            (line("A", {"method": "balance"}), line("B", {"method": "balance"})),
            "Only one wage type",
        ),
        (
            (line("SPECIAL", {"method": "balance"}, kind=T.DEDUCTION),),
            "balance is an earning",
        ),
        (
            (
                line("SPECIAL", {"method": "balance"}),
                line("HRA", {"method": "percent", "value": "40", "of": "BASIC"}),
            ),
            "BASIC, which is not in this structure",
        ),
        (
            (
                line("SPECIAL", {"method": "balance"}),
                line("HRA", {"method": "percent", "value": "40", "of": "HRA"}),
            ),
            "from itself",
        ),
        (
            (
                line("SPECIAL", {"method": "balance"}),
                line("A", {"method": "percent", "value": "10", "of": "B"}),
                line("B", {"method": "percent", "value": "10", "of": "A"}),
            ),
            "in a circle",
        ),
    ],
)
def test_a_template_that_cannot_split_a_ctc_is_refused(lines, message) -> None:
    with pytest.raises(TemplateError, match=message):
        validate(Template(lines=lines))


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ({"method": "percent", "value": "120", "of": "CTC"}, "between 0 and 100"),
        ({"method": "percent", "value": "10"}, "what the percentage is of"),
        ({"method": "percent", "of": "CTC"}, "enter the percentage"),
        ({"method": "fixed", "value": "-5"}, "cannot be negative"),
        ({"method": "fixed", "value": "10.555"}, "more than two decimals"),
        ({"method": "fixed", "value": "abc"}, "not a number"),
        ({"method": "slab", "slabs": []}, "at least one slab"),
        (
            {"method": "slab", "slabs": [{"up_to": None, "amount": "0"}, {"amount": "200"}]},
            "only the last slab is open-ended",
        ),
        (
            {"method": "slab", "slabs": [{"up_to": "1000", "amount": "0"}]},
            "leave its limit empty",
        ),
        (
            {
                "method": "slab",
                "slabs": [
                    {"up_to": "20000", "amount": "0"},
                    {"up_to": "15000", "amount": "150"},
                    {"amount": "200"},
                ],
            },
            "must go up",
        ),
        ({"method": "fixed", "value": "5", "ceiling": "100"}, "only a percentage"),
        ({"method": "balance", "when": {"op": "up_to", "amount": "1"}}, "no condition"),
        ({"method": "nonsense"}, "pick how it is worked out"),
    ],
)
def test_a_rule_that_does_not_read_is_refused(rule, message) -> None:
    with pytest.raises(TemplateError, match=message):
        parse_rule(rule, "LINE")


def test_a_typed_amount_replaces_the_rule_and_the_balance_follows() -> None:
    result = calculate(standard(employer=(PF,)), Decimal("50000"), pinned={"HRA": Decimal("5000")})
    got = amounts(result)
    assert got["HRA"] == Decimal("5000")
    assert result.allocated_monthly == Decimal("50000")
    with pytest.raises(TemplateError, match="balance of the CTC"):
        calculate(standard(), Decimal("50000"), pinned={"SPECIAL": Decimal("1")})


# ── Words and storage ───────────────────────────────────────────────────────


def test_rules_read_as_plain_words() -> None:
    names = {"BASIC": "BASIC SALARY"}
    assert describe(PF.rule, names) == "13% of PF wages up to ₹15,000"
    assert describe(ESI.rule, names) == (
        "3.25% of ESI wages, only while ESI wages are up to ₹21,000 a month, rounded up"
    )
    assert describe(
        parse_rule({"method": "percent", "value": "40", "of": "BASIC"}, "x"), names
    ) == ("40% of BASIC SALARY")
    slab = parse_rule(
        {"method": "slab", "slabs": [{"up_to": "15000", "amount": "0"}, {"amount": "200"}]}, "x"
    )
    assert describe(slab, names) == "By gross a month — up to ₹15,000: ₹0; above ₹15,000: ₹200"


def test_a_rule_survives_being_stored() -> None:
    for rule in (PF.rule, ESI.rule):
        assert parse_rule(rule_json(rule), "x") == rule
    assert rule_json(PF.rule)["value"] == "13"
