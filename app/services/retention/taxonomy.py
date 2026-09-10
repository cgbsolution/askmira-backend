"""The three tiers. This file is the answer to the security reviewer's question.

═══════════════════════════════════════════════════════════════════════════════
TWO OBLIGATIONS THAT POINT IN OPPOSITE DIRECTIONS
═══════════════════════════════════════════════════════════════════════════════

DPDP s.8(7): a Data Fiduciary must erase personal data once the purpose is no
longer served, unless retention is necessary for compliance with law.

The Income-tax Act, the EPF Act, the Payment of Wages Act and every state's
Shops & Establishments rules: keep the register.

Both are binding. A product that resolves the tension by keeping everything is
in breach of the first; a product that resolves it by honouring every erasure
request in full is in breach of the second and exposes its customer to a penalty
the customer never agreed to. So the resolution is a taxonomy, and every
category of personal data belongs to exactly one tier.

**Tier 1 — STATUTORY_RETAINED.** A statute names this and compels retention.
Payroll registers, PF/ESI/TDS records, Form 16, POSH case files — and, less
obviously, the employee, attendance and leave registers, which every state's
Shops & Establishments rules require. An erasure request against these is
refused *in part*, with the statute and the expiry date named. Never silently
ignored.

**Tier 2 — OPERATIONALLY_RETAINED.** No statute compels them, but deleting them
outright destroys aggregate history the business legitimately needs — a
headcount trend with a hole in it is not a trend.

**Tier 3 — CONSENT_BASED.** Held on permission: culture survey responses, AI
processing inputs, candidate data past its stated window. Deleted on withdrawal
or on expiry, whichever comes first.

═══════════════════════════════════════════════════════════════════════════════
THE TIER AND THE ACTION ARE DIFFERENT QUESTIONS
═══════════════════════════════════════════════════════════════════════════════

The first version of this file conflated them, and its own consistency check
caught it: `attendance` and `leave` were filed as operational because we want
them *anonymised* rather than deleted at the end — while both cite a statutory
register. Filing them as operational would have let the job anonymise an
attendance register a labour inspector could still ask for.

They are two axes:

- **The tier** answers *why do we still hold this*. It is **derived** from the
  category's own lawful basis and retention record, so it cannot contradict
  them. There is no hand-written tier map to fall out of step.
- **The action at expiry** answers *what happens once every obligation has run
  out*, and that is a genuine judgement, declared per category below with its
  reason.

A statutory attendance register is therefore tier 1 — untouchable until the
register period ends — and then anonymised rather than deleted, because the
headcount and absence trends built on it outlive the obligation to keep names
against them.

Pure.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from app.services.dpdp.categories import CATEGORIES, DataCategory, LawfulBasis, category

__all__ = [
    "ACTION_AT_EXPIRY",
    "BACKUP_DISCLOSURE",
    "RetentionAction",
    "RetentionTier",
    "TaxonomyError",
    "action_for",
    "tier_for",
    "trust_centre_taxonomy",
]


class TaxonomyError(ValueError):
    """A category with no action, or one whose action contradicts its tier."""


class RetentionTier(str, enum.Enum):
    """Why we still hold something. Derived, never hand-assigned."""

    #: A statute compels retention. An erasure request is refused in part, with
    #: the statute and the expiry named.
    STATUTORY_RETAINED = "statutory_retained"
    #: Nothing compels retention, but the aggregate is legitimately needed.
    OPERATIONALLY_RETAINED = "operationally_retained"
    #: Held on permission. Deleted on withdrawal or expiry, whichever is first.
    CONSENT_BASED = "consent_based"

    @property
    def survives_an_erasure_request(self) -> bool:
        return self is RetentionTier.STATUTORY_RETAINED

    @property
    def explanation(self) -> str:
        return {
            RetentionTier.STATUTORY_RETAINED: (
                "Your employer is required by law to keep this for a set period. "
                "We cannot delete it on request, and we will tell you which law "
                "and until when."
            ),
            RetentionTier.OPERATIONALLY_RETAINED: (
                "No law requires this, but the business needs the totals it "
                "feeds. At the end of the period it is dealt with as described "
                "below — usually anonymised rather than deleted, so the record "
                "stays and stops being about you."
            ),
            RetentionTier.CONSENT_BASED: (
                "We hold this because you allowed it. Withdraw, or let the "
                "period run out, and it is deleted."
            ),
        }[self]


class RetentionAction(str, enum.Enum):
    """What happens once every obligation on a category has expired."""

    DELETE = "delete"
    ANONYMISE = "anonymise"
    #: Moved out of the live system into cold storage, still identifiable.
    #: For a statutory period long enough that the data is not wanted daily but
    #: cannot be touched.
    ARCHIVE = "archive"

    @property
    def is_reversible(self) -> bool:
        """Whether the data can come back. Only an archive can."""
        return self is RetentionAction.ARCHIVE


def tier_for(category_key: str) -> RetentionTier:
    """Which tier a category is in.

    Derived from facts already recorded on the category, in this order:

    1. A statutory reference on the retention record puts it in tier 1. That is
       the only fact that can override a person's erasure request, so it is
       checked first and nothing can demote it.
    2. A lawful basis of consent puts it in tier 3.
    3. Everything else is tier 2.

    Derived rather than declared so the tier cannot disagree with the category —
    a hand-written map is exactly what fell out of step in the first version.
    """
    cat = category(category_key)
    if cat.retention.is_statutory:
        return RetentionTier.STATUTORY_RETAINED
    if cat.lawful_basis is LawfulBasis.CONSENT:
        return RetentionTier.CONSENT_BASED
    return RetentionTier.OPERATIONALLY_RETAINED


#: What happens to each category once every obligation on it has expired. This
#: is the judgement half, so it is written down one category at a time.
#:
#: The rule the invariant enforces: **a consent-based category must be DELETE**.
#: Anonymising your way out of a withdrawn consent keeps the value of data you
#: were told to stop holding, and the person who withdrew would not recognise
#: that as honouring their request.
ACTION_AT_EXPIRY: dict[str, RetentionAction] = {
    # Registers and money. Once the statutory period is over there is nothing
    # left to argue for keeping a name against a rupee figure, so these go.
    "compensation": RetentionAction.DELETE,
    "financial": RetentionAction.DELETE,
    "statutory_identifier": RetentionAction.DELETE,
    "tax": RetentionAction.DELETE,
    "dependent": RetentionAction.DELETE,
    # Archived rather than deleted: a POSH file and the audit trail are the
    # evidence that a process was followed, and the reason to reach for them
    # arrives long after anybody expects it.
    "disciplinary": RetentionAction.ARCHIVE,
    "audit_trail": RetentionAction.ARCHIVE,
    "governance_record": RetentionAction.ARCHIVE,
    # Health data has no aggregate value worth keeping and the highest harm if
    # it leaks. Deleted.
    "health": RetentionAction.DELETE,
    # Anonymised: the trends built on these outlive the obligation to hold a
    # name against them. Headcount by department, absence rates, tenure curves.
    "identity": RetentionAction.ANONYMISE,
    "organisational": RetentionAction.ANONYMISE,
    "attendance": RetentionAction.ANONYMISE,
    "leave": RetentionAction.ANONYMISE,
    "performance": RetentionAction.ANONYMISE,
    # A document is a file with somebody's name on the front. There is no
    # anonymised version of a relieving letter, so it goes.
    "documents": RetentionAction.DELETE,
    "account_security": RetentionAction.DELETE,
    "communication": RetentionAction.ANONYMISE,
    # Consent-based. All DELETE, and the invariant enforces that.
    "culture_response": RetentionAction.DELETE,
    "ai_interaction": RetentionAction.DELETE,
    "recruitment": RetentionAction.DELETE,
    "background_check": RetentionAction.DELETE,
}


def action_for(category_key: str) -> RetentionAction:
    """What happens to this category at expiry, or an error naming it."""
    try:
        return ACTION_AT_EXPIRY[category_key]
    except KeyError:
        raise TaxonomyError(
            f"No expiry action for {category_key!r}. Every category needs one — "
            "an unassigned one falls through the retention job silently, and "
            "'we forgot to classify it' becomes indistinguishable from 'we "
            "decided to keep it forever'."
        ) from None


def contradictions() -> list[str]:
    """Where the taxonomy disagrees with itself. Read by an invariant.

    Two rules, and both were written because the first version broke one of
    them:

    - Every category has an expiry action.
    - A consent-based category's action is DELETE. Anonymising instead keeps
      the value of data somebody told us to stop holding.
    """
    out: list[str] = []

    for key in CATEGORIES:
        if key not in ACTION_AT_EXPIRY:
            out.append(f"{key} has no expiry action.")
            continue
        tier = tier_for(key)
        action = ACTION_AT_EXPIRY[key]
        if tier is RetentionTier.CONSENT_BASED and action is not RetentionAction.DELETE:
            out.append(
                f"{key} is held on consent and its expiry action is "
                f"{action.value}. Anonymising instead of deleting keeps the "
                "value of data somebody told us to stop holding, and the person "
                "who withdrew would not call that honouring their request."
            )

    for key in ACTION_AT_EXPIRY:
        if key not in CATEGORIES:
            out.append(f"{key} has an expiry action but is not a data category.")

    return out


@dataclass(frozen=True, slots=True)
class TierEntry:
    """One category, as the Trust Center shows it."""

    category: DataCategory
    tier: RetentionTier
    action: RetentionAction

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.key,
            "name": self.category.name,
            "tier": self.tier.value,
            "tier_explanation": self.tier.explanation,
            "action_at_expiry": self.action.value,
            "retention_months": self.category.retention.months,
            "clock_starts": self.category.retention.clock_starts,
            "statutory_reference": self.category.retention.statutory_reference,
            "survives_an_erasure_request": self.tier.survives_an_erasure_request,
        }


def entries() -> list[TierEntry]:
    return [
        TierEntry(
            category=CATEGORIES[key],
            tier=tier_for(key),
            action=action_for(key),
        )
        for key in CATEGORIES
        if key in ACTION_AT_EXPIRY
    ]


def trust_centre_taxonomy() -> dict[str, Any]:
    """The taxonomy, written for somebody evaluating whether to trust us.

    Grouped by tier rather than listed alphabetically, because the grouping is
    the argument. A reviewer wants to know which of their employees' data
    survives a deletion request and why, and an A-to-Z list of twenty-one
    categories does not tell them.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries():
        grouped.setdefault(entry.tier.value, []).append(entry.as_dict())

    return {
        "the_conflict": (
            "DPDP requires us to erase personal data once its purpose is served. "
            "Indian tax, provident fund and labour law require your employer to "
            "keep payroll and statutory records for years. Both are binding. "
            "Keeping everything breaches the first; deleting everything on "
            "request breaches the second and exposes you to a penalty you did "
            "not choose. Here is how we resolve it, category by category."
        ),
        "tiers": [
            {
                "tier": tier.value,
                "explanation": tier.explanation,
                "categories": grouped.get(tier.value, []),
            }
            for tier in RetentionTier
        ],
        "anonymisation_is_not_deletion": (
            "Where we say anonymised, we mean the row stays and stops being "
            "about a person: direct identifiers are replaced with a surrogate "
            "that does not map back, and quasi-identifiers are generalised — "
            "date of birth to a year, location to a city, salary to a band. It "
            "is not deletion and we do not describe it as deletion. Where a "
            "surviving record would be the only one in its group, it is "
            "suppressed instead, because a record of one is not anonymous."
        ),
        "backups": BACKUP_DISCLOSURE,
    }


#: What actually happens to deleted data in backups. Written plainly because
#: the alternative — claiming immediate deletion everywhere — is false in every
#: system that takes backups, and a reviewer who knows that will discount
#: everything else on the page.
BACKUP_DISCLOSURE: dict[str, Any] = {
    "summary": (
        "Deleting a record from the live system does not reach into backups "
        "already taken. We do not claim otherwise."
    ),
    "retention_window_days": 30,
    "how_erasure_is_honoured": (
        "An erased record's identifier goes on a suppression list. Backups age "
        "out of the 30-day window on their own. If a restore happens inside "
        "that window, the suppression list is applied as part of the restore and "
        "the erased records are not brought back."
    ),
    "what_is_not_true": (
        "We cannot selectively edit a backup that has already been written, and "
        "any vendor who says they can is describing something other than a "
        "backup."
    ),
    "not_yet_built": (
        "The suppression list is modelled and written to; no restore procedure "
        "reads it, because no restore procedure exists in this codebase. The "
        "runbook that applies it is operational work, not application code, and "
        "it is not written."
    ),
}
