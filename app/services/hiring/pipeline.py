"""Which pipeline moves are allowed, and which need a reason.

═══════════════════════════════════════════════════════════════════════════════
TWO STAGES ARE NOT SOMETHING A PERSON DRAGS A CARD INTO
═══════════════════════════════════════════════════════════════════════════════

`offer` is reached when an offer is released and `hired` when it converts to an
employee. Letting a recruiter move a card there by hand would produce a
candidate "at offer" with no offer anywhere, and a "hired" person with no
employee record — two states the rest of the product cannot reconcile.

A rejection or a withdrawal needs a reason, and so does reopening one. "No"
with nothing written down cannot be explained to the candidate or examined for
bias, and a reopened rejection with no reason reads as a mistake.

Pure. Stage values are plain strings so this module needs no database.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "FORWARD_ORDER",
    "PipelineError",
    "allowed_targets",
    "application_status_for",
    "check_transition",
    "is_forward",
]


class PipelineError(ValueError):
    """A stage move this module refuses, with the reason in words."""


#: The pipeline in order. Rejected and withdrawn sit outside it.
FORWARD_ORDER: tuple[str, ...] = (
    "applied",
    "screening",
    "ai_interview",
    "human_interview",
    "offer",
    "hired",
)

_CLOSED = frozenset({"rejected", "withdrawn"})

#: Moves a person may make by hand. Anything absent is refused.
_ALLOWED: dict[str, frozenset[str]] = {
    "applied": frozenset({"screening", "rejected", "withdrawn"}),
    "screening": frozenset({"ai_interview", "human_interview", "rejected", "withdrawn"}),
    "ai_interview": frozenset({"human_interview", "rejected", "withdrawn"}),
    "human_interview": frozenset({"rejected", "withdrawn"}),
    "offer": frozenset(),
    "hired": frozenset(),
    # Reopening goes back to screening, not straight to wherever they were:
    # whatever led to the rejection has to be looked at again.
    "rejected": frozenset({"screening"}),
    "withdrawn": frozenset({"screening"}),
}

_SYSTEM_ONLY: dict[str, str] = {
    "offer": (
        "A candidate reaches the offer stage when an offer to them is released. Draft "
        "and release the offer from Offers instead."
    ),
    "hired": (
        "A candidate is hired when their accepted offer is converted to an employee. "
        "Convert the offer from Offers instead."
    ),
}


@dataclass(frozen=True, slots=True)
class Transition:
    from_stage: str
    to_stage: str
    reason_required: bool
    forward: bool


def is_forward(from_stage: str, to_stage: str) -> bool:
    """Whether the move advances somebody through the pipeline."""
    if from_stage not in FORWARD_ORDER or to_stage not in FORWARD_ORDER:
        return False
    return FORWARD_ORDER.index(to_stage) > FORWARD_ORDER.index(from_stage)


def allowed_targets(from_stage: str) -> list[str]:
    """Where a person may move a candidate from here, in pipeline order."""
    targets = _ALLOWED.get(from_stage, frozenset())
    order = [*FORWARD_ORDER, "rejected", "withdrawn"]
    return [stage for stage in order if stage in targets]


def check_transition(from_stage: str, to_stage: str, *, reason: str | None) -> Transition:
    """Refuse a move that is not allowed, or that needs a reason it lacks."""
    if from_stage == to_stage:
        raise PipelineError(f"The candidate is already at {to_stage.replace('_', ' ')}.")
    if to_stage in _SYSTEM_ONLY:
        raise PipelineError(_SYSTEM_ONLY[to_stage])
    if from_stage in ("offer", "hired"):
        raise PipelineError(
            f"This candidate is at {from_stage}, which the offer controls. Decline, "
            "revoke or record a drop on the offer rather than moving the candidate."
        )
    if to_stage not in _ALLOWED.get(from_stage, frozenset()):
        readable = ", ".join(s.replace("_", " ") for s in allowed_targets(from_stage))
        raise PipelineError(
            f"A candidate cannot move from {from_stage.replace('_', ' ')} to "
            f"{to_stage.replace('_', ' ')}. From here they can go to: {readable or 'nowhere'}."
        )

    reopening = from_stage in _CLOSED
    closing = to_stage in _CLOSED
    needs_reason = reopening or closing
    if needs_reason and not (reason or "").strip():
        raise PipelineError(
            "Rejecting, withdrawing or reopening a candidate needs a reason. A decision "
            "with nothing written down cannot be explained to the candidate or examined "
            "for bias later."
            if closing
            else "Reopening a closed candidate needs a reason, or it reads as a mistake."
        )
    return Transition(
        from_stage=from_stage,
        to_stage=to_stage,
        reason_required=needs_reason,
        forward=is_forward(from_stage, to_stage),
    )


def application_status_for(stage: str) -> str | None:
    """The coarse status the candidate portal shows for an internal stage.

    Deliberately coarser: a candidate needs to know somebody is looking, not
    that they are at "AI interview round 2".
    """
    return {
        "applied": "applied",
        "screening": "under_review",
        "ai_interview": "interviewing",
        "human_interview": "interviewing",
        "offer": "offered",
        "rejected": "closed",
        "withdrawn": "closed",
    }.get(stage)
