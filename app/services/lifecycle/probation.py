"""Probation reminders, escalation, and what silence means.

═══════════════════════════════════════════════════════════════════════════════
DRIFT IS THE NORMAL FAILURE MODE
═══════════════════════════════════════════════════════════════════════════════

Unconfirmed employees drifting past their probation end date with nobody
noticing is not an edge case — it is what happens by default at every company
that does not actively prevent it. The employee assumes they are confirmed, the
manager assumes HR handled it, and it surfaces two years later when somebody
tries to work out their notice period or their gratuity eligibility.

So reminders escalate. The manager is asked, then asked again, then their
manager is told, then HR is told. Each step is a wider circle of people who
could act, and the last one is somebody whose job it is to notice.

**Default-on-silence is a tenant configuration with no default.** Some clients
auto-confirm — silence means the manager had no objection. Others require an
explicit decision — silence means nothing has happened. Both are defensible and
they lead to opposite outcomes for a real person, so the tenant has to choose,
and `SilencePolicy` has no member that means "we did not think about it".

Pure. `today` is always supplied.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

__all__ = [
    "ESCALATION_SCHEDULE",
    "DriftCase",
    "ProbationError",
    "ReminderStep",
    "SilencePolicy",
    "due_reminders",
    "resolve_silence",
]


class ProbationError(ValueError):
    """A probation configuration this module will not guess at."""


class SilencePolicy(str, enum.Enum):
    """What happens when the review date passes with no decision.

    Three members, and none of them is a default. A tenant that has not chosen
    gets an error at configuration time rather than a behaviour at review time —
    the two live options lead to opposite outcomes for a real person, and
    "whatever the software does" is not a decision anybody made.
    """

    #: Silence is consent. The employee is confirmed and told.
    AUTO_CONFIRM = "auto_confirm"
    #: Silence is nothing. The employee stays on probation and the case escalates
    #: until somebody decides.
    REQUIRE_DECISION = "require_decision"
    #: Silence extends probation by a stated period, once. After that it
    #: escalates like REQUIRE_DECISION — an extension nobody chose should not be
    #: renewable by continuing not to choose.
    AUTO_EXTEND_ONCE = "auto_extend_once"


class ReminderStep(str, enum.Enum):
    """Who is being asked, at each stage.

    Each step is a wider circle of people who could act, ending with somebody
    whose job it is to notice.
    """

    MANAGER_FIRST = "manager_first"
    MANAGER_SECOND = "manager_second"
    SKIP_LEVEL = "skip_level"
    HR = "hr"
    #: Past the end date with no decision. Not a reminder — a state.
    OVERDUE = "overdue"


#: Days *before* the review due date each reminder goes out. Negative means
#: after — the escalation continues past the deadline, because that is exactly
#: when a case stops being a reminder and starts being a problem.
#:
#: Fourteen days is the first nudge: long enough to arrange a conversation,
#: short enough that it is still the current fortnight's problem.
ESCALATION_SCHEDULE: tuple[tuple[int, ReminderStep], ...] = (
    (14, ReminderStep.MANAGER_FIRST),
    (5, ReminderStep.MANAGER_SECOND),
    (0, ReminderStep.SKIP_LEVEL),
    (-7, ReminderStep.HR),
)


@dataclass(frozen=True, slots=True)
class ReviewFacts:
    """One probation review, as it stands."""

    review_id: str
    employee_id: str
    employee_label: str
    probation_end_date: date
    review_due_date: date
    #: `None` while nobody has decided. That is the whole subject of this module.
    outcome: str | None = None
    manager_user_id: str | None = None
    skip_level_user_id: str | None = None
    #: Steps already sent, so a sweep run twice does not send twice.
    reminders_sent: frozenset[ReminderStep] = frozenset()
    #: Whether this review is itself already an extension. AUTO_EXTEND_ONCE only
    #: fires for a review that is not.
    extends_review_id: str | None = None

    @property
    def is_decided(self) -> bool:
        return self.outcome is not None

    def days_until_due(self, today: date) -> int:
        return (self.review_due_date - today).days


@dataclass(frozen=True, slots=True)
class Reminder:
    """One message to send, to one person, about one review."""

    review_id: str
    employee_label: str
    step: ReminderStep
    recipient_user_id: str | None
    days_until_due: int
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "employee": self.employee_label,
            "step": self.step.value,
            "recipient_user_id": self.recipient_user_id,
            "days_until_due": self.days_until_due,
            "message": self.message,
        }


def _recipient(review: ReviewFacts, step: ReminderStep) -> str | None:
    if step in (ReminderStep.MANAGER_FIRST, ReminderStep.MANAGER_SECOND):
        return review.manager_user_id
    if step is ReminderStep.SKIP_LEVEL:
        # Falls back to the manager rather than going nowhere. A skip-level
        # reminder with no skip level configured must not disappear.
        return review.skip_level_user_id or review.manager_user_id
    return None  # HR: resolved by the caller from the tenant's HR users.


def _message(review: ReviewFacts, step: ReminderStep, days: int) -> str:
    name = review.employee_label
    when = review.review_due_date.strftime("%d %b")

    if step is ReminderStep.MANAGER_FIRST:
        return (
            f"{name}'s probation review is due on {when}. Confirming, extending or "
            "ending probation is your decision — please have the conversation and "
            "record the outcome."
        )
    if step is ReminderStep.MANAGER_SECOND:
        return (
            f"{name}'s probation review is due in {days} day(s), on {when}, and has "
            "not been recorded. After the due date this escalates."
        )
    if step is ReminderStep.SKIP_LEVEL:
        return (
            f"{name}'s probation review was due today and no outcome has been "
            "recorded. Their manager has been reminded twice. Somebody needs to "
            "decide — an employee drifting past probation is one who does not know "
            "where they stand."
        )
    return (
        f"{name} is {abs(days)} day(s) past their probation review date with no "
        "decision recorded. This is the failure mode that surfaces two years later "
        "when somebody works out their notice period."
    )


def due_reminders(
    reviews: list[ReviewFacts], *, today: date
) -> list[Reminder]:
    """Which reminders should go out today.

    Only the furthest step that has come due and has not been sent, per review —
    a sweep that had not run for a fortnight would otherwise send four messages
    at once about the same person, which is how a reminder becomes something
    people filter.
    """
    out: list[Reminder] = []

    for review in reviews:
        if review.is_decided:
            continue

        days = review.days_until_due(today)
        due_steps = [
            step for threshold, step in ESCALATION_SCHEDULE if days <= threshold
        ]
        unsent = [step for step in due_steps if step not in review.reminders_sent]
        if not unsent:
            continue

        # The last one is the furthest along; earlier ones are past and sending
        # them now would be sending yesterday's nudge after today's escalation.
        step = unsent[-1]
        out.append(
            Reminder(
                review_id=review.review_id,
                employee_label=review.employee_label,
                step=step,
                recipient_user_id=_recipient(review, step),
                days_until_due=days,
                message=_message(review, step, days),
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class DriftCase:
    """A review that has gone past its date with nobody deciding."""

    review_id: str
    employee_id: str
    employee_label: str
    days_overdue: int
    reminders_sent: int
    #: What the tenant's policy says happens now.
    resolution: str
    #: Set where the policy resolves it automatically.
    auto_outcome: str | None = None
    new_probation_end_date: date | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "employee": self.employee_label,
            "days_overdue": self.days_overdue,
            "reminders_sent": self.reminders_sent,
            "resolution": self.resolution,
            "auto_outcome": self.auto_outcome,
            "new_probation_end_date": (
                self.new_probation_end_date.isoformat()
                if self.new_probation_end_date
                else None
            ),
        }


#: How long AUTO_EXTEND_ONCE extends by. Long enough for a conversation that
#: has not happened in weeks to actually happen; short enough that it is not a
#: second probation.
AUTO_EXTENSION_DAYS = 30


def resolve_silence(
    reviews: list[ReviewFacts],
    *,
    policy: SilencePolicy,
    today: date,
    grace_days: int = 0,
) -> list[DriftCase]:
    """What happens to reviews nobody decided, under this tenant's policy.

    The policy is a parameter with no default, because the two live options lead
    to opposite outcomes for a real person: under AUTO_CONFIRM they are confirmed
    and told; under REQUIRE_DECISION they stay on probation until somebody acts.
    A product that picked one silently would be making an employment decision on
    a client's behalf.
    """
    if not isinstance(policy, SilencePolicy):
        raise ProbationError(
            "A probation silence policy has to be chosen. Auto-confirming and "
            "requiring a decision lead to opposite outcomes for a real person, so "
            "there is no default — the tenant picks."
        )

    out: list[DriftCase] = []
    for review in reviews:
        if review.is_decided:
            continue
        overdue = (today - review.review_due_date).days - grace_days
        if overdue < 0:
            continue

        if policy is SilencePolicy.AUTO_CONFIRM:
            out.append(
                DriftCase(
                    review_id=review.review_id,
                    employee_id=review.employee_id,
                    employee_label=review.employee_label,
                    days_overdue=overdue,
                    reminders_sent=len(review.reminders_sent),
                    resolution=(
                        "Confirmed automatically. This tenant's policy treats silence "
                        "as consent; the employee is confirmed and told, and the "
                        "manager is told it happened without them."
                    ),
                    auto_outcome="confirm",
                )
            )
        elif policy is SilencePolicy.AUTO_EXTEND_ONCE and review.extends_review_id is None:
            new_end = review.probation_end_date + timedelta(days=AUTO_EXTENSION_DAYS)
            out.append(
                DriftCase(
                    review_id=review.review_id,
                    employee_id=review.employee_id,
                    employee_label=review.employee_label,
                    days_overdue=overdue,
                    reminders_sent=len(review.reminders_sent),
                    resolution=(
                        f"Extended automatically by {AUTO_EXTENSION_DAYS} days, once. "
                        "A follow-up review is scheduled; if that one also passes in "
                        "silence it escalates rather than extending again — an "
                        "extension nobody chose should not be renewable by continuing "
                        "not to choose."
                    ),
                    auto_outcome="extend",
                    new_probation_end_date=new_end,
                )
            )
        else:
            out.append(
                DriftCase(
                    review_id=review.review_id,
                    employee_id=review.employee_id,
                    employee_label=review.employee_label,
                    days_overdue=overdue,
                    reminders_sent=len(review.reminders_sent),
                    resolution=(
                        f"Waiting for a decision, {overdue} day(s) past the review "
                        "date. Nothing happens to this employee's status until "
                        "somebody records an outcome — which is this tenant's own "
                        "policy, and is why the escalation keeps going."
                    ),
                )
            )
    return out
