"""CGB's CTC breakup Calculator (50% Basic, PF ceiling 25,000 from 01 Oct 26) as templates.

`scripts/salary_templates/ctc_calculator_50_basic_pf25000.json` holds its two
sheets as salary structures. Each is checked here against the sheet's own
formulas, replayed below: the template rounds every line to the rupee, the sheet
keeps paise on Basic, Bonus and Special, so lines agree within a rupee and the
gross agrees exactly on a whole-rupee CTC.
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


def _sheet(ctc: Decimal) -> dict[str, Decimal]:
    """The calculator's formulas, cell for cell."""
    basic = ctc * Decimal("0.5")  # =C3*50%
    bonus = basic * Decimal("0.0833") if basic <= 21000 else Decimal(0)  # =IF(C11<=21000,…)
    hra = Decimal(math.ceil(basic * Decimal("0.4")))  # =ROUNDUP(C11*40%,0)
    pf = Decimal(math.ceil(basic * Decimal("0.13"))) if basic < 25000 else Decimal(3250)
    esi = Decimal(0) if basic > 21000 else Decimal(math.ceil(basic * Decimal("0.0325")))
    special = ctc - basic - bonus - hra - pf - esi  # =C3-C11-C12-C13-C14-C28
    return {
        "BASIC": basic,
        "BONUS": bonus,
        "HRA": hra,
        "SPECIAL": special,
        "EMPLOYER_PF": pf,
        "EMPLOYER_ESI": esi,
        "GROSS": basic + bonus + hra + special,
        "INSURANCE": Decimal(0) if esi > 0 else Decimal(239),  # =IF(C19>0,0,239)
    }


def _templates() -> list[dict]:
    return json.loads(FILE.read_text(encoding="utf-8"))["templates"]


@pytest.mark.parametrize("raw", _templates(), ids=lambda raw: raw["name"])
@pytest.mark.parametrize(
    "ctc", ["12000", "25000", "33333.33", "40750", "42000", "42001", "50000", "60000", "200000"]
)
def test_each_sheet_splits_a_ctc_as_its_formulas_do(raw: dict, ctc: str) -> None:
    result = ct.calculate(_template(raw), Decimal(ctc))
    sheet = _sheet(Decimal(ctc))
    got = {line.code: line.monthly for line in result.lines}
    got |= {row.code: row.monthly for row in result.employer}
    for code in ("BASIC", "BONUS", "HRA", "SPECIAL", "EMPLOYER_PF", "EMPLOYER_ESI", "INSURANCE"):
        assert abs(got[code] - sheet[code]) < 1, (code, got[code], sheet[code])
    assert abs(result.gross_monthly - sheet["GROSS"]) < 1
    if Decimal(ctc) == Decimal(ctc).to_integral_value():
        assert result.gross_monthly == sheet["GROSS"]
        assert result.allocated_monthly == Decimal(ctc)


def test_the_sheets_examples_come_out_as_shown() -> None:
    """'Without Gratuity': CTC 3,00,000 a year → gross 22,968 a month, PF 1,625,
    ESIC 407. 'External': CTC 4,00,000 a year → HRA 6,667, PF 2,167, ESIC 542."""
    without, external = (_template(raw) for raw in _templates())
    first = ct.calculate(without, Decimal("300000") / 12)
    assert first.gross_monthly == Decimal("22968")
    assert [row.monthly for row in first.employer] == [Decimal("1625"), Decimal("407")]
    assert first.by_code("PT_MANUAL").monthly == Decimal("200")

    second = ct.calculate(external, Decimal("400000") / 12)
    assert second.by_code("HRA").monthly == Decimal("6667")
    assert [row.monthly for row in second.employer] == [Decimal("2167"), Decimal("542")]
    assert second.by_code("PT_MANUAL") is None  # blank on the External sheet
    assert second.by_code("GRATUITY").monthly == 0  # the row is there, empty


def test_employee_pf_esic_and_tds_are_left_to_payroll() -> None:
    """A typed PF would be deducted in full in a month with loss of pay; payroll
    prorates it. TDS typed as 0 would stop payroll deducting any."""
    for raw in _templates():
        codes = {item["component_code"] for item in raw["lines"]}
        assert not codes & {"PF_MANUAL", "ESIC_MANUAL", "TDS_MANUAL"}
        assert all(SPECS[code].kind is not ComponentType.DEDUCTION or not SPECS[code].part_of_ctc
                   for code in codes)  # fmt: skip
