"""Perquisite treatment of non-cash rewards, and what the employee is told.

═══════════════════════════════════════════════════════════════════════════════
EVERY NUMBER IN THIS FILE IS A DEFAULT AWAITING A TAX ADVISOR
═══════════════════════════════════════════════════════════════════════════════

Non-cash rewards to employees are generally a taxable perquisite in India above
applicable thresholds, and the treatment turns on facts this module does not
know: whether the item is a gift in kind, whether it is provided by the employer
or a third party, and how the tenant's advisor reads the relevant rule for their
circumstances.

So `DEFAULT_RULES` carries `reviewed=False` on every entry, `treatment_for`
refuses to compute a perquisite from an unreviewed rule unless the caller
explicitly says it is provisional, and the payload says so in words a finance
team will read. Shipping a confident number here creates a client liability, and
the client discovers it in an assessment years later.

═══════════════════════════════════════════════════════════════════════════════
AND THE EMPLOYEE HAS TO BE TOLD BEFORE THE PAYSLIP
═══════════════════════════════════════════════════════════════════════════════

A recognition programme that produces an unexplained deduction has spent money
to make somebody feel worse. `reward_statement` shows the cumulative value, the
threshold, what is taxable and what is not — while there is still time for that
to be information rather than a surprise.

Pure. The financial year and all dates are supplied.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "DEFAULT_RULES",
    "TaxError",
    "TaxTreatment",
    "TaxTreatmentRule",
    "cumulative_value",
    "financial_year",
    "reward_statement",
    "treatment_for",
]


class TaxError(ValueError):
    """A tax computation this module refuses to produce."""


def money(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class TaxTreatment(str, enum.Enum):
    """How a reward is treated.

    `GIFT_RULE` is separate from `PERQUISITE` because the two behave
    differently at the threshold: under the gift rule the treatment commonly
    applies to the **whole** value once the limit is crossed rather than only
    to the excess, and collapsing them into one enum would quietly pick one
    behaviour for both.
    """

    PERQUISITE = "perquisite"
    EXEMPT = "exempt"
    GIFT_RULE = "gift_rule"


def financial_year(on: date) -> str:
    """The Indian financial year containing a date, as `2026-27`."""
    start = on.year if on.month >= 4 else on.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


@dataclass(frozen=True, slots=True)
class TaxTreatmentRule:
    """How one reward type is treated, from one date.

    `reviewed` is the important field. It is False until a tax advisor has
    signed the row off for this tenant, and an unreviewed rule produces a
    provisional figure that says so rather than a number payroll can act on.
    """

    reward_type: str
    treatment: TaxTreatment
    exemption_threshold_per_year: Decimal
    effective_from: date
    #: What the threshold is understood to rest on. Written out so a reviewer
    #: can disagree with the reading rather than only with the number.
    basis: str = ""
    reviewed: bool = False
    reviewed_by: str = ""
    reviewed_on: date | None = None

    def __post_init__(self) -> None:
        if self.exemption_threshold_per_year < 0:
            raise TaxError("An exemption threshold cannot be negative.")
        if self.reviewed and not self.reviewed_by.strip():
            raise TaxError(
                "A rule marked reviewed has to name who reviewed it. 'Reviewed' "
                "with nobody attached is the state this flag exists to prevent."
            )


#: Defaults, every one of them unreviewed. They are a starting point for a
#: conversation with an advisor, not an answer.
DEFAULT_RULES: tuple[TaxTreatmentRule, ...] = (
    TaxTreatmentRule(
        reward_type="merchandise",
        treatment=TaxTreatment.GIFT_RULE,
        exemption_threshold_per_year=Decimal("5000"),
        effective_from=date(2020, 4, 1),
        basis=(
            "Commonly cited as the annual limit for gifts in kind from an "
            "employer, with the treatment applying to the whole value once "
            "exceeded rather than only to the excess. Whether this programme's "
            "merchandise falls under that rule, and whether the limit is per "
            "occasion or per year for this tenant, is a question for their "
            "advisor."
        ),
    ),
    TaxTreatmentRule(
        reward_type="voucher",
        treatment=TaxTreatment.GIFT_RULE,
        exemption_threshold_per_year=Decimal("5000"),
        effective_from=date(2020, 4, 1),
        basis=(
            "Treated the same as merchandise by default. Vouchers convertible "
            "to cash are frequently treated less favourably; that distinction "
            "is not modelled here because guessing it wrong is expensive."
        ),
    ),
    TaxTreatmentRule(
        reward_type="cash_equivalent",
        treatment=TaxTreatment.PERQUISITE,
        exemption_threshold_per_year=Decimal("0"),
        effective_from=date(2020, 4, 1),
        basis=(
            "Anything convertible to cash is taken as fully taxable from the "
            "first rupee. This is the conservative reading and the one least "
            "likely to create a liability for the client."
        ),
    ),
    TaxTreatmentRule(
        reward_type="long_service_award",
        treatment=TaxTreatment.PERQUISITE,
        exemption_threshold_per_year=Decimal("0"),
        effective_from=date(2020, 4, 1),
        basis=(
            "Long-service awards attract specific treatment in several "
            "jurisdictions and are commonly assumed exempt. No exemption is "
            "assumed here without an advisor saying so."
        ),
    ),
)


def treatment_for(
    rules: list[TaxTreatmentRule], *, reward_type: str, as_of: date
) -> TaxTreatmentRule:
    """The rule in force for this reward type on this date.

    Resolved by the date the reward was given, not by today - the same rule as
    every other effective-dated table here, and for the same reason: a
    threshold that changes in April must not restate a March reward.
    """
    candidates = [r for r in rules if r.reward_type == reward_type and r.effective_from <= as_of]
    if not candidates:
        raise TaxError(
            f"No tax treatment rule for {reward_type!r} in force on "
            f"{as_of.isoformat()}. Treating an unmapped reward as exempt is the "
            "assumption that creates the liability - configure the rule."
        )
    return max(candidates, key=lambda r: r.effective_from)


@dataclass(frozen=True, slots=True)
class RewardEvent:
    """One reward given to one person, valued in rupees."""

    employee_id: str
    reward_type: str
    value: Decimal
    given_on: date
    source_id: str = ""
    description: str = ""


def cumulative_value(events: list[RewardEvent], *, employee_id: str, fy: str) -> Decimal:
    """Total reward value for one person in one financial year."""
    return money(
        sum(
            (
                e.value
                for e in events
                if e.employee_id == employee_id and financial_year(e.given_on) == fy
            ),
            Decimal("0"),
        )
    )


@dataclass(frozen=True, slots=True)
class PerquisiteAssessment:
    """What flows into payroll, and how confident it is."""

    employee_id: str
    financial_year: str
    reward_type: str
    cumulative_value: Decimal
    threshold: Decimal
    taxable_value: Decimal
    treatment: TaxTreatment
    #: False where the rule has not been signed off by an advisor. A payroll
    #: run should refuse to consume a provisional figure.
    reviewed: bool
    note: str

    @property
    def is_taxable(self) -> bool:
        return self.taxable_value > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "financial_year": self.financial_year,
            "reward_type": self.reward_type,
            "cumulative_value": str(self.cumulative_value),
            "threshold": str(self.threshold),
            "taxable_value": str(self.taxable_value),
            "treatment": self.treatment.value,
            "is_taxable": self.is_taxable,
            "reviewed": self.reviewed,
            "note": self.note,
        }


def assess(
    events: list[RewardEvent],
    *,
    employee_id: str,
    reward_type: str,
    fy: str,
    rules: list[TaxTreatmentRule],
    as_of: date,
) -> PerquisiteAssessment:
    """The taxable value of one person's rewards of one type in one year.

    The gift rule and the perquisite rule differ at the threshold and the
    difference is money: under the gift rule the **whole** value becomes taxable
    once the limit is crossed, under a perquisite threshold only the excess
    does. A single "amount over the limit" calculation would silently pick one.
    """
    total = money(
        sum(
            (
                e.value
                for e in events
                if e.employee_id == employee_id
                and e.reward_type == reward_type
                and financial_year(e.given_on) == fy
            ),
            Decimal("0"),
        )
    )
    rule = treatment_for(rules, reward_type=reward_type, as_of=as_of)
    threshold = money(rule.exemption_threshold_per_year)

    if rule.treatment is TaxTreatment.EXEMPT:
        taxable = Decimal("0.00")
        note = f"{reward_type} is treated as exempt for {fy}. Nothing flows into payroll."
    elif rule.treatment is TaxTreatment.GIFT_RULE:
        taxable = total if total > threshold else Decimal("0.00")
        note = (
            f"Cumulative {reward_type} of Rs {total} exceeds the Rs {threshold} "
            f"limit for {fy}, so the **whole** value is taxable under the gift "
            "rule - not only the excess."
            if taxable
            else f"Cumulative {reward_type} of Rs {total} is within the "
            f"Rs {threshold} limit for {fy}. Nothing is taxable yet; the next "
            f"Rs {money(threshold - total)} would cross it and make the whole "
            "year's value taxable."
        )
    else:
        taxable = money(max(Decimal("0"), total - threshold))
        note = (
            f"Rs {taxable} of Rs {total} is taxable as a perquisite for {fy} - "
            f"the amount above the Rs {threshold} threshold."
            if taxable
            else f"Cumulative {reward_type} of Rs {total} is within the "
            f"Rs {threshold} threshold for {fy}."
        )

    if not rule.reviewed:
        note = (
            f"PROVISIONAL - this rule has not been reviewed by a tax advisor. "
            f"{note} Do not file on this figure."
        )

    return PerquisiteAssessment(
        employee_id=employee_id,
        financial_year=fy,
        reward_type=reward_type,
        cumulative_value=total,
        threshold=threshold,
        taxable_value=money(taxable),
        treatment=rule.treatment,
        reviewed=rule.reviewed,
        note=note,
    )


def payroll_perquisites(
    events: list[RewardEvent],
    *,
    employee_id: str,
    fy: str,
    rules: list[TaxTreatmentRule],
    as_of: date,
    allow_provisional: bool = False,
) -> list[PerquisiteAssessment]:
    """Everything that should flow into payroll and TDS for one person.

    Refuses to hand payroll a figure from an unreviewed rule unless the caller
    explicitly accepts a provisional one. A perquisite added to a payslip on an
    unreviewed reading is a deduction the employee queries and the client
    cannot defend.
    """
    types = sorted(
        {
            e.reward_type
            for e in events
            if e.employee_id == employee_id and financial_year(e.given_on) == fy
        }
    )
    assessments = [
        assess(
            events,
            employee_id=employee_id,
            reward_type=t,
            fy=fy,
            rules=rules,
            as_of=as_of,
        )
        for t in types
    ]

    unreviewed = [a for a in assessments if a.is_taxable and not a.reviewed]
    if unreviewed and not allow_provisional:
        raise TaxError(
            "Rule(s) for "
            f"{sorted({a.reward_type for a in unreviewed})} produce a taxable "
            "value and have not been reviewed by a tax advisor. Payroll will "
            "not take a provisional perquisite: it becomes a deduction the "
            "employee queries and the client cannot defend. Mark the rule "
            "reviewed, or pass allow_provisional to model it without filing."
        )
    return assessments


def reward_statement(
    events: list[RewardEvent],
    *,
    employee_id: str,
    fy: str,
    rules: list[TaxTreatmentRule],
    as_of: date,
) -> dict[str, Any]:
    """What the employee sees, before the payslip rather than in it.

    A recognition programme that produces an unexplained deduction has spent
    money to make somebody feel worse. This says what is taxable, what is not,
    and how close they are to a threshold, while that is still information
    rather than a surprise.
    """
    mine = [e for e in events if e.employee_id == employee_id and financial_year(e.given_on) == fy]
    assessments = [
        assess(
            events,
            employee_id=employee_id,
            reward_type=t,
            fy=fy,
            rules=rules,
            as_of=as_of,
        )
        for t in sorted({e.reward_type for e in mine})
    ]

    total = money(sum((e.value for e in mine), Decimal("0")))
    taxable = money(sum((a.taxable_value for a in assessments), Decimal("0")))

    return {
        "employee_id": employee_id,
        "financial_year": fy,
        "total_value": str(total),
        "taxable_value": str(taxable),
        "non_taxable_value": str(money(total - taxable)),
        "rewards": [
            {
                "reward_type": e.reward_type,
                "value": str(money(e.value)),
                "given_on": e.given_on.isoformat(),
                "description": e.description,
            }
            for e in sorted(mine, key=lambda e: e.given_on)
        ],
        "by_type": [a.as_dict() for a in assessments],
        "any_provisional": any(not a.reviewed for a in assessments),
        "note": (
            "Shown here so nothing in your payslip is a surprise. Some of this "
            "may be taxable; the figures marked provisional are awaiting review "
            "by your employer's tax advisor and will be confirmed before "
            "anything is deducted."
        ),
    }
