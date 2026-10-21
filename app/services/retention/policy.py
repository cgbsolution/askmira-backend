"""Retention policies, and the floor a tenant cannot configure below.

═══════════════════════════════════════════════════════════════════════════════
A CONFIGURATION SCREEN THAT CAN BREAK THE LAW IS A BUG
═══════════════════════════════════════════════════════════════════════════════

A client asks to keep payroll records for two years instead of eight. They have
a reason — storage cost, a privacy policy they published, a consultant who told
them to. Every one of those reasons is outranked by the Income-tax Rules, and
the person configuring it will not be the person the Department writes to.

So `statutory_minimum_months` is a floor derived from the DPDP category
catalogue, not a number typed into a form, and `validate_policy` refuses
anything below it **with the statute named**. A refusal that says "invalid
value" teaches nobody anything; a refusal that says "Rule 6F requires 84 months
and you asked for 24" ends the conversation.

The floor can only move by editing `dpdp/categories.py`, which is where the
statutory references live and where a change gets a legal review rather than a
support ticket.

═══════════════════════════════════════════════════════════════════════════════
ANCHORS
═══════════════════════════════════════════════════════════════════════════════

`anchor_event` decides where the clock starts, and getting it wrong is worth
years. A payroll record anchored to its creation date is released nine months
early every single financial year; an employment record anchored to creation
rather than to the last working day is deleted while the person still works
here. The anchors are an enum rather than a string precisely so a typo cannot
silently become "creation date".

Pure. `today` is always supplied.
"""

from __future__ import annotations

import calendar
import enum
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.services.dpdp.categories import CATEGORIES, category
from app.services.retention.taxonomy import (
    RetentionAction,
    RetentionTier,
    action_for,
    tier_for,
)

__all__ = [
    "AnchorEvent",
    "PolicyError",
    "RetentionPolicy",
    "default_policies",
    "due_on",
    "statutory_minimum_months",
    "validate_policy",
]


class PolicyError(ValueError):
    """A retention policy this module will not accept."""


class AnchorEvent(str, enum.Enum):
    """Where the retention clock starts.

    Not interchangeable. Each of these is a different date for the same record
    and the gap between them is measured in years.
    """

    #: The row was written. Right for a log line, wrong for almost everything
    #: about an employee.
    RECORD_CREATED = "record_created"
    #: The last working day. Right for anything that describes the employment
    #: relationship, because the relationship is what the obligation attaches to.
    EMPLOYMENT_ENDED = "employment_ended"
    #: The end of the financial year the record falls in. Right for anything the
    #: Income-tax Rules or the EPF Act reach, because that is how those periods
    #: are counted.
    FINANCIAL_YEAR_END = "financial_year_end"
    #: The last time anybody looked at it. For data whose purpose is served by
    #: being available rather than by being complete — a cached export, a
    #: generated report.
    LAST_ACCESSED = "last_accessed"

    @property
    def description(self) -> str:
        return {
            AnchorEvent.RECORD_CREATED: "the day the record was created",
            AnchorEvent.EMPLOYMENT_ENDED: "the employee's last working day",
            AnchorEvent.FINANCIAL_YEAR_END: ("31 March of the financial year the record falls in"),
            AnchorEvent.LAST_ACCESSED: "the last time the record was read",
        }[self]


#: The category catalogue's own `clock_starts` strings, mapped onto anchors.
#: One place, so the two vocabularies cannot drift — a category naming a clock
#: this map does not know fails an invariant rather than silently defaulting to
#: the creation date, which is the anchor that releases things early.
_CLOCK_TO_ANCHOR: dict[str, AnchorEvent] = {
    "record_created": AnchorEvent.RECORD_CREATED,
    "last_working_day": AnchorEvent.EMPLOYMENT_ENDED,
    "end_of_financial_year": AnchorEvent.FINANCIAL_YEAR_END,
    "application_closed": AnchorEvent.RECORD_CREATED,
    "case_closed": AnchorEvent.RECORD_CREATED,
}


def anchor_for(category_key: str) -> AnchorEvent:
    cat = category(category_key)
    try:
        return _CLOCK_TO_ANCHOR[cat.retention.clock_starts]
    except KeyError:
        raise PolicyError(
            f"Category {category_key!r} anchors its retention to "
            f"{cat.retention.clock_starts!r}, which is not a known anchor. It "
            "would silently fall back to the creation date, which releases "
            "records early — add it to _CLOCK_TO_ANCHOR deliberately."
        ) from None


def statutory_minimum_months(category_key: str) -> int:
    """The floor, in months. Zero where no statute compels retention.

    Derived from the category's own retention record, so the only way to lower
    a floor is to edit the statutory reference beside it — which is a change
    that gets read by whoever reviews the lawful-basis mapping, not by whoever
    is closing a support ticket about storage cost.
    """
    cat = category(category_key)
    return cat.retention.months if cat.retention.is_statutory else 0


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """One tenant's rule for one category.

    `tenant_id=None` is the system default — the policy that applies to a tenant
    that has configured nothing. Defaults are the statutory floor exactly, never
    less and never a round number somebody liked, so an unconfigured tenant is
    lawful rather than lucky.
    """

    data_category_key: str
    retention_period_months: int
    anchor_event: AnchorEvent
    action: RetentionAction
    statutory_minimum_months: int
    legal_basis: str
    tenant_id: str | None = None
    active: bool = True

    @property
    def tier(self) -> RetentionTier:
        return tier_for(self.data_category_key)

    @property
    def is_system_default(self) -> bool:
        return self.tenant_id is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.data_category_key,
            "category_name": category(self.data_category_key).name,
            "tier": self.tier.value,
            "retention_period_months": self.retention_period_months,
            "anchor_event": self.anchor_event.value,
            "anchor_description": self.anchor_event.description,
            "action": self.action.value,
            "statutory_minimum_months": self.statutory_minimum_months,
            "has_statutory_floor": self.statutory_minimum_months > 0,
            "legal_basis": self.legal_basis,
            "is_system_default": self.is_system_default,
            "active": self.active,
        }


def validate_policy(
    *,
    data_category_key: str,
    retention_period_months: int,
    action: RetentionAction | None = None,
) -> RetentionPolicy:
    """Build a policy, refusing one that goes below the statutory floor.

    The refusal names the statute and both numbers. A message saying only that
    the value is invalid leaves the tenant to guess, and the guess is usually
    "try 25".
    """
    if data_category_key not in CATEGORIES:
        raise PolicyError(
            f"No data category {data_category_key!r}. A retention policy for a "
            "category nobody classified would run against nothing."
        )
    if retention_period_months < 1:
        raise PolicyError(
            "A retention period has to be at least one month. Zero would delete "
            "a record as it was written."
        )

    floor = statutory_minimum_months(data_category_key)
    cat = category(data_category_key)

    if retention_period_months < floor:
        raise PolicyError(
            f"{cat.name} has to be kept for at least {floor} months — "
            f"{cat.retention.statutory_reference} requires it. You asked for "
            f"{retention_period_months}. This is not a setting AskMira can "
            "unlock: the obligation is on your organisation, and shortening it "
            "here would not move it."
        )

    chosen = action or action_for(data_category_key)
    if (
        tier_for(data_category_key) is RetentionTier.CONSENT_BASED
        and chosen is not RetentionAction.DELETE
    ):
        raise PolicyError(
            f"{cat.name} is held on consent, so the only lawful action at "
            f"expiry is deletion. {chosen.value} keeps the value of data "
            "somebody told us to stop holding."
        )

    return RetentionPolicy(
        data_category_key=data_category_key,
        retention_period_months=retention_period_months,
        anchor_event=anchor_for(data_category_key),
        action=chosen,
        statutory_minimum_months=floor,
        legal_basis=(
            cat.retention.statutory_reference
            or f"Our own policy: {cat.retention.note or 'no statute applies'}"
        ),
    )


def default_policies() -> list[RetentionPolicy]:
    """The system defaults: the statutory floor, exactly.

    Not a round number, and not "the floor plus a safety margin" — a margin
    somebody added for comfort becomes a year of extra personal data held for no
    articulable reason, which is the DPDP side of the conflict.
    """
    return [
        validate_policy(
            data_category_key=key,
            retention_period_months=cat.retention.months,
        )
        for key, cat in CATEGORIES.items()
    ]


def _add_months(start: date, months: int) -> date:
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    # 31 January plus one month is 28 or 29 February, not 3 March.
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def _financial_year_end(reference: date) -> date:
    """31 March of the financial year the reference falls in.

    India's year runs April to March. Using 31 December would release every
    record three months early.
    """
    year = reference.year if reference.month > 3 else reference.year - 1
    return date(year + 1, 3, 31)


def due_on(
    policy: RetentionPolicy,
    *,
    record_created: date,
    employment_ended: date | None = None,
    last_accessed: date | None = None,
) -> date | None:
    """When this record's period expires, or `None` if the clock has not started.

    `None` is the important return. An employment-anchored policy applied to
    somebody still employed has no expiry date — not an expiry date far in the
    future, and certainly not one computed from the creation date. Returning a
    date there would schedule the deletion of a current employee's record.
    """
    if policy.anchor_event is AnchorEvent.EMPLOYMENT_ENDED:
        if employment_ended is None:
            return None
        anchor = employment_ended
    elif policy.anchor_event is AnchorEvent.FINANCIAL_YEAR_END:
        anchor = _financial_year_end(employment_ended or record_created)
    elif policy.anchor_event is AnchorEvent.LAST_ACCESSED:
        if last_accessed is None:
            return None
        anchor = last_accessed
    else:
        anchor = record_created

    return _add_months(anchor, policy.retention_period_months)
