"""Offer terms: what is material, what deviates, and who else earns this.

═══════════════════════════════════════════════════════════════════════════════
THE THREE THINGS AN APPROVER CANNOT SEE FOR THEMSELVES
═══════════════════════════════════════════════════════════════════════════════

**1. That the offer changed after they approved it.** An approval is a decision
about specific terms. Change the CTC after the CFO signed and their approval is
being used as cover for a number they never saw. So the approval records a
fingerprint of the material terms, and any later change to those terms voids it.
Not "flags", not "warns" — voids, because a voided approval sends it back round
and a warning gets closed.

**2. That the requisition budget has been exceeded.** Approvers approve. It is
what the button says and it is what the day is full of. A deviation that is
merely visible on the page is a deviation that gets approved, which is where
salary creep starts — one offer at a time, each individually defensible.

**3. What comparable people already earn.** This is the expensive one. Nobody
approving an offer knows the salaries of the eleven other L4 engineers, and
paying a new hire above existing staff costs far more than the difference: it
costs the correction, and it costs the people who find out. The parity view is
computed at approval time, not left to be looked up.

═══════════════════════════════════════════════════════════════════════════════

Pure. No database, no session. It takes facts and returns judgements, which is
what makes each of the three testable against numbers rather than against a
screenshot.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

__all__ = [
    "MATERIAL_TERMS",
    "BudgetDeviation",
    "OfferTermsError",
    "ParityView",
    "budget_deviation",
    "canonical_terms",
    "internal_parity",
    "material_changes",
    "terms_fingerprint",
]


class OfferTermsError(ValueError):
    """Offer terms are internally inconsistent."""


# ═══════════════════════════════════════════════════════════════════════════
# Material terms
# ═══════════════════════════════════════════════════════════════════════════

# The terms an approver was actually deciding about. Changing any of them after
# approval voids it.
#
# What is deliberately *not* here: the candidate's phone number, the recruiter,
# internal notes, the letter template. Those change constantly during an offer's
# life and voiding the approval over a corrected phone number would train
# everyone to treat re-approval as noise — which is precisely how a real CTC
# change slips through the next time.
MATERIAL_TERMS = frozenset(
    {
        "ctc_annual",
        "salary_structure_id",
        "grade",
        "designation",
        "joining_date",
        "joining_location_id",
        "probation_months",
        "notice_period_days",
        # Offer-level money that never appears on a payslip but is absolutely
        # part of what was agreed. Excluding it would let a joining bonus be
        # added after approval, which is the cheapest way to break an approval
        # chain that otherwise works.
        "offer_components",
    }
)


def _normalise(value: Any) -> Any:
    """Make a value comparable across a round trip through JSON and Postgres.

    `Decimal("1200000")` and `Decimal("1200000.00")` are the same money and must
    fingerprint the same, or an offer would appear to have been edited by being
    saved. `Decimal.normalize` handles that; dates go to ISO; UUIDs to str.
    """
    if isinstance(value, Decimal):
        # normalize() turns 1200000.00 into 1.2E+6 — correct but unreadable in
        # a diff, so bring the exponent back to zero for integral values.
        normalised = value.normalize()
        _sign, _digits, exponent = normalised.as_tuple()
        if isinstance(exponent, int) and exponent > 0:
            normalised = normalised.quantize(Decimal(1))
        return str(normalised)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _normalise(v) for k, v in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_normalise(v) for v in value]
    if value is None or isinstance(value, str | int | bool):
        return value
    return str(value)


def canonical_terms(terms: dict[str, Any]) -> dict[str, Any]:
    """Just the material terms, normalised, in a fixed order.

    Keys outside `MATERIAL_TERMS` are dropped rather than rejected: callers pass
    whole offer payloads, and an offer legitimately carries plenty that does not
    bear on the approval.
    """
    return {
        key: _normalise(terms.get(key))
        for key in sorted(MATERIAL_TERMS)
        if key in terms
    }


def terms_fingerprint(terms: dict[str, Any]) -> str:
    """A SHA-256 over the material terms, and only the material terms.

    Stored on the approval. If it stops matching the offer, the approval is not
    an approval of this offer any more.
    """
    canonical = canonical_terms(terms)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def material_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Which material terms differ, named.

    Named rather than counted, because the message an approver gets on
    re-approval is the whole value of the check: "CTC changed" sends them to the
    number; "this offer was edited" sends them to nothing.
    """
    old = canonical_terms(before)
    new = canonical_terms(after)
    changed = []
    for key in sorted(MATERIAL_TERMS):
        if old.get(key) != new.get(key):
            changed.append(key)
    return changed


# ═══════════════════════════════════════════════════════════════════════════
# Budget deviation
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class BudgetDeviation:
    """How far outside the requisition's budgeted band this offer sits."""

    offered_ctc: Decimal
    band_min: Decimal | None
    band_max: Decimal | None
    amount_over: Decimal
    percent_over: Decimal
    is_over_band: bool
    is_under_band: bool
    # Non-empty means the approval screen must show this at the top, not in a
    # detail panel. Emptiness is the only "no deviation" signal — a caller
    # cannot mistake a falsy zero for "fine".
    headline: str

    @property
    def requires_prominent_flag(self) -> bool:
        return bool(self.headline)

    def as_dict(self) -> dict[str, Any]:
        return {
            "offered_ctc": str(self.offered_ctc),
            "band_min": str(self.band_min) if self.band_min is not None else None,
            "band_max": str(self.band_max) if self.band_max is not None else None,
            "amount_over": str(self.amount_over),
            "percent_over": str(self.percent_over),
            "is_over_band": self.is_over_band,
            "is_under_band": self.is_under_band,
            "headline": self.headline,
            "requires_prominent_flag": self.requires_prominent_flag,
        }


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def budget_deviation(
    *,
    offered_ctc: Decimal,
    band_min: Decimal | None,
    band_max: Decimal | None,
) -> BudgetDeviation:
    """Compare an offer against the requisition's budgeted band.

    Over-band is the case that matters and the case the headline is written for.
    Under-band is reported too but not flagged: paying below the band is not a
    budget problem, though it is worth an approver seeing in case the band was
    the *point* of the requisition.
    """
    if not isinstance(offered_ctc, Decimal):
        raise OfferTermsError("Offered CTC must be a Decimal, not a float")

    over = band_max is not None and offered_ctc > band_max
    under = band_min is not None and offered_ctc < band_min

    amount_over = _money(offered_ctc - band_max) if over else Decimal("0.00")
    percent_over = Decimal("0.00")
    if over and band_max and band_max > 0:
        percent_over = ((offered_ctc - band_max) / band_max * 100).quantize(
            Decimal("0.01")
        )

    headline = ""
    if over:
        headline = (
            f"This offer is {amount_over} above the approved band "
            f"({percent_over}% over the {band_max} ceiling on the requisition)."
        )
    elif under:
        headline = (
            f"This offer is {_money(band_min - offered_ctc)} below the requisition's "
            f"{band_min} floor."
        )

    return BudgetDeviation(
        offered_ctc=_money(offered_ctc),
        band_min=_money(band_min) if band_min is not None else None,
        band_max=_money(band_max) if band_max is not None else None,
        amount_over=amount_over,
        percent_over=percent_over,
        is_over_band=over,
        is_under_band=under,
        headline=headline,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Internal parity
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ParityView:
    """What comparable employees already earn, against what is being offered.

    Comparable means same grade, and same department where one is specified.
    Deliberately coarse: a similarity model here would produce a number nobody
    can argue with in a room, and this exists to be argued with in a room.
    """

    offered_ctc: Decimal
    comparable_count: int
    minimum: Decimal | None
    median: Decimal | None
    maximum: Decimal | None
    above_every_comparable: bool
    percent_above_median: Decimal | None
    headline: str
    # Below this, the numbers describe individuals rather than a market. The
    # view still renders — hiding it would be worse — but it says so.
    is_indicative_only: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "offered_ctc": str(self.offered_ctc),
            "comparable_count": self.comparable_count,
            "minimum": str(self.minimum) if self.minimum is not None else None,
            "median": str(self.median) if self.median is not None else None,
            "maximum": str(self.maximum) if self.maximum is not None else None,
            "above_every_comparable": self.above_every_comparable,
            "percent_above_median": (
                str(self.percent_above_median)
                if self.percent_above_median is not None
                else None
            ),
            "headline": self.headline,
            "is_indicative_only": self.is_indicative_only,
        }


# Under three comparables, min/median/max are three people's salaries wearing a
# statistical hat. The view is still shown, labelled.
MIN_COMPARABLES_FOR_A_DISTRIBUTION = 3


def internal_parity(
    *, offered_ctc: Decimal, comparable_ctcs: list[Decimal], grade: str | None = None
) -> ParityView:
    """The check that prevents new hires being paid above existing staff.

    The most expensive offer mistake in the market, and the one nobody approving
    an offer can catch unaided — no approver knows what the other eleven L4s
    earn.
    """
    if not isinstance(offered_ctc, Decimal):
        raise OfferTermsError("Offered CTC must be a Decimal, not a float")

    label = f"grade {grade}" if grade else "this grade"
    values = sorted(Decimal(str(c)) for c in comparable_ctcs)

    if not values:
        return ParityView(
            offered_ctc=_money(offered_ctc),
            comparable_count=0,
            minimum=None,
            median=None,
            maximum=None,
            above_every_comparable=False,
            percent_above_median=None,
            headline=(
                f"No existing employees at {label} to compare against. This offer "
                "sets the internal benchmark rather than being measured by one."
            ),
            is_indicative_only=True,
        )

    middle = len(values) // 2
    median = (
        values[middle]
        if len(values) % 2
        else ((values[middle - 1] + values[middle]) / 2)
    )
    maximum = values[-1]
    above_all = offered_ctc > maximum

    percent_above_median = None
    if median > 0:
        percent_above_median = ((offered_ctc - median) / median * 100).quantize(
            Decimal("0.01")
        )

    if above_all:
        headline = (
            f"This offer is above every one of the {len(values)} employees at {label}. "
            f"The highest paid earns {_money(maximum)}."
        )
    elif percent_above_median is not None and percent_above_median > 0:
        headline = (
            f"{percent_above_median}% above the median for {label} "
            f"({_money(median)} across {len(values)} employees)."
        )
    else:
        headline = (
            f"At or below the median for {label} "
            f"({_money(median)} across {len(values)} employees)."
        )

    return ParityView(
        offered_ctc=_money(offered_ctc),
        comparable_count=len(values),
        minimum=_money(values[0]),
        median=_money(median),
        maximum=_money(maximum),
        above_every_comparable=above_all,
        percent_above_median=percent_above_median,
        headline=headline,
        is_indicative_only=len(values) < MIN_COMPARABLES_FOR_A_DISTRIBUTION,
    )
