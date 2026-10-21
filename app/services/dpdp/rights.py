"""Data-principal rights: clocks that run, and an erasure that tells the truth.

═══════════════════════════════════════════════════════════════════════════════
ERASURE IS THE HARD ONE AND THE HONEST ANSWER IS "PARTLY"
═══════════════════════════════════════════════════════════════════════════════

Somebody who left three years ago asks to be erased. Their payroll records
cannot go: the Income-tax rules require the books for six years from the end of
the assessment year, the EPF Act requires the contribution record, and if the
Department opens a TDS proceeding in year five the employer needs the evidence.
Their survey responses, their photo, their emergency contact and their talent-
pool record can all go today.

There are three ways to handle that and two of them are wrong:

1. **Delete everything.** Feels responsive; destroys records the employer is
   legally required to hold, and the person asking is not the one penalised.
2. **Refuse, citing retention.** Feels safe; is a blanket refusal to honour a
   statutory right, and most of what they asked about was never subject to a
   retention obligation at all.
3. **Purge what can be purged, retain what law requires, and say precisely
   which is which and under what obligation.** Longer to build. The only one
   that is both lawful and honest.

`reconcile_erasure` implements (3). Its output is designed to be read by the
person who asked — every retained category names the statute, the date the
obligation expires, and what will happen then.

**Nothing is ever silently ignored.** A category that is neither purged nor
retained-with-a-reason is a `ReconciliationError`, because the failure mode of a
missing branch here is a record quietly surviving an erasure request with
nobody able to say why.

═══════════════════════════════════════════════════════════════════════════════
THE CLOCKS
═══════════════════════════════════════════════════════════════════════════════

DPDP puts the obligation on the Data Fiduciary to publish and meet its own
response period, with the Rules setting an outer bound for grievance redressal.
So `SLA_DAYS` is a **default, not a constant** — a tenant may commit to
something shorter and cannot commit to something longer than the statutory
outer bound, which `validate_sla` enforces.

Pure. `today` and `now` are always supplied.
"""

from __future__ import annotations

import calendar
import enum
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.services.dpdp.categories import CATEGORIES, DataCategory, category

__all__ = [
    "STATUTORY_OUTER_BOUND_DAYS",
    "ErasureOutcome",
    "ErasurePlan",
    "ReconciliationError",
    "RequestClock",
    "RequestStatus",
    "RequestType",
    "categories_for_export",
    "clock_for",
    "escalations",
    "reconcile_erasure",
    "validate_sla",
]


class ReconciliationError(ValueError):
    """An erasure decision this module refuses to make silently."""


class RequestType(str, enum.Enum):
    """The rights a data principal can exercise.

    NOMINATION is the one people forget: DPDP s.14 lets a principal nominate
    somebody to exercise their rights if they die or become incapacitated. In an
    HRMS that is not theoretical — it is the person who will need the final
    settlement and the gratuity, and they need standing to ask for it.
    """

    ACCESS = "access"
    CORRECTION = "correction"
    ERASURE = "erasure"
    GRIEVANCE = "grievance"
    NOMINATION = "nomination"

    @property
    def label(self) -> str:
        return {
            RequestType.ACCESS: "Access to your information",
            RequestType.CORRECTION: "Correction of your information",
            RequestType.ERASURE: "Erasure of your information",
            RequestType.GRIEVANCE: "Grievance",
            RequestType.NOMINATION: "Nomination",
        }[self]


class RequestStatus(str, enum.Enum):
    RECEIVED = "received"
    #: We are not satisfied the requester is who they say. The clock keeps
    #: running: identity verification is our problem, not a way to pause it.
    VERIFYING_IDENTITY = "verifying_identity"
    IN_PROGRESS = "in_progress"
    #: Answered in full.
    COMPLETED = "completed"
    #: Answered in part, with the rest refused and the refusal explained. The
    #: normal outcome of an erasure request from a former employee.
    PARTIALLY_COMPLETED = "partially_completed"
    REJECTED = "rejected"
    #: Escalated past the first responder. Not a terminal state.
    ESCALATED = "escalated"

    @property
    def is_open(self) -> bool:
        return self in (
            RequestStatus.RECEIVED,
            RequestStatus.VERIFYING_IDENTITY,
            RequestStatus.IN_PROGRESS,
            RequestStatus.ESCALATED,
        )


#: The outer bound the DPDP Rules place on grievance redressal. A tenant's own
#: published period may be shorter and must not be longer, which `validate_sla`
#: enforces rather than trusting a configuration screen.
STATUTORY_OUTER_BOUND_DAYS: dict[RequestType, int] = {
    RequestType.ACCESS: 90,
    RequestType.CORRECTION: 90,
    RequestType.ERASURE: 90,
    RequestType.GRIEVANCE: 90,
    RequestType.NOMINATION: 90,
}

#: What we commit to by default. Deliberately well inside the bound: a response
#: period set at the legal maximum is a response period designed around the
#: penalty rather than around the person waiting.
DEFAULT_SLA_DAYS: dict[RequestType, int] = {
    RequestType.ACCESS: 30,
    RequestType.CORRECTION: 30,
    RequestType.ERASURE: 45,
    RequestType.GRIEVANCE: 30,
    RequestType.NOMINATION: 15,
}


def validate_sla(request_type: RequestType, days: int) -> int:
    """Check a tenant's chosen response period against the statutory bound."""
    bound = STATUTORY_OUTER_BOUND_DAYS[request_type]
    if days < 1:
        raise ValueError("A response period has to be at least one day.")
    if days > bound:
        raise ValueError(
            f"A {request_type.value} request must be answered within {bound} days. "
            f"{days} is longer than that, so it is not a period this tenant can "
            "publish — the outer bound is not ours to extend."
        )
    return days


@dataclass(frozen=True, slots=True)
class RequestClock:
    """When a request is due, and how close it is.

    `escalate_at` is deliberately before `due_by`. A clock that only tells you
    about a breach on the day of the breach is a clock that has already failed;
    the point of the escalation date is that somebody with authority hears about
    it while there is still time to answer.
    """

    request_type: RequestType
    received_on: date
    due_by: date
    escalate_at: date
    sla_days: int
    statutory_bound_days: int

    def days_remaining(self, today: date) -> int:
        return (self.due_by - today).days

    def is_overdue(self, today: date) -> bool:
        return today > self.due_by

    def needs_escalation(self, today: date) -> bool:
        return today >= self.escalate_at

    def as_dict(self, *, today: date | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "request_type": self.request_type.value,
            "received_on": self.received_on.isoformat(),
            "due_by": self.due_by.isoformat(),
            "escalate_at": self.escalate_at.isoformat(),
            "sla_days": self.sla_days,
            "statutory_bound_days": self.statutory_bound_days,
        }
        if today is not None:
            payload |= {
                "days_remaining": self.days_remaining(today),
                "is_overdue": self.is_overdue(today),
                "needs_escalation": self.needs_escalation(today),
            }
        return payload


def clock_for(
    request_type: RequestType,
    *,
    received_on: date,
    sla_days: int | None = None,
    escalate_after_fraction: float = 0.6,
) -> RequestClock:
    """Build the clock for one request.

    Escalation at 60% of the period rather than at a fixed number of days: a
    fifteen-day nomination request and a forty-five-day erasure request need
    warning at different points, and a constant would either shout on day two
    of the long one or arrive too late on the short one.
    """
    days = validate_sla(request_type, sla_days or DEFAULT_SLA_DAYS[request_type])
    due = received_on + timedelta(days=days)
    escalate = received_on + timedelta(days=max(1, int(days * escalate_after_fraction)))
    return RequestClock(
        request_type=request_type,
        received_on=received_on,
        due_by=due,
        escalate_at=escalate,
        sla_days=days,
        statutory_bound_days=STATUTORY_OUTER_BOUND_DAYS[request_type],
    )


@dataclass(frozen=True, slots=True)
class Escalation:
    """One request that needs somebody more senior to look at it."""

    request_id: str
    request_type: RequestType
    days_remaining: int
    is_overdue: bool
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "request_type": self.request_type.value,
            "days_remaining": self.days_remaining,
            "is_overdue": self.is_overdue,
            "message": self.message,
        }


def escalations(open_requests: list[tuple[str, RequestClock]], *, today: date) -> list[Escalation]:
    """Which open requests need escalating, worst first.

    Overdue ones sort ahead of approaching ones, and among overdue ones the
    longest overdue is first — a list ordered by receipt date buries the one
    that has been sitting for two months behind the ones that arrived this week.
    """
    out: list[Escalation] = []
    for request_id, clock in open_requests:
        if not clock.needs_escalation(today):
            continue
        remaining = clock.days_remaining(today)
        overdue = clock.is_overdue(today)
        if overdue:
            message = (
                f"{clock.request_type.label} received on "
                f"{clock.received_on.isoformat()} is {abs(remaining)} day(s) past "
                f"its {clock.sla_days}-day response commitment. The statutory "
                f"outer bound is {clock.statutory_bound_days} days from receipt."
            )
        else:
            message = (
                f"{clock.request_type.label} received on "
                f"{clock.received_on.isoformat()} is due in {remaining} day(s) and "
                "has not been answered."
            )
        out.append(
            Escalation(
                request_id=request_id,
                request_type=clock.request_type,
                days_remaining=remaining,
                is_overdue=overdue,
                message=message,
            )
        )
    return sorted(out, key=lambda e: (not e.is_overdue, e.days_remaining))


# ══════════════════════════════════════════════════════════════════════════════
# Erasure
# ══════════════════════════════════════════════════════════════════════════════


class ErasureOutcome(str, enum.Enum):
    """What happens to one category of a person's data.

    Four outcomes, and the difference between the middle two is the difference
    between "gone" and "unreadable but still there" — which the person asking
    is entitled to know, because it is not the same answer.
    """

    #: Deleted. The rows are gone.
    PURGED = "purged"
    #: Identifying values overwritten, the row kept. Used where a record has to
    #: survive for its aggregate or its integrity but need not name anybody —
    #: the audit trail, mainly.
    PSEUDONYMISED = "pseudonymised"
    #: Kept in full, because a statute requires it. Names the statute and the
    #: date the obligation ends.
    RETAINED_STATUTORY = "retained_statutory"
    #: Kept in full because a live legal claim or proceeding needs it. Distinct
    #: from a statutory period because it has no fixed end date and has to be
    #: reviewed by a person.
    RETAINED_LEGAL_HOLD = "retained_legal_hold"

    @property
    def is_retention(self) -> bool:
        return self in (
            ErasureOutcome.RETAINED_STATUTORY,
            ErasureOutcome.RETAINED_LEGAL_HOLD,
        )


@dataclass(frozen=True, slots=True)
class CategoryDecision:
    """What happens to one category, and the sentence the person is told."""

    category_key: str
    category_name: str
    outcome: ErasureOutcome
    explanation: str
    retained_until: date | None = None
    statutory_reference: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category_key,
            "category_name": self.category_name,
            "outcome": self.outcome.value,
            "explanation": self.explanation,
            "retained_until": (self.retained_until.isoformat() if self.retained_until else None),
            "statutory_reference": self.statutory_reference,
        }


@dataclass(frozen=True, slots=True)
class ErasurePlan:
    """The full answer to an erasure request, ready to be read by the person.

    `is_complete` is False whenever anything was retained, and the request
    status that follows from it is PARTIALLY_COMPLETED — never COMPLETED. A
    partial erasure reported as complete is the specific lie this module was
    written to make impossible.
    """

    decisions: tuple[CategoryDecision, ...]
    requested_on: date
    #: The last date any retention obligation expires. The date the person can
    #: come back and ask again with a different answer.
    review_again_on: date | None
    summary: str

    @property
    def purged(self) -> tuple[CategoryDecision, ...]:
        return tuple(d for d in self.decisions if d.outcome is ErasureOutcome.PURGED)

    @property
    def retained(self) -> tuple[CategoryDecision, ...]:
        return tuple(d for d in self.decisions if d.outcome.is_retention)

    @property
    def is_complete(self) -> bool:
        return not self.retained

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_on": self.requested_on.isoformat(),
            "is_complete": self.is_complete,
            "purged_count": len(self.purged),
            "retained_count": len(self.retained),
            "review_again_on": (self.review_again_on.isoformat() if self.review_again_on else None),
            "summary": self.summary,
            "decisions": [d.as_dict() for d in self.decisions],
        }


#: Categories whose rows survive an erasure with their identifying values
#: overwritten rather than being deleted. The audit trail is here because
#: deleting the audit rows about a person would also delete the record of the
#: erasure — the request would erase its own evidence.
PSEUDONYMISE_INSTEAD: frozenset[str] = frozenset({"audit_trail"})


def _obligation_ends(
    cat: DataCategory,
    *,
    last_working_day: date | None,
    financial_year_end: date | None,
    requested_on: date,
) -> date:
    """When a retention obligation expires, from the right starting point.

    Payroll retention runs from the end of the financial year, not from the
    payslip's date, and employment records run from the last working day. Using
    the record's creation date — the obvious shortcut — would release a March
    payslip nine months early every single year.
    """
    if cat.retention.clock_starts == "last_working_day" and last_working_day:
        start = last_working_day
    elif cat.retention.clock_starts == "end_of_financial_year" and financial_year_end:
        start = financial_year_end
    else:
        start = last_working_day or requested_on

    # Calendar months, not 30-day approximations: an obligation that expires on
    # "the same day of the month, N months on" is what a statute means, and a
    # day of drift either way is a day a record is held without a basis or
    # released with one still running.
    total = start.month - 1 + cat.retention.months
    year = start.year + total // 12
    month = total % 12 + 1
    # 31 January plus one month is 28 or 29 February, not 3 March.
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def reconcile_erasure(
    category_keys: tuple[str, ...],
    *,
    requested_on: date,
    last_working_day: date | None = None,
    financial_year_end: date | None = None,
    legal_holds: frozenset[str] = frozenset(),
) -> ErasurePlan:
    """Decide, per category, what can go and what has to stay.

    Every category gets a decision. A key this function does not recognise
    raises rather than being skipped, because a skipped category is a record
    that quietly survives an erasure request with nobody able to say why.
    """
    unknown = [key for key in category_keys if key not in CATEGORIES]
    if unknown:
        raise ReconciliationError(
            f"No decision can be made for {', '.join(sorted(unknown))} — not in "
            "the category catalogue. Every category has to reach a decision: a "
            "category with no branch is a record that survives an erasure "
            "request silently."
        )

    decisions: list[CategoryDecision] = []
    expiries: list[date] = []

    for key in category_keys:
        cat = category(key)

        if key in legal_holds:
            decisions.append(
                CategoryDecision(
                    category_key=key,
                    category_name=cat.name,
                    outcome=ErasureOutcome.RETAINED_LEGAL_HOLD,
                    explanation=(
                        f"{cat.name} is kept because it is subject to a legal hold "
                        "— an active claim, investigation or proceeding needs it. "
                        "This has no fixed end date; it is reviewed when the "
                        "matter closes, and you will be told when it does."
                    ),
                )
            )
            continue

        if cat.retention.is_statutory:
            ends = _obligation_ends(
                cat,
                last_working_day=last_working_day,
                financial_year_end=financial_year_end,
                requested_on=requested_on,
            )
            if ends > requested_on:
                expiries.append(ends)
                decisions.append(
                    CategoryDecision(
                        category_key=key,
                        category_name=cat.name,
                        outcome=ErasureOutcome.RETAINED_STATUTORY,
                        explanation=(
                            f"{cat.name} is kept until {ends.isoformat()} because "
                            f"{cat.retention.statutory_reference} requires your "
                            f"employer to hold it for {cat.retention.months} "
                            "months. This is not a decision your employer or "
                            "AskMira can waive on request. It will be deleted "
                            "after that date."
                        ),
                        retained_until=ends,
                        statutory_reference=cat.retention.statutory_reference,
                    )
                )
                continue
            # The obligation has already expired — this category is now
            # ordinary data and goes with the rest.

        if key in PSEUDONYMISE_INSTEAD:
            decisions.append(
                CategoryDecision(
                    category_key=key,
                    category_name=cat.name,
                    outcome=ErasureOutcome.PSEUDONYMISED,
                    explanation=(
                        f"{cat.name} is kept with everything identifying you "
                        "removed. The record that an action happened survives; "
                        "your name, email and the details in it do not. Deleting "
                        "these rows outright would also delete the record of this "
                        "erasure request being honoured."
                    ),
                )
            )
            continue

        decisions.append(
            CategoryDecision(
                category_key=key,
                category_name=cat.name,
                outcome=ErasureOutcome.PURGED,
                explanation=f"{cat.name} has been deleted.",
            )
        )

    retained = [d for d in decisions if d.outcome.is_retention]
    if retained:
        summary = (
            f"{len(decisions) - len(retained)} of {len(decisions)} categories of "
            f"your information have been deleted. {len(retained)} could not be, "
            "because your employer is required by law to keep them. Each one is "
            "listed below with the obligation it falls under and the date it "
            "expires."
        )
    else:
        summary = (
            f"All {len(decisions)} categories of your information have been "
            "deleted or stripped of anything identifying you."
        )

    return ErasurePlan(
        decisions=tuple(decisions),
        requested_on=requested_on,
        review_again_on=max(expiries) if expiries else None,
        summary=summary,
    )


def categories_for_export(
    principal_categories: tuple[str, ...],
) -> tuple[DataCategory, ...]:
    """The categories an access export must cover, in catalogue order.

    Ordered by the catalogue rather than by whatever the caller passed, so two
    exports of the same person's data are byte-comparable and a reviewer
    checking module coverage reads the same sections in the same places.
    """
    wanted = set(principal_categories)
    return tuple(CATEGORIES[key] for key in CATEGORIES if key in wanted)
