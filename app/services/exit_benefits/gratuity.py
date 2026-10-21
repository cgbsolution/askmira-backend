"""Gratuity computation.

═══════════════════════════════════════════════════════════════════════════════
ON THE "4 YEARS 240 DAYS" QUESTION
═══════════════════════════════════════════════════════════════════════════════

Whether an employee who has served four full years and 240 days of the fifth
has completed five years of continuous service is genuinely contested. The
Madras High Court has held that it does; other benches have read section 4(1)
more strictly; the Payment of Gratuity Act's own section 2A defines continuous
service in terms that both sides cite.

This module takes no position. `GratuityConfig.eligibility_days_in_final_year`
turns the treatment on and sets the day count; leaving it null turns it off.
A tenant's own counsel decides, and the decision is visible in configuration
rather than buried in a conditional.

Baking in one interpretation would make the product quietly wrong for whichever
half of the customer base holds the other view — and wrong in a direction that
either underpays a leaver or overstates a liability.
═══════════════════════════════════════════════════════════════════════════════

No ceiling, divisor or eligibility period appears as a literal here. The
statutory exemption has been raised repeatedly and will be again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.models.exit_benefits import ExitReason, GratuityConfig, ServiceRounding

__all__ = [
    "GratuityError",
    "GratuityResult",
    "ServiceComputation",
    "compute_gratuity",
    "compute_service",
]

ZERO = Decimal(0)
RUPEE = Decimal("0.01")
DAYS_PER_YEAR = Decimal("365.25")  # Calendar arithmetic, not a gratuity rule.


class GratuityError(ValueError):
    """The configuration is missing or inconsistent."""


def _money(value: Decimal) -> Decimal:
    return value.quantize(RUPEE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class ServiceComputation:
    """Continuous service, and how it was arrived at.

    Two year figures, and conflating them is a real bug rather than a
    simplification:

    - `completed_years` is whole years actually served. **Eligibility is tested
      against this.** A qualifying period asks whether five years were
      completed, not whether a rounding rule reaches five.
    - `rounded_years` applies the tenant's rounding rule and is the multiplier
      in the payout formula, used only once eligibility is established.

    Using the rounded figure for eligibility would let the conventional
    six-month rule grant gratuity at four years and seven months — which no
    reading of the Act supports, and which would silently make the contested
    240-day setting irrelevant.
    """

    total_days: int
    excluded_days: int
    countable_days: int
    raw_years: Decimal
    # Whole years served. The eligibility test.
    completed_years: int
    # After the rounding rule. The payout multiplier.
    rounded_years: Decimal
    rounding_applied: str
    # True when the configured final-year day rule completed a part year.
    final_year_rule_applied: bool = False
    days_into_final_year: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GratuityResult:
    is_eligible: bool
    eligibility_waived: bool
    service: ServiceComputation
    monthly_wages: Decimal
    gross_amount: Decimal
    exempt_amount: Decimal
    # The excess above the exemption ceiling. Goes into TDS.
    taxable_amount: Decimal
    capped_by_contract: bool = False
    ineligibility_reason: str | None = None

    def as_working(self) -> dict[str, Any]:
        return {
            "is_eligible": self.is_eligible,
            "eligibility_waived": self.eligibility_waived,
            "ineligibility_reason": self.ineligibility_reason,
            "service": {
                "total_days": self.service.total_days,
                "excluded_days": self.service.excluded_days,
                "countable_days": self.service.countable_days,
                "raw_years": str(self.service.raw_years),
                "completed_years": self.service.completed_years,
                "days_into_final_year": self.service.days_into_final_year,
                "rounded_years": str(self.service.rounded_years),
                "rounding_applied": self.service.rounding_applied,
                "final_year_rule_applied": self.service.final_year_rule_applied,
                "notes": self.service.notes,
            },
            "monthly_wages": str(self.monthly_wages),
            "gross_amount": str(self.gross_amount),
            "exempt_amount": str(self.exempt_amount),
            "taxable_amount": str(self.taxable_amount),
            "capped_by_contract": self.capped_by_contract,
        }


def compute_service(
    *,
    joined_on: date,
    as_of: date,
    config: GratuityConfig,
    unpaid_leave_days: int = 0,
    notice_period_days: int = 0,
) -> ServiceComputation:
    """Continuous service between two dates, under a tenant's rules.

    Unpaid leave beyond the configured threshold is excluded. Below it, the
    convention is that continuity is not broken, so it counts.
    """
    if as_of < joined_on:
        raise GratuityError("The as-of date is before the joining date")

    total_days = (as_of - joined_on).days
    notes: list[str] = []

    excluded = 0
    threshold = config.unpaid_leave_break_threshold_days
    if threshold is not None and unpaid_leave_days > threshold:
        excluded = unpaid_leave_days
        notes.append(
            f"{unpaid_leave_days} days of unpaid leave exceed the {threshold}-day "
            "threshold, so they do not count toward continuous service."
        )
    elif unpaid_leave_days > 0:
        notes.append(
            f"{unpaid_leave_days} days of unpaid leave are within the threshold and "
            "count toward continuous service."
        )

    if not config.include_notice_period_in_service and notice_period_days > 0:
        excluded += notice_period_days
        notes.append(f"{notice_period_days} days of notice period are excluded by configuration.")

    countable = max(0, total_days - excluded)
    raw_years = Decimal(countable) / DAYS_PER_YEAR

    # Whole years served, and how far into the next one.
    completed_years = int(raw_years)
    days_into_final_year = countable - int(Decimal(completed_years) * DAYS_PER_YEAR)

    rounded, rule = _round_service(raw_years, config.service_rounding)

    # ── The contested final-year treatment ──────────────────────────────────
    # Applies to *eligibility* — whether the part year counts as complete —
    # not to the payout multiplier, which the rounding rule already governs.
    final_year_applied = False
    day_rule = config.eligibility_days_in_final_year
    if day_rule is not None and days_into_final_year >= day_rule:
        completed_years += 1
        final_year_applied = True
        notes.append(
            f"{days_into_final_year} days served in the final year meet the configured "
            f"{day_rule}-day rule, so that year counts as complete for eligibility. "
            "This interpretation is a tenant setting, not a fixed reading of the Act."
        )

    return ServiceComputation(
        total_days=total_days,
        excluded_days=excluded,
        countable_days=countable,
        raw_years=raw_years.quantize(Decimal("0.01")),
        completed_years=completed_years,
        rounded_years=rounded,
        rounding_applied=rule,
        final_year_rule_applied=final_year_applied,
        days_into_final_year=days_into_final_year,
        notes=notes,
    )


def _round_service(raw_years: Decimal, mode: ServiceRounding) -> tuple[Decimal, str]:
    """Apply the configured rounding rule."""
    whole = Decimal(int(raw_years))
    fraction = raw_years - whole

    if mode is ServiceRounding.EXACT_FRACTION:
        return raw_years.quantize(Decimal("0.01")), "exact_fraction"
    if mode is ServiceRounding.ALWAYS_ROUND_DOWN:
        return whole, "always_round_down"
    if mode is ServiceRounding.ALWAYS_ROUND_UP:
        return (whole + 1 if fraction > 0 else whole), "always_round_up"

    # The conventional Indian reading: more than six months rounds up. Half a
    # year expressed as a fraction, not a hardcoded month count.
    half = Decimal("0.5")
    if fraction > half:
        return whole + 1, "nearest_year_six_month_rule (rounded up)"
    return whole, "nearest_year_six_month_rule (rounded down)"


def compute_gratuity(
    *,
    joined_on: date,
    exit_date: date,
    exit_reason: ExitReason,
    monthly_wages: Decimal,
    config: GratuityConfig,
    unpaid_leave_days: int = 0,
    notice_period_days: int = 0,
) -> GratuityResult:
    """Compute a gratuity payout.

    `monthly_wages` is the sum of the components the tenant's SME has mapped as
    the wage basis — conventionally BASIC + DA, but that mapping is
    configuration and is signed off separately.

    The formula is `(wages / divisor) x days_per_year x service_years`. The
    structure is statute; every number in it is configuration.
    """
    if not isinstance(monthly_wages, Decimal):
        raise GratuityError("Wages must be a Decimal, not a float")
    if not config.formula_basis:
        raise GratuityError(
            "No wage-basis components are configured for gratuity. The Compliance SME "
            "maps which components constitute wages before gratuity can be computed."
        )
    if config.days_per_year_divisor <= 0:
        raise GratuityError("The gratuity divisor must be greater than zero")

    service = compute_service(
        joined_on=joined_on,
        as_of=exit_date,
        config=config,
        unpaid_leave_days=unpaid_leave_days,
        notice_period_days=notice_period_days,
    )

    # ── Eligibility ─────────────────────────────────────────────────────────
    # Death and disablement waive the qualifying period entirely — the Act is
    # unambiguous there, and it is the case where getting it wrong is worst.
    waived = exit_reason.value in (config.eligibility_waived_reasons or [])

    # Tested against *completed* years. The rounding rule is a payout
    # multiplier and must not decide eligibility — see `ServiceComputation`.
    eligible = waived or service.completed_years >= config.eligibility_years
    reason = None
    if not eligible:
        reason = (
            f"{service.completed_years} completed years of continuous service "
            f"(plus {service.days_into_final_year} days) is below the "
            f"{config.eligibility_years} years required."
        )
        if config.eligibility_days_in_final_year is None:
            reason += (
                " The final-year day rule is switched off for this workspace; enabling "
                "it may change this outcome."
            )

    if not eligible:
        return GratuityResult(
            is_eligible=False,
            eligibility_waived=False,
            service=service,
            monthly_wages=_money(monthly_wages),
            gross_amount=ZERO,
            exempt_amount=ZERO,
            taxable_amount=ZERO,
            ineligibility_reason=reason,
        )

    # ── The formula ─────────────────────────────────────────────────────────
    daily_rate = monthly_wages / Decimal(config.days_per_year_divisor)
    gross = daily_rate * Decimal(str(config.days_per_year)) * service.rounded_years

    capped = False
    if config.max_payable_amount is not None:
        ceiling = Decimal(str(config.max_payable_amount))
        if gross > ceiling:
            gross = ceiling
            capped = True

    gross = _money(gross)

    # ── Tax ─────────────────────────────────────────────────────────────────
    exemption_ceiling = Decimal(str(config.max_exempt_amount))
    exempt = min(gross, exemption_ceiling)
    taxable = _money(max(ZERO, gross - exempt))

    return GratuityResult(
        is_eligible=True,
        eligibility_waived=waived,
        service=service,
        monthly_wages=_money(monthly_wages),
        gross_amount=gross,
        exempt_amount=_money(exempt),
        taxable_amount=taxable,
        capped_by_contract=capped,
    )
