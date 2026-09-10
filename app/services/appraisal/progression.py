"""Stage progression: what can move where, and what must not.

Pure logic over stage definitions and entries — no database, so the rules can be
exercised exhaustively without building a cycle.

Three rules carry the weight:

1. **Stages advance in sequence.** A mandatory stage cannot be skipped; an
   optional one can. Skipping a mandatory manager review produces an appraisal
   nobody in the reporting line signed off, which is not an appraisal.

2. **A return is a normal move.** Backwards to any earlier stage, with a
   mandatory reason. Managers send self-appraisals back constantly, and a
   workflow that only goes forward makes people work around it — usually by
   rating something they think is wrong.

3. **Nobody appraises themselves as their own manager.** It happens for real —
   a department head whose manager field points at themselves, a founder — and
   silently allowing it produces a rating with no independent view in it. It is
   detected and escalated, not permitted and not blocked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ConflictCheck",
    "ProgressionError",
    "StageDefinition",
    "Transition",
    "detect_self_appraisal_conflict",
    "next_stage",
    "plan_return",
    "resolve_stage_owner",
]


class ProgressionError(ValueError):
    """A stage transition is not allowed."""


@dataclass(frozen=True, slots=True)
class StageDefinition:
    """A stage, as the progression rules see it."""

    stage_id: str
    sequence: int
    role: str
    label: str
    is_mandatory: bool = True
    sees_previous_entries: bool = True
    visible_to_employee_before_release: bool = False


@dataclass(frozen=True, slots=True)
class Transition:
    from_stage_id: str | None
    to_stage_id: str | None
    is_return: bool
    reason: str
    # Stages passed over because they were optional and unfilled.
    skipped: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        """No stage left to move to — the chain is finished."""
        return self.to_stage_id is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "from_stage_id": self.from_stage_id,
            "to_stage_id": self.to_stage_id,
            "is_return": self.is_return,
            "reason": self.reason,
            "skipped": self.skipped,
        }


def _ordered(stages: list[StageDefinition]) -> list[StageDefinition]:
    if not stages:
        raise ProgressionError("A cycle with no stages has no chain to run.")
    ordered = sorted(stages, key=lambda s: s.sequence)
    seen = {s.sequence for s in ordered}
    if len(seen) != len(ordered):
        raise ProgressionError(
            "Two stages share a sequence number, so the order they run in is undefined."
        )
    return ordered


def next_stage(
    *,
    stages: list[StageDefinition],
    current_stage_id: str | None,
    completed_stage_ids: set[str],
) -> Transition:
    """Where the appraisal goes after the current stage submits.

    Optional stages with no entry are stepped over and reported in `skipped`,
    so a cycle that quietly bypassed the reviewer says so rather than looking
    as though the reviewer signed off.
    """
    ordered = _ordered(stages)

    if current_stage_id is None:
        first = ordered[0]
        return Transition(
            from_stage_id=None,
            to_stage_id=first.stage_id,
            is_return=False,
            reason=f"Starting at '{first.label}'.",
        )

    position = next(
        (index for index, s in enumerate(ordered) if s.stage_id == current_stage_id), None
    )
    if position is None:
        raise ProgressionError(
            f"Stage '{current_stage_id}' is not part of this cycle, so there is no next "
            "stage from it."
        )

    skipped: list[str] = []
    for candidate in ordered[position + 1 :]:
        if candidate.is_mandatory or candidate.stage_id in completed_stage_ids:
            return Transition(
                from_stage_id=current_stage_id,
                to_stage_id=candidate.stage_id,
                is_return=False,
                reason=f"Advancing to '{candidate.label}'.",
                skipped=skipped,
            )
        skipped.append(candidate.label)

    return Transition(
        from_stage_id=current_stage_id,
        to_stage_id=None,
        is_return=False,
        reason="The last stage has submitted; the chain is complete.",
        skipped=skipped,
    )


def plan_return(
    *,
    stages: list[StageDefinition],
    current_stage_id: str,
    target_stage_id: str,
    reason: str,
) -> Transition:
    """Send the appraisal back to an earlier stage.

    The reason is mandatory. "Returned" with no reason tells the recipient
    nothing about what to change, and they will either guess or ask — both of
    which are slower than a sentence.
    """
    if not reason.strip():
        raise ProgressionError(
            "A return needs a reason. Without one the person receiving it back does not "
            "know what to change."
        )

    ordered = _ordered(stages)
    by_id = {s.stage_id: s for s in ordered}

    if current_stage_id not in by_id:
        raise ProgressionError(f"Stage '{current_stage_id}' is not part of this cycle.")
    if target_stage_id not in by_id:
        raise ProgressionError(f"Stage '{target_stage_id}' is not part of this cycle.")

    current = by_id[current_stage_id]
    target = by_id[target_stage_id]

    if target.sequence >= current.sequence:
        raise ProgressionError(
            f"'{target.label}' is not before '{current.label}'. A return goes backwards; "
            "to move forward, submit."
        )

    return Transition(
        from_stage_id=current_stage_id,
        to_stage_id=target_stage_id,
        is_return=True,
        reason=reason.strip(),
    )


# ═══════════════════════════════════════════════════════════════════════════
# The self-appraisal conflict
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ConflictCheck:
    has_conflict: bool
    reason: str
    # Who the stage should escalate to instead. None where there is nobody —
    # which is itself worth surfacing rather than falling back to the subject.
    escalate_to_user_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "has_conflict": self.has_conflict,
            "reason": self.reason,
            "escalate_to_user_id": self.escalate_to_user_id,
        }


def detect_self_appraisal_conflict(
    *,
    stage_role: str,
    subject_user_id: str,
    stage_owner_user_id: str | None,
    escalation_chain: list[str],
) -> ConflictCheck:
    """Whether the stage owner is the person being appraised.

    The SELF stage is meant to be the subject, so it is exempt. Every other
    stage exists to bring an independent view, and an employee rating
    themselves as their own manager produces a record with no such view in it.

    It happens for real: a department head whose manager field points at
    themselves, a founder, someone whose manager left and was never replaced.
    So this escalates rather than blocking — blocking would stall the cycle on
    an org-chart problem HR may not be able to fix today.
    """
    if stage_role == "self":
        return ConflictCheck(
            has_conflict=False,
            reason="The self-appraisal stage is the subject's own by design.",
        )

    if stage_owner_user_id is None:
        upward = [u for u in escalation_chain if u != subject_user_id]
        return ConflictCheck(
            has_conflict=True,
            reason=(
                f"The '{stage_role}' stage has no owner. Somebody has to give an "
                "independent view, so it escalates."
            ),
            escalate_to_user_id=upward[0] if upward else None,
        )

    if stage_owner_user_id != subject_user_id:
        return ConflictCheck(
            has_conflict=False, reason="The stage owner is not the subject."
        )

    upward = [u for u in escalation_chain if u != subject_user_id]
    if not upward:
        return ConflictCheck(
            has_conflict=True,
            reason=(
                f"This employee owns their own '{stage_role}' stage and there is nobody "
                "above them to escalate to. HR has to assign a reviewer — an appraisal "
                "with no independent view in it is not an appraisal, and this is an "
                "org-chart problem rather than a workflow one."
            ),
            escalate_to_user_id=None,
        )

    return ConflictCheck(
        has_conflict=True,
        reason=(
            f"This employee owns their own '{stage_role}' stage, so it escalates one "
            "level up. Rating yourself as your own manager produces a record with no "
            "independent view in it."
        ),
        escalate_to_user_id=upward[0],
    )


def resolve_stage_owner(
    *,
    nominal_owner_user_id: str | None,
    delegate_user_id: str | None,
    is_overdue: bool,
    escalation_chain: list[str],
) -> tuple[str | None, str | None, bool, str]:
    """Who actually acts on a stage right now.

    Returns `(acting_user_id, delegated_from_user_id, is_escalated, reason)`.

    Order matters: an explicit delegation beats an escalation. Someone who said
    "Priya covers my reviews while I am away" has already answered the question
    the escalation would be asking.
    """
    if delegate_user_id:
        return (
            delegate_user_id,
            nominal_owner_user_id,
            False,
            "Acting under an explicit delegation; the original owner is recorded.",
        )

    if is_overdue:
        upward = [u for u in escalation_chain if u != nominal_owner_user_id]
        if upward:
            return (
                upward[0],
                nominal_owner_user_id,
                True,
                (
                    "Escalated one level up after the stage went overdue. Without this a "
                    "cycle stalls on one absent manager and HR chases people by hand."
                ),
            )
        return (
            nominal_owner_user_id,
            None,
            False,
            (
                "Overdue, but there is nobody above this stage's owner to escalate to. "
                "It stays with them and HR is notified."
            ),
        )

    return nominal_owner_user_id, None, False, "The stage's own owner is acting."
