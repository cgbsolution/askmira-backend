"""Expense policy, evaluated at capture rather than at approval.

═══════════════════════════════════════════════════════════════════════════════
THREE WEEKS LATER IS TOO LATE
═══════════════════════════════════════════════════════════════════════════════

An employee submits a hotel bill, waits three weeks, and is told it was ₹1,200
over the tier-B limit. They did not know there was a tier-B limit. They now have
a rejected claim, a bill they paid for, and a lasting belief that the expense
process is a lottery — so they stop itemising, start rounding, and the next
genuine claim gets less care than the one before.

So `evaluate` runs the moment the amount and category are extracted, and returns
the limit, the overage and what is required to proceed. The employee finds out
while they still have the choice to book something cheaper.

An invariant checks the evaluation is called from the capture path, not only
from the submission path.

═══════════════════════════════════════════════════════════════════════════════
THREE VIOLATION ACTIONS, AND ONLY ONE OF THEM BLOCKS
═══════════════════════════════════════════════════════════════════════════════

BLOCK is for the rule nobody may break — a category the company does not
reimburse at all. FLAG and REQUIRE_JUSTIFICATION exist because most policy
overages have a reason, and a system that only blocks teaches people to
mis-categorise. The employee who books an expensive hotel because it is next to
the client site is not committing fraud; they are doing their job, and the policy
should ask them to say so rather than refuse.

Pure. `today` is always supplied.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "CityTier",
    "ClaimFacts",
    "ExpenseRule",
    "LimitType",
    "PolicyError",
    "PolicyVerdict",
    "PriorClaim",
    "VerdictKind",
    "ViolationAction",
    "ViolationRate",
    "aggregate_window",
    "evaluate",
    "money",
]


class PolicyError(ValueError):
    """A policy this module will not evaluate."""


def money(value: Decimal | int | float | str) -> Decimal:
    """Two decimal places, half-up. Every amount in this package goes through it.

    Not for tidiness: a per-day limit compared against a sum of claims rounded
    differently at each step drifts, and the employee whose fourth claim of the
    day tips over by eleven paise is being told their honest expense is out of
    policy by a rounding artefact.
    """
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class CityTier(str, enum.Enum):
    """Indian travel policies are almost universally tier-based.

    The tiers are the company's, not the government's — a firm's "tier A" is
    wherever its expensive offices are, and hard-coding the Census
    classification would produce a policy nobody recognises.
    """

    A = "a"
    B = "b"
    C = "c"
    INTERNATIONAL = "international"


class LimitType(str, enum.Enum):
    """What the limit is measured over.

    The aggregating ones are the interesting half. A per-claim limit is checked
    against one number; a per-trip limit has to look at every other claim on the
    trip, which is where a policy engine either works or quietly does not.
    """

    PER_CLAIM = "per_claim"
    PER_DAY = "per_day"
    PER_MONTH = "per_month"
    PER_TRIP = "per_trip"

    @property
    def aggregates(self) -> bool:
        return self is not LimitType.PER_CLAIM


class ViolationAction(str, enum.Enum):
    """What happens when a claim exceeds its limit."""

    #: Cannot be submitted at all. For a category the company does not
    #: reimburse — not for an overage, because an overage usually has a reason.
    BLOCK = "block"
    #: Submits, with a visible marker for the approver.
    FLAG = "flag"
    #: Submits only with a written reason, shown to the approver.
    REQUIRE_JUSTIFICATION = "require_justification"

    @property
    def permits_submission(self) -> bool:
        return self is not ViolationAction.BLOCK


@dataclass(frozen=True, slots=True)
class ExpenseRule:
    """One limit, for one category, in one city tier."""

    category_key: str
    city_tier: CityTier
    limit_type: LimitType
    limit_amount: Decimal
    violation_action: ViolationAction
    requires_receipt: bool = True
    #: Below this, no receipt is needed. A ₹40 auto fare with a demand for a
    #: receipt is a policy that produces fake receipts.
    receipt_threshold: Decimal = Decimal("0")
    requires_pre_approval: bool = False
    #: Per-diem and similar. A per-diem claim needs no receipt by definition,
    #: and demanding one is asking for a document that does not exist.
    is_per_diem: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "category_key": self.category_key,
            "city_tier": self.city_tier.value,
            "limit_type": self.limit_type.value,
            "limit_amount": str(money(self.limit_amount)),
            "violation_action": self.violation_action.value,
            "requires_receipt": self.requires_receipt,
            "receipt_threshold": str(money(self.receipt_threshold)),
            "requires_pre_approval": self.requires_pre_approval,
            "is_per_diem": self.is_per_diem,
        }


@dataclass(frozen=True, slots=True)
class ClaimFacts:
    """The claim being evaluated, as it stands at capture."""

    category_key: str
    city_tier: CityTier
    amount: Decimal
    incurred_on: date
    has_receipt: bool = False
    justification: str = ""
    trip_id: str | None = None
    pre_approved: bool = False


@dataclass(frozen=True, slots=True)
class PriorClaim:
    """Another claim that counts toward an aggregating limit."""

    category_key: str
    amount: Decimal
    incurred_on: date
    trip_id: str | None = None


def aggregate_window(
    limit_type: LimitType,
    claim: ClaimFacts,
    priors: list[PriorClaim],
) -> Decimal:
    """What has already been claimed against this limit, before this claim.

    The per-trip case is the one that goes wrong. Matching on the trip id and
    nothing else is right — a per-trip hotel limit covers every night of the
    trip, and filtering by date as well would silently make it a per-day limit
    that happens to be named per-trip.
    """
    if limit_type is LimitType.PER_CLAIM:
        return Decimal("0")

    same_category = [p for p in priors if p.category_key == claim.category_key]

    if limit_type is LimitType.PER_DAY:
        relevant = [p for p in same_category if p.incurred_on == claim.incurred_on]
    elif limit_type is LimitType.PER_MONTH:
        relevant = [
            p
            for p in same_category
            if (p.incurred_on.year, p.incurred_on.month)
            == (claim.incurred_on.year, claim.incurred_on.month)
        ]
    else:  # PER_TRIP
        if claim.trip_id is None:
            raise PolicyError(
                f"A per-trip limit applies to {claim.category_key!r} and this "
                "claim is not attached to a trip. Evaluating it against nothing "
                "would let every claim through — the claim needs a trip or the "
                "rule needs a different limit type."
            )
        relevant = [p for p in same_category if p.trip_id == claim.trip_id]

    return money(sum((p.amount for p in relevant), Decimal("0")))


class VerdictKind(str, enum.Enum):
    """The outcome, from the employee's point of view."""

    WITHIN_POLICY = "within_policy"
    #: Over the limit, and the policy asks for something before submission.
    NEEDS_JUSTIFICATION = "needs_justification"
    #: Over the limit; submits with a marker for the approver.
    FLAGGED = "flagged"
    BLOCKED = "blocked"
    #: A receipt is required and absent.
    RECEIPT_REQUIRED = "receipt_required"
    #: Pre-approval was required and not obtained.
    PRE_APPROVAL_REQUIRED = "pre_approval_required"
    #: No rule covers this category and tier. Allowed, and reported — a policy
    #: with a gap should be visible rather than silently permissive.
    NO_RULE = "no_rule"

    @property
    def permits_submission(self) -> bool:
        return self not in (
            VerdictKind.BLOCKED,
            VerdictKind.RECEIPT_REQUIRED,
            VerdictKind.PRE_APPROVAL_REQUIRED,
            VerdictKind.NEEDS_JUSTIFICATION,
        )


@dataclass(frozen=True, slots=True)
class PolicyVerdict:
    """What the employee is told, at capture, in numbers they can act on."""

    kind: VerdictKind
    rule: ExpenseRule | None
    limit_amount: Decimal | None
    already_claimed: Decimal
    this_claim: Decimal
    overage: Decimal
    message: str

    @property
    def permits_submission(self) -> bool:
        return self.kind.permits_submission

    @property
    def is_violation(self) -> bool:
        return self.kind not in (VerdictKind.WITHIN_POLICY, VerdictKind.NO_RULE)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "permits_submission": self.permits_submission,
            "is_violation": self.is_violation,
            "limit_amount": (
                str(money(self.limit_amount))
                if self.limit_amount is not None
                else None
            ),
            "already_claimed": str(money(self.already_claimed)),
            "this_claim": str(money(self.this_claim)),
            "overage": str(money(self.overage)),
            "message": self.message,
            "rule": self.rule.as_dict() if self.rule else None,
        }


def _match_rule(
    rules: list[ExpenseRule], claim: ClaimFacts
) -> ExpenseRule | None:
    """The rule for this category and tier, if there is one.

    Exact match on both. No fallback to a "default" tier: a policy that silently
    applies the tier-A limit in a tier-C city is a policy nobody wrote, and the
    employee cannot tell which limit they are being held to.
    """
    for rule in rules:
        if rule.category_key == claim.category_key and rule.city_tier is claim.city_tier:
            return rule
    return None


def evaluate(
    claim: ClaimFacts,
    *,
    rules: list[ExpenseRule],
    priors: list[PriorClaim],
) -> PolicyVerdict:
    """Judge one claim, at capture, and say what to do about it.

    Order matters. Pre-approval is checked before the limit, because a trip that
    needed sign-off and did not get it is a different conversation from a hotel
    that was ₹800 over — and telling somebody about the overage first sends them
    to find a justification for a claim that was never going to be accepted.
    """
    amount = money(claim.amount)
    rule = _match_rule(rules, claim)

    if rule is None:
        return PolicyVerdict(
            kind=VerdictKind.NO_RULE,
            rule=None,
            limit_amount=None,
            already_claimed=Decimal("0"),
            this_claim=amount,
            overage=Decimal("0"),
            message=(
                f"No policy rule covers {claim.category_key!r} in a tier "
                f"{claim.city_tier.value.upper()} location, so nothing limits "
                "this claim. That is a gap in the policy rather than a "
                "permission — your finance team may want to know."
            ),
        )

    if rule.requires_pre_approval and not claim.pre_approved:
        return PolicyVerdict(
            kind=VerdictKind.PRE_APPROVAL_REQUIRED,
            rule=rule,
            limit_amount=money(rule.limit_amount),
            already_claimed=Decimal("0"),
            this_claim=amount,
            overage=Decimal("0"),
            message=(
                f"{claim.category_key} needs pre-approval before it is incurred. "
                "This claim has none, so it cannot be submitted — raise the "
                "request and attach it."
            ),
        )

    # A per-diem needs no receipt by definition. Demanding one is asking for a
    # document that does not exist, and the employee will produce something.
    if (
        rule.requires_receipt
        and not rule.is_per_diem
        and not claim.has_receipt
        and amount > money(rule.receipt_threshold)
    ):
        return PolicyVerdict(
            kind=VerdictKind.RECEIPT_REQUIRED,
            rule=rule,
            limit_amount=money(rule.limit_amount),
            already_claimed=Decimal("0"),
            this_claim=amount,
            overage=Decimal("0"),
            message=(
                f"A receipt is required above ₹{money(rule.receipt_threshold)} "
                f"for {claim.category_key}. This claim is ₹{amount}."
            ),
        )

    already = aggregate_window(rule.limit_type, claim, priors)
    total = money(already + amount)
    limit = money(rule.limit_amount)

    if total <= limit:
        return PolicyVerdict(
            kind=VerdictKind.WITHIN_POLICY,
            rule=rule,
            limit_amount=limit,
            already_claimed=already,
            this_claim=amount,
            overage=Decimal("0"),
            message=(
                f"Within policy. The {_window_words(rule.limit_type)} limit is "
                f"₹{limit}"
                + (f", of which ₹{already} is already claimed." if already else ".")
            ),
        )

    overage = money(total - limit)
    window = _window_words(rule.limit_type)
    context = (
        f"₹{already} is already claimed against this {window} limit of ₹{limit}, "
        f"and this claim of ₹{amount} takes the total to ₹{total} — ₹{overage} "
        "over."
        if already
        else (
            f"This claim of ₹{amount} is ₹{overage} over the {window} limit of "
            f"₹{limit}."
        )
    )

    if rule.violation_action is ViolationAction.BLOCK:
        return PolicyVerdict(
            kind=VerdictKind.BLOCKED,
            rule=rule,
            limit_amount=limit,
            already_claimed=already,
            this_claim=amount,
            overage=overage,
            message=(
                f"{context} This category cannot be claimed above its limit, so "
                "the claim cannot be submitted. If there is a reason, your "
                "finance team can raise it as an exception."
            ),
        )

    if rule.violation_action is ViolationAction.REQUIRE_JUSTIFICATION:
        if claim.justification.strip():
            return PolicyVerdict(
                kind=VerdictKind.FLAGGED,
                rule=rule,
                limit_amount=limit,
                already_claimed=already,
                this_claim=amount,
                overage=overage,
                message=(
                    f"{context} Your reason will be shown to the approver with "
                    "the claim."
                ),
            )
        return PolicyVerdict(
            kind=VerdictKind.NEEDS_JUSTIFICATION,
            rule=rule,
            limit_amount=limit,
            already_claimed=already,
            this_claim=amount,
            overage=overage,
            message=(
                f"{context} Add a short reason and this can be submitted — most "
                "overages have one, and the approver would rather read it than "
                "guess."
            ),
        )

    return PolicyVerdict(
        kind=VerdictKind.FLAGGED,
        rule=rule,
        limit_amount=limit,
        already_claimed=already,
        this_claim=amount,
        overage=overage,
        message=f"{context} It will be submitted with a flag for the approver.",
    )


def _window_words(limit_type: LimitType) -> str:
    return {
        LimitType.PER_CLAIM: "per-claim",
        LimitType.PER_DAY: "daily",
        LimitType.PER_MONTH: "monthly",
        LimitType.PER_TRIP: "per-trip",
    }[limit_type]


# ══════════════════════════════════════════════════════════════════════════════
# Violation rates
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ViolationRate:
    """How often one group breaches policy, and what that probably means.

    The interpretation is the point. A department at 60% is not sixty per cent
    dishonest — it is a department whose limits do not match what their work
    costs, and the fix is the policy. One person at 60% in a department at 5% is
    a different conversation. The data distinguishes them and the summary says
    which reading it supports.
    """

    scope: str
    key: str
    total_claims: int
    violations: int

    @property
    def rate(self) -> float | None:
        """`None` below a usable sample. Two out of three is not a rate."""
        if self.total_claims < 10:
            return None
        return self.violations / self.total_claims

    def interpretation(self, *, peer_rate: float | None = None) -> str:
        rate = self.rate
        if rate is None:
            return (
                f"{self.violations} of {self.total_claims} claims breached "
                "policy — too few to read as a rate."
            )
        if rate < 0.1:
            return f"{rate:.0%} of claims breach policy. Normal."
        if peer_rate is not None and peer_rate >= 0.3:
            return (
                f"{rate:.0%} of claims breach policy, against {peer_rate:.0%} "
                "across the comparison group. When most people breach a limit, "
                "the limit is usually the thing that is wrong."
            )
        if rate >= 0.3:
            return (
                f"{rate:.0%} of claims breach policy. That is high enough to be "
                "either a limit that does not match what this work costs or a "
                "pattern worth a conversation — look at which categories before "
                "deciding which."
            )
        return f"{rate:.0%} of claims breach policy. Worth watching."

    def as_dict(self, *, peer_rate: float | None = None) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "key": self.key,
            "total_claims": self.total_claims,
            "violations": self.violations,
            "rate": round(self.rate, 3) if self.rate is not None else None,
            "interpretation": self.interpretation(peer_rate=peer_rate),
        }
