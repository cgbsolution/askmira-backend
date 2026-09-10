"""Labour Welfare Fund, statutory bonus, and the awkward statutory edges.

Everything here extends the existing `PayrollRuleSet` rather than introducing
parallel configuration. Two config systems for statutory rules means two places
an SME has to approve and two places to be out of date.

═══════════════════════════════════════════════════════════════════════════════
WHY THE LWF CONFIG LOOKS THE WAY IT DOES
═══════════════════════════════════════════════════════════════════════════════

State variation in LWF is genuinely large and does not reduce to a formula:

- Karnataka deducts once a year, in December, at a flat amount.
- Maharashtra deducts twice, in June and December, at flat amounts that differ
  by wage band.
- Kerala deducts monthly.
- Several states do not levy it at all.
- The employer's contribution is often a different flat amount, sometimes a
  multiple of the employee's, sometimes unrelated.

So the config carries `deduction_months`, a rate *or* a flat amount, optional
wage bands, and an `applicable` switch — and the engine branches on data rather
than on a state name. A `if state == "MH"` anywhere in this file would be the
beginning of a per-state ladder that nobody can maintain.

**A non-deduction month produces exactly zero, never a proration.** LWF is a
periodic levy, not an annual amount spread across twelve months, and prorating
it would be wrong in both directions.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "BonusResult",
    "EsiContributionPeriod",
    "LwfResult",
    "PfBase",
    "PfElection",
    "PtStateResolution",
    "StatutoryError",
    "compute_bonus",
    "compute_lwf",
    "compute_vpf",
    "esi_contribution_period",
    "resolve_pf_base",
    "resolve_professional_tax_state",
    "should_continue_esi",
]

ZERO = Decimal(0)
RUPEE = Decimal("0.01")
PERCENT = Decimal(100)


class StatutoryError(ValueError):
    """A statutory configuration is missing or malformed."""


def _money(value: Decimal) -> Decimal:
    return value.quantize(RUPEE, rounding=ROUND_HALF_UP)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


# ── Labour Welfare Fund ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LwfResult:
    employee_contribution: Decimal
    employer_contribution: Decimal
    is_deduction_month: bool
    reason: str
    wage_band: str | None = None

    @property
    def total(self) -> Decimal:
        return self.employee_contribution + self.employer_contribution


def compute_lwf(
    *,
    config: dict[str, Any],
    month: int,
    monthly_wages: Decimal,
    designation: str | None = None,
) -> LwfResult:
    """LWF for one employee for one month.

    Args:
        config: the `lwf` block of a `PayrollRuleSet`, supplied by the SME for
            that state.
        month: calendar month, 1-12.
        monthly_wages: for the ceiling test and any wage banding.

    Returns zero — with a reason — when the state does not levy it, the month
    is not a deduction month, the wage is above the ceiling, or the designation
    is exempt.
    """
    if not isinstance(monthly_wages, Decimal):
        raise StatutoryError("Wages must be a Decimal, not a float")

    if not config.get("applicable", False):
        return LwfResult(ZERO, ZERO, False, "This state does not levy Labour Welfare Fund.")

    deduction_months = config.get("deduction_months")
    if not deduction_months:
        raise StatutoryError(
            "The LWF configuration is applicable but names no deduction months. The "
            "Compliance SME supplies them per state."
        )

    if month not in deduction_months:
        # Exactly zero. Not a twelfth of the annual amount: LWF is a periodic
        # levy, and prorating it is wrong in both directions.
        months = ", ".join(str(m) for m in sorted(deduction_months))
        return LwfResult(
            ZERO, ZERO, False, f"LWF is deducted only in month(s) {months} in this state."
        )

    exempt = {d.lower() for d in config.get("exempt_designations", [])}
    if designation and designation.lower() in exempt:
        return LwfResult(
            ZERO, ZERO, True, f"The designation '{designation}' is exempt from LWF here."
        )

    ceiling = config.get("wage_ceiling")
    if ceiling is not None and monthly_wages > _decimal(ceiling):
        return LwfResult(
            ZERO,
            ZERO,
            True,
            f"Monthly wages of {monthly_wages} are above the LWF ceiling of {ceiling}.",
        )

    # ── Wage bands, where a state uses them ─────────────────────────────────
    band_label = None
    employee_amount, employer_amount = _lwf_amounts(config, monthly_wages)
    if "wage_bands" in config:
        band = _matching_band(config["wage_bands"], monthly_wages)
        band_label = band.get("label") or f"up to {band.get('up_to')}"

    return LwfResult(
        employee_contribution=_money(employee_amount),
        employer_contribution=_money(employer_amount),
        is_deduction_month=True,
        reason=f"Deducted in month {month}.",
        wage_band=band_label,
    )


def _matching_band(bands: list[dict], wages: Decimal) -> dict:
    """The wage band a salary falls into. Ordered, last one open."""
    for band in bands:
        upper = band.get("up_to")
        if upper is None or wages <= _decimal(upper):
            return band
    raise StatutoryError(
        f"Monthly wages of {wages} fall outside every configured LWF wage band. The "
        "last band needs an open upper bound."
    )


def _lwf_amounts(config: dict[str, Any], wages: Decimal) -> tuple[Decimal, Decimal]:
    """Employee and employer contribution, flat or percentage.

    Both shapes are supported by the same code path — a state using flat
    amounts and one using a percentage differ only in which keys their config
    carries.
    """
    source: dict[str, Any] = config
    if "wage_bands" in config:
        source = _matching_band(config["wage_bands"], wages)

    if "employee_amount" in source or "employer_amount" in source:
        return (
            _decimal(source.get("employee_amount", 0)),
            _decimal(source.get("employer_amount", 0)),
        )

    if "employee_percent" in source or "employer_percent" in source:
        return (
            wages * _decimal(source.get("employee_percent", 0)) / PERCENT,
            wages * _decimal(source.get("employer_percent", 0)) / PERCENT,
        )

    raise StatutoryError(
        "The LWF configuration carries neither flat amounts "
        "(employee_amount/employer_amount) nor percentages "
        "(employee_percent/employer_percent)."
    )


# ── Statutory bonus ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BonusResult:
    is_eligible: bool
    eligible_wage: Decimal
    days_worked: Decimal
    percentage: Decimal
    amount: Decimal
    ineligibility_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_working(self) -> dict[str, Any]:
        return {
            "is_eligible": self.is_eligible,
            "eligible_wage": str(self.eligible_wage),
            "days_worked": str(self.days_worked),
            "percentage": str(self.percentage),
            "amount": str(self.amount),
            "ineligibility_reason": self.ineligibility_reason,
            "notes": self.notes,
        }


def compute_bonus(
    *,
    config: dict[str, Any],
    monthly_wages: Decimal,
    days_worked: Decimal,
    total_working_days: Decimal,
    percentage: Decimal | None = None,
) -> BonusResult:
    """Statutory bonus for one employee for one year.

    Two ceilings, and conflating them is a common error:

    - `eligibility_wage_ceiling` decides *whether* someone qualifies.
    - `calculation_wage_ceiling` caps the wage the bonus is *computed on*.

    Someone earning above the calculation ceiling but below the eligibility
    ceiling is eligible, and their bonus is computed on the lower figure.
    """
    if not isinstance(monthly_wages, Decimal) or not isinstance(days_worked, Decimal):
        raise StatutoryError("Wages and days must be Decimals, not floats")
    if total_working_days <= ZERO:
        raise StatutoryError("Total working days must be greater than zero")

    eligibility_ceiling = _decimal(_require(config, "eligibility_wage_ceiling"))
    calculation_ceiling = _decimal(_require(config, "calculation_wage_ceiling"))
    minimum_percent = _decimal(_require(config, "minimum_percentage"))
    maximum_percent = _decimal(_require(config, "maximum_percentage"))
    minimum_days = _decimal(_require(config, "minimum_service_days"))

    notes: list[str] = []

    if monthly_wages > eligibility_ceiling:
        return BonusResult(
            is_eligible=False,
            eligible_wage=ZERO,
            days_worked=days_worked,
            percentage=ZERO,
            amount=ZERO,
            ineligibility_reason=(
                f"Monthly wages of {monthly_wages} are above the eligibility ceiling of "
                f"{eligibility_ceiling}."
            ),
        )

    if days_worked < minimum_days:
        return BonusResult(
            is_eligible=False,
            eligible_wage=ZERO,
            days_worked=days_worked,
            percentage=ZERO,
            amount=ZERO,
            ineligibility_reason=(
                f"{days_worked} days worked is below the {minimum_days} days required."
            ),
        )

    # Computed on the lower of actual wage and the calculation ceiling.
    eligible_wage = min(monthly_wages, calculation_ceiling)
    if monthly_wages > calculation_ceiling:
        notes.append(
            f"Bonus is computed on {calculation_ceiling}, the calculation ceiling, "
            f"rather than the actual wage of {monthly_wages}."
        )

    rate = percentage if percentage is not None else minimum_percent
    if rate < minimum_percent:
        raise StatutoryError(
            f"A bonus percentage of {rate} is below the statutory minimum of "
            f"{minimum_percent}."
        )
    if rate > maximum_percent:
        raise StatutoryError(
            f"A bonus percentage of {rate} is above the statutory maximum of "
            f"{maximum_percent}."
        )

    annual_wage = eligible_wage * Decimal(12)
    full_year_bonus = annual_wage * rate / PERCENT

    # Loss of pay reduces it proportionally — days worked comes from
    # attendance, so an employee with unpaid absence gets a smaller bonus.
    proportion = days_worked / total_working_days
    if proportion < 1:
        notes.append(
            f"Prorated to {days_worked} of {total_working_days} working days."
        )

    return BonusResult(
        is_eligible=True,
        eligible_wage=_money(eligible_wage),
        days_worked=days_worked,
        percentage=rate,
        amount=_money(full_year_bonus * proportion),
        notes=notes,
    )


def _require(config: dict[str, Any], key: str) -> Any:
    if key not in config:
        raise StatutoryError(
            f"The bonus configuration is missing '{key}'. The Compliance SME supplies "
            "it — this value is never assumed."
        )
    return config[key]


# ── ESI contribution periods ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EsiContributionPeriod:
    """One ESI contribution period, and when it ends.

    The periods themselves are configuration. India conventionally runs
    April-September and October-March, but the rule this module implements is
    "contributions continue to the end of the period", not "the period is
    April to September".
    """

    start_month: int
    end_month: int
    label: str

    def contains(self, month: int) -> bool:
        if self.start_month <= self.end_month:
            return self.start_month <= month <= self.end_month
        # Wraps the calendar year.
        return month >= self.start_month or month <= self.end_month


def esi_contribution_period(
    month: int, config: dict[str, Any]
) -> EsiContributionPeriod:
    """The contribution period a month falls in."""
    periods = config.get("contribution_periods")
    if not periods:
        raise StatutoryError(
            "The ESI configuration names no contribution periods. Without them the "
            "mid-period ceiling rule cannot be applied."
        )

    for spec in periods:
        period = EsiContributionPeriod(
            start_month=int(spec["start_month"]),
            end_month=int(spec["end_month"]),
            label=spec.get("label", ""),
        )
        if period.contains(month):
            return period

    raise StatutoryError(f"No ESI contribution period covers month {month}")


def should_continue_esi(
    *,
    current_wages: Decimal,
    threshold: Decimal,
    month: int,
    config: dict[str, Any],
    was_contributing_at_period_start: bool,
) -> tuple[bool, str]:
    """Whether ESI applies this month.

    ═══════════════════════════════════════════════════════════════════════
    THE RULE THIS EXISTS FOR
    ═══════════════════════════════════════════════════════════════════════

    An employee whose wages cross the ESI ceiling mid-period keeps
    contributing until the end of that contribution period. Dropping them the
    month they cross is the intuitive implementation and it is wrong — it
    under-contributes and leaves the employee without cover they are entitled
    to for the rest of the period.

    Conversely someone who *starts* a period above the ceiling does not become
    covered mid-period by a wage cut.
    ═══════════════════════════════════════════════════════════════════════

    Returns `(applies, reason)`.
    """
    period = esi_contribution_period(month, config)

    if was_contributing_at_period_start:
        if current_wages > threshold:
            return True, (
                f"Wages of {current_wages} are above the ESI ceiling of {threshold}, but "
                f"contributions continue to the end of the {period.label or 'current'} "
                "contribution period."
            )
        return True, "Wages are within the ESI ceiling."

    # Not contributing at the start of the period.
    if current_wages <= threshold:
        return False, (
            "Wages are now within the ceiling, but coverage begins at the start of the "
            "next contribution period, not mid-period."
        )
    return False, f"Wages of {current_wages} are above the ESI ceiling of {threshold}."


# ── Multi-state Professional Tax ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PtStateResolution:
    state_code: str | None
    needs_review: bool
    reason: str


def resolve_professional_tax_state(
    *,
    location_history: list[tuple[date, str]],
    period_start: date,
    period_end: date,
) -> PtStateResolution:
    """Which state's Professional Tax applies for a period.

    `location_history` is `[(effective_from, state_code)]`, oldest first.

    An employee who worked in one state all period resolves cleanly. One whose
    location changed mid-period is **flagged for review rather than guessed**:
    PT is a state levy with different slabs and different filing, and picking
    the wrong state means filing in the wrong state — which is a compliance
    failure that nobody notices until an inspection.
    """
    if not location_history:
        return PtStateResolution(
            state_code=None,
            needs_review=True,
            reason="This employee has no work location, so no Professional Tax state "
            "can be determined.",
        )

    in_period = [
        (effective, state)
        for effective, state in location_history
        if period_start <= effective <= period_end
    ]

    # The state in force at the start of the period.
    before = [
        (effective, state)
        for effective, state in location_history
        if effective <= period_start
    ]
    opening_state = before[-1][1] if before else location_history[0][1]

    if not in_period:
        return PtStateResolution(
            state_code=opening_state,
            needs_review=False,
            reason=f"Worked in {opening_state} for the whole period.",
        )

    states = {opening_state, *(state for _, state in in_period)}
    if len(states) == 1:
        only = next(iter(states))
        return PtStateResolution(
            state_code=only,
            needs_review=False,
            reason=f"Worked in {only} for the whole period.",
        )

    return PtStateResolution(
        state_code=None,
        needs_review=True,
        reason=(
            f"This employee's work location changed during the period "
            f"({' then '.join(sorted(states))}). Professional Tax is a state levy with "
            "different slabs and different filing, so payroll will not guess which "
            "applies — confirm the primary work location for this period."
        ),
    )


# ── VPF and the PF ceiling election ─────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PfElection:
    """An employee's own PF choices.

    Two separate things that are easy to confuse:

    - `on_actual_wage`: PF computed on the full wage rather than capped at the
      statutory ceiling. This raises **both** the employee and the employer
      contribution, so it is not a change the employee makes alone — it is an
      election the employer has to have agreed to, which is why it is stored
      per employee and effective-dated rather than being a tenant setting.
    - `vpf_percent` / `vpf_amount`: Voluntary PF is an *additional employee*
      contribution. It never changes the employer's. Getting this wrong
      silently inflates employer cost.
    """

    on_actual_wage: bool = False
    vpf_percent: Decimal | None = None
    vpf_amount: Decimal | None = None

    def __post_init__(self) -> None:
        if self.vpf_percent is not None and self.vpf_amount is not None:
            raise StatutoryError(
                "A VPF election carries either a percentage or a fixed amount, not both."
            )
        for value in (self.vpf_percent, self.vpf_amount):
            if value is not None and not isinstance(value, Decimal):
                raise StatutoryError("VPF values must be Decimals, not floats")
            if value is not None and value < ZERO:
                raise StatutoryError("A VPF election cannot be negative")


@dataclass(frozen=True, slots=True)
class PfBase:
    base: Decimal
    ceiling_applied: bool
    reason: str


def resolve_pf_base(
    *,
    wage_base: Decimal,
    ceiling: Decimal,
    ceiling_applies_by_default: bool,
    election: PfElection | None = None,
) -> PfBase:
    """The wage PF is computed on, honouring the employee's election."""
    if election is not None and election.on_actual_wage:
        return PfBase(
            base=wage_base,
            ceiling_applied=False,
            reason=(
                "This employee has elected PF on actual wages, so the statutory ceiling "
                f"of {ceiling} is not applied."
            ),
        )

    if not ceiling_applies_by_default:
        return PfBase(
            base=wage_base,
            ceiling_applied=False,
            reason="This rule set computes PF on actual wages rather than the ceiling.",
        )

    if wage_base <= ceiling:
        return PfBase(
            base=wage_base,
            ceiling_applied=False,
            reason=f"Wages of {wage_base} are within the PF ceiling of {ceiling}.",
        )

    return PfBase(
        base=ceiling,
        ceiling_applied=True,
        reason=f"PF is computed on the ceiling of {ceiling} rather than wages of {wage_base}.",
    )


def compute_vpf(*, pf_wage_base: Decimal, election: PfElection | None) -> Decimal:
    """The employee's voluntary contribution, on top of the statutory one.

    VPF is computed on the PF **wage base**, not on the capped PF base — an
    employee electing VPF is contributing out of their actual wages.
    """
    if election is None:
        return ZERO
    if election.vpf_amount is not None:
        return _money(election.vpf_amount)
    if election.vpf_percent is not None:
        return _money(pf_wage_base * election.vpf_percent / PERCENT)
    return ZERO
