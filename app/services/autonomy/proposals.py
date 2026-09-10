"""The proposal queue: one list, evaluable, and it expires.

═══════════════════════════════════════════════════════════════════════════════
ONE QUEUE, BECAUSE AN HR OPS LEAD WILL NOT CHECK FIVE PLACES
═══════════════════════════════════════════════════════════════════════════════

Every agent that proposes something puts it in the same queue. A per-agent inbox
looks tidier in each module and produces a person with five tabs open who checks
two of them — after which the other three are a backlog nobody owns, and the
proposals in them expire unread.

An invariant checks no agent has a private queue.

═══════════════════════════════════════════════════════════════════════════════
EVALUABLE, NOT RUBBER-STAMPABLE
═══════════════════════════════════════════════════════════════════════════════

A proposal with no reasoning and no citations gets approved in a batch by
somebody clicking through forty of them, and the approval step becomes theatre
that adds latency and no safety. So `Proposal` requires reasoning **and**
citations, and `assert_evaluable` refuses one without them — the approver has to
be able to disagree, which means they have to be able to check.

Confidence travels too, from slice 28: an approver looking at a 51%-confident
proposal and a 95%-confident one should not see the same thing.

═══════════════════════════════════════════════════════════════════════════════
EXPIRY IS NOT TIDYING
═══════════════════════════════════════════════════════════════════════════════

A statutory rate change proposed in March and approved in September applies a
rate that was superseded in June, to a payroll that has already run. The
proposal was correct when it was made and is wrong now, and nothing about the
approval screen says so.

So proposals expire, the window depends on impact, and an expired proposal
**cannot be approved** — `decide` raises rather than applying it late. Expiry is
a correctness property, not housekeeping.

Pure. `now` is always supplied.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.services.autonomy.catalogue import (
    ACTIONS,
    AutonomyError,
    AutonomyLevel,
    ImpactLevel,
    action,
)

__all__ = [
    "EXPIRY_WINDOWS",
    "ActionStats",
    "Proposal",
    "ProposalStatus",
    "assert_evaluable",
    "decide",
    "expire_due",
    "expires_at",
    "route",
    "stats_for",
]


class ProposalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    #: Ran out of time. Distinct from rejected: nobody decided, and the
    #: difference matters for the rejection-rate metric — counting expiries as
    #: rejections would make an ignored queue look like a defective agent.
    EXPIRED = "expired"
    #: Applied without a human, under the tenant's configured autonomy.
    AUTO_APPLIED = "auto_applied"
    #: Applied and then undone.
    REVERTED = "reverted"

    @property
    def is_open(self) -> bool:
        return self is ProposalStatus.PENDING

    @property
    def counts_toward_rejection_rate(self) -> bool:
        """Only decisions a person actually made.

        An expired proposal says something about the queue, not about the agent,
        and folding the two together hides both.
        """
        return self in (ProposalStatus.APPROVED, ProposalStatus.REJECTED)


#: How long a proposal stays approvable, by impact. Shorter for higher impact:
#: a critical proposal that has been sitting for a fortnight is one nobody
#: understood, and applying it late is worse than losing it.
EXPIRY_WINDOWS: dict[ImpactLevel, timedelta] = {
    ImpactLevel.CRITICAL: timedelta(days=7),
    ImpactLevel.HIGH: timedelta(days=14),
    ImpactLevel.MEDIUM: timedelta(days=30),
    ImpactLevel.LOW: timedelta(days=30),
}


def expires_at(action_key: str, *, created_at: datetime) -> datetime:
    return created_at + EXPIRY_WINDOWS[action(action_key).impact]


@dataclass(frozen=True, slots=True)
class Proposal:
    """One thing an agent wants to do, and everything needed to judge it."""

    action_key: str
    subject_type: str
    subject_id: str
    proposed_change: dict[str, Any]
    #: Why the agent thinks this. Prose an approver reads, not a rule name.
    reasoning: str
    #: `[[source:id]]` identifiers from slice 28, already verified.
    citations: tuple[str, ...]
    confidence: float
    created_at: datetime
    status: ProposalStatus = ProposalStatus.PENDING
    #: The state before, for a reversible action. Captured at proposal time
    #: rather than at apply time: by the time somebody wants to revert, the
    #: record has moved on and "what was it before" is a reconstruction.
    previous_state: dict[str, Any] = field(default_factory=dict)
    decision_note: str = ""

    @property
    def impact(self) -> ImpactLevel:
        return action(self.action_key).impact

    @property
    def reversible(self) -> bool:
        return action(self.action_key).reversible

    def is_expired(self, now: datetime) -> bool:
        return now > expires_at(self.action_key, created_at=self.created_at)

    def age_days(self, now: datetime) -> int:
        return (now - self.created_at).days

    def as_dict(self, *, now: datetime | None = None) -> dict[str, Any]:
        spec = action(self.action_key)
        payload: dict[str, Any] = {
            "action_key": self.action_key,
            "agent_key": spec.agent_key,
            "description": spec.description,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "proposed_change": self.proposed_change,
            "reasoning": self.reasoning,
            "citations": list(self.citations),
            "confidence": round(self.confidence, 3),
            "impact": self.impact.value,
            "reversible": self.reversible,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "expires_at": expires_at(
                self.action_key, created_at=self.created_at
            ).isoformat(),
            "decision_note": self.decision_note,
        }
        if now is not None:
            payload |= {
                "age_days": self.age_days(now),
                "is_expired": self.is_expired(now),
            }
        return payload


def assert_evaluable(proposal: Proposal) -> None:
    """Refuse a proposal an approver cannot actually judge.

    Raises. A proposal with no reasoning gets approved in a batch by somebody
    clicking through forty of them, and the approval step becomes latency
    without safety — which is worse than no approval step, because it looks like
    one.
    """
    if not proposal.reasoning.strip():
        raise AutonomyError(
            f"A proposal for {proposal.action_key!r} has no reasoning. An "
            "approver who cannot see why will approve it in a batch, and the "
            "approval step becomes theatre that adds latency and no safety."
        )
    if not proposal.citations:
        raise AutonomyError(
            f"A proposal for {proposal.action_key!r} cites nothing. The "
            "approver has to be able to check the claim, which means being able "
            "to disagree with it."
        )
    if not 0.0 <= proposal.confidence <= 1.0:
        raise AutonomyError(
            f"Confidence {proposal.confidence} is outside 0 to 1."
        )
    if proposal.reversible and not proposal.previous_state:
        raise AutonomyError(
            f"{proposal.action_key!r} is reversible and the proposal captured no "
            "previous state. By the time somebody wants to revert, the record "
            "has moved on — capturing it later is a reconstruction, not a "
            "revert."
        )


@dataclass(frozen=True, slots=True)
class Routing:
    """What happens to a proposal under the tenant's configuration."""

    autonomy: AutonomyLevel
    applies_automatically: bool
    needs_approval: bool
    notify: bool
    #: Why it routed this way, in words. Shown in the queue, so "why did this
    #: one need me and that one not" is answerable without reading config.
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "autonomy": self.autonomy.value,
            "applies_automatically": self.applies_automatically,
            "needs_approval": self.needs_approval,
            "notify": self.notify,
            "reason": self.reason,
        }


def route(
    action_key: str,
    *,
    configured: AutonomyLevel,
    magnitude: Decimal | None = None,
    threshold: Decimal | None = None,
) -> Routing:
    """Decide whether this specific proposal applies itself or waits.

    The threshold is the interesting part: an expense claim of ₹1,999 auto-
    applies and one of ₹2,000 routes to a person. **At** the threshold it
    routes — the boundary belongs to the human side, because "under ₹2,000" is
    what the client configured and 2,000 is not under 2,000.
    """
    spec = action(action_key)
    ceiling = spec.max_autonomy

    effective = configured
    if configured.rank > ceiling.rank:
        # Should be unreachable — `validate_autonomy` refuses it at write time.
        # Capping here rather than trusting that is deliberate: this function is
        # the last thing between a stored value and an automatic action.
        effective = ceiling

    reason_parts: list[str] = []

    if threshold is not None and magnitude is not None:
        if magnitude >= threshold:
            reason_parts.append(
                f"{magnitude} is at or above the configured threshold of "
                f"{threshold} {spec.threshold_unit or ''}".strip()
                + ", so it routes to a person."
            )
            effective = min(
                effective,
                AutonomyLevel.PROPOSE_FOR_APPROVAL,
                key=lambda level: level.rank,
            )
        else:
            reason_parts.append(
                f"{magnitude} is below the configured threshold of {threshold} "
                f"{spec.threshold_unit or ''}".strip() + "."
            )

    if effective.applies_automatically:
        reason_parts.append(
            f"This tenant has {spec.description.lower().rstrip('.')} set to "
            f"{effective.label!r}."
        )
    else:
        reason_parts.append(
            f"This tenant has it set to {effective.label!r}."
        )

    return Routing(
        autonomy=effective,
        applies_automatically=effective.applies_automatically,
        needs_approval=effective is AutonomyLevel.PROPOSE_FOR_APPROVAL,
        notify=effective is AutonomyLevel.AUTO_APPLY_WITH_NOTIFY,
        reason=" ".join(reason_parts),
    )


def decide(
    proposal: Proposal,
    *,
    approve: bool,
    now: datetime,
    note: str = "",
) -> Proposal:
    """Approve or reject, refusing to act on one that has expired.

    An expired proposal is not approvable. A statutory rate proposed in March
    and approved in September applies a rate superseded in June to a payroll
    that has already run — the proposal was right when it was made and is wrong
    now, and nothing on the approval screen would say so.
    """
    if proposal.status is not ProposalStatus.PENDING:
        raise AutonomyError(
            f"This proposal is already {proposal.status.value}. Deciding it "
            "again would produce a second outcome for one decision."
        )
    if proposal.is_expired(now):
        raise AutonomyError(
            f"This proposal expired on "
            f"{expires_at(proposal.action_key, created_at=proposal.created_at).date()}"
            f" and cannot be applied. It was made {proposal.age_days(now)} days "
            "ago against facts that may since have changed — if it is still "
            "wanted, it should be proposed again against today's data."
        )
    if not approve and not note.strip():
        raise AutonomyError(
            "A rejection needs a note. The rejection rate is a product signal: "
            "a proposal type rejected ninety per cent of the time is a defective "
            "agent, and without the reasons nobody can tell which part is wrong."
        )

    return Proposal(
        action_key=proposal.action_key,
        subject_type=proposal.subject_type,
        subject_id=proposal.subject_id,
        proposed_change=proposal.proposed_change,
        reasoning=proposal.reasoning,
        citations=proposal.citations,
        confidence=proposal.confidence,
        created_at=proposal.created_at,
        status=ProposalStatus.APPROVED if approve else ProposalStatus.REJECTED,
        previous_state=proposal.previous_state,
        decision_note=note,
    )


def expire_due(
    proposals: list[Proposal], *, now: datetime
) -> tuple[list[Proposal], list[Proposal]]:
    """Split pending proposals into those still live and those that have run out.

    Returns both. The expired list is not rubbish — it is the evidence that a
    queue is not being worked, and a sweep that silently dropped them would hide
    exactly the problem worth seeing.
    """
    live: list[Proposal] = []
    expired: list[Proposal] = []
    for proposal in proposals:
        if not proposal.status.is_open:
            continue
        (expired if proposal.is_expired(now) else live).append(proposal)
    return live, expired


@dataclass(frozen=True, slots=True)
class ActionStats:
    """Approval and rejection rates for one action.

    The rejection rate is a **product signal to us**, not only a client metric.
    A proposal type rejected ninety per cent of the time is a defective agent,
    and the client experiencing it will not file a bug — they will turn the
    agent off and mention it at renewal.
    """

    action_key: str
    approved: int
    rejected: int
    expired: int
    auto_applied: int
    reverted: int

    @property
    def decided(self) -> int:
        return self.approved + self.rejected

    @property
    def rejection_rate(self) -> float | None:
        """`None` below a usable sample. Three rejections out of three is noise."""
        if self.decided < 5:
            return None
        return self.rejected / self.decided

    @property
    def revert_rate(self) -> float | None:
        if self.auto_applied < 5:
            return None
        return self.reverted / self.auto_applied

    @property
    def is_defective_signal(self) -> bool:
        """Whether this looks like a broken agent rather than a cautious client."""
        rate = self.rejection_rate
        if rate is not None and rate >= 0.7:
            return True
        revert = self.revert_rate
        return revert is not None and revert >= 0.3

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_key": self.action_key,
            "approved": self.approved,
            "rejected": self.rejected,
            "expired": self.expired,
            "auto_applied": self.auto_applied,
            "reverted": self.reverted,
            "decided": self.decided,
            "rejection_rate": (
                round(self.rejection_rate, 3)
                if self.rejection_rate is not None
                else None
            ),
            "revert_rate": (
                round(self.revert_rate, 3) if self.revert_rate is not None else None
            ),
            "looks_defective": self.is_defective_signal,
            "note": (
                "Rejected far more often than not. This is a signal about the "
                "agent, not about the client — they will not file a bug, they "
                "will turn it off and mention it at renewal."
                if self.is_defective_signal
                else ""
            ),
        }


def stats_for(
    action_key: str, statuses: list[ProposalStatus]
) -> ActionStats:
    """Fold a list of outcomes into the rates.

    Expiries are counted and excluded from the rejection rate. Counting them as
    rejections would make an unworked queue look like a defective agent, and the
    two need opposite responses.
    """
    if action_key not in ACTIONS:
        raise AutonomyError(f"No agent action {action_key!r}.")
    return ActionStats(
        action_key=action_key,
        approved=sum(1 for s in statuses if s is ProposalStatus.APPROVED),
        rejected=sum(1 for s in statuses if s is ProposalStatus.REJECTED),
        expired=sum(1 for s in statuses if s is ProposalStatus.EXPIRED),
        auto_applied=sum(1 for s in statuses if s is ProposalStatus.AUTO_APPLIED),
        reverted=sum(1 for s in statuses if s is ProposalStatus.REVERTED),
    )
