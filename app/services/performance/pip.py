"""The PIP lifecycle, where the audit trail is the deliverable.

═══════════════════════════════════════════════════════════════════════════════
A PIP THAT CANNOT BE EVIDENCED IS WORSE THAN NO PIP
═══════════════════════════════════════════════════════════════════════════════

That is not a stylistic preference. A performance improvement plan that ends in
a dismissal and cannot be evidenced afterwards creates the *appearance* of
process without the substance, and in front of a tribunal that reads worse than
having had no process at all — because the company will be asked why it built
one and did not follow it.

So this module refuses the things that hollow out the trail:

- a PIP with no expectations, because "improve your performance" is not a
  standard anybody can meet or fail;
- a PIP with no support recorded, because an improvement plan the employer
  contributed nothing to is a notice period with paperwork;
- an outcome recorded against a plan whose check-ins were never done;
- and, above all, a check-in with no **employee** comment.

═══════════════════════════════════════════════════════════════════════════════
THE EMPLOYEE'S CONTEMPORANEOUS ACCOUNT IS THE PART THAT MATTERS
═══════════════════════════════════════════════════════════════════════════════

A trail of manager observations with no employee response is one side of a
conversation, and its evidential value is close to zero — anybody can write that
somebody was underperforming. An employee's own words, written at the time,
either corroborate the manager or contradict them, and both are useful. What is
not useful is discovering at the tribunal that the employee was never asked.

`record_check_in` therefore requires the employee's input or an explicit,
reasoned record that they declined to give it. Declining is a real thing people
do and it has to be representable; silently permitting a blank is not.

Pure. `today` is always supplied.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from typing import Any

__all__ = [
    "MIN_CHECK_INS",
    "CheckIn",
    "PipError",
    "PipFacts",
    "PipOutcome",
    "PipStatus",
    "close_pip",
    "evidence_pack",
    "record_check_in",
    "trail_quality",
]


class PipError(ValueError):
    """A PIP operation this module refuses."""


#: Below this many recorded check-ins, an outcome is not evidenced. Three is the
#: minimum that shows a plan was run rather than declared: a beginning, a middle
#: where the employee could still change the outcome, and an end.
MIN_CHECK_INS = 3


class PipStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    EXTENDED = "extended"
    SUCCESSFUL = "successful"
    UNSUCCESSFUL = "unsuccessful"
    #: Stopped without an outcome - the manager left, the role changed, the
    #: business reorganised. Honest, and distinct from a failure.
    ABANDONED = "abandoned"

    @property
    def is_open(self) -> bool:
        return self in (PipStatus.DRAFT, PipStatus.ACTIVE, PipStatus.EXTENDED)

    @property
    def is_outcome(self) -> bool:
        return self in (
            PipStatus.SUCCESSFUL,
            PipStatus.UNSUCCESSFUL,
            PipStatus.ABANDONED,
        )


class PipOutcome(str, enum.Enum):
    SUCCESSFUL = "successful"
    EXTENDED = "extended"
    UNSUCCESSFUL = "unsuccessful"
    ABANDONED = "abandoned"


class EmployeeInputState(str, enum.Enum):
    """Whether the employee's own account was captured, and if not, why.

    `DECLINED` is a real state that people genuinely occupy, and it has to be
    representable — otherwise a manager faced with a required field types
    something on the employee's behalf, which is worse than a recorded refusal.
    """

    GIVEN = "given"
    DECLINED = "declined"
    NOT_ASKED = "not_asked"


@dataclass(frozen=True, slots=True)
class CheckIn:
    """One scheduled conversation, with both sides of it."""

    check_in_id: str
    scheduled_for: date
    held_on: date | None
    manager_comments: str
    employee_comments: str
    employee_input_state: EmployeeInputState
    milestone_id: str = ""
    #: Recorded where the employee declined, in their words or the manager's
    #: account of what they said.
    decline_reason: str = ""

    @property
    def was_held(self) -> bool:
        return self.held_on is not None

    @property
    def has_employee_voice(self) -> bool:
        return self.employee_input_state is EmployeeInputState.GIVEN and bool(
            self.employee_comments.strip()
        )

    @property
    def is_evidenced(self) -> bool:
        """Held, with a manager record and either the employee's words or a
        reasoned note that they declined to give them."""
        if not self.was_held or not self.manager_comments.strip():
            return False
        if self.has_employee_voice:
            return True
        return self.employee_input_state is EmployeeInputState.DECLINED and bool(
            self.decline_reason.strip()
        )


def record_check_in(
    *,
    check_in_id: str,
    scheduled_for: date,
    held_on: date | None,
    manager_comments: str,
    employee_comments: str = "",
    employee_input_state: EmployeeInputState = EmployeeInputState.NOT_ASKED,
    decline_reason: str = "",
    milestone_id: str = "",
) -> CheckIn:
    """Build a check-in, refusing the shapes that hollow out the trail.

    A check-in with no employee input is one side of a conversation, and in
    front of a tribunal its evidential value is close to zero. Declining is
    permitted and recorded; being silently absent is not.
    """
    if held_on is None:
        raise PipError(
            "A check-in with no date held is a meeting that has not happened "
            "yet. Schedule it, but do not record it as evidence."
        )
    if not manager_comments.strip():
        raise PipError(
            "A check-in needs the manager's account of it. A dated row with no "
            "content evidences that a calendar entry existed."
        )
    if employee_input_state is EmployeeInputState.GIVEN and not (employee_comments.strip()):
        raise PipError(
            "The employee's input is marked as given and is empty. Record what "
            "they said, or record that they declined and why."
        )
    if employee_input_state is EmployeeInputState.DECLINED and not (decline_reason.strip()):
        raise PipError(
            "An employee declining to comment is a real thing and it has to say "
            "so in their words or in the manager's account of what they said. "
            "A bare 'declined' is indistinguishable from never having asked."
        )
    if employee_input_state is EmployeeInputState.NOT_ASKED:
        raise PipError(
            "The employee was not asked for their account. A trail of manager "
            "observations with no employee response is one side of a "
            "conversation - anybody can write that somebody was "
            "underperforming, and the tribunal's first question is what the "
            "employee said at the time."
        )

    return CheckIn(
        check_in_id=check_in_id,
        scheduled_for=scheduled_for,
        held_on=held_on,
        manager_comments=manager_comments,
        employee_comments=employee_comments,
        employee_input_state=employee_input_state,
        decline_reason=decline_reason,
        milestone_id=milestone_id,
    )


@dataclass(frozen=True, slots=True)
class Milestone:
    """One measurable expectation, with a date."""

    milestone_id: str
    description: str
    target_date: date
    measurable_criteria: str
    met: bool | None = None

    def __post_init__(self) -> None:
        if not self.measurable_criteria.strip():
            raise PipError(
                f"Milestone {self.description!r} has no measurable criteria. "
                "'Improve communication' is not a standard anybody can meet or "
                "fail, and a plan built from them cannot be defended."
            )


@dataclass(frozen=True, slots=True)
class PipFacts:
    """One improvement plan as it stands."""

    pip_id: str
    employee_id: str
    initiated_by: str
    start_date: date
    end_date: date
    reason: str
    expectations: str
    support_provided: str
    status: PipStatus
    milestones: tuple[Milestone, ...] = ()
    check_ins: tuple[CheckIn, ...] = ()
    appraisal_id: str = ""

    def __post_init__(self) -> None:
        if self.end_date < self.start_date:
            raise PipError("A PIP cannot end before it starts.")
        if not self.reason.strip():
            raise PipError("A PIP needs a stated reason.")
        if not self.expectations.strip():
            raise PipError(
                "A PIP needs written expectations. 'Improve your performance' "
                "is not a standard anybody can meet or fail."
            )
        if not self.support_provided.strip():
            raise PipError(
                "A PIP needs the support the employer is providing recorded. An "
                "improvement plan the employer contributed nothing to is a "
                "notice period with paperwork, and it reads that way to anybody "
                "reviewing it later."
            )

    @property
    def held_check_ins(self) -> tuple[CheckIn, ...]:
        return tuple(c for c in self.check_ins if c.was_held)

    @property
    def evidenced_check_ins(self) -> tuple[CheckIn, ...]:
        return tuple(c for c in self.check_ins if c.is_evidenced)

    def duration_days(self) -> int:
        return (self.end_date - self.start_date).days


# ══════════════════════════════════════════════════════════════════════════════
# Trail quality
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class TrailAssessment:
    """Whether this plan could be handed to employment counsel."""

    is_defensible: bool
    check_ins_held: int
    check_ins_evidenced: int
    employee_voice_count: int
    gaps: tuple[str, ...]
    summary: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "is_defensible": self.is_defensible,
            "check_ins_held": self.check_ins_held,
            "check_ins_evidenced": self.check_ins_evidenced,
            "employee_voice_count": self.employee_voice_count,
            "gaps": list(self.gaps),
            "summary": self.summary,
        }


def trail_quality(pip: PipFacts, *, minimum_check_ins: int = MIN_CHECK_INS) -> TrailAssessment:
    """What is missing before this could be handed to employment counsel.

    Reported as a list of gaps rather than a score. "72% complete" tells nobody
    what to do; "no check-in records the employee's own account" does.
    """
    gaps: list[str] = []

    if not pip.milestones:
        gaps.append(
            "No milestones. Without measurable expectations there is nothing "
            "the employee could have met, and nothing they can be said to have "
            "failed."
        )

    held = pip.held_check_ins
    evidenced = pip.evidenced_check_ins
    voices = sum(1 for c in pip.check_ins if c.has_employee_voice)

    if len(held) < minimum_check_ins:
        gaps.append(
            f"{len(held)} check-in(s) held against a minimum of "
            f"{minimum_check_ins}. A plan with one conversation at the start "
            "and one at the end was declared rather than run - there was no "
            "middle in which the employee could still change the outcome."
        )
    if len(evidenced) < len(held):
        gaps.append(
            f"{len(held) - len(evidenced)} check-in(s) are recorded but not "
            "evidenced: no manager account, or no employee response and no note "
            "that they declined."
        )
    if voices == 0 and held:
        gaps.append(
            "No check-in records the employee's own words. A trail of manager "
            "observations with no employee response is one side of a "
            "conversation."
        )
    if pip.duration_days() < 30:
        gaps.append(
            f"The plan runs {pip.duration_days()} days. A period too short for "
            "the employee to have plausibly changed anything reads as a "
            "decision already made."
        )

    unmet_undecided = [m for m in pip.milestones if m.met is None]
    if pip.status.is_outcome and unmet_undecided:
        gaps.append(
            f"{len(unmet_undecided)} milestone(s) were never assessed, and the "
            "plan has an outcome. Whether they were met is the substance of "
            "that outcome."
        )

    defensible = not gaps
    return TrailAssessment(
        is_defensible=defensible,
        check_ins_held=len(held),
        check_ins_evidenced=len(evidenced),
        employee_voice_count=voices,
        gaps=tuple(gaps),
        summary=(
            "The trail is complete: measurable milestones, enough check-ins to "
            "show the plan was run, and the employee's own account at the time."
            if defensible
            else f"{len(gaps)} gap(s) would be the first thing asked about. A "
            "PIP that cannot be evidenced is worse than no PIP, because it "
            "creates the appearance of process without the substance."
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Closing
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class CloseResult:
    """The outcome, and what follows from it."""

    outcome: PipOutcome
    new_status: PipStatus
    routes_to_exit: bool
    trail: TrailAssessment
    new_end_date: date | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "new_status": self.new_status.value,
            "routes_to_exit": self.routes_to_exit,
            "new_end_date": self.new_end_date.isoformat() if self.new_end_date else None,
            "trail": self.trail.as_dict(),
            "message": self.message,
        }


def close_pip(
    pip: PipFacts,
    *,
    outcome: PipOutcome,
    today: date,
    reason: str = "",
    new_end_date: date | None = None,
    minimum_check_ins: int = MIN_CHECK_INS,
    acknowledge_weak_trail: bool = False,
) -> CloseResult:
    """Record an outcome, refusing an unsuccessful one on an unevidenced plan.

    The asymmetry is deliberate. A *successful* PIP closed on a thin trail costs
    nobody anything - the employee keeps their job. An *unsuccessful* one routes
    to an exit, and an exit built on a plan that cannot be evidenced is the
    situation this module exists to prevent.

    `acknowledge_weak_trail` exists because a real organisation sometimes has to
    proceed anyway, and forcing them to falsify a check-in to do it would be far
    worse. It records that somebody decided to proceed knowing the trail was
    thin, which is itself the useful fact.
    """
    if not pip.status.is_open:
        raise PipError(
            f"This PIP is already {pip.status.value}. Recording a second "
            "outcome would leave two accounts of how it ended."
        )

    trail = trail_quality(pip, minimum_check_ins=minimum_check_ins)

    if outcome is PipOutcome.EXTENDED:
        if new_end_date is None:
            raise PipError(
                "An extension needs a new end date. A PIP extended to nowhere "
                "runs indefinitely, which is not an improvement plan."
            )
        if new_end_date <= pip.end_date:
            raise PipError(
                f"The new end date {new_end_date.isoformat()} is not after the "
                f"current one {pip.end_date.isoformat()}."
            )
        if not reason.strip():
            raise PipError(
                "An extension needs a reason. Extending without one reads, "
                "later, as having moved the goalposts."
            )
        return CloseResult(
            outcome=outcome,
            new_status=PipStatus.EXTENDED,
            routes_to_exit=False,
            trail=trail,
            new_end_date=new_end_date,
            message=(
                f"Extended to {new_end_date.isoformat()}: {reason} The "
                "check-in schedule continues; an extension with no further "
                "conversations is the same plan with a later date on it."
            ),
        )

    if outcome is PipOutcome.UNSUCCESSFUL:
        if not reason.strip():
            raise PipError(
                "An unsuccessful outcome needs the reason written out. It is "
                "the document an employment tribunal reads first."
            )
        if not trail.is_defensible and not acknowledge_weak_trail:
            raise PipError(
                "This plan's trail has gaps and the outcome routes to an exit:\n"
                + "\n".join(f"  - {g}" for g in trail.gaps)
                + "\nFix what can still be fixed, or record explicitly that you "
                "are proceeding knowing the trail is thin. Falsifying a "
                "check-in to get past this would be far worse than proceeding "
                "with it recorded."
            )
        return CloseResult(
            outcome=outcome,
            new_status=PipStatus.UNSUCCESSFUL,
            routes_to_exit=True,
            trail=trail,
            new_end_date=None,
            message=(
                f"Unsuccessful: {reason} The full trail travels with the exit "
                "record."
                + (
                    " **Proceeding was acknowledged despite gaps in the "
                    "trail**, and that acknowledgement is part of the record."
                    if not trail.is_defensible
                    else ""
                )
            ),
        )

    if outcome is PipOutcome.ABANDONED:
        if not reason.strip():
            raise PipError(
                "An abandoned PIP needs a reason. Without one it is "
                "indistinguishable from one that was quietly dropped, and the "
                "employee is left not knowing where they stand."
            )
        return CloseResult(
            outcome=outcome,
            new_status=PipStatus.ABANDONED,
            routes_to_exit=False,
            trail=trail,
            new_end_date=None,
            message=(
                f"Abandoned: {reason} This is not a failure by the employee and the record says so."
            ),
        )

    return CloseResult(
        outcome=PipOutcome.SUCCESSFUL,
        new_status=PipStatus.SUCCESSFUL,
        routes_to_exit=False,
        trail=trail,
        new_end_date=None,
        message=(
            "Successful. The employee returns to the normal appraisal cycle, "
            "and the plan stays on the record as evidence that it was run and "
            "met - which protects them as much as anybody."
        ),
    )


def evidence_pack(pip: PipFacts, *, today: date) -> dict[str, Any]:
    """Everything an employment adviser would ask for, in one payload.

    Assembled rather than summarised: the adviser reads the check-ins, and a
    précis of somebody's contemporaneous account is not their account.
    """
    return {
        "pip_id": pip.pip_id,
        "employee_id": pip.employee_id,
        "initiated_by": pip.initiated_by,
        "appraisal_id": pip.appraisal_id or None,
        "period": {
            "start": pip.start_date.isoformat(),
            "end": pip.end_date.isoformat(),
            "days": pip.duration_days(),
        },
        "reason": pip.reason,
        "expectations": pip.expectations,
        "support_provided": pip.support_provided,
        "status": pip.status.value,
        "milestones": [
            {
                "milestone_id": m.milestone_id,
                "description": m.description,
                "target_date": m.target_date.isoformat(),
                "measurable_criteria": m.measurable_criteria,
                "met": m.met,
            }
            for m in pip.milestones
        ],
        "check_ins": [
            {
                "check_in_id": c.check_in_id,
                "scheduled_for": c.scheduled_for.isoformat(),
                "held_on": c.held_on.isoformat() if c.held_on else None,
                "manager_comments": c.manager_comments,
                "employee_comments": c.employee_comments,
                "employee_input_state": c.employee_input_state.value,
                "decline_reason": c.decline_reason,
                "is_evidenced": c.is_evidenced,
            }
            for c in sorted(pip.check_ins, key=lambda c: c.scheduled_for)
        ],
        "trail": trail_quality(pip).as_dict(),
        "assembled_on": today.isoformat(),
        "note": (
            "The check-ins are reproduced in full rather than summarised. A "
            "précis of somebody's contemporaneous account is not their account."
        ),
    }
