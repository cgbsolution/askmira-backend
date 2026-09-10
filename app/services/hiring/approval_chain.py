"""Approval chains for requisitions and offers.

═══════════════════════════════════════════════════════════════════════════════
A JUNIOR BACKFILL AND A SENIOR NEW POSITION ARE NOT THE SAME DECISION
═══════════════════════════════════════════════════════════════════════════════

One chain for everything is a chain that is wrong twice. Set it at the level a
senior hire needs and the head of engineering signs eleven backfills a week
without reading any of them — which is how signatures stop meaning anything. Set
it at the level a backfill needs and a director-level offer goes out on a
recruiter's judgement.

So the chain is resolved from the *decision*: the CTC, the grade, and whether it
breaks the budget the requisition was approved on. Breaking the band adds a step
rather than raising a warning, because a step is a person and a warning is a
colour.

**Bands must cover everything.** A gap in the bands means an offer at exactly
₹15,00,001 resolves to no chain at all and releases unapproved. Validated when
the rules are saved, not discovered when the offer goes out.
═══════════════════════════════════════════════════════════════════════════════

Pure. No database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

__all__ = [
    "ApprovalChainError",
    "ChainStep",
    "RuleSpec",
    "resolve_chain",
    "validate_rules",
]


class ApprovalChainError(ValueError):
    """A rule set cannot produce a chain, or produces one nobody intended."""


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """One rule: a band, optional grade scope, and who must sign inside it.

    `ctc_max` is exclusive so adjacent bands meet exactly. Half-open avoids the
    off-by-one where 15,00,000 belongs to two bands or to neither, which is the
    boundary every band scheme gets wrong at least once.
    """

    rule_id: str
    sequence: int
    approver_role: str
    ctc_min: Decimal = Decimal(0)
    # None means unbounded above — the top band, which must exist.
    ctc_max: Decimal | None = None
    grades: tuple[str, ...] = ()
    # A step that applies only when the offer exceeds the requisition's band.
    only_when_over_band: bool = False
    label: str = ""

    def covers_ctc(self, ctc: Decimal) -> bool:
        if ctc < self.ctc_min:
            return False
        return self.ctc_max is None or ctc < self.ctc_max

    def covers_grade(self, grade: str | None) -> bool:
        # No grades listed means every grade. A rule scoped to grades an
        # employee is not in simply does not apply.
        return not self.grades or (grade is not None and grade in self.grades)


@dataclass(frozen=True, slots=True)
class ChainStep:
    rule_id: str
    sequence: int
    approver_role: str
    label: str
    # Why this step is in the chain. Shown to the approver, because "you are
    # step 3 of 4" tells them nothing and "you are here because this offer
    # exceeds the approved band" tells them what to look at.
    reason: str


@dataclass(frozen=True, slots=True)
class ChainRequest:
    ctc_annual: Decimal
    grade: str | None = None
    over_band: bool = False
    labels: dict[str, str] = field(default_factory=dict)


def validate_rules(rules: list[RuleSpec]) -> None:
    """Refuse a rule set that could leave an offer with no chain.

    Checked when the rules are saved. Discovering a gap at release time means
    discovering it as an offer that went out unapproved.
    """
    if not rules:
        raise ApprovalChainError(
            "An approval chain needs at least one rule. With none, every offer "
            "releases on a recruiter's own signature."
        )

    baseline = [r for r in rules if not r.only_when_over_band and not r.grades]
    if not baseline:
        raise ApprovalChainError(
            "Every rule is conditional — on a grade, or on breaking the budget. An "
            "offer that matches no condition would resolve to an empty chain and "
            "release unapproved."
        )

    # Unbounded top band. Without one, the highest offers — the ones that most
    # need a chain — are the ones that fall out of the bands entirely.
    if not any(r.ctc_max is None for r in baseline):
        ceiling = max(r.ctc_max for r in baseline if r.ctc_max is not None)
        raise ApprovalChainError(
            f"The unconditional bands stop at {ceiling}. An offer above that resolves "
            "to no approver at all — and the offers above the top band are exactly "
            "the ones that need one."
        )

    # Gaps between adjacent unconditional bands.
    ordered = sorted(baseline, key=lambda r: r.ctc_min)
    covered_to: Decimal | None = ordered[0].ctc_min
    if covered_to > 0:
        raise ApprovalChainError(
            f"The unconditional bands start at {covered_to}. An offer below that has "
            "no approver."
        )
    for rule in ordered:
        if covered_to is None:
            break
        if rule.ctc_min > covered_to:
            raise ApprovalChainError(
                f"Nothing covers CTC between {covered_to} and {rule.ctc_min}. An offer "
                "in that range resolves to an empty chain and releases unapproved."
            )
        if rule.ctc_max is None:
            covered_to = None
        elif rule.ctc_max > covered_to:
            covered_to = rule.ctc_max

    sequences = [r.sequence for r in rules]
    duplicates = {s for s in sequences if sequences.count(s) > 1}
    if duplicates:
        raise ApprovalChainError(
            f"These sequence numbers are used more than once: {sorted(duplicates)}. "
            "Approval order is the point of a chain; two rules at the same position "
            "means the order depends on which row comes back first."
        )


def resolve_chain(
    rules: list[RuleSpec],
    *,
    ctc_annual: Decimal,
    grade: str | None = None,
    over_band: bool = False,
) -> list[ChainStep]:
    """The signatures this particular decision needs, in order.

    Every matching rule contributes a step — this is not first-match-wins. A
    senior offer that also breaks the band needs both the seniority step and the
    band step; taking only the first would drop exactly the approval the band
    exists to force.
    """
    if not isinstance(ctc_annual, Decimal):
        raise ApprovalChainError("CTC must be a Decimal, not a float")

    steps: list[ChainStep] = []
    for rule in sorted(rules, key=lambda r: (r.sequence, r.rule_id)):
        if rule.only_when_over_band and not over_band:
            continue
        if not rule.covers_ctc(ctc_annual):
            continue
        if not rule.covers_grade(grade):
            continue

        if rule.only_when_over_band:
            reason = (
                "This offer exceeds the band the requisition was approved on, which "
                "adds this approval."
            )
        elif rule.grades:
            reason = f"Required for grade {grade}."
        else:
            reason = f"Required for offers in this CTC band (from {rule.ctc_min})."

        steps.append(
            ChainStep(
                rule_id=rule.rule_id,
                sequence=rule.sequence,
                approver_role=rule.approver_role,
                label=rule.label or rule.approver_role,
                reason=reason,
            )
        )

    if not steps:
        raise ApprovalChainError(
            f"No approval rule matches a CTC of {ctc_annual} at grade {grade or '—'}. "
            "Releasing on no signature is not a fallback; fix the rules."
        )

    # The same role reached by two rules signs once, at its earliest position.
    # Asking the CFO to approve the same offer twice teaches them the second
    # request is noise.
    seen: set[str] = set()
    deduped: list[ChainStep] = []
    for step in steps:
        if step.approver_role in seen:
            continue
        seen.add(step.approver_role)
        deduped.append(step)
    return deduped
