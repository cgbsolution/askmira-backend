"""Reimbursement entitlement, claims, and the year-end taxable balance.

The surprise this module exists to prevent: an employee with a 24,000 annual
fuel entitlement who submitted no bills discovers in March that the whole
24,000 is taxable salary and the tax comes out of one month's pay.

It is avoidable, and only avoidable in advance. So `unclaimed_at` is computable
at any point in the year, not just at the end, and the component carries
`reminder_months_before_year_end` — warning someone in April about a March
consequence is a warning; telling them in March is an announcement.

Nothing here is an expense claim. See the note in `app/models/loans.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "EntitlementPosition",
    "ReimbursementError",
    "accepted_for_payment",
    "position",
    "should_warn",
]

ZERO = Decimal(0)
RUPEE = Decimal("0.01")


class ReimbursementError(ValueError):
    """A reimbursement position cannot be computed from what was supplied."""


def _money(value: Decimal) -> Decimal:
    return value.quantize(RUPEE, rounding=ROUND_HALF_UP)


def _check(name: str, value: Decimal) -> Decimal:
    if not isinstance(value, Decimal):
        raise ReimbursementError(f"{name} must be a Decimal, not a float")
    if value < ZERO:
        raise ReimbursementError(f"{name} cannot be negative")
    return value


@dataclass(frozen=True, slots=True)
class EntitlementPosition:
    """Where an employee stands against one component this year."""

    component_code: str
    financial_year: str
    entitlement: Decimal
    claimed: Decimal
    accepted: Decimal
    # Entitlement not yet used. Taxable if it is still unused at year end.
    unclaimed: Decimal
    # Accepted beyond the entitlement, which is simply not exempt. It is not an
    # error — people do submit more bills than they are entitled to claim.
    excess_accepted: Decimal
    notes: list[str] = field(default_factory=list)

    @property
    def taxable_if_year_ended_now(self) -> Decimal:
        return self.unclaimed

    def as_dict(self) -> dict[str, Any]:
        return {
            "component_code": self.component_code,
            "financial_year": self.financial_year,
            "entitlement": str(self.entitlement),
            "claimed": str(self.claimed),
            "accepted": str(self.accepted),
            "unclaimed": str(self.unclaimed),
            "excess_accepted": str(self.excess_accepted),
            "taxable_if_year_ended_now": str(self.taxable_if_year_ended_now),
            "notes": self.notes,
        }


def position(
    *,
    component_code: str,
    financial_year: str,
    entitlement: Decimal,
    claims: list[dict[str, Any]],
) -> EntitlementPosition:
    """Where the employee stands, at any point in the year.

    `claims` is `[{"amount", "accepted_amount", "status"}]`. Only accepted
    amounts consume entitlement — a submitted-but-unverified bill has not
    reduced anyone's taxable balance, and treating it as though it had is how
    an employee is told in February that they are covered and in March that
    they are not.
    """
    entitlement = _check("The entitlement", entitlement)
    notes: list[str] = []

    claimed = ZERO
    accepted = ZERO
    pending = ZERO

    for claim in claims:
        amount = _check("A claim amount", Decimal(str(claim.get("amount", 0))))
        accepted_amount = _check(
            "An accepted amount", Decimal(str(claim.get("accepted_amount", 0)))
        )
        status = str(claim.get("status", "")).lower()

        claimed += amount
        if status in ("verified", "partial"):
            accepted += accepted_amount
        elif status == "submitted":
            pending += amount

    if pending > ZERO:
        notes.append(
            f"{_money(pending)} is submitted but not yet verified. It does not reduce "
            "the taxable balance until it is — being told in February that you are "
            "covered and in March that you are not is the surprise this avoids."
        )

    exempt_accepted = min(accepted, entitlement)
    excess = accepted - exempt_accepted
    if excess > ZERO:
        notes.append(
            f"{_money(excess)} was accepted beyond the annual entitlement of "
            f"{entitlement}. It is not exempt under this component."
        )

    unclaimed = entitlement - exempt_accepted
    if unclaimed > ZERO:
        notes.append(
            f"{_money(unclaimed)} of entitlement is unused. If the year ends with it "
            "unused it becomes taxable salary, and the tax falls in one month."
        )

    return EntitlementPosition(
        component_code=component_code,
        financial_year=financial_year,
        entitlement=_money(entitlement),
        claimed=_money(claimed),
        accepted=_money(accepted),
        unclaimed=_money(unclaimed),
        excess_accepted=_money(excess),
        notes=notes,
    )


def accepted_for_payment(
    *,
    claim_amount: Decimal,
    accepted_amount: Decimal,
    remaining_entitlement: Decimal,
    status: str,
) -> tuple[Decimal, str]:
    """What actually pays out, exempt, for one claim.

    A partial acceptance pays the accepted amount and no more — which is the
    whole point of recording acceptance separately from submission.
    """
    claim_amount = _check("The claim amount", claim_amount)
    accepted_amount = _check("The accepted amount", accepted_amount)
    remaining_entitlement = _check("The remaining entitlement", remaining_entitlement)

    normalised = status.lower()
    if normalised == "rejected":
        return ZERO, "The claim was rejected, so nothing is paid under this component."
    if normalised == "submitted":
        return ZERO, "The claim is awaiting verification and is not yet payable."
    if accepted_amount > claim_amount:
        raise ReimbursementError(
            f"{accepted_amount} was accepted against a claim of {claim_amount}. More "
            "cannot be accepted than was submitted."
        )

    payable = min(accepted_amount, remaining_entitlement)
    if payable < accepted_amount:
        return payable, (
            f"{accepted_amount} was accepted but only {remaining_entitlement} of the "
            "annual entitlement is left, so that is what pays exempt."
        )
    if normalised == "partial":
        return payable, (
            f"{accepted_amount} of the {claim_amount} claimed was accepted; the rest is "
            "not paid under this component."
        )
    return payable, f"{payable} accepted in full and paid exempt."


def should_warn(
    *,
    today: date,
    financial_year_end: date,
    unclaimed: Decimal,
    reminder_months_before_year_end: int,
) -> tuple[bool, str]:
    """Whether to warn now about unclaimed entitlement.

    In advance, or not at all. A notification in March about a March
    consequence is not a warning — it is an announcement, and there is nothing
    the employee can do about it by then.
    """
    if unclaimed <= ZERO:
        return False, "The entitlement is fully used."

    months_left = (
        (financial_year_end.year - today.year) * 12
        + financial_year_end.month
        - today.month
    )
    if months_left < 0:
        return False, "The year has ended; the unclaimed balance is now taxable."
    if months_left > reminder_months_before_year_end:
        return False, (
            f"{months_left} month(s) remain, and the reminder window is "
            f"{reminder_months_before_year_end}."
        )

    return True, (
        f"{_money(unclaimed)} of entitlement is unclaimed with {months_left} month(s) "
        "of the year left. Submit bills before the year ends, or the balance becomes "
        "taxable salary and the tax falls in a single month."
    )
