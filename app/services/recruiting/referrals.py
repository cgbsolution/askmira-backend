"""The referral programme: eligibility, duplicates, and milestone bonuses.

═══════════════════════════════════════════════════════════════════════════════
THE DUPLICATE RULE HAS TO BE VISIBLE BEFORE ANYBODY REFERS
═══════════════════════════════════════════════════════════════════════════════

Two people refer the same candidate; the candidate is hired; both expect the
bonus. Whatever the rule is, the only version that does not produce a grievance
is the one both of them read *before* they referred — so `programme_terms`
returns the rule in the same payload as the share link, and `resolve_duplicate`
states it again in the refusal.

First submission wins, by timestamp. Not "the one whose candidate was better
prepared", not "split it" — a tie-break that requires judgement produces an
argument, and an argument about somebody else's bonus is worse than losing it.

═══════════════════════════════════════════════════════════════════════════════
FRICTIONLESS OR NOT AT ALL
═══════════════════════════════════════════════════════════════════════════════

A referral flow that requires uploading a CV to a portal gets used once. So the
unit of referral here is a **tracked share link** an employee sends to WhatsApp
or LinkedIn, and the candidate applies through it. The employee does not handle
a CV at all.

═══════════════════════════════════════════════════════════════════════════════
AND THE BONUS PAYS IN MILESTONES, FOR A REASON
═══════════════════════════════════════════════════════════════════════════════

Paying the whole bonus on joining rewards a referral that leaves in six weeks
exactly as much as one that stays five years. Splitting it — commonly half on
joining and half at ninety days — costs the company nothing extra and aligns the
referrer with the thing the programme is actually for.

Pure. Dates and amounts are supplied.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "BonusMilestone",
    "EligibilityCheck",
    "ReferralError",
    "ReferralFacts",
    "ReferralStatus",
    "check_eligibility",
    "payout_schedule",
    "programme_terms",
    "resolve_duplicate",
    "share_link",
]


class ReferralError(ValueError):
    """A referral operation this module refuses."""


def money(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class ReferralStatus(str, enum.Enum):
    SUBMITTED = "submitted"
    #: Another employee referred the same candidate first.
    DUPLICATE = "duplicate"
    IN_PROCESS = "in_process"
    HIRED = "hired"
    NOT_SELECTED = "not_selected"
    #: The referrer stopped being eligible before the payout - they left, or
    #: went onto a plan. Distinct from the candidate not being selected.
    INELIGIBLE = "ineligible"


class IneligibilityReason(str, enum.Enum):
    NONE = "none"
    NOT_CONFIRMED = "not_confirmed"
    ON_PIP = "on_pip"
    IN_HIRING_TEAM = "in_hiring_team"
    ROLE_NOT_ELIGIBLE = "role_not_eligible"
    SELF_REFERRAL = "self_referral"


@dataclass(frozen=True, slots=True)
class BonusMilestone:
    """One slice of the bonus, and when it becomes payable."""

    key: str
    label: str
    #: Percentage of the total bonus. The set must sum to 100.
    share_percent: Decimal
    #: Days after joining. Zero means on joining.
    days_after_joining: int

    def __post_init__(self) -> None:
        if self.share_percent <= 0:
            raise ReferralError(
                f"Milestone {self.key!r} pays nothing. A milestone worth zero is "
                "a date somebody is waiting for with no reason."
            )
        if self.days_after_joining < 0:
            raise ReferralError(
                f"Milestone {self.key!r} pays before the candidate joins."
            )


#: Half on joining and half at ninety days. Paying the whole bonus on joining
#: rewards a referral that leaves in six weeks exactly as much as one that
#: stays five years.
DEFAULT_MILESTONES: tuple[BonusMilestone, ...] = (
    BonusMilestone("on_joining", "On joining", Decimal("50"), 0),
    BonusMilestone("at_90_days", "At ninety days", Decimal("50"), 90),
)


@dataclass(frozen=True, slots=True)
class ReferrerFacts:
    """What decides whether somebody may refer."""

    employee_id: str
    is_confirmed: bool
    is_on_pip: bool
    is_active: bool
    hiring_team_requisitions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EligibilityCheck:
    """Whether this person may refer for this role, and why not."""

    eligible: bool
    reason: IneligibilityReason
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "reason": self.reason.value,
            "message": self.message,
        }


def check_eligibility(
    referrer: ReferrerFacts,
    *,
    requisition_id: str,
    eligible_roles: tuple[str, ...] = (),
    role_key: str = "",
    candidate_employee_id: str = "",
) -> EligibilityCheck:
    """Whether this employee may refer for this requisition.

    Being on the hiring team for the role is the one that matters. A panel
    member who refers into their own requisition is deciding on a candidate they
    are paid for, and however honest they are it is not a position to put
    somebody in.
    """
    if candidate_employee_id and candidate_employee_id == referrer.employee_id:
        return EligibilityCheck(
            False,
            IneligibilityReason.SELF_REFERRAL,
            "Somebody cannot refer themselves.",
        )
    if not referrer.is_active:
        return EligibilityCheck(
            False,
            IneligibilityReason.NOT_CONFIRMED,
            "Referrals are for current employees. A leaver's pending bonus is "
            "settled through their full and final, not through this programme.",
        )
    if not referrer.is_confirmed:
        return EligibilityCheck(
            False,
            IneligibilityReason.NOT_CONFIRMED,
            "The referrer is still on probation. Most programmes require "
            "confirmation first, and this one is configured that way.",
        )
    if referrer.is_on_pip:
        return EligibilityCheck(
            False,
            IneligibilityReason.ON_PIP,
            "The referrer is on a performance improvement plan. That is a "
            "configured rule rather than a judgement about their referrals - "
            "and it is worth saying so, because they will ask.",
        )
    if requisition_id in referrer.hiring_team_requisitions:
        return EligibilityCheck(
            False,
            IneligibilityReason.IN_HIRING_TEAM,
            "The referrer is on the hiring team for this role. Deciding on a "
            "candidate you are paid for is not a position to put somebody in, "
            "however honest they are.",
        )
    if eligible_roles and role_key not in eligible_roles:
        return EligibilityCheck(
            False,
            IneligibilityReason.ROLE_NOT_ELIGIBLE,
            f"This programme covers {sorted(eligible_roles)} and this role is "
            f"{role_key!r}.",
        )
    return EligibilityCheck(
        True, IneligibilityReason.NONE, "Eligible to refer for this role."
    )


def programme_terms(
    *,
    name: str,
    bonus_by_grade: dict[str, Decimal],
    milestones: tuple[BonusMilestone, ...] = DEFAULT_MILESTONES,
    eligible_roles: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The terms, in the shape an employee reads before they refer.

    The duplicate rule is in here rather than in a policy document, because the
    argument it prevents happens between two people who both believed they were
    entitled.
    """
    total = sum((m.share_percent for m in milestones), Decimal("0"))
    if total != Decimal("100"):
        raise ReferralError(
            f"The milestones pay {total}% of the bonus rather than 100%. An "
            "employee who is paid 80% of what they were promised will notice, "
            "and one paid 120% is a reconciliation nobody spots."
        )

    return {
        "name": name,
        "bonus_by_grade": {k: str(money(v)) for k, v in sorted(bonus_by_grade.items())},
        "milestones": [
            {
                "key": m.key,
                "label": m.label,
                "share_percent": str(m.share_percent),
                "days_after_joining": m.days_after_joining,
            }
            for m in milestones
        ],
        "eligible_roles": list(eligible_roles) or "all open roles",
        "duplicate_rule": (
            "If two people refer the same candidate, the **first submission "
            "wins** by timestamp. This is stated here rather than in a policy "
            "document because the argument it prevents happens between two "
            "colleagues who both believed they were entitled."
        ),
        "eligibility_rules": [
            "You must be confirmed, not on a performance improvement plan, and "
            "not on the hiring team for the role.",
            "You cannot refer yourself.",
        ],
        "tax_note": (
            "A referral bonus is salary income and is paid through payroll with "
            "tax deducted. The amounts above are gross."
        ),
    }


def share_link(
    *, base_url: str, requisition_id: str, referrer_id: str, channel: str
) -> dict[str, Any]:
    """A tracked link for an employee to send to WhatsApp or LinkedIn.

    The employee never handles a CV. A referral flow that requires uploading one
    to a portal gets used once, and the whole value of the channel is that it is
    two taps from a conversation somebody was already having.
    """
    if channel not in {"whatsapp", "linkedin", "email", "copy"}:
        raise ReferralError(
            f"{channel!r} is not a share channel. Tracking an untracked channel "
            "produces referrals nobody can attribute, and an unattributed "
            "referral is an unpaid bonus."
        )
    token = f"{requisition_id}.{referrer_id}"
    return {
        "channel": channel,
        "url": f"{base_url.rstrip('/')}/apply/{requisition_id}?ref={token}",
        "referrer_id": referrer_id,
        "requisition_id": requisition_id,
        "note": (
            "The candidate applies through this link and the referral is "
            "attributed automatically. Nobody uploads a CV on anybody else's "
            "behalf."
        ),
    }


@dataclass(frozen=True, slots=True)
class ReferralFacts:
    """One referral, for duplicate resolution."""

    referral_id: str
    referrer_id: str
    candidate_key: str
    submitted_at: datetime
    status: ReferralStatus = ReferralStatus.SUBMITTED


def resolve_duplicate(
    existing: list[ReferralFacts], *, incoming: ReferralFacts
) -> dict[str, Any]:
    """First submission wins, by timestamp, with the rule restated.

    Not "the better-prepared candidate", not "split it". A tie-break requiring
    judgement produces an argument, and an argument about somebody else's bonus
    is worse for a programme than losing one.
    """
    same = [
        r
        for r in existing
        if r.candidate_key == incoming.candidate_key
        and r.referral_id != incoming.referral_id
        and r.status is not ReferralStatus.DUPLICATE
    ]
    if not same:
        return {
            "is_duplicate": False,
            "winner_referral_id": incoming.referral_id,
            "message": "First referral for this candidate.",
        }

    winner = min([*same, incoming], key=lambda r: (r.submitted_at, r.referral_id))
    if winner.referral_id == incoming.referral_id:
        return {
            "is_duplicate": False,
            "winner_referral_id": incoming.referral_id,
            "superseded": [r.referral_id for r in same],
            "message": (
                "This referral came in first and stands. The later one(s) are "
                "marked duplicate under the first-submission rule stated in the "
                "programme terms."
            ),
        }

    return {
        "is_duplicate": True,
        "winner_referral_id": winner.referral_id,
        "winner_referrer_id": winner.referrer_id,
        "winner_submitted_at": winner.submitted_at.isoformat(),
        "message": (
            "This candidate was already referred by a colleague on "
            f"{winner.submitted_at.date().isoformat()}. The programme's rule is "
            "that the first submission wins - it is in the terms shown before "
            "anybody refers, precisely so this is not a surprise."
        ),
    }


def payout_schedule(
    *,
    bonus_amount: Decimal,
    joined_on: date,
    milestones: tuple[BonusMilestone, ...] = DEFAULT_MILESTONES,
) -> dict[str, Any]:
    """When each slice of the bonus becomes payable, and for how much.

    The last milestone carries any rounding remainder, so the parts sum exactly
    to the whole. Splitting 10,000 three ways and paying 3,333.33 three times
    leaves a paisa nobody can account for, and it shows up in a reconciliation
    two quarters later.
    """
    if bonus_amount <= 0:
        raise ReferralError("A referral bonus of zero is not a bonus.")
    total_share = sum((m.share_percent for m in milestones), Decimal("0"))
    if total_share != Decimal("100"):
        raise ReferralError(
            f"The milestones pay {total_share}% rather than 100% of the bonus."
        )

    gross = money(bonus_amount)
    rows = []
    allocated = Decimal("0.00")
    for index, milestone in enumerate(milestones):
        last = index == len(milestones) - 1
        amount = (
            money(gross - allocated)
            if last
            else money(gross * milestone.share_percent / Decimal("100"))
        )
        allocated += amount
        rows.append(
            {
                "key": milestone.key,
                "label": milestone.label,
                "share_percent": str(milestone.share_percent),
                "amount": str(amount),
                "payable_on": date.fromordinal(
                    joined_on.toordinal() + milestone.days_after_joining
                ).isoformat(),
                "days_after_joining": milestone.days_after_joining,
            }
        )

    return {
        "bonus_amount": str(gross),
        "milestones": rows,
        "total_scheduled": str(money(allocated)),
        "reconciles": money(allocated) == gross,
        "note": (
            "Paid through payroll at each milestone, with tax deducted - a "
            "referral bonus is salary income. The final milestone carries any "
            "rounding remainder so the parts sum exactly to the whole."
        ),
    }


def retention_comparison(
    *,
    referred_tenures_days: list[int],
    other_tenures_days: list[int],
    minimum: int = 10,
) -> dict[str, Any]:
    """Whether referred hires stay longer, with the sample size attached.

    This is the number that justifies the programme's cost, which is exactly why
    it must not be produced from six people. Below the floor it returns `None`
    rather than a flattering figure somebody will put in a board pack.
    """
    if len(referred_tenures_days) < minimum or len(other_tenures_days) < minimum:
        return {
            "referred_count": len(referred_tenures_days),
            "other_count": len(other_tenures_days),
            "median_referred_days": None,
            "median_other_days": None,
            "difference_days": None,
            "note": (
                f"Fewer than {minimum} hires in one of the groups. This is the "
                "figure that justifies the programme's cost, so it is not "
                "produced from a handful of people - somebody would put it in a "
                "board pack."
            ),
        }

    import statistics as _stats

    referred = _stats.median(referred_tenures_days)
    other = _stats.median(other_tenures_days)
    return {
        "referred_count": len(referred_tenures_days),
        "other_count": len(other_tenures_days),
        "median_referred_days": round(referred, 1),
        "median_other_days": round(other, 1),
        "difference_days": round(referred - other, 1),
        "note": (
            f"Referred hires stay a median of {round(referred - other)} days "
            f"{'longer' if referred >= other else 'less'} than others, across "
            f"{len(referred_tenures_days)} and {len(other_tenures_days)} hires. "
            "A median, not a mean: one long-serving employee should not carry "
            "the argument."
        ),
    }
