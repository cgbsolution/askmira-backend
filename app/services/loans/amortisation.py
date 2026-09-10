"""Loan amortisation, prepayment, and the concessional-loan perquisite.

═══════════════════════════════════════════════════════════════════════════════
THE INVARIANT THIS MODULE HOLDS
═══════════════════════════════════════════════════════════════════════════════

**The principal components across the whole schedule sum to exactly the
principal.** Not approximately — exactly.

Every EMI is a rounded figure, and rounding twelve or sixty of them independently
leaves a remainder of a few rupees. Where that remainder goes decides whether the
loan closes cleanly or leaves someone owing four rupees forever, and an
amortisation ledger that does not reconcile is one the employee can dispute and
nobody can defend.

So the final instalment absorbs the remainder: it is computed as "whatever is
left", not as the same EMI as the others. That is also how banks do it, and it
is why the last EMI on a real loan statement is a slightly odd number.
═══════════════════════════════════════════════════════════════════════════════

No rate, threshold or ceiling is written in this file. The perquisite threshold
and the notional rate come from configuration the Compliance SME supplies —
they change, and a change should not be a release.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "AmortisationError",
    "Instalment",
    "PrepaymentMode",
    "Schedule",
    "build_schedule",
    "concessional_loan_perquisite",
    "recompute_after_prepayment",
]

ZERO = Decimal(0)
RUPEE = Decimal("0.01")
MONTHS_IN_YEAR = Decimal(12)
PERCENT = Decimal(100)


class AmortisationError(ValueError):
    """A schedule cannot be built from what was supplied."""


class PrepaymentMode:
    """What a part-prepayment does to the remaining schedule.

    Both are legitimate and employees ask for both: reducing the EMI frees up
    monthly cash, reducing the tenure saves interest. Assuming one is how a
    product ends up arguing with a borrower about what they asked for.
    """

    REDUCE_EMI = "reduce_emi"
    REDUCE_TENURE = "reduce_tenure"


def _money(value: Decimal) -> Decimal:
    return value.quantize(RUPEE, rounding=ROUND_HALF_UP)


def _check(name: str, value: Decimal) -> Decimal:
    if not isinstance(value, Decimal):
        raise AmortisationError(f"{name} must be a Decimal, not a float")
    if value < ZERO:
        raise AmortisationError(f"{name} cannot be negative")
    return value


@dataclass(frozen=True, slots=True)
class Instalment:
    number: int
    emi: Decimal
    principal: Decimal
    interest: Decimal
    closing_balance: Decimal
    # True for a month inside a moratorium: no principal is recovered, and
    # depending on configuration interest may or may not accrue.
    is_moratorium: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "emi": str(self.emi),
            "principal": str(self.principal),
            "interest": str(self.interest),
            "closing_balance": str(self.closing_balance),
            "is_moratorium": self.is_moratorium,
        }


@dataclass(frozen=True, slots=True)
class Schedule:
    principal: Decimal
    annual_rate: Decimal
    tenure_months: int
    emi: Decimal
    instalments: list[Instalment] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total_principal(self) -> Decimal:
        return sum((i.principal for i in self.instalments), start=ZERO)

    @property
    def total_interest(self) -> Decimal:
        return sum((i.interest for i in self.instalments), start=ZERO)

    @property
    def reconciles(self) -> bool:
        """The invariant, exposed so a caller can assert it too."""
        return self.total_principal == self.principal

    def as_dict(self) -> dict[str, Any]:
        return {
            "principal": str(self.principal),
            "annual_rate": str(self.annual_rate),
            "tenure_months": self.tenure_months,
            "emi": str(self.emi),
            "total_principal": str(self.total_principal),
            "total_interest": str(self.total_interest),
            "reconciles": self.reconciles,
            "instalments": [i.as_dict() for i in self.instalments],
            "notes": self.notes,
        }


def _flat_emi(principal: Decimal, tenure: int) -> Decimal:
    return _money(principal / Decimal(tenure))


def _reducing_emi(principal: Decimal, annual_rate: Decimal, tenure: int) -> Decimal:
    """The standard reducing-balance EMI.

    Where the rate is zero the formula divides by zero, so that case is the
    flat one — which is also what an interest-free loan actually is.
    """
    if annual_rate == ZERO:
        return _flat_emi(principal, tenure)

    monthly = annual_rate / PERCENT / MONTHS_IN_YEAR
    factor = (Decimal(1) + monthly) ** tenure
    return _money(principal * monthly * factor / (factor - Decimal(1)))


def build_schedule(
    *,
    principal: Decimal,
    annual_rate: Decimal,
    tenure_months: int,
    interest_type: str = "reducing",
    moratorium_months: int = 0,
    accrue_interest_during_moratorium: bool = True,
) -> Schedule:
    """The amortisation ledger.

    Args:
        interest_type: `reducing`, `simple`, or `interest_free`.
        moratorium_months: months at the start with no principal recovery.
            Whether interest still accrues is configuration — both
            arrangements exist, and assuming one silently changes what the
            borrower owes.
    """
    principal = _check("Principal", principal)
    annual_rate = _check("The interest rate", annual_rate)
    if principal == ZERO:
        raise AmortisationError("A loan of zero has no schedule.")
    if tenure_months <= 0:
        raise AmortisationError("The tenure must be at least one month.")
    if moratorium_months < 0:
        raise AmortisationError("A moratorium cannot be negative.")
    if moratorium_months >= tenure_months:
        raise AmortisationError(
            f"A moratorium of {moratorium_months} months covers the whole "
            f"{tenure_months}-month tenure, so nothing would ever be recovered."
        )

    notes: list[str] = []
    if interest_type == "interest_free":
        annual_rate = ZERO
        notes.append(
            "Interest free. Above the configured threshold this is a taxable "
            "perquisite — see `concessional_loan_perquisite`."
        )

    repaying_months = tenure_months - moratorium_months
    instalments: list[Instalment] = []
    balance = principal
    monthly_rate = annual_rate / PERCENT / MONTHS_IN_YEAR

    # ── The moratorium ──────────────────────────────────────────────────────
    for number in range(1, moratorium_months + 1):
        interest = _money(balance * monthly_rate) if accrue_interest_during_moratorium else ZERO
        instalments.append(
            Instalment(
                number=number,
                emi=interest,
                principal=ZERO,
                interest=interest,
                closing_balance=balance,
                is_moratorium=True,
            )
        )
    if moratorium_months:
        notes.append(
            f"{moratorium_months} month(s) of moratorium: no principal is recovered, "
            + (
                "and interest is serviced monthly."
                if accrue_interest_during_moratorium
                else "and no interest accrues."
            )
        )

    # ── The repayment months ────────────────────────────────────────────────
    if interest_type == "simple":
        # Interest computed once on the full principal and spread evenly, which
        # is what "simple" means here and is materially more expensive than
        # reducing balance for the same headline rate.
        total_interest = _money(
            principal * annual_rate / PERCENT * Decimal(repaying_months) / MONTHS_IN_YEAR
        )
        per_month_interest = _money(total_interest / Decimal(repaying_months))
        emi = _money((principal + total_interest) / Decimal(repaying_months))
        notes.append(
            "Simple interest: computed once on the full principal, so the effective "
            "cost is higher than a reducing-balance loan at the same headline rate."
        )
    else:
        per_month_interest = None
        emi = _reducing_emi(principal, annual_rate, repaying_months)

    for index in range(1, repaying_months + 1):
        number = moratorium_months + index
        is_last = index == repaying_months

        if interest_type == "simple":
            interest = per_month_interest or ZERO
        else:
            interest = _money(balance * monthly_rate)

        if is_last:
            # The final instalment is whatever is left, which is what makes the
            # ledger reconcile exactly. Rounding sixty EMIs independently
            # otherwise leaves the borrower owing a few rupees forever.
            principal_part = balance
            instalment_amount = _money(principal_part + interest)
        else:
            principal_part = _money(emi - interest)
            if principal_part > balance:
                principal_part = balance
            instalment_amount = emi

        balance = _money(balance - principal_part)
        instalments.append(
            Instalment(
                number=number,
                emi=instalment_amount,
                principal=principal_part,
                interest=interest,
                closing_balance=balance,
            )
        )

    schedule = Schedule(
        principal=principal,
        annual_rate=annual_rate,
        tenure_months=tenure_months,
        emi=emi,
        instalments=instalments,
        notes=notes,
    )

    if not schedule.reconciles:
        # Unreachable by construction — the final instalment absorbs the
        # remainder — but a ledger that silently fails to reconcile is worse
        # than one that refuses to exist.
        raise AmortisationError(
            f"The schedule does not reconcile: {schedule.total_principal} of principal "
            f"across the instalments against a loan of {principal}."
        )
    return schedule


def recompute_after_prepayment(
    *,
    outstanding_principal: Decimal,
    annual_rate: Decimal,
    remaining_months: int,
    prepayment: Decimal,
    mode: str,
    current_emi: Decimal,
    interest_type: str = "reducing",
) -> Schedule:
    """Rebuild the schedule after a part-prepayment.

    `mode` decides what the borrower actually asked for:

    - REDUCE_EMI keeps the tenure and lowers the monthly instalment, freeing up
      cash now.
    - REDUCE_TENURE keeps the instalment and shortens the loan, saving interest.

    Both are ordinary requests. Picking one for the borrower is how a product
    ends up arguing about what they asked for.
    """
    outstanding_principal = _check("The outstanding principal", outstanding_principal)
    prepayment = _check("The prepayment", prepayment)

    if prepayment <= ZERO:
        raise AmortisationError("A prepayment of zero changes nothing.")
    if prepayment > outstanding_principal:
        raise AmortisationError(
            f"The prepayment of {prepayment} exceeds the outstanding principal of "
            f"{outstanding_principal}. To close the loan, pay exactly the outstanding "
            "balance."
        )
    if remaining_months <= 0:
        raise AmortisationError("There are no months left to reschedule.")

    reduced = _money(outstanding_principal - prepayment)
    if reduced == ZERO:
        raise AmortisationError(
            "The prepayment clears the loan. Close the account rather than "
            "rescheduling it."
        )

    if mode == PrepaymentMode.REDUCE_EMI:
        schedule = build_schedule(
            principal=reduced,
            annual_rate=annual_rate,
            tenure_months=remaining_months,
            interest_type=interest_type,
        )
        note = (
            f"Prepayment of {prepayment} applied. The tenure stays at "
            f"{remaining_months} month(s) and the instalment falls from "
            f"{current_emi} to {schedule.emi}."
        )
    elif mode == PrepaymentMode.REDUCE_TENURE:
        months = _months_at_emi(reduced, annual_rate, current_emi)
        schedule = build_schedule(
            principal=reduced,
            annual_rate=annual_rate,
            tenure_months=months,
            interest_type=interest_type,
        )
        note = (
            f"Prepayment of {prepayment} applied. The instalment stays near "
            f"{current_emi} and the tenure falls from {remaining_months} to "
            f"{months} month(s)."
        )
    else:
        raise AmortisationError(
            f"'{mode}' is not a prepayment mode. Use "
            f"'{PrepaymentMode.REDUCE_EMI}' or '{PrepaymentMode.REDUCE_TENURE}'."
        )

    return Schedule(
        principal=schedule.principal,
        annual_rate=schedule.annual_rate,
        tenure_months=schedule.tenure_months,
        emi=schedule.emi,
        instalments=schedule.instalments,
        notes=[note, *schedule.notes],
    )


def _months_at_emi(principal: Decimal, annual_rate: Decimal, emi: Decimal) -> int:
    """How many months a given EMI takes to clear a balance.

    Iterative rather than logarithmic: the log form is exact for a continuous
    rate but disagrees with the rounded month-by-month arithmetic the ledger
    actually uses, and the ledger is what the borrower sees.
    """
    if emi <= ZERO:
        raise AmortisationError("The instalment must be greater than zero.")

    monthly = annual_rate / PERCENT / MONTHS_IN_YEAR
    first_interest = _money(principal * monthly)
    if emi <= first_interest:
        raise AmortisationError(
            f"An instalment of {emi} does not cover the first month's interest of "
            f"{first_interest}, so the loan would never be repaid."
        )

    balance = principal
    months = 0
    while balance > ZERO and months < 1200:
        interest = _money(balance * monthly)
        balance = _money(balance - (emi - interest))
        months += 1
    return max(1, months)


# ═══════════════════════════════════════════════════════════════════════════
# The concessional-loan perquisite
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class PerquisiteResult:
    is_perquisite: bool
    notional_interest: Decimal
    actual_interest: Decimal
    perquisite_value: Decimal
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "is_perquisite": self.is_perquisite,
            "notional_interest": str(self.notional_interest),
            "actual_interest": str(self.actual_interest),
            "perquisite_value": str(self.perquisite_value),
            "reason": self.reason,
        }


def concessional_loan_perquisite(
    *,
    outstanding_by_month: list[Decimal],
    actual_interest_charged: Decimal,
    config: dict[str, Any],
) -> PerquisiteResult:
    """The taxable value of a loan given below the notional rate.

    An interest-free or cheap loan from an employer is a benefit, and above a
    threshold it is taxed as one. The value is the interest that *would* have
    been charged at the notional rate, less what actually was.

    The notional rate and the threshold are configuration — both change, and a
    change should not be a release. `outstanding_by_month` is the balance on
    which the notional interest accrues, month by month, because a loan being
    repaid carries less benefit than one that is not.

    Small loans are exempt regardless of rate, which is why the threshold is
    checked against the *maximum* outstanding rather than the average: a loan
    that was once large was once a benefit.
    """
    if "notional_rate" not in config:
        raise AmortisationError(
            "The loan rule set is missing 'notional_rate'. The rate a concessional "
            "loan is valued against is not assumed — the Compliance SME supplies it."
        )
    if "exempt_threshold" not in config:
        raise AmortisationError(
            "The loan rule set is missing 'exempt_threshold'. Small loans are exempt "
            "regardless of rate, and the boundary is configuration."
        )

    notional_rate = Decimal(str(config["notional_rate"]))
    threshold = Decimal(str(config["exempt_threshold"]))
    actual_interest = _check("The interest actually charged", actual_interest_charged)

    if not outstanding_by_month:
        raise AmortisationError(
            "No monthly balances were supplied, so there is nothing to value."
        )
    for balance in outstanding_by_month:
        _check("A monthly outstanding balance", balance)

    highest = max(outstanding_by_month)
    if highest <= threshold:
        return PerquisiteResult(
            is_perquisite=False,
            notional_interest=ZERO,
            actual_interest=actual_interest,
            perquisite_value=ZERO,
            reason=(
                f"The largest outstanding balance of {highest} is within the exempt "
                f"threshold of {threshold}, so no perquisite arises."
            ),
        )

    monthly_rate = notional_rate / PERCENT / MONTHS_IN_YEAR
    notional = _money(
        sum((balance * monthly_rate for balance in outstanding_by_month), start=ZERO)
    )
    value = notional - actual_interest

    if value <= ZERO:
        return PerquisiteResult(
            is_perquisite=False,
            notional_interest=notional,
            actual_interest=actual_interest,
            perquisite_value=ZERO,
            reason=(
                f"The interest charged ({actual_interest}) is at or above the notional "
                f"{notional_rate}% ({notional}), so the loan is not concessional."
            ),
        )

    return PerquisiteResult(
        is_perquisite=True,
        notional_interest=notional,
        actual_interest=actual_interest,
        perquisite_value=_money(value),
        reason=(
            f"Interest of {notional} would have been charged at the notional "
            f"{notional_rate}% against {actual_interest} actually charged. The "
            f"difference is a taxable perquisite and is added to salary income."
        ),
    )
