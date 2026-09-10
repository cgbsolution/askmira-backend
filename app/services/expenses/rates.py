"""Mileage, per-diem and currency conversion. All effective-dated.

═══════════════════════════════════════════════════════════════════════════════
A RATE CHANGE IS A NEW ROW, NEVER AN EDIT
═══════════════════════════════════════════════════════════════════════════════

The same rule as `PayrollRuleSet`, for the same reason. A mileage rate revised in
April must not change what a March claim was worth: the employee was reimbursed
at the March rate, the payment is made, and a system that recomputes it from
today's rate produces a number that does not match anything anybody paid.

So `rate_on` resolves by the claim's own date, and an invariant checks nothing
updates a rate row in place.

═══════════════════════════════════════════════════════════════════════════════
A PER-DIEM NEEDS NO RECEIPT, BY DEFINITION
═══════════════════════════════════════════════════════════════════════════════

That is what a per-diem *is* — a fixed amount for a day away, paid instead of
itemising. Demanding a receipt is asking for a document that does not exist, and
the employee will produce something. `PerDiemRate` therefore carries what it
covers (`includes_meals`, `includes_accommodation`) so a policy can say what a
person may *also* claim, rather than asking them to prove what the per-diem was
for.

═══════════════════════════════════════════════════════════════════════════════
BOTH CURRENCY CONVENTIONS ARE USED, SO IT IS A SETTING
═══════════════════════════════════════════════════════════════════════════════

Some finance teams convert at the transaction date — the employee spent €80 on
the 3rd, and €80 on the 3rd is what it cost. Others convert at settlement — the
company paid rupees on the 20th, and that is the outflow. Both are defensible
and both are in use, so `ConversionConvention` is configured rather than assumed,
and the converted claim records which one produced the number.

Pure. Dates and rates are always supplied.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.services.expenses.policy import CityTier, money

__all__ = [
    "ConversionConvention",
    "ConvertedAmount",
    "MileageRate",
    "PerDiemRate",
    "RateError",
    "convert",
    "mileage_claim",
    "per_diem_claim",
    "rate_on",
]


class RateError(ValueError):
    """A rate this module will not guess at."""


@dataclass(frozen=True, slots=True)
class MileageRate:
    """What one kilometre is worth in one vehicle, from one date."""

    vehicle_type: str
    rate_per_km: Decimal
    effective_from: date
    #: Distance beyond the declared route before the claim is flagged. A route
    #: is a straight line and a road is not; a claim flagged for taking the
    #: only available road is a claim the employee learns to inflate to match.
    tolerance_percent: Decimal = Decimal("15")


@dataclass(frozen=True, slots=True)
class PerDiemRate:
    """A fixed daily amount for one grade in one city tier, from one date."""

    city_tier: CityTier
    grade: str
    amount: Decimal
    effective_from: date
    includes_meals: bool = True
    includes_accommodation: bool = False

    def covers(self) -> tuple[str, ...]:
        out = []
        if self.includes_meals:
            out.append("meals")
        if self.includes_accommodation:
            out.append("accommodation")
        return tuple(out)


def rate_on(rates: list[Any], as_of: date, **match: Any) -> Any:
    """The rate in force on a date, for a matching key.

    Resolves by the claim's own date, never by today. A mileage rate revised in
    April must not change what a March claim was worth — the employee was
    reimbursed at the March rate and the payment has already been made.
    """
    candidates = [
        rate
        for rate in rates
        if rate.effective_from <= as_of
        and all(getattr(rate, key) == value for key, value in match.items())
    ]
    if not candidates:
        described = ", ".join(f"{k}={v!r}" for k, v in match.items())
        raise RateError(
            f"No rate in force on {as_of.isoformat()} for {described}. Guessing "
            "one would produce a reimbursement nobody authorised — the rate has "
            "to be configured with an effective date on or before the claim."
        )
    return max(candidates, key=lambda rate: rate.effective_from)


@dataclass(frozen=True, slots=True)
class MileageClaim:
    """One mileage claim, with the arithmetic shown."""

    distance_km: Decimal
    rate_per_km: Decimal
    amount: Decimal
    declared_route_km: Decimal | None
    variance_percent: Decimal | None
    exceeds_tolerance: bool
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "distance_km": str(self.distance_km),
            "rate_per_km": str(self.rate_per_km),
            "amount": str(self.amount),
            "declared_route_km": (
                str(self.declared_route_km)
                if self.declared_route_km is not None
                else None
            ),
            "variance_percent": (
                str(self.variance_percent)
                if self.variance_percent is not None
                else None
            ),
            "exceeds_tolerance": self.exceeds_tolerance,
            "note": self.note,
        }


def mileage_claim(
    *,
    distance_km: Decimal,
    rate: MileageRate,
    route_km: Decimal | None = None,
) -> MileageClaim:
    """Value a mileage claim, and say whether it is further than the route.

    Flags rather than refuses. A route is a straight line and a road is not; a
    claim rejected for taking a diversion round roadworks is a claim the
    employee learns to declare as exactly the route distance next time, which
    destroys the only signal this check had.
    """
    if distance_km < 0:
        raise RateError("A distance cannot be negative.")

    amount = money(Decimal(str(distance_km)) * rate.rate_per_km)

    if route_km is None or route_km <= 0:
        return MileageClaim(
            distance_km=Decimal(str(distance_km)),
            rate_per_km=rate.rate_per_km,
            amount=amount,
            declared_route_km=None,
            variance_percent=None,
            exceeds_tolerance=False,
            note="No route distance to compare against.",
        )

    variance = (
        (Decimal(str(distance_km)) - route_km) / route_km * Decimal("100")
    ).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    exceeds = variance > rate.tolerance_percent

    return MileageClaim(
        distance_km=Decimal(str(distance_km)),
        rate_per_km=rate.rate_per_km,
        amount=amount,
        declared_route_km=route_km,
        variance_percent=variance,
        exceeds_tolerance=exceeds,
        note=(
            f"{variance}% further than the {route_km} km route, above the "
            f"{rate.tolerance_percent}% tolerance. Flagged for the approver, not "
            "refused — roads are not straight lines."
            if exceeds
            else f"{variance}% against the route distance, inside tolerance."
        ),
    )


@dataclass(frozen=True, slots=True)
class PerDiemClaim:
    """One per-diem claim. No receipt, by definition."""

    days: int
    daily_amount: Decimal
    amount: Decimal
    covers: tuple[str, ...]
    requires_receipt: bool
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "daily_amount": str(self.daily_amount),
            "amount": str(self.amount),
            "covers": list(self.covers),
            "requires_receipt": self.requires_receipt,
            "note": self.note,
        }


def per_diem_claim(*, days: int, rate: PerDiemRate) -> PerDiemClaim:
    """Value a per-diem, and say what it covers so the employee knows.

    `requires_receipt` is always False and is returned anyway, because a caller
    that has to remember not to ask is a caller that eventually asks.
    """
    if days < 1:
        raise RateError("A per-diem claim needs at least one day.")

    covers = rate.covers()
    also = (
        "You can claim meals separately."
        if not rate.includes_meals
        else "Meals are covered by this and should not be claimed again."
    )

    return PerDiemClaim(
        days=days,
        daily_amount=money(rate.amount),
        amount=money(rate.amount * days),
        covers=covers,
        requires_receipt=False,
        note=(
            f"{days} day(s) at ₹{money(rate.amount)}. No receipt is needed — "
            f"that is what a per-diem is. {also}"
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Currency
# ══════════════════════════════════════════════════════════════════════════════


class ConversionConvention(str, enum.Enum):
    """Which date's rate converts a foreign claim.

    Both are in use and both are defensible, so this is configured rather than
    assumed. A product that picked one silently would be making a finance
    policy decision on the client's behalf, and the difference on a long trip
    through a moving rupee is real money.
    """

    #: The employee spent €80 on the 3rd, and €80 on the 3rd is what it cost.
    TRANSACTION_DATE = "transaction_date"
    #: The company paid rupees on the 20th, and that is the actual outflow.
    SETTLEMENT_DATE = "settlement_date"


@dataclass(frozen=True, slots=True)
class ConvertedAmount:
    """A foreign claim, with both amounts and how the conversion was reached.

    Both are stored. Keeping only the converted figure loses what the employee
    actually spent — which is the number on their card statement and the one
    they will query — and keeping only the original leaves the ledger unable to
    say what it paid.
    """

    original_amount: Decimal
    original_currency: str
    converted_amount: Decimal
    converted_currency: str
    rate: Decimal
    rate_date: date
    convention: ConversionConvention
    rate_source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "original_amount": str(self.original_amount),
            "original_currency": self.original_currency,
            "converted_amount": str(self.converted_amount),
            "converted_currency": self.converted_currency,
            "rate": str(self.rate),
            "rate_date": self.rate_date.isoformat(),
            "convention": self.convention.value,
            "rate_source": self.rate_source,
            "note": (
                f"{self.original_amount} {self.original_currency} converted at "
                f"{self.rate} on {self.rate_date.isoformat()} "
                f"({self.convention.value.replace('_', ' ')}, {self.rate_source})."
            ),
        }


def convert(
    *,
    amount: Decimal,
    currency: str,
    transaction_date: date,
    settlement_date: date | None,
    convention: ConversionConvention,
    rate: Decimal,
    rate_source: str,
    to_currency: str = "INR",
) -> ConvertedAmount:
    """Convert a foreign claim, recording which date's rate was used.

    A settlement-date conversion with no settlement date raises rather than
    quietly falling back to the transaction date: the two produce different
    numbers, and silently substituting one convention for the other is exactly
    the discrepancy a finance team will find at the year end and not be able to
    explain.
    """
    if amount <= 0:
        raise RateError("A claim amount has to be positive.")
    if rate <= 0:
        raise RateError(
            "An exchange rate has to be positive. A zero or negative rate would "
            "convert a real expense into nothing."
        )
    if currency == to_currency:
        raise RateError(
            f"{currency} is already the reporting currency; converting it would "
            "record a rate on a claim that never crossed a border."
        )

    if convention is ConversionConvention.SETTLEMENT_DATE:
        if settlement_date is None:
            raise RateError(
                "This tenant converts at the settlement date and this claim has "
                "not settled. Falling back to the transaction date would give a "
                "different number under a convention nobody chose — the "
                "conversion waits until settlement."
            )
        rate_date = settlement_date
    else:
        rate_date = transaction_date

    return ConvertedAmount(
        original_amount=money(amount),
        original_currency=currency,
        converted_amount=money(Decimal(str(amount)) * rate),
        converted_currency=to_currency,
        rate=rate,
        rate_date=rate_date,
        convention=convention,
        rate_source=rate_source,
    )
