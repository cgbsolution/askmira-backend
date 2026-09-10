"""What a lifecycle event is, what it changes, and what that change drags with it.

═══════════════════════════════════════════════════════════════════════════════
A TRANSFER IS NOT A FIELD EDIT
═══════════════════════════════════════════════════════════════════════════════

Editing `employee.location_id` takes one keystroke and changes, in order: which
state's Professional Tax the person pays, which holiday calendar applies to
them, which shift they are rostered on, and where their geofence is. None of
those follow from the edit; all of them follow from the *transfer*, and the
person approving it should be told before they approve, not after payroll runs.

So an event carries its consequences with it. `consequences_of` is a pure
function from "what changed" to "what else changes", and the approval screen
shows the list. An approver who signs a cross-state transfer without knowing PT
changes is approving something they did not read.

**Previous values are recorded, not derived.** "What was their grade before the
promotion" has to be answerable from the event, because the employee record has
moved on and reconstructing it from an audit log is a reconstruction.

Pure. No database.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

__all__ = [
    "CHANGED_FIELDS",
    "Consequence",
    "EventType",
    "LifecycleError",
    "consequences_of",
    "describe",
    "validate_event",
]


class LifecycleError(ValueError):
    """An event this module will not model."""


class EventType(str, enum.Enum):
    """The things that actually happen to somebody's employment.

    Deliberately specific rather than a generic "change". A promotion and a
    redesignation both change the title; only one of them changes the pay, and
    an HR team that cannot tell them apart in a report cannot answer "how many
    people did we promote this year".
    """

    CONFIRMATION = "confirmation"
    PROBATION_EXTENSION = "probation_extension"
    TRANSFER = "transfer"
    PROMOTION = "promotion"
    DEMOTION = "demotion"
    #: A title change with no change in grade or pay. Common at the end of a
    #: financial year and frequently mistaken for a promotion in reporting.
    REDESIGNATION = "redesignation"
    DEPUTATION = "deputation"
    GRADE_CHANGE = "grade_change"
    DEPARTMENT_CHANGE = "department_change"
    LOCATION_CHANGE = "location_change"
    MANAGER_CHANGE = "manager_change"
    EMPLOYMENT_TYPE_CHANGE = "employment_type_change"

    @property
    def affects_pay(self) -> bool:
        """Whether this normally comes with a salary revision.

        "Normally" — a transfer sometimes does and a redesignation sometimes
        does. The flag drives what the approval screen *asks about*, not what it
        allows.
        """
        return self in (
            EventType.PROMOTION,
            EventType.DEMOTION,
            EventType.GRADE_CHANGE,
            EventType.CONFIRMATION,
        )

    @property
    def needs_a_letter(self) -> bool:
        """Whether the employee gets a document.

        Everything that changes their title, their grade, their location or
        their status does. A manager change does not: it is an internal routing
        change and a letter about it would puzzle the recipient.
        """
        return self is not EventType.MANAGER_CHANGE


#: What each event type is allowed to change on the employee record. An event
#: that could change anything is a field edit with a nicer name, and the whole
#: point is that it is not one.
CHANGED_FIELDS: dict[EventType, frozenset[str]] = {
    EventType.CONFIRMATION: frozenset({"status", "probation_end_date"}),
    EventType.PROBATION_EXTENSION: frozenset({"probation_end_date"}),
    EventType.TRANSFER: frozenset(
        {"location_id", "department_id", "manager_id", "designation"}
    ),
    EventType.PROMOTION: frozenset({"grade", "designation", "manager_id"}),
    EventType.DEMOTION: frozenset({"grade", "designation"}),
    EventType.REDESIGNATION: frozenset({"designation"}),
    EventType.DEPUTATION: frozenset({"location_id", "department_id", "manager_id"}),
    EventType.GRADE_CHANGE: frozenset({"grade"}),
    EventType.DEPARTMENT_CHANGE: frozenset({"department_id", "manager_id"}),
    EventType.LOCATION_CHANGE: frozenset({"location_id"}),
    EventType.MANAGER_CHANGE: frozenset({"manager_id"}),
    EventType.EMPLOYMENT_TYPE_CHANGE: frozenset({"employment_type", "notice_period_days"}),
}


class Severity(str, enum.Enum):
    """How much an approver needs to read this line.

    PAYROLL is separated from INFORMATIONAL because a consequence that changes
    what somebody is paid is a different kind of thing from one that changes
    which holidays they get, and a single "warning" style flattens them.
    """

    PAYROLL = "payroll"
    OPERATIONAL = "operational"
    INFORMATIONAL = "informational"


@dataclass(frozen=True, slots=True)
class Consequence:
    """One thing that follows from an event, in words.

    Written as a sentence rather than a code, because the reader is an approver
    on a Tuesday afternoon and "PT_STATE_CHANGE" tells them nothing.
    """

    kind: str
    severity: Severity
    message: str
    #: What the caller has to do about it on effect. `None` means the system
    #: handles it; a string is an instruction for a person.
    action_required: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity.value,
            "message": self.message,
            "action_required": self.action_required,
        }


@dataclass(frozen=True, slots=True)
class LocationFacts:
    """What a location implies. Supplied by the caller, so this stays pure."""

    location_id: str
    name: str
    state_code: str
    timezone_name: str = "Asia/Kolkata"


@dataclass(frozen=True, slots=True)
class EventFacts:
    """One proposed event, before anything has been applied."""

    event_type: EventType
    employee_id: str
    effective_from: date
    previous_values: dict[str, Any] = field(default_factory=dict)
    new_values: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    #: Set when the event carries a pay change. Separate from `new_values`
    #: because salary is its own effective-dated series, not a field on the
    #: employee.
    new_ctc_annual: Decimal | None = None
    previous_location: LocationFacts | None = None
    new_location: LocationFacts | None = None
    #: How many approvals the outgoing manager is currently holding. A manager
    #: change that orphans them is the failure this number exists to prevent.
    pending_approvals_held: int = 0

    @property
    def is_retrospective(self) -> bool:
        return False  # decided against `today` by the caller; see `describe`


def validate_event(facts: EventFacts, *, today: date) -> None:
    """Refuse an event that cannot mean what it says.

    Retrospective events are allowed — a promotion agreed in April and processed
    in June is genuinely effective in April, and the arrears engine exists for
    exactly that. What is refused is an event that changes nothing, or changes
    something its own type has no business changing.
    """
    if not facts.new_values and facts.new_ctc_annual is None:
        raise LifecycleError(
            f"This {facts.event_type.value} changes nothing. An event that records no "
            "change is a letter with no content behind it."
        )

    allowed = CHANGED_FIELDS[facts.event_type]
    unexpected = set(facts.new_values) - allowed
    if unexpected:
        raise LifecycleError(
            f"A {facts.event_type.value} cannot change {sorted(unexpected)}. It is "
            f"allowed to change {sorted(allowed)}. Raise the other changes as their own "
            "events — an event that can change anything is a field edit with a nicer "
            "name, and the point of these is that they are not field edits."
        )

    unchanged = [
        key
        for key, value in facts.new_values.items()
        if key in facts.previous_values and facts.previous_values[key] == value
    ]
    if unchanged and len(unchanged) == len(facts.new_values):
        raise LifecycleError(
            f"Every value on this {facts.event_type.value} is the same as the current "
            f"one ({sorted(unchanged)}). Nothing would change."
        )

    if not facts.reason.strip():
        raise LifecycleError(
            f"A {facts.event_type.value} needs a reason. It goes on the letter, into "
            "the employee's own history, and into whatever conversation happens if the "
            "decision is ever questioned."
        )

    if facts.new_ctc_annual is not None and facts.new_ctc_annual <= 0:
        raise LifecycleError("A salary revision has to be greater than zero.")

    # An effective date years out is almost always a typo for the current year,
    # and a promotion silently scheduled for 2036 is one nobody chases.
    if (facts.effective_from - today).days > 730:
        raise LifecycleError(
            f"This event is dated {facts.effective_from}, more than two years away. "
            "That is almost always a typo for this year — and if it is not, nothing "
            "will chase it between now and then."
        )


def consequences_of(facts: EventFacts) -> list[Consequence]:
    """Everything that follows from this event, for the approval screen.

    The point of this function is that an approver signing a cross-state
    transfer is told, before signing, that Professional Tax changes. Discovering
    it when payroll runs is discovering it from the employee.
    """
    out: list[Consequence] = []

    previous = facts.previous_location
    new = facts.new_location

    if new is not None and previous is not None and new.state_code != previous.state_code:
        # The big one. PT is a state tax with different slabs, different
        # frequencies and in some states no tax at all.
        out.append(
            Consequence(
                kind="professional_tax",
                severity=Severity.PAYROLL,
                message=(
                    f"Professional Tax changes from the {previous.state_code} rules to "
                    f"the {new.state_code} rules. The deduction on their payslip will "
                    "change, and in some states there is no PT at all — either way it "
                    "is a different number from the month this takes effect."
                ),
                action_required=(
                    "Check the employee's PT registration for the new state is in "
                    "place before the first payroll after the effective date."
                ),
            )
        )
        out.append(
            Consequence(
                kind="holiday_calendar",
                severity=Severity.OPERATIONAL,
                message=(
                    f"Their holiday calendar changes from {previous.state_code} to "
                    f"{new.state_code}. State holidays differ substantially across "
                    "India — leave already approved on a day that is no longer a "
                    "holiday, or vice versa, needs looking at."
                ),
                action_required=(
                    "Review any approved leave between the effective date and the end "
                    "of the year."
                ),
            )
        )

    if new is not None and previous is not None and new.location_id != previous.location_id:
        out.append(
            Consequence(
                kind="geofence",
                severity=Severity.OPERATIONAL,
                message=(
                    f"Their work location moves from {previous.name} to {new.name}. "
                    "Geofenced attendance will start expecting them at the new site — "
                    "before the effective date, punches from there would be flagged."
                ),
                action_required=None,
            )
        )
        if new.timezone_name != previous.timezone_name:
            out.append(
                Consequence(
                    kind="timezone",
                    severity=Severity.OPERATIONAL,
                    message=(
                        f"The site's timezone changes from {previous.timezone_name} to "
                        f"{new.timezone_name}. Shift start times and attendance "
                        "boundaries are computed in the site's own zone."
                    ),
                )
            )
        out.append(
            Consequence(
                kind="shift",
                severity=Severity.INFORMATIONAL,
                message=(
                    "Their shift may not exist at the new location. A transfer that "
                    "leaves somebody on a shift their new site does not run produces "
                    "attendance exceptions from day one."
                ),
                action_required="Assign a shift at the new location.",
            )
        )

    if facts.new_ctc_annual is not None:
        out.append(
            Consequence(
                kind="salary_revision",
                severity=Severity.PAYROLL,
                message=(
                    f"A salary revision to {facts.new_ctc_annual} takes effect on "
                    f"{facts.effective_from}. The full breakup is resolved through the "
                    "same structure payroll runs, so the letter and the payslip agree."
                ),
            )
        )

    if "manager_id" in facts.new_values and facts.pending_approvals_held:
        out.append(
            Consequence(
                kind="pending_approvals",
                severity=Severity.OPERATIONAL,
                message=(
                    f"Their manager changes, and the outgoing manager currently holds "
                    f"{facts.pending_approvals_held} pending approval(s) for them. "
                    "Those move to the new manager on effect — left behind, they sit "
                    "with somebody who no longer has any reason to look at them."
                ),
            )
        )
    elif "manager_id" in facts.new_values:
        out.append(
            Consequence(
                kind="approval_routing",
                severity=Severity.INFORMATIONAL,
                message=(
                    "Future leave and expense approvals will route to the new manager "
                    "from the effective date."
                ),
            )
        )

    if "employment_type" in facts.new_values:
        out.append(
            Consequence(
                kind="statutory_eligibility",
                severity=Severity.PAYROLL,
                message=(
                    "Changing employment type can change PF, ESI and gratuity "
                    "eligibility. A contractor moving to full-time starts accruing "
                    "gratuity from this date, not from when they first walked in."
                ),
                action_required="Confirm the statutory position with payroll.",
            )
        )

    if facts.event_type is EventType.CONFIRMATION:
        out.append(
            Consequence(
                kind="notice_period",
                severity=Severity.INFORMATIONAL,
                message=(
                    "A confirmed employee's notice period is usually longer than a "
                    "probationer's. Check the employment terms if it should change."
                ),
            )
        )

    return out


def retrospective_note(facts: EventFacts, *, today: date) -> Consequence | None:
    """An event effective in the past drags arrears behind it.

    Not refused — a promotion agreed in April and processed in June genuinely is
    effective in April, and this is exactly what the arrears engine exists for.
    Flagged, because the approver needs to know they are approving back pay.
    """
    if facts.effective_from >= today:
        return None

    days = (today - facts.effective_from).days
    if facts.new_ctc_annual is None:
        return Consequence(
            kind="retrospective",
            severity=Severity.INFORMATIONAL,
            message=(
                f"This event is effective {days} day(s) ago. The employee record will "
                "show the change as having applied from that date."
            ),
        )
    return Consequence(
        kind="retrospective_pay",
        severity=Severity.PAYROLL,
        message=(
            f"This is a backdated pay change, effective {days} day(s) ago. Arrears will "
            "be due from the effective date, and they are calculated against the rule "
            "versions in force in each affected month rather than today's."
        ),
        action_required="Run an arrears batch after this event is effected.",
    )


def describe(facts: EventFacts, *, today: date) -> dict[str, Any]:
    """The whole approval payload: what changes, and what that drags with it."""
    consequences = consequences_of(facts)
    retrospective = retrospective_note(facts, today=today)
    if retrospective is not None:
        consequences.insert(0, retrospective)

    return {
        "event_type": facts.event_type.value,
        "effective_from": facts.effective_from.isoformat(),
        "is_retrospective": facts.effective_from < today,
        "is_future_dated": facts.effective_from > today,
        "changes": [
            {
                "field": key,
                "from": _plain(facts.previous_values.get(key)),
                "to": _plain(value),
            }
            for key, value in sorted(facts.new_values.items())
        ],
        "salary_revision": (
            str(facts.new_ctc_annual) if facts.new_ctc_annual is not None else None
        ),
        "reason": facts.reason,
        "needs_a_letter": facts.event_type.needs_a_letter,
        "consequences": [c.as_dict() for c in consequences],
        "payroll_consequences": sum(
            1 for c in consequences if c.severity is Severity.PAYROLL
        ),
    }


def _plain(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if value is None or isinstance(value, str | int | bool | float):
        return value
    return str(value)
