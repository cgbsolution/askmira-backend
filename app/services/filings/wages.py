"""The four PF wage figures, computed distinctly.

═══════════════════════════════════════════════════════════════════════════════
WHY THIS MODULE EXISTS
═══════════════════════════════════════════════════════════════════════════════

A PF ECR line carries four wage columns, and they are routinely filled with the
same number:

- **Gross wages** — everything paid this month. Not a PF concept at all; it sits
  on the return for reconciliation.
- **EPF wages** — the wage the provident fund contribution is computed on. The
  ceiling applies unless the employee has elected PF on actual wages (slice 09).
- **EPS wages** — the wage the *pension* contribution is computed on. The EPS
  ceiling is separate from the EPF one and, critically, **is not lifted by an
  employee's election to contribute on actual wages**. An employee on 40,000
  who has elected PF on full wages has EPF wages of 40,000 and EPS wages of the
  ceiling.
- **EDLI wages** — the wage the insurance contribution is computed on, with its
  own ceiling again.

Aliasing them produces a file the portal accepts and a contribution that is
wrong, which is the worst of the available outcomes: nobody finds out until an
employee's pension is short years later.

**No ceiling is written in this file.** Each comes from the SME-approved PF rule
set, and a missing one is an error naming the missing key rather than a
plausible default.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "EcrWages",
    "WageError",
    "compute_ecr_wages",
    "ncp_days",
]

ZERO = Decimal(0)
RUPEE = Decimal("0.01")


class WageError(ValueError):
    """A wage figure cannot be computed from the configuration supplied."""


def _money(value: Decimal) -> Decimal:
    return value.quantize(RUPEE, rounding=ROUND_HALF_UP)


def _whole_rupee(value: Decimal) -> Decimal:
    """To the rupee, as the return carries it, kept as a money figure."""
    return value.quantize(Decimal(1), rounding=ROUND_HALF_UP).quantize(RUPEE)


def _rule(rules: dict[str, Any], key: str) -> Decimal:
    if key not in rules:
        raise WageError(
            f"The PF rule set is missing '{key}'. EPF, EPS and EDLI have separate "
            "ceilings and none of them is assumed here — the Compliance SME supplies "
            "each one."
        )
    return Decimal(str(rules[key]))


@dataclass(frozen=True, slots=True)
class EcrWages:
    """The wage columns of one ECR line, each derived on its own basis."""

    gross_wages: Decimal
    epf_wages: Decimal
    eps_wages: Decimal
    edli_wages: Decimal

    employee_share: Decimal
    employer_share: Decimal
    eps_contribution: Decimal
    # The employer's EPF share is what is left after the pension contribution is
    # carved out of it. Reporting the whole employer share as EPF, with EPS also
    # reported in full, double-counts.
    employer_epf_share: Decimal

    ncp_days: Decimal
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, str]:
        return {
            "gross_wages": str(self.gross_wages),
            "epf_wages": str(self.epf_wages),
            "eps_wages": str(self.eps_wages),
            "edli_wages": str(self.edli_wages),
            "employee_share": str(self.employee_share),
            "employer_share": str(self.employer_share),
            "eps_contribution": str(self.eps_contribution),
            "employer_epf_share": str(self.employer_epf_share),
            "ncp_days": str(self.ncp_days),
        }


def compute_ecr_wages(
    *,
    rules: dict[str, Any],
    gross_earnings: Decimal,
    pf_wage_base: Decimal,
    pf_employee: Decimal,
    pf_employer: Decimal,
    lop_days: Decimal,
    eps_member: bool = True,
    pf_on_actual_wage: bool = False,
    vpf: Decimal = ZERO,
) -> EcrWages:
    """The four wage figures and the contribution split for one ECR line.

    `pf_wage_base`, `pf_employee` and `pf_employer` come off the payslip rather
    than being recomputed. A return that recomputes can disagree with what was
    actually deducted from the employee, and the payslip is the authority.

    Args:
        eps_member: whether this employee is a pension-scheme member. A member
            who joined above the wage ceiling is commonly excluded, but that is
            a determination made when they were enrolled — not something this
            function can infer, so it is passed in.
        pf_on_actual_wage: the slice 09 election. It raises the EPF wage. It
            does **not** raise the EPS or EDLI wage.
        vpf: voluntary PF deducted on the payslip. It is the employee's money
            for their PF account, so it is remitted with the employee's share;
            left off, it is deducted and never reaches the fund.
    """
    for name, value in (
        ("gross_earnings", gross_earnings),
        ("pf_wage_base", pf_wage_base),
        ("pf_employee", pf_employee),
        ("pf_employer", pf_employer),
        ("lop_days", lop_days),
        ("vpf", vpf),
    ):
        if not isinstance(value, Decimal):
            raise WageError(f"{name} must be a Decimal, not a float")
        if value < ZERO:
            raise WageError(
                f"{name} is negative ({value}). The portal rejects negative values, and "
                "a negative wage is a data problem upstream rather than something to "
                "file."
            )

    notes: list[str] = []

    epf_ceiling = _rule(rules, "wage_ceiling")
    eps_ceiling = _rule(rules, "eps_wage_ceiling")
    edli_ceiling = _rule(rules, "edli_wage_ceiling")
    eps_rate = _rule(rules, "eps_rate")

    # ── EPF wages ───────────────────────────────────────────────────────────
    if pf_on_actual_wage:
        epf_wages = pf_wage_base
        notes.append("This employee has elected PF on actual wages, so EPF wages are not capped.")
    else:
        epf_wages = min(pf_wage_base, epf_ceiling)

    # ── EPS wages ───────────────────────────────────────────────────────────
    # The pension ceiling stands regardless of the employee's EPF election.
    # This is the distinction the return is most often filed wrongly on.
    if not eps_member:
        eps_wages = ZERO
        notes.append("Not an EPS member, so EPS wages and contribution are nil.")
    else:
        eps_wages = min(pf_wage_base, eps_ceiling)
        if pf_on_actual_wage and pf_wage_base > eps_ceiling:
            notes.append(
                f"EPS wages stay at the ceiling of {eps_ceiling} despite the actual-wage "
                "election — the election lifts EPF, not EPS."
            )

    # ── EDLI wages ──────────────────────────────────────────────────────────
    edli_wages = min(pf_wage_base, edli_ceiling)

    # ── The contribution split ──────────────────────────────────────────────
    # The return carries whole rupees, so the pension share is rounded first and
    # the EPF share is what is left of the employer's. Rounded separately, 8.33%
    # of 15,000 (1,249.50) and its remainder (550.50) become 1,250 and 551: a
    # rupee more than the employer contributed, on every line at the ceiling.
    eps_contribution = _whole_rupee(eps_wages * eps_rate / Decimal(100))
    employer_epf_share = _whole_rupee(pf_employer) - eps_contribution

    if employer_epf_share < ZERO:
        raise WageError(
            f"The EPS contribution of {eps_contribution} exceeds the employer's total "
            f"share of {pf_employer}. Check the EPS rate and the PF employer rate "
            "against each other — one of them is wrong."
        )

    return EcrWages(
        gross_wages=_money(gross_earnings),
        epf_wages=_money(epf_wages),
        eps_wages=_money(eps_wages),
        edli_wages=_money(edli_wages),
        employee_share=_money(pf_employee + vpf),
        employer_share=_money(pf_employer),
        eps_contribution=eps_contribution,
        employer_epf_share=employer_epf_share,
        ncp_days=ncp_days(lop_days),
        notes=notes,
    )


def ncp_days(lop_days: Decimal) -> Decimal:
    """Non-contributing period days, from attendance loss of pay.

    NCP is the ECR's name for days the employee was not paid, and it comes from
    attendance — which is why payroll refuses to run on an unlocked attendance
    period. Filing a zero here for someone who had unpaid absence overstates
    their contributory service.

    Half days are real (slice 02 computes a payable fraction), so this does not
    round to whole days.
    """
    if not isinstance(lop_days, Decimal):
        raise WageError("Loss-of-pay days must be a Decimal, not a float")
    if lop_days < ZERO:
        raise WageError("Loss-of-pay days cannot be negative")
    return lop_days
