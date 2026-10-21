"""Breach notification: the clock, the template and the evidence trail.

═══════════════════════════════════════════════════════════════════════════════
BUILT BEFORE IT IS NEEDED, BECAUSE IT CANNOT BE DESIGNED DURING AN INCIDENT
═══════════════════════════════════════════════════════════════════════════════

At the moment a breach is discovered, the people who understand the system are
busy stopping it. Nobody in that room should be deciding what a notification
says, who signs it, or whether 72 hours runs from discovery or from containment.

So the clocks are constants here, the template is written in advance, and
`rehearse` produces a full dry run against a fictional incident — which is the
only way the answer to "have you rehearsed this" is yes.

═══════════════════════════════════════════════════════════════════════════════
TWO CLOCKS, AND THEY ARE NOT THE SAME
═══════════════════════════════════════════════════════════════════════════════

**Affected data principals: without delay.** Not "within 72 hours" — the DPDP
Rules require intimation to each affected principal without delay, in a form
they can understand, describing the breach and what they should do.

**The Data Protection Board: without delay, and a detailed report within 72
hours** of becoming aware. The detailed report is the one with the facts,
circumstances, mitigation and remediation.

Modelling them as one clock would produce a system that waits three days to
tell the people whose data it was, which is both wrong and the version most
teams build.

`becoming aware` is the start, and it is deliberately not "when we finished
investigating". An incident where somebody says "we did not really *know* until
Thursday" is the incident where the clock was already running on Monday.

Pure. `now` is always supplied.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.services.dpdp.categories import CATEGORIES, HIGH_HARM_CATEGORIES, category

__all__ = [
    "BOARD_DETAILED_REPORT_HOURS",
    "BreachClock",
    "BreachError",
    "BreachSeverity",
    "NotificationDraft",
    "assess_severity",
    "clock_from",
    "notification_to_principals",
    "outstanding_obligations",
    "rehearse",
]


class BreachError(ValueError):
    """A breach record this module will not accept as complete."""


#: Hours from becoming aware to the detailed report to the Board.
BOARD_DETAILED_REPORT_HOURS = 72

#: What "without delay" is operationalised as internally. Not a legal
#: interpretation — a target that makes "without delay" schedulable, and being
#: late against it is visible long before being late against the statute.
INTIMATION_TARGET_HOURS = 24


class BreachSeverity(str, enum.Enum):
    """How bad, judged on harm to people rather than on systems affected.

    A misconfigured bucket exposing one person's Aadhaar and bank details is
    more serious than a leak of ten thousand work email addresses, and a
    severity scale keyed on record counts gets that backwards.
    """

    #: Contained, no personal data reached anybody who should not have it.
    NEGLIGIBLE = "negligible"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    #: Identity-theft or financial-fraud potential; or any exposure of a
    #: category in HIGH_HARM_CATEGORIES at scale.
    SEVERE = "severe"

    @property
    def notify_principals(self) -> bool:
        """Whether affected people are told individually.

        NEGLIGIBLE is the only severity that does not, and it is defined as "no
        personal data reached anybody" — so this is not a discretion to stay
        quiet, it is the case where there is nobody to tell.
        """
        return self is not BreachSeverity.NEGLIGIBLE


def assess_severity(
    *,
    category_keys: tuple[str, ...],
    affected_principal_count: int,
    data_left_our_control: bool,
) -> BreachSeverity:
    """Grade an incident from what was exposed and to how many.

    `data_left_our_control` is separated from the count deliberately. An
    internal over-permission — a manager who could see a report they should not
    — is a real incident with a real obligation, and it is not the same as a
    dump on a forum. Grading both by count alone would either overstate the
    first or understate the second.
    """
    unknown = [key for key in category_keys if key not in CATEGORIES]
    if unknown:
        raise BreachError(
            f"Cannot grade an incident involving {', '.join(sorted(unknown))} — "
            "not in the category catalogue. An incident whose exposed categories "
            "are unclassified cannot be graded, and guessing low is how a "
            "notifiable breach goes unreported."
        )
    if affected_principal_count < 0:
        raise BreachError("An affected-principal count cannot be negative.")

    if not category_keys or affected_principal_count == 0:
        return BreachSeverity.NEGLIGIBLE

    high_harm = bool(set(category_keys) & HIGH_HARM_CATEGORIES)

    if not data_left_our_control:
        # Internal over-exposure. Still notifiable, graded a step down.
        if high_harm and affected_principal_count >= 100:
            return BreachSeverity.HIGH
        if high_harm:
            return BreachSeverity.MEDIUM
        return BreachSeverity.LOW

    if high_harm and affected_principal_count >= 100:
        return BreachSeverity.SEVERE
    if high_harm:
        return BreachSeverity.HIGH
    if affected_principal_count >= 1000:
        return BreachSeverity.HIGH
    return BreachSeverity.MEDIUM


@dataclass(frozen=True, slots=True)
class BreachClock:
    """The two deadlines, from the moment of becoming aware."""

    became_aware_at: datetime
    #: Internal target for telling affected people. "Without delay" made
    #: schedulable; the statute is not a fixed number of hours.
    intimation_target_at: datetime
    #: Statutory: the detailed report to the Board.
    board_report_due_at: datetime

    def hours_remaining_for_board(self, now: datetime) -> float:
        return (self.board_report_due_at - now).total_seconds() / 3600

    def board_report_overdue(self, now: datetime) -> bool:
        return now > self.board_report_due_at

    def intimation_overdue(self, now: datetime) -> bool:
        return now > self.intimation_target_at

    def as_dict(self, *, now: datetime | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "became_aware_at": self.became_aware_at.isoformat(),
            "intimation_target_at": self.intimation_target_at.isoformat(),
            "board_report_due_at": self.board_report_due_at.isoformat(),
            "board_report_window_hours": BOARD_DETAILED_REPORT_HOURS,
        }
        if now is not None:
            payload |= {
                "hours_remaining_for_board": round(self.hours_remaining_for_board(now), 1),
                "board_report_overdue": self.board_report_overdue(now),
                "intimation_overdue": self.intimation_overdue(now),
            }
        return payload


def clock_from(became_aware_at: datetime) -> BreachClock:
    """Both clocks, from the moment of becoming aware.

    Not from containment, and not from the end of the investigation. The
    temptation to start the clock later is strongest exactly when starting it
    later is least defensible.
    """
    return BreachClock(
        became_aware_at=became_aware_at,
        intimation_target_at=became_aware_at + timedelta(hours=INTIMATION_TARGET_HOURS),
        board_report_due_at=became_aware_at + timedelta(hours=BOARD_DETAILED_REPORT_HOURS),
    )


@dataclass(frozen=True, slots=True)
class NotificationDraft:
    """What one affected person is sent.

    Written to be read by somebody who is not a lawyer and is worried. Four
    questions in order: what happened, what of mine was in it, what could go
    wrong, what do I do now. Anything that does not answer one of those is
    padding, and padding in a breach notice reads as evasion.
    """

    subject: str
    body: str
    categories_named: tuple[str, ...]
    actions_advised: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "body": self.body,
            "categories_named": list(self.categories_named),
            "actions_advised": list(self.actions_advised),
        }


#: What we tell people to do, by category. Concrete actions only — "remain
#: vigilant" is not an action, it is the absence of one.
_ADVICE: dict[str, tuple[str, ...]] = {
    "financial": (
        "Watch your bank statement for transactions you do not recognise, and "
        "tell your bank that your account number may have been exposed.",
        "Do not act on any call or message asking you to confirm bank details — "
        "we will never ask you for them, and neither will your bank.",
    ),
    "statutory_identifier": (
        "You can lock your Aadhaar biometrics at myaadhaar.uidai.gov.in, which "
        "prevents it being used for authentication until you unlock it.",
        "If your PAN was involved, check your Form 26AS for entries you do not recognise.",
    ),
    "account_security": (
        "Change your AskMira password now.",
        "If you used the same password anywhere else, change it there too.",
    ),
    "health": (
        "There is no action you can take to un-disclose this, and we are sorry. "
        "If it causes you a problem at work, tell your HR team or use the "
        "grievance route below — this was our failure, not yours.",
    ),
    "compensation": (
        "Your salary details may be known to others. If you are approached by "
        "anybody who appears to know what you earn, treat it as suspicious.",
    ),
    "disciplinary": (
        "If this disclosure causes you difficulty, the grievance route below "
        "reaches somebody who can act on it.",
    ),
}

_DEFAULT_ADVICE = (
    "Be cautious about messages that refer to your employment or use details "
    "about you that a stranger should not know.",
)


def notification_to_principals(
    *,
    category_keys: tuple[str, ...],
    severity: BreachSeverity,
    what_happened: str,
    became_aware_at: datetime,
    remediation: str,
    grievance_contact: str,
) -> NotificationDraft:
    """Draft the message an affected person receives.

    `what_happened` and `remediation` are the incident's own facts and have to
    be supplied — a template that generated them would produce a notification
    that says nothing, which is the failure mode of every breach notice anybody
    has ever received and ignored.
    """
    if not severity.notify_principals:
        raise BreachError(
            "This incident is graded negligible — no personal data reached "
            "anybody outside its intended audience — so there is nobody to "
            "notify. If that grading is wrong, change the grading rather than "
            "sending a notification the assessment does not support."
        )
    if not what_happened.strip():
        raise BreachError(
            "A breach notification needs a description of what actually "
            "happened. A notice that only says 'an incident occurred' tells the "
            "person nothing they can act on."
        )
    if not remediation.strip():
        raise BreachError(
            "A breach notification needs to say what has been done about it. "
            "Without that, the reader has no reason to believe it has stopped."
        )

    unknown = [key for key in category_keys if key not in CATEGORIES]
    if unknown:
        raise BreachError(
            f"Cannot name {', '.join(sorted(unknown))} in a notification — not "
            "in the category catalogue."
        )

    names = tuple(category(key).name for key in category_keys)
    advice: list[str] = []
    for key in category_keys:
        advice.extend(_ADVICE.get(key, ()))
    if not advice:
        advice.extend(_DEFAULT_ADVICE)
    advice.append(
        f"If you have questions or this causes you a problem, contact {grievance_contact}."
    )

    listed = "\n".join(f"  - {name}" for name in names)
    actions = "\n".join(f"  - {line}" for line in advice)

    body = (
        f"We are writing to tell you that some information about you held in "
        f"AskMira was exposed. We found out on "
        f"{became_aware_at.strftime('%d %B %Y at %H:%M')}.\n\n"
        f"What happened\n{what_happened.strip()}\n\n"
        f"What of yours was involved\n{listed}\n\n"
        f"What we have done\n{remediation.strip()}\n\n"
        f"What you should do\n{actions}\n\n"
        "We are sorry. You did not do anything wrong here and you do not have "
        "to take any of these steps to be protected by us — they are things you "
        "can do to protect yourself as well."
    )

    return NotificationDraft(
        subject="Important: information about you was exposed",
        body=body,
        categories_named=names,
        actions_advised=tuple(advice),
    )


@dataclass(frozen=True, slots=True)
class Obligation:
    """One thing that still has to happen, and by when."""

    key: str
    description: str
    due_at: datetime
    is_done: bool
    is_overdue: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "description": self.description,
            "due_at": self.due_at.isoformat(),
            "is_done": self.is_done,
            "is_overdue": self.is_overdue,
        }


def outstanding_obligations(
    *,
    clock: BreachClock,
    severity: BreachSeverity,
    principals_notified_at: datetime | None,
    board_reported_at: datetime | None,
    now: datetime,
) -> list[Obligation]:
    """What is still owed on this incident, overdue first.

    Returns done obligations too, marked done. During an incident the question
    is as often "did anybody actually send that" as "what is left", and a list
    that hides the completed items cannot answer the first one.
    """
    obligations = [
        Obligation(
            key="board_detailed_report",
            description=(
                f"Detailed report to the Data Protection Board — the facts, the "
                f"circumstances, the mitigation and the remediation. Due within "
                f"{BOARD_DETAILED_REPORT_HOURS} hours of becoming aware."
            ),
            due_at=clock.board_report_due_at,
            is_done=board_reported_at is not None,
            is_overdue=board_reported_at is None and clock.board_report_overdue(now),
        )
    ]

    if severity.notify_principals:
        obligations.append(
            Obligation(
                key="intimate_principals",
                description=(
                    "Intimate every affected data principal, without delay, in a "
                    "form they can understand — what happened, what of theirs was "
                    "involved, and what they should do."
                ),
                due_at=clock.intimation_target_at,
                is_done=principals_notified_at is not None,
                is_overdue=(principals_notified_at is None and clock.intimation_overdue(now)),
            )
        )

    return sorted(obligations, key=lambda o: (o.is_done, not o.is_overdue, o.due_at))


def rehearse(*, now: datetime, grievance_contact: str) -> dict[str, Any]:
    """A full dry run against a fictional incident.

    Exists so "has the breach workflow been rehearsed end to end" has an answer
    that is not somebody's memory of a meeting. It grades a made-up incident,
    computes both clocks, drafts the notification and lists what would be owed —
    and it is exercised by the test suite on every run, so the answer stays yes
    rather than becoming yes-in-2026.

    Everything it returns is clearly fictional. It writes nothing.
    """
    categories = ("statutory_identifier", "financial", "identity")
    severity = assess_severity(
        category_keys=categories,
        affected_principal_count=412,
        data_left_our_control=True,
    )
    clock = clock_from(now - timedelta(hours=6))
    draft = notification_to_principals(
        category_keys=categories,
        severity=severity,
        what_happened=(
            "REHEARSAL — NOT A REAL INCIDENT. A misconfigured export job wrote a "
            "payroll file to a storage location that was readable without "
            "authentication for four hours."
        ),
        became_aware_at=clock.became_aware_at,
        remediation=(
            "REHEARSAL — NOT A REAL INCIDENT. The location was made private "
            "within eleven minutes of discovery, the file was deleted, and "
            "access logs were reviewed to identify what was read."
        ),
        grievance_contact=grievance_contact,
    )
    obligations = outstanding_obligations(
        clock=clock,
        severity=severity,
        principals_notified_at=None,
        board_reported_at=None,
        now=now,
    )
    return {
        "is_rehearsal": True,
        "scenario": "Payroll export briefly readable without authentication",
        "assessed_severity": severity.value,
        "affected_principal_count": 412,
        "categories": list(categories),
        "clock": clock.as_dict(now=now),
        "notification": draft.as_dict(),
        "obligations": [o.as_dict() for o in obligations],
    }
