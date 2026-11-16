"""The shared non-salary TDS calculation: pure, no database."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.contractors import ContractorKind, TdsSection
from app.models.payroll import PayrollRuleSet
from app.services.tds_non_salary import Payee, YearToDate, compute, fy_bounds

RULES = {
    "no_pan_rate": "20",
    "sections": {
        "194C": {
            "rate_individual": "1",
            "rate_other": "2",
            "single_threshold": "30000",
            "annual_threshold": "100000",
        },
        "194J_professional": {"rate": "10", "annual_threshold": "50000"},
        "194J_technical": {"rate": "2", "annual_threshold": "50000"},
    },
}

PAN = "ABCPR1234K"
NOTHING = YearToDate(total=Decimal(0), taxed_base=Decimal(0))


def _rule_set() -> PayrollRuleSet:
    return PayrollRuleSet(
        id=uuid.uuid4(),
        rule_type="tds_non_salary",
        jurisdiction="IN",
        effective_from=date(2025, 4, 1),
        rules=RULES,
        source_reference="Test rules",
    )


def _professional(pan: str | None = PAN) -> Payee:
    return Payee(pan=pan, kind=ContractorKind.INDIVIDUAL, section=TdsSection.S194J_PROFESSIONAL)


def test_194j_professional_below_annual_threshold_is_untaxed() -> None:
    result = compute(_rule_set(), _professional(), Decimal(40000), NOTHING)
    assert result.tds == Decimal(0)
    assert result.base == Decimal(0)
    assert result.note is not None


def test_crossing_annual_threshold_catches_up() -> None:
    rule_set = _rule_set()
    ytd = YearToDate(total=Decimal(40000), taxed_base=Decimal(0))
    result = compute(rule_set, _professional(), Decimal(20000), ytd)
    assert result.base == Decimal(60000)
    assert result.rate == Decimal(10)
    assert result.tds == Decimal("6000.00")
    assert result.note is not None and "earlier" in result.note
    assert result.rule_set_id == rule_set.id


def test_no_pan_is_20_percent() -> None:
    result = compute(_rule_set(), _professional(pan=None), Decimal(60000), NOTHING)
    assert result.rate == Decimal(20)
    assert result.note is not None and "No PAN" in result.note


def test_194c_single_payment_over_30000() -> None:
    payee = Payee(pan=PAN, kind=ContractorKind.INDIVIDUAL, section=TdsSection.S194C)
    result = compute(_rule_set(), payee, Decimal(35000), NOTHING)
    assert result.rate == Decimal(1)
    assert result.tds == Decimal("350.00")


def test_missing_section_entry_is_a_409() -> None:
    rule_set = _rule_set()
    rule_set.rules = {"no_pan_rate": "20", "sections": {}}
    with pytest.raises(HTTPException) as caught:
        compute(rule_set, _professional(), Decimal(60000), NOTHING)
    assert caught.value.status_code == 409


def test_financial_year_runs_april_to_march() -> None:
    assert fy_bounds(date(2026, 3, 31)) == (date(2025, 4, 1), date(2026, 3, 31))
    assert fy_bounds(date(2026, 4, 1)) == (date(2026, 4, 1), date(2027, 3, 31))
