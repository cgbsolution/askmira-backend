"""SIEM export: a standard schema, and an alert when delivery stops.

═══════════════════════════════════════════════════════════════════════════════
DO NOT INVENT A BESPOKE FORMAT
═══════════════════════════════════════════════════════════════════════════════

A client's SOC has parsers, dashboards and detections built on a schema they
already use. Shipping them `{"what_happened": "...", "who": "..."}` means
somebody on their side writes a mapping, gets it slightly wrong, and the
detection that should have fired on a bulk export does not.

So the wire format is **Elastic Common Schema** field names — `event.action`,
`event.category`, `user.id`, `source.ip`, `organization.id`. ECS is what Splunk,
Sentinel and Datadog ingestion pipelines already understand, and the fields are
named exactly as ECS names them rather than approximately.

`ECS_FIELDS` is the documented set and an invariant checks every emitted event
uses only fields from it — a field invented in one code path is a field the
client's parser drops.

═══════════════════════════════════════════════════════════════════════════════
A LOG THAT SILENTLY STOPS SHIPPING IS WORSE THAN NONE
═══════════════════════════════════════════════════════════════════════════════

Because it is trusted. A SOC with no AskMira events knows it has no AskMira
events; a SOC whose AskMira feed died three weeks ago believes nothing has
happened here in three weeks, which is a much more dangerous belief.

So `delivery_health` alerts on **silence** as well as on failure. A destination
that has not received an event in longer than its expected interval is
unhealthy, even if nothing errored — that is the shape of a feed that died
quietly rather than one that is failing loudly.

Pure. Nothing here opens a socket.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

__all__ = [
    "ECS_FIELDS",
    "EXPORTED_CATEGORIES",
    "DeliveryHealth",
    "SiemError",
    "SiemEvent",
    "SiemVendor",
    "delivery_health",
    "to_ecs",
]


class SiemError(ValueError):
    """An event this module will not emit."""


class SiemVendor(str, enum.Enum):
    """Where events go. The payload is the same for all of them.

    Vendor-specific framing belongs in the transport, not in the event: a
    schema that varies by destination is three schemas, and the third one is
    always the one nobody tested.
    """

    SPLUNK = "splunk"
    SENTINEL = "microsoft_sentinel"
    DATADOG = "datadog"
    #: A plain HTTPS endpoint taking newline-delimited JSON. The escape hatch
    #: for a client whose SIEM is not one of the three.
    GENERIC_HTTPS = "generic_https"


class EventCategory(str, enum.Enum):
    """ECS `event.category` values, restricted to the ones we emit.

    ECS defines more. Emitting only the ones we actually produce means a client
    building a dashboard from this list is building from what will arrive.
    """

    AUTHENTICATION = "authentication"
    #: ECS calls an authorisation failure `iam` when it is about entitlement
    #: and `intrusion_detection` when it is about an attack. A manager reaching
    #: for another team's payroll is the first.
    IAM = "iam"
    #: Reads and writes of records. Bulk exports live here.
    DATABASE = "database"
    CONFIGURATION = "configuration"
    #: Our own addition to the vocabulary, and the one place we extend ECS.
    #: There is no ECS category for "an AI agent did something", and inventing
    #: a field would be worse than inventing a category value — a client's
    #: parser reads `event.category` as a string either way.
    PROCESS = "process"


class EventOutcome(str, enum.Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"


#: The exact fields we emit, as ECS names them. An invariant checks nothing
#: escapes this set — a field invented in one code path is a field the client's
#: parser drops silently.
ECS_FIELDS: frozenset[str] = frozenset(
    {
        "@timestamp",
        "event.kind",
        "event.category",
        "event.action",
        "event.outcome",
        "event.severity",
        "event.dataset",
        "event.id",
        "user.id",
        "user.name",
        "user.roles",
        "source.ip",
        "user_agent.original",
        "organization.id",
        "organization.name",
        "service.name",
        "labels.entity_type",
        "labels.entity_id",
        "labels.actor_type",
        "labels.agent_key",
        "labels.record_count",
        "message",
    }
)

#: What we ship, and why each one is security-relevant. A client asking "what
#: will I get" gets this list rather than a promise of "audit events".
EXPORTED_CATEGORIES: dict[str, str] = {
    "authentication": (
        "Sign-ins, sign-outs, failed attempts and SSO assertions. The baseline "
        "for credential-stuffing and impossible-travel detections."
    ),
    "authorisation_failure": (
        "A request refused for lack of permission. One is noise; a burst from "
        "one account is somebody probing."
    ),
    "data_export": (
        "Any bulk read leaving the product — a report download, a disbursement "
        "file, a data-principal export. The highest-value event in the feed."
    ),
    "permission_change": (
        "Role and entitlement changes. A privilege escalation shows up here "
        "before it shows up anywhere else."
    ),
    "agent_action": (
        "Anything an AI agent did or proposed, with its confidence and its "
        "citations."
    ),
    "payroll_operation": (
        "Rule changes, run approvals, disbursement releases. Money and "
        "statutory filings."
    ),
}

#: Which audit actions map to which ECS category and dataset. One table, so a
#: new audit action added without a mapping is visible rather than silently
#: unexported — `unmapped_actions` is read by an invariant.
_ACTION_MAP: dict[str, tuple[EventCategory, str]] = {
    "login": (EventCategory.AUTHENTICATION, "authentication"),
    "logout": (EventCategory.AUTHENTICATION, "authentication"),
    "login_failed": (EventCategory.AUTHENTICATION, "authentication"),
    "permission_change": (EventCategory.IAM, "permission_change"),
    "export": (EventCategory.DATABASE, "data_export"),
    "sensitive_read": (EventCategory.DATABASE, "data_export"),
    "ai_action": (EventCategory.PROCESS, "agent_action"),
    "rule_change": (EventCategory.CONFIGURATION, "payroll_operation"),
    "approve": (EventCategory.CONFIGURATION, "payroll_operation"),
    "reject": (EventCategory.CONFIGURATION, "payroll_operation"),
    "create": (EventCategory.DATABASE, "record_change"),
    "update": (EventCategory.DATABASE, "record_change"),
    "delete": (EventCategory.DATABASE, "record_change"),
}


def unmapped_actions(known_actions: frozenset[str]) -> set[str]:
    """Audit actions with no SIEM mapping. Read by an invariant.

    An action added without a mapping would be silently absent from the client's
    feed — and absent is exactly what a compromised account looks like.
    """
    return known_actions - set(_ACTION_MAP)


@dataclass(frozen=True, slots=True)
class SiemEvent:
    """One event, in ECS field names.

    `as_wire` is the only serialisation. Constructing the dict at a call site
    is how a field gets invented, so there is one place it happens and an
    invariant checks the keys.
    """

    timestamp: datetime
    action: str
    category: EventCategory
    dataset: str
    outcome: EventOutcome
    severity: int
    event_id: str
    tenant_id: str
    tenant_name: str
    message: str
    user_id: str | None = None
    user_name: str | None = None
    user_roles: tuple[str, ...] = ()
    source_ip: str | None = None
    user_agent: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    actor_type: str = "user"
    agent_key: str | None = None
    record_count: int | None = None

    def as_wire(self) -> dict[str, Any]:
        """The payload, with ECS field names and no nulls.

        Nulls are dropped rather than sent. A SIEM ingesting `"user.id": null`
        creates the field with a null value, and a detection filtering on
        `user.id` then matches events that have no user — which is how a
        system-initiated action gets attributed to nobody in particular and
        then to everybody.
        """
        payload: dict[str, Any] = {
            "@timestamp": self.timestamp.isoformat(),
            "event.kind": "event",
            "event.category": self.category.value,
            "event.action": self.action,
            "event.outcome": self.outcome.value,
            "event.severity": self.severity,
            "event.dataset": f"askmira.{self.dataset}",
            "event.id": self.event_id,
            "organization.id": self.tenant_id,
            "organization.name": self.tenant_name,
            "service.name": "askmira",
            "labels.actor_type": self.actor_type,
            "message": self.message,
        }
        optional = {
            "user.id": self.user_id,
            "user.name": self.user_name,
            "user.roles": list(self.user_roles) or None,
            "source.ip": self.source_ip,
            "user_agent.original": self.user_agent,
            "labels.entity_type": self.entity_type,
            "labels.entity_id": self.entity_id,
            "labels.agent_key": self.agent_key,
            "labels.record_count": self.record_count,
        }
        payload.update({k: v for k, v in optional.items() if v is not None})

        unknown = set(payload) - ECS_FIELDS
        if unknown:
            raise SiemError(
                f"Event carries non-ECS fields: {sorted(unknown)}. A field the "
                "client's parser does not know is a field it drops, and the "
                "detection built on it never fires."
            )
        return payload


#: Severity per dataset, on the 1-99 ECS scale. A bulk export outranks a login
#: because it is the event a SOC wants paged on.
_SEVERITY: dict[str, int] = {
    "data_export": 60,
    "payroll_operation": 55,
    "permission_change": 50,
    "authentication": 20,
    "agent_action": 30,
    "record_change": 10,
}


def to_ecs(
    *,
    audit_action: str,
    timestamp: datetime,
    event_id: str,
    tenant_id: str,
    tenant_name: str,
    summary: str,
    user_id: str | None = None,
    user_name: str | None = None,
    user_roles: tuple[str, ...] = (),
    source_ip: str | None = None,
    user_agent: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor_type: str = "user",
    agent_key: str | None = None,
    record_count: int | None = None,
    succeeded: bool = True,
) -> SiemEvent:
    """Map one audit row onto the standard schema.

    An unmapped action raises rather than being dropped. An event silently not
    exported is indistinguishable, in the client's SIEM, from an event that
    never happened.
    """
    mapping = _ACTION_MAP.get(audit_action)
    if mapping is None:
        raise SiemError(
            f"Audit action {audit_action!r} has no SIEM mapping. It would be "
            "silently absent from the client's feed, which in their SIEM looks "
            "exactly like it never happened. Add it to _ACTION_MAP."
        )
    category, dataset = mapping

    return SiemEvent(
        timestamp=timestamp,
        action=audit_action,
        category=category,
        dataset=dataset,
        outcome=EventOutcome.SUCCESS if succeeded else EventOutcome.FAILURE,
        severity=_SEVERITY.get(dataset, 10),
        event_id=event_id,
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        message=summary,
        user_id=user_id,
        user_name=user_name,
        user_roles=user_roles,
        source_ip=source_ip,
        user_agent=user_agent,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_type=actor_type,
        agent_key=agent_key,
        record_count=record_count,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Delivery health
# ══════════════════════════════════════════════════════════════════════════════


class HealthState(str, enum.Enum):
    HEALTHY = "healthy"
    #: Errors, and the client's SOC will see the gap.
    FAILING = "failing"
    #: No errors and no events either, for longer than expected. The dangerous
    #: one: a feed that died quietly is trusted.
    SILENT = "silent"
    #: Configured and never used.
    NEVER_DELIVERED = "never_delivered"

    @property
    def needs_alert(self) -> bool:
        return self is not HealthState.HEALTHY


@dataclass(frozen=True, slots=True)
class DeliveryHealth:
    """Whether one destination is actually receiving events."""

    destination: str
    state: HealthState
    last_success_at: datetime | None
    last_failure_at: datetime | None
    consecutive_failures: int
    minutes_since_success: int | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "destination": self.destination,
            "state": self.state.value,
            "needs_alert": self.state.needs_alert,
            "last_success_at": (
                self.last_success_at.isoformat() if self.last_success_at else None
            ),
            "last_failure_at": (
                self.last_failure_at.isoformat() if self.last_failure_at else None
            ),
            "consecutive_failures": self.consecutive_failures,
            "minutes_since_success": self.minutes_since_success,
            "message": self.message,
        }


def delivery_health(
    *,
    destination: str,
    last_success_at: datetime | None,
    last_failure_at: datetime | None,
    consecutive_failures: int,
    expected_interval_minutes: int,
    now: datetime,
) -> DeliveryHealth:
    """Judge one destination, alerting on silence as well as on failure.

    Silence is the case people miss. A destination with no errors and no events
    for a day looks fine on a dashboard counting failures, and the client's SOC
    believes nothing has happened in our product for a day.
    """
    minutes_since = (
        int((now - last_success_at).total_seconds() // 60)
        if last_success_at
        else None
    )

    if last_success_at is None and consecutive_failures == 0:
        return DeliveryHealth(
            destination=destination,
            state=HealthState.NEVER_DELIVERED,
            last_success_at=None,
            last_failure_at=last_failure_at,
            consecutive_failures=consecutive_failures,
            minutes_since_success=None,
            message=(
                "This destination is configured and has never received an "
                "event. Either nothing has happened, or delivery has never "
                "worked — and the client's SOC cannot tell those apart."
            ),
        )

    if consecutive_failures > 0:
        return DeliveryHealth(
            destination=destination,
            state=HealthState.FAILING,
            last_success_at=last_success_at,
            last_failure_at=last_failure_at,
            consecutive_failures=consecutive_failures,
            minutes_since_success=minutes_since,
            message=(
                f"{consecutive_failures} consecutive delivery failure(s). The "
                "client's SOC has a gap in their AskMira events and will not "
                "know unless we tell them."
            ),
        )

    if minutes_since is not None and minutes_since > expected_interval_minutes:
        return DeliveryHealth(
            destination=destination,
            state=HealthState.SILENT,
            last_success_at=last_success_at,
            last_failure_at=last_failure_at,
            consecutive_failures=0,
            minutes_since_success=minutes_since,
            message=(
                f"No event delivered for {minutes_since} minutes, against an "
                f"expected interval of {expected_interval_minutes}. Nothing has "
                "errored, which is what makes this the dangerous state: a SOC "
                "with a dead feed believes nothing has happened here."
            ),
        )

    return DeliveryHealth(
        destination=destination,
        state=HealthState.HEALTHY,
        last_success_at=last_success_at,
        last_failure_at=last_failure_at,
        consecutive_failures=0,
        minutes_since_success=minutes_since,
        message="Delivering.",
    )


def schema_documentation() -> dict[str, Any]:
    """What a client's SIEM engineer needs to build a parser.

    Published rather than described on a call. Every field, its ECS name, and
    what we put in it.
    """
    return {
        "schema": "Elastic Common Schema (ECS)",
        "why": (
            "Your SOC already has parsers and detections built on ECS field "
            "names. A bespoke format means somebody on your side writes a "
            "mapping, gets one field slightly wrong, and the detection that "
            "should fire on a bulk export does not."
        ),
        "fields": sorted(ECS_FIELDS),
        "categories": EXPORTED_CATEGORIES,
        "extension": (
            "One value is ours rather than ECS's: agent actions are emitted "
            "with event.category 'process' and event.dataset "
            "'askmira.agent_action'. ECS has no category for an AI agent doing "
            "something, and extending the vocabulary of an existing field is "
            "safer than adding a field your parser would drop."
        ),
        "delivery": (
            "Newline-delimited JSON over HTTPS. Failure and silence both raise "
            "an alert — a feed that stops without erroring is trusted, which is "
            "worse than one that visibly fails."
        ),
        "not_built": (
            "No transport is implemented. Events are mapped and their delivery "
            "state is modelled; nothing opens a connection to Splunk, Sentinel "
            "or Datadog."
        ),
    }


def next_retry_delay(consecutive_failures: int) -> timedelta:
    """Exponential backoff, capped.

    Capped at an hour rather than growing without limit: a destination that has
    been failing for a day should still be retried hourly, because the fix is
    usually on the client's side and we will not be told when it lands.
    """
    seconds = min(3600, 30 * (2 ** min(consecutive_failures, 8)))
    return timedelta(seconds=seconds)
