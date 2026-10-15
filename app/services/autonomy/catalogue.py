"""What each agent may do, and the ceiling a tenant cannot raise.

═══════════════════════════════════════════════════════════════════════════════
"AGENTS PROPOSE AND HUMANS APPROVE" HAS TO BE A SETTING, NOT A PROMISE
═══════════════════════════════════════════════════════════════════════════════

We say it in every client conversation. Right now it is hardcoded behaviour and
an implicit promise, which means two things are true at once: a client who wants
more automation cannot have it, and a client who wants none cannot verify they
have none. Both are the same problem — the behaviour is not inspectable.

So autonomy is per agent, per action, configured by the tenant, and visible.

═══════════════════════════════════════════════════════════════════════════════
AND SOME THINGS ARE NOT CONFIGURABLE
═══════════════════════════════════════════════════════════════════════════════

`max_autonomy` is a platform ceiling. A client asking for auto-apply on payroll
rule changes is told no, and told why — not silently given a setting that does
something narrower than it says.

Four actions can never be auto-applied whatever anybody wants:

- **Applying a payroll rule change.** A wrong PF ceiling applied unattended is
  four hundred people paid incorrectly and a statutory filing built on it.
- **Releasing a disbursement.** Money leaves. There is no revert.
- **Changing an appraisal rating.** It changes somebody's increment, their
  promotion case, and what their manager thinks happened — and the person will
  be told a system did it.
- **Any Your Voice case action.** The channel's value is that a person handles
  it. An agent touching a case is the same breach as an agent reading one.

The ceiling is enforced server-side in `validate_autonomy`, and an invariant
checks each protected action's ceiling directly, so it cannot be raised by
editing a config row, a request body or a seed script.

═══════════════════════════════════════════════════════════════════════════════
IRREVERSIBLE IMPLIES APPROVAL
═══════════════════════════════════════════════════════════════════════════════

`reversible=False` caps an action at PROPOSE_FOR_APPROVAL, derived rather than
declared. An action nobody can undo, applied automatically, is a decision with
no way back and no person who made it. The rule is a function of `reversible`,
so a new action cannot be added with an inconsistent pair.

Pure.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

__all__ = [
    "ACTIONS",
    "AgentAction",
    "AutonomyError",
    "AutonomyLevel",
    "ImpactLevel",
    "action",
    "ceiling_for",
    "protected_actions",
    "validate_autonomy",
]


class AutonomyError(ValueError):
    """A configuration this module refuses, with the reason."""


class AutonomyLevel(str, enum.Enum):
    """How far an agent may go on its own.

    Ordered. `rank` is what the ceiling comparison uses, so the ordering is a
    property of the enum rather than a table somebody keeps in step with it.
    """

    #: Shows a suggestion. Nothing is created, nothing is queued.
    SUGGEST_ONLY = "suggest_only"
    #: Creates a proposal in the queue. A person approves before anything
    #: happens. The default for almost everything.
    PROPOSE_FOR_APPROVAL = "propose_for_approval"
    #: Applies, and tells somebody it did.
    AUTO_APPLY_WITH_NOTIFY = "auto_apply_with_notify"
    #: Applies silently. Legitimate for genuinely trivial, reversible actions —
    #: and a client choosing it for something consequential is a conversation,
    #: not a setting.
    AUTO_APPLY_SILENT = "auto_apply_silent"

    @property
    def rank(self) -> int:
        return {
            AutonomyLevel.SUGGEST_ONLY: 0,
            AutonomyLevel.PROPOSE_FOR_APPROVAL: 1,
            AutonomyLevel.AUTO_APPLY_WITH_NOTIFY: 2,
            AutonomyLevel.AUTO_APPLY_SILENT: 3,
        }[self]

    @property
    def applies_automatically(self) -> bool:
        return self.rank >= AutonomyLevel.AUTO_APPLY_WITH_NOTIFY.rank

    @property
    def label(self) -> str:
        return {
            AutonomyLevel.SUGGEST_ONLY: "Suggests only",
            AutonomyLevel.PROPOSE_FOR_APPROVAL: "Proposes; a person approves",
            AutonomyLevel.AUTO_APPLY_WITH_NOTIFY: "Applies and notifies",
            AutonomyLevel.AUTO_APPLY_SILENT: "Applies silently",
        }[self]


class ImpactLevel(str, enum.Enum):
    """What it costs to get one wrong.

    Judged by what happens to a person, not by how much code runs. Changing a
    rating is CRITICAL and updating a leave balance is LOW, even though the
    second touches money and the first does not.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class AgentAction:
    """One thing an agent can do, and how far it may go doing it."""

    agent_key: str
    action_key: str
    description: str
    default_autonomy: AutonomyLevel
    #: The platform ceiling. A tenant may configure at or below this, never
    #: above.
    max_autonomy: AutonomyLevel
    reversible: bool
    impact: ImpactLevel
    #: Why the ceiling is where it is, in a sentence a client can be shown. A
    #: refusal with no reason reads as an arbitrary limit, and the client will
    #: ask for it to be lifted.
    ceiling_reason: str = ""
    #: The unit a threshold is measured in, where the action supports one.
    #: Explicit because "under 2000" is meaningless without it — rupees, days
    #: and percent are all plausible for an expense action.
    threshold_unit: str | None = None

    @property
    def key(self) -> str:
        return f"{self.agent_key}.{self.action_key}"

    @property
    def is_protected(self) -> bool:
        """Whether the ceiling forbids automatic application entirely."""
        return not self.max_autonomy.applies_automatically

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "agent_key": self.agent_key,
            "action_key": self.action_key,
            "description": self.description,
            "default_autonomy": self.default_autonomy.value,
            "max_autonomy": self.max_autonomy.value,
            "max_autonomy_label": self.max_autonomy.label,
            "is_protected": self.is_protected,
            "ceiling_reason": self.ceiling_reason,
            "reversible": self.reversible,
            "impact": self.impact.value,
            "threshold_unit": self.threshold_unit,
        }


def _a(**kwargs: Any) -> AgentAction:
    return AgentAction(**kwargs)


#: Every action any agent can take, with its ceiling.
#:
#: The `max_autonomy` column is the interesting one. Read it top to bottom: most
#: actions cap at PROPOSE_FOR_APPROVAL, and the ones that do not are the ones
#: where being wrong costs somebody an afternoon rather than their increment.
ACTIONS: dict[str, AgentAction] = {
    a.key: a
    for a in (
        # ── Protected: never automatic, whatever a client asks for ───────────
        _a(
            agent_key="compliance_monitor",
            action_key="apply_payroll_rule_change",
            description=(
                "Update a statutory rate — a PF ceiling, an ESI threshold, a PT "
                "slab — from a detected regulatory change."
            ),
            default_autonomy=AutonomyLevel.PROPOSE_FOR_APPROVAL,
            max_autonomy=AutonomyLevel.PROPOSE_FOR_APPROVAL,
            reversible=False,
            impact=ImpactLevel.CRITICAL,
            ceiling_reason=(
                "A wrong rate applied unattended is four hundred people paid "
                "incorrectly and a statutory filing built on the error. The "
                "detection is useful; the application is a person's decision, "
                "and no configuration changes that."
            ),
        ),
        _a(
            agent_key="disbursement",
            action_key="release_payment_file",
            description="Release a salary disbursement file to the bank.",
            default_autonomy=AutonomyLevel.PROPOSE_FOR_APPROVAL,
            max_autonomy=AutonomyLevel.PROPOSE_FOR_APPROVAL,
            reversible=False,
            impact=ImpactLevel.CRITICAL,
            ceiling_reason=(
                "Money leaves and there is no revert. A person releases it."
            ),
        ),
        _a(
            agent_key="appraisal_assistant",
            action_key="change_rating",
            description="Change an employee's appraisal rating.",
            default_autonomy=AutonomyLevel.SUGGEST_ONLY,
            max_autonomy=AutonomyLevel.PROPOSE_FOR_APPROVAL,
            reversible=False,
            impact=ImpactLevel.CRITICAL,
            ceiling_reason=(
                "A rating changes somebody's increment, their promotion case "
                "and what their manager believes happened. The person affected "
                "will be told a system did it, and that has to be untrue."
            ),
        ),
        _a(
            agent_key="your_voice",
            action_key="act_on_case",
            description=(
                "Any action on a confidential reporting case — triage, "
                "assignment, closure, response."
            ),
            default_autonomy=AutonomyLevel.SUGGEST_ONLY,
            max_autonomy=AutonomyLevel.SUGGEST_ONLY,
            reversible=False,
            impact=ImpactLevel.CRITICAL,
            ceiling_reason=(
                "The channel's entire value is that a person handles it. An "
                "agent touching a case is the same breach of the promise as an "
                "agent reading one — which is why it cannot even propose."
            ),
        ),
        # ── Consequential but reversible: approval by default ────────────────
        _a(
            agent_key="lifecycle",
            action_key="raise_confirmation",
            description="Raise a probation confirmation for approval.",
            default_autonomy=AutonomyLevel.PROPOSE_FOR_APPROVAL,
            max_autonomy=AutonomyLevel.AUTO_APPLY_WITH_NOTIFY,
            reversible=True,
            impact=ImpactLevel.HIGH,
            ceiling_reason=(
                "Reversible, and a client with a settled probation policy may "
                "reasonably automate it. Never silent: the employee and the "
                "manager both need to know it happened."
            ),
        ),
        _a(
            agent_key="expense_extraction",
            action_key="approve_claim",
            description="Approve a reimbursement claim from an extracted receipt.",
            default_autonomy=AutonomyLevel.PROPOSE_FOR_APPROVAL,
            max_autonomy=AutonomyLevel.AUTO_APPLY_WITH_NOTIFY,
            reversible=True,
            impact=ImpactLevel.MEDIUM,
            threshold_unit="INR",
            ceiling_reason=(
                "Reversible and routinely small. A threshold is the right shape "
                "here — auto-approve below it, route above it."
            ),
        ),
        _a(
            agent_key="retention_risk",
            action_key="flag_to_manager",
            description="Surface a retention-risk signal to a manager.",
            default_autonomy=AutonomyLevel.PROPOSE_FOR_APPROVAL,
            max_autonomy=AutonomyLevel.AUTO_APPLY_WITH_NOTIFY,
            reversible=True,
            impact=ImpactLevel.HIGH,
            ceiling_reason=(
                "Reversible in the system and not in the manager's head — once "
                "somebody has been told a person may leave, they have been told. "
                "Never silent, so the flag is attributable."
            ),
        ),
        _a(
            agent_key="fit_score",
            action_key="advance_candidate",
            description="Move a candidate to the next stage on a fit score.",
            default_autonomy=AutonomyLevel.PROPOSE_FOR_APPROVAL,
            max_autonomy=AutonomyLevel.AUTO_APPLY_WITH_NOTIFY,
            reversible=True,
            impact=ImpactLevel.MEDIUM,
            threshold_unit="score",
        ),
        _a(
            agent_key="timesheets",
            action_key="recommend_approval",
            description=(
                "Approve a submitted timesheet whose hours match recorded attendance, "
                "day by day."
            ),
            default_autonomy=AutonomyLevel.PROPOSE_FOR_APPROVAL,
            max_autonomy=AutonomyLevel.PROPOSE_FOR_APPROVAL,
            reversible=False,
            impact=ImpactLevel.MEDIUM,
            ceiling_reason=(
                "An approved timesheet becomes pay. The agent does the checking against "
                "attendance; the timesheet's own approver still says yes."
            ),
        ),
        _a(
            agent_key="scheduling",
            action_key="cover_short_shift",
            description=(
                "Put a qualified, available person on a shift that is short of its "
                "minimum headcount, for one day."
            ),
            default_autonomy=AutonomyLevel.PROPOSE_FOR_APPROVAL,
            max_autonomy=AutonomyLevel.PROPOSE_FOR_APPROVAL,
            reversible=False,
            impact=ImpactLevel.MEDIUM,
            ceiling_reason=(
                "It changes when somebody is expected at work, often on their day off. "
                "The agent finds who could cover; a person asks them and approves."
            ),
        ),
        # ── Low impact and reversible: silent automation is defensible ───────
        _a(
            agent_key="attendance",
            action_key="regularise_missing_punch",
            description=(
                "Raise a regularisation for a missing punch where the shift and "
                "the geofence both confirm attendance."
            ),
            default_autonomy=AutonomyLevel.PROPOSE_FOR_APPROVAL,
            max_autonomy=AutonomyLevel.AUTO_APPLY_SILENT,
            reversible=True,
            impact=ImpactLevel.LOW,
            ceiling_reason=(
                "Reversible, low impact, and high volume. A client automating "
                "this silently is saving their HR team an afternoon a week."
            ),
        ),
        _a(
            agent_key="companion",
            action_key="answer_question",
            description="Answer a question from the employee's own record.",
            default_autonomy=AutonomyLevel.AUTO_APPLY_SILENT,
            max_autonomy=AutonomyLevel.AUTO_APPLY_SILENT,
            reversible=True,
            impact=ImpactLevel.LOW,
            ceiling_reason=(
                "Answering a question changes nothing. Requiring approval to "
                "answer would make the assistant useless."
            ),
        ),
        _a(
            agent_key="notifications",
            action_key="send_reminder",
            description="Send a reminder about a due task or an expiring window.",
            default_autonomy=AutonomyLevel.AUTO_APPLY_SILENT,
            max_autonomy=AutonomyLevel.AUTO_APPLY_SILENT,
            reversible=True,
            impact=ImpactLevel.LOW,
        ),
    )
}


def action(key: str) -> AgentAction:
    try:
        return ACTIONS[key]
    except KeyError:
        raise AutonomyError(
            f"No agent action {key!r}. Known actions: "
            f"{', '.join(sorted(ACTIONS))}."
        ) from None


def ceiling_for(key: str) -> AutonomyLevel:
    """The highest autonomy a tenant may configure for this action.

    Derived, not read from a column: an irreversible action is capped at
    PROPOSE_FOR_APPROVAL whatever its declared ceiling says, so the two cannot
    contradict each other. An action added with `reversible=False` and a ceiling
    of AUTO_APPLY_SILENT is capped correctly and flagged by
    `catalogue_contradictions` rather than quietly honoured.
    """
    spec = action(key)
    declared = spec.max_autonomy
    if not spec.reversible and declared.rank > AutonomyLevel.PROPOSE_FOR_APPROVAL.rank:
        return AutonomyLevel.PROPOSE_FOR_APPROVAL
    return declared


def validate_autonomy(
    key: str,
    level: AutonomyLevel,
    *,
    threshold: Decimal | None = None,
) -> AutonomyLevel:
    """Refuse a configuration above the ceiling, with the reason.

    The message names the action, the ceiling and why it is there. "Invalid
    configuration" tells a client nothing and they will ask again; "a wrong PF
    rate applied unattended is four hundred people paid incorrectly" ends the
    conversation.
    """
    spec = action(key)
    ceiling = ceiling_for(key)

    if level.rank > ceiling.rank:
        if not spec.reversible and level.applies_automatically:
            raise AutonomyError(
                f"{spec.description} cannot be applied automatically because it "
                "cannot be undone. An irreversible action applied by a system is "
                "a decision with no way back and nobody who made it, so the "
                f"highest setting available is "
                f"{AutonomyLevel.PROPOSE_FOR_APPROVAL.label!r}."
            )
        raise AutonomyError(
            f"{spec.description} is capped at {ceiling.label!r}, and "
            f"{level.label!r} is above that. "
            + (
                spec.ceiling_reason
                or "This ceiling is set by the platform and is not configurable."
            )
        )

    if threshold is not None and spec.threshold_unit is None:
        raise AutonomyError(
            f"{spec.description} does not take a threshold. A number with no "
            "unit and nothing to compare against would be stored and never read."
        )
    if threshold is not None and threshold < 0:
        raise AutonomyError("A threshold cannot be negative.")

    return level


def protected_actions() -> tuple[AgentAction, ...]:
    """Actions no configuration can make automatic. Shown to the client."""
    return tuple(a for a in ACTIONS.values() if a.is_protected)


def catalogue_contradictions() -> list[str]:
    """Where the catalogue disagrees with itself. Read by an invariant.

    Two rules:

    - An irreversible action must not *declare* a ceiling above
      PROPOSE_FOR_APPROVAL. `ceiling_for` caps it anyway, but a declared value
      that has to be corrected at read time is a value somebody will read
      directly one day.
    - A protected action needs a `ceiling_reason`. A refusal with no reason
      reads as an arbitrary limit and the client asks for it to be lifted.
    """
    problems: list[str] = []
    for key, spec in ACTIONS.items():
        if (
            not spec.reversible
            and spec.max_autonomy.rank > AutonomyLevel.PROPOSE_FOR_APPROVAL.rank
        ):
            problems.append(
                f"{key} is irreversible and declares a ceiling of "
                f"{spec.max_autonomy.value}. An action nobody can undo, applied "
                "automatically, is a decision with no way back."
            )
        if spec.default_autonomy.rank > ceiling_for(key).rank:
            problems.append(
                f"{key} defaults to {spec.default_autonomy.value}, which is "
                f"above its own ceiling of {ceiling_for(key).value}."
            )
        if spec.is_protected and not spec.ceiling_reason:
            problems.append(
                f"{key} is protected and gives no reason. A client told no with "
                "no explanation asks again."
            )
        if spec.threshold_unit is not None and not spec.threshold_unit.strip():
            problems.append(f"{key} has an empty threshold unit.")
    return problems


#: The four the brief names, and the timesheet agent (irreversible: approval is pay),
#: by key. Asserted directly by an invariant so the
#: protection cannot be lost by editing a `max_autonomy` field — the invariant
#: names them rather than deriving them from the same data it is checking.
PROTECTED_KEYS: frozenset[str] = frozenset(
    {
        "compliance_monitor.apply_payroll_rule_change",
        "disbursement.release_payment_file",
        "appraisal_assistant.change_rating",
        "your_voice.act_on_case",
        "timesheets.recommend_approval",
        "scheduling.cover_short_shift",
    }
)
