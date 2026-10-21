"""What the retention job would do tonight, and who has to say yes first.

═══════════════════════════════════════════════════════════════════════════════
AN UNATTENDED JOB DELETING FOUR THOUSAND RECORDS IS NOT A GOOD SURPRISE
═══════════════════════════════════════════════════════════════════════════════

The retention sweep is the most destructive thing in this product. It runs
unattended, it acts on rules written months earlier by somebody who has since
left, and its mistakes are not recoverable — a deleted payroll record is not
coming back from a 30-day backup a year later.

So three brakes, and none of them is optional:

**A legal hold beats every policy.** Not "unless the policy is statutory", not
"unless the hold has expired but nobody released it". Held is held. `plan` puts
the hold check first and an invariant asserts nothing can reorder it, because a
hold that is checked after a filter is a hold that missed the records the filter
dropped.

**Volume above a threshold needs a person.** Below it, the job runs. Above it,
the plan is produced and waits. The threshold is per tenant with a conservative
default, and the approval is recorded against the plan — so "who authorised
deleting four thousand attendance records" has an answer.

**Dry run is the default posture, not a debug flag.** `plan` never writes;
`Plan.execute_ready` is False until somebody has approved it where approval is
needed. Producing the plan and acting on it are different function calls in
different modules.

═══════════════════════════════════════════════════════════════════════════════
NOTHING FALLS THROUGH
═══════════════════════════════════════════════════════════════════════════════

Every candidate record reaches exactly one outcome: acted on, held, not yet due,
or blocked. A record that matched no branch would be a record silently retained
forever, which is the failure the whole slice exists to fix — so `plan` asserts
its own arithmetic and an invariant re-checks it against a fixture.

Pure. `today` is always supplied.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.services.retention.policy import RetentionPolicy, due_on
from app.services.retention.taxonomy import RetentionAction

__all__ = [
    "DEFAULT_APPROVAL_THRESHOLD",
    "CandidateRecord",
    "Disposition",
    "LegalHoldScope",
    "PlannedAction",
    "RetentionPlan",
    "approve",
    "plan",
]

#: Above this many records in one plan, a person approves before anything runs.
#: Two hundred is deliberately low: it is roughly a department, and the point of
#: the gate is that somebody looks at an unusual night, not that the number is
#: precisely calibrated.
DEFAULT_APPROVAL_THRESHOLD = 200


class Disposition(str, enum.Enum):
    """What happens to one candidate record. Exactly one applies.

    `BLOCKED` is separate from `NOT_YET_DUE` on purpose. Not-yet-due is the
    normal case and needs no attention; blocked means the policy could not be
    evaluated — usually an employment-anchored rule against somebody still
    employed — and a plan that folded the two together would hide the records
    nobody can ever schedule.
    """

    ACT = "act"
    #: Under a legal hold. Exempt from everything.
    HELD = "held"
    NOT_YET_DUE = "not_yet_due"
    #: The clock has not started, so there is no expiry date to compare against.
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class LegalHoldScope:
    """What a hold covers.

    Any of the three dimensions may be empty, meaning "all". A hold with all
    three empty covers the whole tenant, which is legitimate — a regulator
    freezing everything is a real instruction — and `covers` returns True for
    it rather than treating an empty scope as no scope. Getting that backwards
    would turn the broadest possible hold into no hold at all.
    """

    employee_ids: frozenset[str] = frozenset()
    departments: frozenset[str] = frozenset()
    from_date: date | None = None
    to_date: date | None = None

    def covers(self, record: CandidateRecord) -> bool:
        if self.employee_ids and record.subject_id not in self.employee_ids:
            return False
        if self.departments and record.department not in self.departments:
            return False
        if self.from_date and record.record_created < self.from_date:
            return False
        # Written as an early return rather than `return not (...)` so each
        # dimension reads the same way. A hold's scope check is somewhere a
        # reviewer has to be able to follow line by line.
        return not (self.to_date and record.record_created > self.to_date)


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    """One row the retention job is considering."""

    record_id: str
    table: str
    data_category_key: str
    subject_id: str
    record_created: date
    employment_ended: date | None = None
    last_accessed: date | None = None
    department: str | None = None


@dataclass(frozen=True, slots=True)
class PlannedAction:
    """One record and its outcome, with the reason in words."""

    record: CandidateRecord
    disposition: Disposition
    action: RetentionAction | None
    due_on: date | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record.record_id,
            "table": self.record.table,
            "category": self.record.data_category_key,
            "disposition": self.disposition.value,
            "action": self.action.value if self.action else None,
            "due_on": self.due_on.isoformat() if self.due_on else None,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    """What tonight's job would do. Produced by a function that writes nothing."""

    actions: tuple[PlannedAction, ...]
    evaluated_on: date
    approval_threshold: int
    is_dry_run: bool
    approved_by_user_id: str | None = None

    @property
    def to_act(self) -> tuple[PlannedAction, ...]:
        return tuple(a for a in self.actions if a.disposition is Disposition.ACT)

    @property
    def held(self) -> tuple[PlannedAction, ...]:
        return tuple(a for a in self.actions if a.disposition is Disposition.HELD)

    @property
    def blocked(self) -> tuple[PlannedAction, ...]:
        return tuple(a for a in self.actions if a.disposition is Disposition.BLOCKED)

    @property
    def needs_approval(self) -> bool:
        return len(self.to_act) > self.approval_threshold

    @property
    def execute_ready(self) -> bool:
        """Whether anything may actually run.

        A dry run is never ready. A plan over the threshold is ready only once
        somebody has approved it. Derived from both, so a caller cannot set a
        flag and skip the gate.
        """
        if self.is_dry_run:
            return False
        if self.needs_approval:
            return self.approved_by_user_id is not None
        return True

    def counts_by_action(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for planned in self.to_act:
            if planned.action:
                out[planned.action.value] = out.get(planned.action.value, 0) + 1
        return out

    def as_dict(self, *, sample_size: int = 20) -> dict[str, Any]:
        return {
            "evaluated_on": self.evaluated_on.isoformat(),
            "is_dry_run": self.is_dry_run,
            "total_evaluated": len(self.actions),
            "to_act": len(self.to_act),
            "held_under_legal_hold": len(self.held),
            "not_yet_due": sum(1 for a in self.actions if a.disposition is Disposition.NOT_YET_DUE),
            "blocked": len(self.blocked),
            "counts_by_action": self.counts_by_action(),
            "approval_threshold": self.approval_threshold,
            "needs_approval": self.needs_approval,
            "approved_by_user_id": self.approved_by_user_id,
            "execute_ready": self.execute_ready,
            # A sample, not the whole list. A plan over four thousand records
            # returned in full is a payload nobody reads, and the counts are
            # what the approver is actually deciding on.
            "sample": [a.as_dict() for a in self.to_act[:sample_size]],
            "blocked_sample": [a.as_dict() for a in self.blocked[:sample_size]],
        }


def plan(
    records: list[CandidateRecord],
    *,
    policies: dict[str, RetentionPolicy],
    legal_holds: list[LegalHoldScope],
    today: date,
    approval_threshold: int = DEFAULT_APPROVAL_THRESHOLD,
    is_dry_run: bool = True,
) -> RetentionPlan:
    """Decide what happens to every candidate record. Writes nothing.

    The order of the checks is the design:

    1. **Legal hold first.** Before the policy lookup, before the due-date
       arithmetic, before anything that could drop a record from consideration.
       A hold checked later is a hold that misses whatever an earlier filter
       already removed.
    2. Policy lookup — a category with no active policy is blocked, not
       defaulted. Defaulting to "keep" would be safe and would also mean a
       category nobody wrote a policy for silently retains forever, which is the
       exact failure this module exists to fix; it is reported instead.
    3. Due-date arithmetic, which may return `None` for a clock that has not
       started. That is blocked too, and named.
    """
    actions: list[PlannedAction] = []

    for record in records:
        # 1. Held beats everything, including a statutory expiry.
        holding = [hold for hold in legal_holds if hold.covers(record)]
        if holding:
            actions.append(
                PlannedAction(
                    record=record,
                    disposition=Disposition.HELD,
                    action=None,
                    due_on=None,
                    reason=(
                        "Under a legal hold. Exempt from every retention policy "
                        "until the hold is released — this is not overridden by "
                        "a policy, an expiry or an erasure request."
                    ),
                )
            )
            continue

        # 2. A category with no active policy is reported, never defaulted.
        policy = policies.get(record.data_category_key)
        if policy is None or not policy.active:
            actions.append(
                PlannedAction(
                    record=record,
                    disposition=Disposition.BLOCKED,
                    action=None,
                    due_on=None,
                    reason=(
                        f"No active retention policy for "
                        f"{record.data_category_key!r}, so nothing can be "
                        "scheduled. This record will be kept indefinitely until "
                        "somebody writes one."
                    ),
                )
            )
            continue

        # 3. The clock may not have started.
        expiry = due_on(
            policy,
            record_created=record.record_created,
            employment_ended=record.employment_ended,
            last_accessed=record.last_accessed,
        )
        if expiry is None:
            actions.append(
                PlannedAction(
                    record=record,
                    disposition=Disposition.BLOCKED,
                    action=None,
                    due_on=None,
                    reason=(
                        f"The policy is anchored to "
                        f"{policy.anchor_event.description}, which has not "
                        "happened for this record. There is no expiry date to "
                        "compare against yet."
                    ),
                )
            )
            continue

        if expiry > today:
            actions.append(
                PlannedAction(
                    record=record,
                    disposition=Disposition.NOT_YET_DUE,
                    action=policy.action,
                    due_on=expiry,
                    reason=(
                        f"Due on {expiry.isoformat()}, {(expiry - today).days} day(s) from now."
                    ),
                )
            )
            continue

        actions.append(
            PlannedAction(
                record=record,
                disposition=Disposition.ACT,
                action=policy.action,
                due_on=expiry,
                reason=(
                    f"Retention period of {policy.retention_period_months} "
                    f"months from {policy.anchor_event.description} expired on "
                    f"{expiry.isoformat()}. Action: {policy.action.value}."
                ),
            )
        )

    # Nothing falls through. Asserted rather than assumed, because a record with
    # no disposition is a record retained forever with no trace of the decision.
    if len(actions) != len(records):
        raise AssertionError(
            f"{len(records)} records went in and {len(actions)} came out. Every "
            "record has to reach exactly one disposition; one that reaches none "
            "is retained forever with nothing recording why."
        )

    return RetentionPlan(
        actions=tuple(actions),
        evaluated_on=today,
        approval_threshold=approval_threshold,
        is_dry_run=is_dry_run,
    )


def approve(plan_to_approve: RetentionPlan, *, user_id: str) -> RetentionPlan:
    """Record who authorised a plan above the threshold.

    Returns a new plan rather than mutating: the unapproved plan is what was
    shown to the approver, and keeping it intact is what makes "they approved
    this, and this is what it said" answerable later.
    """
    if plan_to_approve.is_dry_run:
        raise ValueError(
            "A dry run cannot be approved — it was produced to be read, not "
            "executed. Re-plan with is_dry_run=False if this is meant to run."
        )
    return RetentionPlan(
        actions=plan_to_approve.actions,
        evaluated_on=plan_to_approve.evaluated_on,
        approval_threshold=plan_to_approve.approval_threshold,
        is_dry_run=False,
        approved_by_user_id=user_id,
    )
