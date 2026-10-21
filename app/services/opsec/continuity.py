"""RPO, RTO, runbooks and drills — published numbers, not aspirational ones.

═══════════════════════════════════════════════════════════════════════════════
A MISSED RTO IS WORSE THAN A MODEST PUBLISHED ONE
═══════════════════════════════════════════════════════════════════════════════

The temptation in a procurement questionnaire is to write the number that wins
the deal. It wins the deal and then, during a real incident, the client has a
contractual expectation nobody can meet and a conversation about credits while
their payroll is down.

So `TIERS` holds the numbers we believe we can hit, and `evaluate_drill`
compares a **measured** restore against the published RTO. A tier whose last
drill exceeded its RTO is reported as breaching — the register does not quietly
keep the number that reads better.

And **an untested backup is not a backup.** The question in a security review is
"when did you last test a restore", not "do you take backups". `drill_status`
reports the age of the last successful drill per tier and flags an overdue one,
because a restore procedure that has not been exercised in a year is a document.

═══════════════════════════════════════════════════════════════════════════════
THE PAYROLL DEADLINE DOES NOT MOVE
═══════════════════════════════════════════════════════════════════════════════

A client's statutory filing deadline is not affected by our outage. The 24Q
deadline is the 31st whether or not our database was up on the 30th, and the
penalty lands on them.

So `RUNBOOKS` includes a payroll-deadline runbook with an explicit degraded mode
and a manual fallback, and an invariant requires it — a continuity plan that
covers database failure and not this one has covered the easy case.

Pure.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from typing import Any

__all__ = [
    "RUNBOOKS",
    "TIERS",
    "ContinuityError",
    "DataTier",
    "DrillOutcome",
    "Runbook",
    "TierTarget",
    "drill_status",
    "evaluate_drill",
    "published_posture",
]


class ContinuityError(ValueError):
    """A continuity claim this module will not make."""


class DataTier(str, enum.Enum):
    """What is being recovered, ordered by how fast it has to come back.

    Tiered because a single RTO for the whole product is either impossibly tight
    for the analytics warehouse or uselessly loose for payroll, and publishing
    one number means publishing the loose one.
    """

    #: Payroll, statutory records, disbursement. An outage here has a deadline
    #: attached to it that belongs to somebody else.
    CRITICAL = "critical"
    #: Core HR: employee records, leave, attendance. The product is unusable
    #: without it and nothing is legally due.
    CORE = "core"
    #: Reporting, analytics, culture. Degraded operation is tolerable.
    ANALYTICAL = "analytical"


@dataclass(frozen=True, slots=True)
class TierTarget:
    """The published recovery objectives for one tier.

    `rpo_minutes` is how much data we may lose; `rto_minutes` is how long
    recovery may take. Both are what we believe we can hit — see the module
    docstring for why that is not the same as what sounds good.
    """

    tier: DataTier
    rpo_minutes: int
    rto_minutes: int
    #: What the number rests on. A published objective with no mechanism behind
    #: it is a promise, and this field is where the mechanism is named.
    mechanism: str
    #: How often a restore has to be drilled for the number to stay credible.
    drill_interval_days: int
    #: What the client experiences while recovery is in progress.
    degraded_mode: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier.value,
            "rpo_minutes": self.rpo_minutes,
            "rto_minutes": self.rto_minutes,
            "rpo_human": _humanise(self.rpo_minutes),
            "rto_human": _humanise(self.rto_minutes),
            "mechanism": self.mechanism,
            "drill_interval_days": self.drill_interval_days,
            "degraded_mode": self.degraded_mode,
        }


def _humanise(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} minutes"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.0f} hour(s)" if hours == int(hours) else f"{hours:.1f} hours"
    return f"{hours / 24:.1f} days"


#: The published objectives. Deliberately unambitious where the mechanism is
#: unambitious — an hour of RTO on the analytical tier is honest, and fifteen
#: minutes would be a number nobody could defend during an incident.
TIERS: dict[DataTier, TierTarget] = {
    DataTier.CRITICAL: TierTarget(
        tier=DataTier.CRITICAL,
        rpo_minutes=5,
        rto_minutes=60,
        mechanism=(
            "Continuous WAL archiving to object storage in ap-south-1, plus a "
            "full snapshot every six hours. Point-in-time recovery to any "
            "moment inside the retention window."
        ),
        drill_interval_days=90,
        degraded_mode=(
            "Payroll computation is read-only: existing runs and payslips are "
            "visible, new runs cannot be started, and disbursement release is "
            "blocked. See the payroll-deadline runbook."
        ),
    ),
    DataTier.CORE: TierTarget(
        tier=DataTier.CORE,
        rpo_minutes=5,
        rto_minutes=240,
        mechanism=(
            "The same WAL stream. Core HR restores after the critical tier, "
            "because a payroll deadline outranks a leave application."
        ),
        drill_interval_days=180,
        degraded_mode=(
            "Read-only. Employees can see their records; approvals and "
            "submissions queue until recovery completes."
        ),
    ),
    DataTier.ANALYTICAL: TierTarget(
        tier=DataTier.ANALYTICAL,
        rpo_minutes=1440,
        rto_minutes=1440,
        mechanism=(
            "Rebuilt from the restored primary rather than restored directly. "
            "A day of lost analytics is a day of lost analytics."
        ),
        drill_interval_days=365,
        degraded_mode="Reports and dashboards are unavailable.",
    ),
}


class DrillOutcome(str, enum.Enum):
    """What a restore drill actually did."""

    MET = "met"
    #: Restored, and took longer than the published RTO. Reported as a breach
    #: rather than rounded into a pass.
    EXCEEDED_RTO = "exceeded_rto"
    #: Restored, and lost more data than the published RPO allows.
    EXCEEDED_RPO = "exceeded_rpo"
    FAILED = "failed"

    @property
    def is_credible(self) -> bool:
        return self is DrillOutcome.MET


@dataclass(frozen=True, slots=True)
class DrillResult:
    """One timed restore, judged against the published numbers."""

    tier: DataTier
    performed_on: date
    #: Measured, not estimated. A drill whose duration was estimated is a
    #: rehearsal of the paperwork.
    measured_rto_minutes: int
    measured_rpo_minutes: int
    succeeded: bool
    outcome: DrillOutcome
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        target = TIERS[self.tier]
        return {
            "tier": self.tier.value,
            "performed_on": self.performed_on.isoformat(),
            "measured_rto_minutes": self.measured_rto_minutes,
            "published_rto_minutes": target.rto_minutes,
            "measured_rpo_minutes": self.measured_rpo_minutes,
            "published_rpo_minutes": target.rpo_minutes,
            "succeeded": self.succeeded,
            "outcome": self.outcome.value,
            "meets_published_objectives": self.outcome.is_credible,
            "notes": self.notes,
        }


def evaluate_drill(
    *,
    tier: DataTier,
    performed_on: date,
    measured_rto_minutes: int,
    measured_rpo_minutes: int,
    succeeded: bool,
    notes: str = "",
) -> DrillResult:
    """Judge a drill against the published objectives.

    A drill that took longer than the published RTO is `EXCEEDED_RTO`, not a
    pass with a note. Rounding it into a pass is how a published number survives
    the evidence against it — and the evidence is the only reason the number is
    worth anything.
    """
    if measured_rto_minutes < 0 or measured_rpo_minutes < 0:
        raise ContinuityError("A measured duration cannot be negative.")

    target = TIERS[tier]
    if not succeeded:
        outcome = DrillOutcome.FAILED
    elif measured_rto_minutes > target.rto_minutes:
        outcome = DrillOutcome.EXCEEDED_RTO
    elif measured_rpo_minutes > target.rpo_minutes:
        outcome = DrillOutcome.EXCEEDED_RPO
    else:
        outcome = DrillOutcome.MET

    return DrillResult(
        tier=tier,
        performed_on=performed_on,
        measured_rto_minutes=measured_rto_minutes,
        measured_rpo_minutes=measured_rpo_minutes,
        succeeded=succeeded,
        outcome=outcome,
        notes=notes,
    )


@dataclass(frozen=True, slots=True)
class TierDrillStatus:
    """Whether one tier's published numbers are currently backed by evidence."""

    tier: DataTier
    last_drill_on: date | None
    last_outcome: DrillOutcome | None
    days_since: int | None
    interval_days: int

    @property
    def is_overdue(self) -> bool:
        if self.last_drill_on is None:
            return True
        return (self.days_since or 0) > self.interval_days

    @property
    def is_evidenced(self) -> bool:
        """Whether the published number currently rests on a passing drill."""
        return (
            self.last_outcome is not None and self.last_outcome.is_credible and not self.is_overdue
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier.value,
            "last_drill_on": (self.last_drill_on.isoformat() if self.last_drill_on else None),
            "last_outcome": self.last_outcome.value if self.last_outcome else None,
            "days_since": self.days_since,
            "drill_interval_days": self.interval_days,
            "is_overdue": self.is_overdue,
            "is_evidenced": self.is_evidenced,
            "statement": (
                "The published objectives for this tier are backed by a passing "
                "drill inside the interval."
                if self.is_evidenced
                else (
                    "The published objectives for this tier are NOT currently "
                    "backed by evidence. An untested backup is not a backup, and "
                    "the question in a security review is when a restore was "
                    "last tested."
                )
            ),
        }


def drill_status(drills: list[DrillResult], *, today: date) -> list[TierDrillStatus]:
    """Per tier, whether the published numbers are currently evidenced.

    Uses the last drill of each tier, not the last *passing* one. A tier whose
    most recent drill exceeded its RTO is not evidenced, even if an older one
    passed — the newer measurement is the one that describes the system.
    """
    latest: dict[DataTier, DrillResult] = {}
    for drill in sorted(drills, key=lambda d: d.performed_on):
        latest[drill.tier] = drill

    out: list[TierDrillStatus] = []
    for tier, target in TIERS.items():
        drill = latest.get(tier)
        out.append(
            TierDrillStatus(
                tier=tier,
                last_drill_on=drill.performed_on if drill else None,
                last_outcome=drill.outcome if drill else None,
                days_since=(today - drill.performed_on).days if drill else None,
                interval_days=target.drill_interval_days,
            )
        )
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Runbooks
# ══════════════════════════════════════════════════════════════════════════════


class IncidentSeverity(str, enum.Enum):
    """Graded by what the client loses, not by how much of our stack is down."""

    #: Statutory deadline at risk, or data loss. Wake people up.
    SEV1 = "sev1"
    #: Product unusable for a tenant, no deadline in the window.
    SEV2 = "sev2"
    #: Degraded, with a workaround.
    SEV3 = "sev3"
    SEV4 = "sev4"

    @property
    def response_target_minutes(self) -> int:
        return {
            IncidentSeverity.SEV1: 15,
            IncidentSeverity.SEV2: 60,
            IncidentSeverity.SEV3: 240,
            IncidentSeverity.SEV4: 1440,
        }[self]


@dataclass(frozen=True, slots=True)
class Runbook:
    """One documented failure mode and what is done about it."""

    key: str
    title: str
    #: What has actually gone wrong, in the words somebody would use at 3am.
    trigger: str
    severity: IncidentSeverity
    #: Ordered. Each step is something a named person does, not a principle.
    steps: tuple[str, ...]
    #: What the product does while this is in progress.
    degraded_mode: str
    #: What the client does by hand if we cannot recover in time. Empty is a
    #: real answer for some failures and a gap for others — the payroll one
    #: must have it, and an invariant checks that.
    manual_fallback: str = ""
    #: Whether a person has read this since it was last changed. A runbook
    #: nobody has reviewed is a draft.
    reviewed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "trigger": self.trigger,
            "severity": self.severity.value,
            "response_target_minutes": self.severity.response_target_minutes,
            "steps": list(self.steps),
            "degraded_mode": self.degraded_mode,
            "manual_fallback": self.manual_fallback,
            "reviewed": self.reviewed,
        }


def _r(**kwargs: Any) -> Runbook:
    return Runbook(**kwargs)


#: The failure modes that actually happen. Not an exhaustive taxonomy — a
#: runbook for every conceivable failure is a document nobody reads at 3am.
RUNBOOKS: dict[str, Runbook] = {
    r.key: r
    for r in (
        _r(
            key="payroll_deadline_during_incident",
            title="A statutory deadline falls during an outage",
            trigger=(
                "We are down or degraded, and a client has a 24Q, ECR or ESI "
                "deadline inside the outage window."
            ),
            severity=IncidentSeverity.SEV1,
            steps=(
                "Identify every tenant with a statutory deadline in the next "
                "72 hours. This is a query, not a memory — run it first, before "
                "diagnosing anything.",
                "Contact those tenants directly within 30 minutes. Not the "
                "status page: a named person, by phone, because a payroll "
                "manager watching a deadline will not be reading our status "
                "page.",
                "Restore the critical tier first. Core HR and analytics wait — "
                "a leave application is not a filing deadline.",
                "If recovery will not complete before the deadline, produce the "
                "filing data from the last good snapshot and hand it over as a "
                "file. A late filing built on slightly stale data is recoverable; "
                "a missed filing is a penalty.",
                "Write the incident up with the exact times, and send it to the "
                "affected tenants unprompted. They will need it if the "
                "department asks why the filing was late.",
            ),
            degraded_mode=(
                "Payroll is read-only. Existing runs and payslips are visible; "
                "new runs and disbursement release are blocked."
            ),
            manual_fallback=(
                "We generate the statutory return from the last good snapshot "
                "and send it to the client as a file, with a covering note "
                "stating the data cut-off. The client files it manually on the "
                "portal. Their deadline does not move because of our outage, "
                "and this is the path that keeps them compliant when we cannot "
                "recover in time."
            ),
            reviewed=False,
        ),
        _r(
            key="primary_database_failure",
            title="The primary database is unavailable",
            trigger="Connections failing or the instance is unreachable.",
            severity=IncidentSeverity.SEV1,
            steps=(
                "Confirm it is the database and not the network path.",
                "Check whether the managed failover has already promoted a "
                "replica; if it has, verify data currency against the WAL "
                "position before declaring recovery.",
                "If a restore is needed, restore the critical tier first to a "
                "point immediately before the failure.",
                "Run the payroll-deadline runbook in parallel if any tenant has "
                "a deadline in the window.",
            ),
            degraded_mode="The product is unavailable.",
            manual_fallback="",
            reviewed=False,
        ),
        _r(
            key="region_outage",
            title="The ap-south-1 region is degraded",
            trigger="Multiple services unavailable in the primary region.",
            severity=IncidentSeverity.SEV1,
            steps=(
                "Confirm the scope with the provider's status page before "
                "failing over — a partial degradation is often faster to wait "
                "out than to fail over and fail back.",
                "Fail over to the secondary Indian region. **Not to a region "
                "outside India**: a tenant configured for Indian residency does "
                "not stop being configured that way during an incident, and "
                "moving their data would be a cross-border transfer nobody "
                "disclosed.",
                "Verify the residency configuration of every failed-over tenant "
                "before accepting traffic.",
            ),
            degraded_mode="Read-only in the secondary region until failover completes.",
            manual_fallback="",
            reviewed=False,
        ),
        _r(
            key="model_provider_outage",
            title="The AI model provider is unavailable",
            trigger="Agent calls failing or timing out.",
            severity=IncidentSeverity.SEV3,
            steps=(
                "Disable the agent surface rather than letting it fail per "
                "request. A feature that errors on every use is worse than one "
                "that says it is unavailable.",
                "Confirm no proposal was half-applied: an agent action that "
                "began and did not finish is the failure worth checking for.",
                "Nothing in payroll, leave or attendance depends on the model "
                "provider. Say so on the status page — clients assume AI "
                "features and core HR share a fate and they do not.",
            ),
            degraded_mode=("AI features are unavailable. Every other module is unaffected."),
            manual_fallback="The work is done the way it was done before the agent existed.",
            reviewed=False,
        ),
    )
}


def published_posture(drills: list[DrillResult], *, today: date) -> dict[str, Any]:
    """What we publish to a procurement questionnaire.

    Every objective is shown alongside whether it is currently evidenced by a
    passing drill inside the interval. A number with no drill behind it is
    labelled as such rather than being quietly published on its own.
    """
    statuses = {s.tier: s for s in drill_status(drills, today=today)}
    return {
        "tiers": [
            {
                **target.as_dict(),
                "evidence": statuses[tier].as_dict(),
            }
            for tier, target in TIERS.items()
        ],
        "runbooks": [r.as_dict() for r in RUNBOOKS.values()],
        "severity_definitions": [
            {
                "severity": severity.value,
                "response_target_minutes": severity.response_target_minutes,
            }
            for severity in IncidentSeverity
        ],
        "residency_statement": (
            "The disaster-recovery region is also in India for tenants "
            "configured for Indian residency. A failover does not move their "
            "data outside the country — that would be a cross-border transfer "
            "nobody disclosed, made at the worst possible moment."
        ),
        "honesty_statement": (
            "These are the numbers we believe we can hit, not the numbers that "
            "read best. A tier with no passing drill inside its interval is "
            "marked as not evidenced, because an untested backup is not a "
            "backup."
        ),
        "not_built": [
            "No backup or restore machinery lives in this codebase — the WAL "
            "archiving, the snapshots and the failover are infrastructure. This "
            "module holds the published objectives and the record of drills "
            "against them.",
            "No drill has been performed. Every tier reports as not evidenced "
            "until one is recorded.",
            "The runbooks are written and none has been reviewed by a person.",
        ],
    }
