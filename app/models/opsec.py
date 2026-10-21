"""Operational security records: sessions, SIEM, drills and findings.

The shapes follow from what a procurement questionnaire asks and what an
incident needs:

- **`user_sessions` records the device and the address**, because "list my
  active sessions and let me kill one" is only useful if the person can tell
  which is which. A list of five identical rows is a list nobody acts on.

- **`siem_deliveries` records successes as well as failures.** Health is
  measured on silence too — a destination with no errors and no events for a
  day is a dead feed that a SOC is trusting, and you cannot see that in a table
  of failures.

- **`restore_drills` stores the measured durations beside the published
  targets.** A drill that took longer than the RTO is evidence against the
  published number, and the register keeps it rather than the number that reads
  better.

- **`pentest_findings` runs its SLA clock from `discovered_on`.** An SLA
  measured from triage is met by triaging late.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class SiemVendorEnum(str, enum.Enum):
    SPLUNK = "splunk"
    SENTINEL = "microsoft_sentinel"
    DATADOG = "datadog"
    GENERIC_HTTPS = "generic_https"


class DeliveryStateEnum(str, enum.Enum):
    HEALTHY = "healthy"
    FAILING = "failing"
    SILENT = "silent"
    NEVER_DELIVERED = "never_delivered"


class DataTierEnum(str, enum.Enum):
    CRITICAL = "critical"
    CORE = "core"
    ANALYTICAL = "analytical"


class DrillOutcomeEnum(str, enum.Enum):
    MET = "met"
    EXCEEDED_RTO = "exceeded_rto"
    EXCEEDED_RPO = "exceeded_rpo"
    FAILED = "failed"


class FindingSeverityEnum(str, enum.Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFORMATIONAL = "informational"


class TenantSessionPolicy(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One tenant's session and access rules."""

    __tablename__ = "tenant_session_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_session_policy_per_tenant"),
        CheckConstraint("idle_timeout_minutes >= 1", name="ck_session_idle_positive"),
        CheckConstraint("absolute_lifetime_hours >= 1", name="ck_session_lifetime_positive"),
        CheckConstraint("max_concurrent_sessions >= 0", name="ck_session_concurrency"),
    )

    idle_timeout_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    absolute_lifetime_hours: Mapped[int] = mapped_column(Integer, default=12, nullable=False)
    #: Zero means unlimited. A consultant on a laptop and a phone is not an
    #: attack, so a limit is opt-in.
    max_concurrent_sessions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enforce_ip_allowlist_for_privileged: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    #: Who must use two-step sign-in: "off", "privileged" (admins and
    #: sub-admins) or "everyone". Somebody it applies to who has not set it up
    #: can only reach sign-in and two-step set-up until they do.
    require_two_step: Mapped[str] = mapped_column(
        String(16), default="off", server_default="off", nullable=False
    )
    #: CIDR ranges. **Empty means unconfigured, which permits everything** —
    #: see `services.opsec.access.check_allowlist` for why that is not the same
    #: as deny-all.
    ip_allowlist: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    configured_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class UserSession(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One active or ended session, with enough to tell it apart from another.

    The device and the address are the point. "List your active sessions" with
    five identical rows is a list nobody can act on, and forced logout is only
    useful if the person can pick the right one.
    """

    __tablename__ = "user_sessions"
    __table_args__ = (
        Index("ix_session_user_active", "tenant_id", "user_id", "revoked_at"),
        Index("ix_session_last_seen", "tenant_id", "last_seen_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: A hash, never the token. A session table holding live refresh tokens is
    #: a credential store with a friendly name.
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(160), nullable=True)

    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: Best-effort, from the address. Shown as a hint, never as a fact — a VPN
    #: makes this wrong, and a UI presenting it as certain teaches people to
    #: dismiss a real alert.
    approximate_location: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: When the person last proved who they are, for step-up. Separate from
    #: session age on purpose: a ten-minute-old session is exactly as
    #: untrustworthy at a disbursement as a ten-hour-old one.
    step_up_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class StepUpChallenge(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One request for fresh proof before a high-impact action. Insert-only.

    Recorded whether or not it succeeded. A run of failed challenges against a
    disbursement release is exactly the signal a SOC wants, and it only exists
    if the failures are stored.
    """

    __tablename__ = "step_up_challenges"
    __table_args__ = (Index("ix_stepup_user", "tenant_id", "user_id", "created_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("user_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    action_key: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    #: Why the prompt appeared, shown to the person. An unexplained challenge is
    #: friction, and friction gets routed around.
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    satisfied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)


class SiemDestination(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Where a tenant's security events are shipped."""

    __tablename__ = "siem_destinations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_siem_destination_name"),
        CheckConstraint("expected_interval_minutes >= 1", name="ck_siem_interval_positive"),
    )

    name: Mapped[str] = mapped_column(String(80), nullable=False)
    vendor: Mapped[SiemVendorEnum] = mapped_column(
        Enum(SiemVendorEnum, name="siem_vendor"), nullable=False
    )
    endpoint_url: Mapped[str] = mapped_column(String(500), nullable=False)
    #: Encrypted at rest. Never returned by any endpoint.
    credential_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    #: How long a gap is normal for this tenant. A quiet tenant legitimately
    #: goes hours between events, and alerting them on a normal quiet period is
    #: how the alert gets muted.
    expected_interval_minutes: Mapped[int] = mapped_column(Integer, default=1440, nullable=False)
    #: Which datasets this destination receives. Empty means all.
    datasets: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    health_state: Mapped[DeliveryStateEnum] = mapped_column(
        Enum(DeliveryStateEnum, name="siem_delivery_state"),
        default=DeliveryStateEnum.NEVER_DELIVERED,
        nullable=False,
        index=True,
    )
    #: When somebody was last told this destination is unhealthy. Recorded so a
    #: failing feed is alerted once and then at a sensible interval, rather than
    #: every minute until the alert is muted.
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SiemDelivery(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One delivery attempt. Insert-only, successes included.

    Successes are stored because health is measured on silence: a destination
    with no failures and no events for a day is a dead feed, and a table of
    failures cannot show that.
    """

    __tablename__ = "siem_deliveries"
    __table_args__ = (
        Index("ix_siem_delivery_when", "tenant_id", "destination_id", "created_at"),
        CheckConstraint("event_count >= 0", name="ck_siem_event_count"),
    )

    destination_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("siem_destinations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: The transport's error, verbatim. A summarised error is one nobody can
    #: debug from.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class RestoreDrill(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One timed restore, with the measured numbers beside the published ones.

    Not tenant-specific in reality — a restore drill is a platform activity —
    but recorded per tenant so a client asking "when did you last test a
    restore" gets an answer they can see rather than one they are told.
    """

    __tablename__ = "restore_drills"
    __table_args__ = (
        Index("ix_drill_tier", "tenant_id", "tier", "performed_on"),
        CheckConstraint(
            "measured_rto_minutes >= 0 AND measured_rpo_minutes >= 0",
            name="ck_drill_durations_non_negative",
        ),
    )

    tier: Mapped[DataTierEnum] = mapped_column(
        Enum(DataTierEnum, name="restore_data_tier"), nullable=False, index=True
    )
    performed_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    #: Measured, not estimated. A drill whose duration was estimated is a
    #: rehearsal of the paperwork.
    measured_rto_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    measured_rpo_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The targets as published on the day. Stored so revising a published
    #: number later does not retroactively turn a breach into a pass.
    published_rto_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    published_rpo_minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    outcome: Mapped[DrillOutcomeEnum] = mapped_column(
        Enum(DrillOutcomeEnum, name="restore_drill_outcome"), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    performed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class PenTestFinding(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One finding from a third-party test, with its remediation clock."""

    __tablename__ = "pentest_findings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "reference", name="uq_finding_reference"),
        Index("ix_finding_open", "tenant_id", "severity", "remediated_on"),
    )

    reference: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    severity: Mapped[FindingSeverityEnum] = mapped_column(
        Enum(FindingSeverityEnum, name="pentest_finding_severity"),
        nullable=False,
        index=True,
    )
    area: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    #: The clock starts here, not at triage. An SLA measured from triage is met
    #: by triaging late.
    discovered_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    due_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    remediated_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: Set where the risk was accepted rather than fixed. Distinct from an
    #: unfixed finding, so an open critical cannot hide as a decision.
    risk_accepted_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    tester: Mapped[str | None] = mapped_column(String(160), nullable=True)
    #: Deliberately no reproduction detail. The summary is shareable with a
    #: client's procurement team, and a document telling somebody how to get in
    #: is not.
    remediation_note: Mapped[str | None] = mapped_column(Text, nullable=True)
