"""CGB's CTC breakup Calculator (50% Basic, PF ceiling 25,000 from 01 Oct 26) as templates.

`scripts/salary_templates/ctc_calculator_50_basic_pf25000.json` holds its three
sheets — Without Gratuity, Internal, External — as salary structures. Each is
checked here against the sheet's own formulas, replayed below: the template
rounds every line to the rupee, the sheet keeps paise on Basic, Bonus and
Special, so lines agree within a rupee and the gross agrees exactly on a
whole-rupee CTC.

Gratuity (Internal, External) is ROUNDUP(Basic * 15 / 26 / 12). A rule takes four
decimals, so it is 4.8077% rounded up: the sheet's, or ₹1 more (and Special ₹1
less) when Basic is a multiple of ₹104 or, now and then, above ₹1,25,000.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.payroll import ComponentType
from app.services.payroll import ctc_template as ct
from app.services.payroll.standard_setup import STANDARD

FILE = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "salary_templates"
    / "ctc_calculator_50_basic_pf25000.json"
)
SPECS = {spec.code: spec for spec in STANDARD}


def _template(raw: dict) -> ct.Template:
    """The template on the standard wage types, as a workspace set up from them has."""
    lines = []
    for item in raw["lines"]:
        spec = SPECS[item["component_code"]]
        lines.append(
            ct.Line(
                code=spec.code,
                name=spec.name,
                component_type=spec.kind,
                group=spec.group,
                rule=ct.parse_rule(item["rule"], spec.name),
                part_of_ctc=spec.part_of_ctc,
                counts_toward_pf=spec.pf,
                counts_toward_esi=spec.esi,
                display_order=spec.order,
            )
        )
    employer = tuple(
        ct.EmployerRow(key, name, ct.parse_rule(raw[field], name))
        for key, name, field in (
            (ct.EMPLOYER_PF, "EMPLOYER PF", "employer_pf"),
            (ct.EMPLOYER_ESI, "EMPLOYER ESIC", "employer_esi"),
        )
        if raw.get(field)
    )
    return ct.Template(lines=tuple(lines), employer=employer, precision=raw["rounding_precision"])


def _sheet(ctc: Decimal, *, gratuity: bool) -> dict[str, Decimal]:
    """The calculator's formulas, cell for cell."""
    basic = ctc * Decimal("0.5")  # =C3*50%
    bonus = basic * Decimal("0.0833") if basic <= 21000 else Decimal(0)  # =IF(C11<=21000,…)
    hra = Decimal(math.ceil(basic * Decimal("0.4")))  # =ROUNDUP(C11*40%,0)
    pf = Decimal(math.ceil(basic * Decimal("0.13"))) if basic < 25000 else Decimal(3250)
    esi = Decimal(0) if basic > 21000 else Decimal(math.ceil(basic * Decimal("0.0325")))
    # =ROUNDUP((C11*15/26)/12,0), on Internal and External only.
    grat = Decimal(math.ceil(basic * 15 / 26 / 12)) if gratuity else Decimal(0)
    special = ctc - basic - bonus - hra - pf - esi - grat  # =C3-C11-C12-C13-C14-C29
    return {
        "BASIC": basic,
        "BONUS": bonus,
        "HRA": hra,
        "SPECIAL": special,
        "GRATUITY": grat,
        "EMPLOYER_PF": pf,
        "EMPLOYER_ESI": esi,
        "GROSS": basic + bonus + hra + special,
        "INSURANCE": Decimal(0) if esi > 0 else Decimal(239),  # =IF(C19>0,0,239)
    }


def _has_gratuity(raw: dict) -> bool:
    return any(item["component_code"] == "GRATUITY" for item in raw["lines"])


def _templates() -> list[dict]:
    return json.loads(FILE.read_text(encoding="utf-8"))["templates"]


def test_the_file_holds_the_three_sheets() -> None:
    names = [raw["name"] for raw in _templates()]
    assert names == [
        "50% Basic - Without Gratuity",
        "50% Basic - Internal",
        "50% Basic - External",
    ]
    assert [_has_gratuity(raw) for raw in _templates()] == [False, True, True]


@pytest.mark.parametrize("raw", _templates(), ids=lambda raw: raw["name"])
@pytest.mark.parametrize(
    "ctc",
    ["12000", "25000", "33333.33", "40750", "42000", "42001", "50000", "53300", "60000",
     "70000", "200000"],
)  # fmt: skip
def test_each_sheet_splits_a_ctc_as_its_formulas_do(raw: dict, ctc: str) -> None:
    result = ct.calculate(_template(raw), Decimal(ctc))
    sheet = _sheet(Decimal(ctc), gratuity=_has_gratuity(raw))
    got = {line.code: line.monthly for line in result.lines}
    got |= {row.code: row.monthly for row in result.employer}
    got.setdefault("GRATUITY", Decimal(0))
    for code in ("BASIC", "BONUS", "HRA", "SPECIAL", "GRATUITY", "EMPLOYER_PF", "EMPLOYER_ESI",
                 "INSURANCE"):  # fmt: skip
        assert abs(got[code] - sheet[code]) < 1, (code, got[code], sheet[code])
    assert abs(result.gross_monthly - sheet["GROSS"]) < 1
    if Decimal(ctc) == Decimal(ctc).to_integral_value():
        assert result.gross_monthly == sheet["GROSS"]
        assert result.allocated_monthly == Decimal(ctc)


def test_gratuity_is_the_sheets_roundup_or_one_rupee_more() -> None:
    """4.8077% rounded up against ROUNDUP(Basic*15/26/12): never less, at most ₹1
    more, and exactly the sheet's for a Basic up to ₹1,25,000 that is not a
    multiple of ₹104."""
    rule = ct.parse_rule(
        {"method": "percent", "value": "4.8077", "of": "BASIC", "rounding": "up"}, "Gratuity"
    )
    for basic in range(1, 300001):
        ours = (Decimal(basic) * rule.value / 100).to_integral_value(rounding="ROUND_CEILING")
        sheet = math.ceil(Decimal(basic) * 15 / 26 / 12)
        assert 0 <= ours - sheet <= 1, basic
        if basic < 125000 and basic % 104:
            assert ours == sheet, basic


def test_the_sheets_examples_come_out_as_shown() -> None:
    """Each sheet's own example. 'Without Gratuity': CTC 52,000 → Basic 26,000,
    HRA 10,400, Special 12,350, gross 48,750, PF 3,250, insurance 239, no PT.
    'Internal': CTC 70,000 → Basic 35,000, HRA 14,000, gratuity 1,683, Special
    16,067, gross 65,067, PT 200. 'External': CTC 53,300 → Basic 26,650, HRA
    10,660, gratuity 1,282, Special 11,458, gross 48,768, PT 200."""
    without, internal, external = (_template(raw) for raw in _templates())

    first = ct.calculate(without, Decimal("52000"))
    assert [first.by_code(c).monthly for c in ("BASIC", "HRA", "SPECIAL", "INSURANCE")] == [
        Decimal("26000"), Decimal("10400"), Decimal("12350"), Decimal("239"),
    ]  # fmt: skip
    assert first.gross_monthly == Decimal("48750")
    assert [row.monthly for row in first.employer] == [Decimal("3250"), Decimal("0")]
    assert first.by_code("PT_MANUAL") is None  # blank on 'Without Gratuity'
    assert first.by_code("GRATUITY") is None

    second = ct.calculate(internal, Decimal("70000"))
    assert [second.by_code(c).monthly for c in ("BASIC", "HRA", "GRATUITY", "SPECIAL")] == [
        Decimal("35000"), Decimal("14000"), Decimal("1683"), Decimal("16067"),
    ]  # fmt: skip
    assert second.gross_monthly == Decimal("65067")
    assert second.by_code("PT_MANUAL").monthly == Decimal("200")

    third = ct.calculate(external, Decimal("53300"))
    assert [third.by_code(c).monthly for c in ("BASIC", "HRA", "GRATUITY", "SPECIAL")] == [
        Decimal("26650"), Decimal("10660"), Decimal("1282"), Decimal("11458"),
    ]  # fmt: skip
    assert third.gross_monthly == Decimal("48768")
    assert third.by_code("PT_MANUAL").monthly == Decimal("200")
    assert [row.monthly for row in third.employer] == [Decimal("3250"), Decimal("0")]


def test_employee_pf_esic_and_tds_are_left_to_payroll() -> None:
    """A typed PF would be deducted in full in a month with loss of pay; payroll
    prorates it. TDS typed as 0 would stop payroll deducting any."""
    for raw in _templates():
        codes = {item["component_code"] for item in raw["lines"]}
        assert not codes & {"PF_MANUAL", "ESIC_MANUAL", "TDS_MANUAL"}
        assert all(SPECS[code].kind is not ComponentType.DEDUCTION or not SPECS[code].part_of_ctc
                   for code in codes)  # fmt: skip
