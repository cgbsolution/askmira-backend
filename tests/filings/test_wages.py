"""Slice 10 gate: EPF, EPS and EDLI wages are computed distinctly.

The failure this guards against is silent and expensive: three columns filled
from one number produces a file the portal accepts and a pension contribution
that is wrong, and nobody finds out until an employee's pension is short years
later.

Every ceiling here is a fixture, not an assertion about India's real values.
The figures are chosen so the three bases land on visibly different numbers.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.filings.wages import WageError, compute_ecr_wages, ncp_days


def D(value: str | int) -> Decimal:  # noqa: N802 - a Decimal alias, used throughout
    return Decimal(str(value))


# Three deliberately different ceilings, so aliasing is visible immediately.
PF_RULES = {
    "wage_ceiling": 15_000,
    "eps_wage_ceiling": 15_000,
    "edli_wage_ceiling": 15_000,
    "employee_rate": 12,
    "employer_rate": 12,
    "eps_rate": "8.33",
}

# A tenant whose SME has supplied genuinely distinct ceilings.
SPLIT_RULES = {
    "wage_ceiling": 20_000,
    "eps_wage_ceiling": 15_000,
    "edli_wage_ceiling": 12_000,
    "employee_rate": 12,
    "employer_rate": 12,
    "eps_rate": "8.33",
}


def wages(**overrides):
    base = {
        "rules": PF_RULES,
        "gross_earnings": D(50_000),
        "pf_wage_base": D(40_000),
        "pf_employee": D(1_800),
        "pf_employer": D(1_800),
        "lop_days": D(0),
    }
    base.update(overrides)
    return compute_ecr_wages(**base)


# ═══════════════════════════════════════════════════════════════════════════
# The four wage figures
# ═══════════════════════════════════════════════════════════════════════════


def test_the_four_wage_figures_are_computed_on_four_bases() -> None:
    """The gate: with three different ceilings, four different numbers."""
    result = wages(rules=SPLIT_RULES)

    assert result.gross_wages == D(50_000), "everything paid"
    assert result.epf_wages == D(20_000), "capped at the EPF ceiling"
    assert result.eps_wages == D(15_000), "capped at the separate EPS ceiling"
    assert result.edli_wages == D(12_000), "capped at the separate EDLI ceiling"

    assert len({result.gross_wages, result.epf_wages, result.eps_wages, result.edli_wages}) == 4


def test_gross_wages_are_not_a_pf_concept() -> None:
    """Gross sits on the return for reconciliation and is never capped."""
    result = wages(gross_earnings=D(500_000))
    assert result.gross_wages == D(500_000)


def test_a_low_earner_lands_on_the_same_figure_for_all_three_bases() -> None:
    """Which is correct, not a bug — the aliasing check is a warning, not a
    blocking error, for exactly this case."""
    result = wages(
        rules=SPLIT_RULES, gross_earnings=D(10_000), pf_wage_base=D(10_000),
        pf_employee=D(1_200), pf_employer=D(1_200),
    )
    assert result.epf_wages == result.eps_wages == result.edli_wages == D(10_000)


# ═══════════════════════════════════════════════════════════════════════════
# The election lifts EPF and not EPS
# ═══════════════════════════════════════════════════════════════════════════


def test_the_actual_wage_election_lifts_epf_but_not_eps_or_edli() -> None:
    """The distinction the return is most often filed wrongly on.

    An employee on 40,000 who elected PF on full wages has EPF wages of 40,000.
    Their pension contribution is still computed on the EPS ceiling — the
    election is an EPF arrangement, not a pension one.
    """
    result = wages(
        rules=SPLIT_RULES, pf_on_actual_wage=True,
        pf_employee=D(4_800), pf_employer=D(4_800),
    )
    assert result.epf_wages == D(40_000)
    assert result.eps_wages == D(15_000)
    assert result.edli_wages == D(12_000)
    assert any("lifts EPF, not EPS" in note for note in result.notes)


def test_without_the_election_epf_is_capped_like_everything_else() -> None:
    result = wages(rules=SPLIT_RULES)
    assert result.epf_wages == D(20_000)
    assert not any("actual wages" in note for note in result.notes)


# ═══════════════════════════════════════════════════════════════════════════
# The contribution split
# ═══════════════════════════════════════════════════════════════════════════


def test_the_employer_share_splits_into_eps_and_epf() -> None:
    """Reporting the whole employer share as EPF *and* EPS in full
    double-counts, which is the other common ECR error."""
    result = wages(pf_wage_base=D(15_000), pf_employee=D(1_800), pf_employer=D(1_800))

    # 8.33% of 15,000 = 1,249.50
    assert result.eps_contribution == D("1249.50")
    assert result.employer_epf_share == D("550.50")
    assert result.eps_contribution + result.employer_epf_share == result.employer_share


def test_a_non_eps_member_has_no_pension_wage_or_contribution() -> None:
    """Someone who first joined the fund above the ceiling is commonly excluded.

    That is decided at enrolment, so it is passed in rather than inferred — and
    the whole employer share then goes to EPF.
    """
    result = wages(eps_member=False, pf_wage_base=D(15_000), pf_employer=D(1_800))

    assert result.eps_wages == D(0)
    assert result.eps_contribution == D(0)
    assert result.employer_epf_share == D(1_800)
    assert any("Not an EPS member" in note for note in result.notes)


def test_an_eps_contribution_exceeding_the_employer_share_is_refused() -> None:
    """Two rates that disagree with each other, caught before the file is
    written rather than after the portal reads it."""
    broken = {**PF_RULES, "eps_rate": 50}
    with pytest.raises(WageError, match="exceeds the employer's total share"):
        wages(rules=broken, pf_wage_base=D(15_000), pf_employer=D(1_800))


# ═══════════════════════════════════════════════════════════════════════════
# Nothing is assumed
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "missing", ["wage_ceiling", "eps_wage_ceiling", "edli_wage_ceiling", "eps_rate"]
)
def test_every_ceiling_and_rate_must_be_supplied(missing: str) -> None:
    """No default. The error names the key so the SME knows what to add."""
    rules = {key: value for key, value in PF_RULES.items() if key != missing}
    with pytest.raises(WageError, match=missing):
        wages(rules=rules)


def test_a_float_is_refused() -> None:
    with pytest.raises(WageError, match="must be a Decimal"):
        wages(gross_earnings=50000.0)


def test_a_negative_value_is_refused_before_it_reaches_the_portal() -> None:
    with pytest.raises(WageError, match="portal rejects negative values"):
        wages(pf_employee=D(-100))


# ═══════════════════════════════════════════════════════════════════════════
# NCP days
# ═══════════════════════════════════════════════════════════════════════════


def test_ncp_days_come_from_attendance_loss_of_pay() -> None:
    """The checklist gate.

    Filing zero for someone who had unpaid absence overstates their
    contributory service.
    """
    assert wages(lop_days=D(3)).ncp_days == D(3)
    assert wages(lop_days=D(0)).ncp_days == D(0)


def test_half_day_loss_of_pay_survives_into_ncp() -> None:
    """Slice 02 computes a payable fraction, so half days are real here.

    The serialiser is what rounds them for the file; this layer keeps the truth.
    """
    assert wages(lop_days=D("2.5")).ncp_days == D("2.5")


def test_negative_ncp_is_refused() -> None:
    with pytest.raises(WageError, match="cannot be negative"):
        ncp_days(D(-1))


def test_a_float_lop_is_refused() -> None:
    with pytest.raises(WageError, match="must be a Decimal"):
        ncp_days(2.5)


# ═══════════════════════════════════════════════════════════════════════════
# Shape
# ═══════════════════════════════════════════════════════════════════════════


def test_the_working_is_json_serialisable() -> None:
    import json

    json.dumps(wages().as_dict())


def test_the_computation_is_deterministic() -> None:
    assert wages().as_dict() == wages().as_dict()
