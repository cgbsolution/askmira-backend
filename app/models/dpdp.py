"""DPDP records: consent, rights requests, breaches and transfers.

Four tables, and each one exists because a question has to be answerable after
the fact rather than in the moment:

- **`consent_records` is insert-only.** A withdrawal is a new row. "When did
  they turn this off, and what had they agreed to before" is not answerable from
  a mutable boolean, and it is exactly what gets asked when a consent is
  challenged. The `notice_id` and `notice_version` columns are the other half:
  a consent recorded against a notice whose text has since changed is a consent
  to something nobody can now read.

- **`data_principal_requests` carries its own clock.** `due_by` is stored, not
  computed at read time, because a tenant that shortens its published SLA next
  month must not retrospectively make last month's answered request late — or,
  worse, retrospectively make an open one on time.

- **`breach_incidents` stores `became_aware_at` separately from `created_at`.**
  The row is often created hours after the moment the clock started, and the
  clock runs from the moment, not the row.

- **`cross_border_transfers` is one row per transfer, not per configuration.**
  "Has any of our data gone to a model provider" is not answered by a settings
  screen.
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class DataPrincipalKind(str, enum.Enum):
    EMPLOYEE = "employee"
    CANDIDATE = "candidate"
    DEPENDENT = "dependent"
    EMERGENCY_CONTACT = "emergency_contact"
    PLATFORM_USER = "platform_user"
    THIRD_PARTY = "third_party"


class ConsentCapturedVia(str, enum.Enum):
    ONBOARDING = "onboarding"
    SELF_SERVICE = "self_service"
    HR_ON_BEHALF = "hr_on_behalf"
    CAREERS_PORTAL = "careers_portal"
    IMPORTED = "imported"


class DprRequestType(str, enum.Enum):
    ACCESS = "access"
    CORRECTION = "correction"
    ERASURE = "erasure"
    GRIEVANCE = "grievance"
    NOMINATION = "nomination"


class DprRequestStatus(str, enum.Enum):
    RECEIVED = "received"
    VERIFYING_IDENTITY = "verifying_identity"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    REJECTED = "rejected"
    ESCALATED = "escalated"


class BreachSeverityLevel(str, enum.Enum):
    NEGLIGIBLE = "negligible"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    SEVERE = "severe"


class ConsentRecord(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One recorded consent decision. Insert-only.

    There is no `withdrawn_at` column and that is deliberate. A withdrawal is a
    row with `granted=False`; storing it as a mutation of the grant would lose
    the grant, and "they consented in March and withdrew in July" would become
    "they did not consent", which is a different and false statement.
    """

    __tablename__ = "consent_records"
    __table_args__ = (
        Index(
            "ix_consent_principal",
            "tenant_id",
            "principal_kind",
            "principal_id",
            "purpose_key",
            "decided_at",
        ),
        CheckConstraint("notice_version >= 1", name="ck_consent_notice_version"),
    )

    #: Stable across employee/candidate conversion, so a candidate who is hired
    #: keeps the consent history they gave as a candidate rather than starting
    #: again with a blank record.
    principal_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    principal_kind: Mapped[DataPrincipalKind] = mapped_column(
        Enum(DataPrincipalKind, name="dpdp_principal_kind"), nullable=False
    )
    #: A key from `services.dpdp.consent.PURPOSES`. Not a foreign key: the
    #: catalogue is code, reviewed in code, and a database row that could drift
    #: from it would be a second source of truth for the lawful basis.
    purpose_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    #: The version of the purpose they agreed to. A later version is a different
    #: thing to have agreed to, and the state machine reports the difference as
    #: stale rather than carrying the old tick forward.
    purpose_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    captured_via: Mapped[ConsentCapturedVia] = mapped_column(
        Enum(ConsentCapturedVia, name="dpdp_consent_captured_via"), nullable=False
    )
    #: Which notice they were shown, and its version. Without these, "what did
    #: it say when they agreed" is answerable only from today's template.
    notice_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notice_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    notice_language: Mapped[str | None] = mapped_column(String(8), nullable=True)

    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class DataPrincipalRequest(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One exercised right, with its clock."""

    __tablename__ = "data_principal_requests"
    __table_args__ = (
        Index("ix_dpr_open", "tenant_id", "status", "due_by"),
        Index("ix_dpr_principal", "tenant_id", "principal_id", "received_at"),
        CheckConstraint("sla_days >= 1", name="ck_dpr_sla_positive"),
    )

    principal_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    principal_kind: Mapped[DataPrincipalKind] = mapped_column(
        Enum(DataPrincipalKind, name="dpdp_principal_kind", create_type=False),
        nullable=False,
    )
    #: Denormalised so a request from somebody whose record was subsequently
    #: erased is still contactable and still auditable. The one place in this
    #: module where duplicating a name is the right call.
    principal_label: Mapped[str] = mapped_column(String(200), nullable=False)
    principal_contact: Mapped[str | None] = mapped_column(String(200), nullable=True)

    request_type: Mapped[DprRequestType] = mapped_column(
        Enum(DprRequestType, name="dpdp_request_type"), nullable=False, index=True
    )
    status: Mapped[DprRequestStatus] = mapped_column(
        Enum(DprRequestStatus, name="dpdp_request_status"),
        default=DprRequestStatus.RECEIVED,
        nullable=False,
        index=True,
    )
    details: Mapped[str | None] = mapped_column(Text, nullable=True)

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    #: Stored, not computed. A tenant shortening its published SLA next month
    #: must not retrospectively change whether this one was answered on time.
    due_by: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    escalate_at: Mapped[date] = mapped_column(Date, nullable=False)
    sla_days: Mapped[int] = mapped_column(Integer, nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: The answer. For ACCESS, the export manifest; for ERASURE, the plan with
    #: every category's decision and the statute behind each retention.
    response_payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    #: Why, where a request was refused in whole or in part. Never null on a
    #: REJECTED row — checked in the service, because a refusal with no reason
    #: is the thing DPDP most directly forbids.
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    assigned_to_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Insert-only trail of what was done. A list of
    #: `{at, actor, action, note}` — the evidence that the request was worked
    #: rather than left until the day before the deadline.
    actions: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)


class BreachIncident(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One personal-data breach, its clocks and its evidence trail."""

    __tablename__ = "breach_incidents"
    __table_args__ = (
        Index("ix_breach_open", "tenant_id", "became_aware_at"),
        CheckConstraint("affected_principal_count >= 0", name="ck_breach_affected_count"),
    )

    reference: Mapped[str] = mapped_column(String(40), nullable=False)
    #: When the clock started. Separate from `created_at`: the row is usually
    #: written hours after the moment somebody first knew, and the statute runs
    #: from the moment.
    became_aware_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    #: Category keys from the catalogue.
    affected_data_categories: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    affected_principal_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    data_left_our_control: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    severity: Mapped[BreachSeverityLevel] = mapped_column(
        Enum(BreachSeverityLevel, name="dpdp_breach_severity"), nullable=False
    )

    board_report_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reported_to_board_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    principals_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: What was actually sent, kept verbatim. Regenerating it from today's
    #: template later would answer "what would we say" rather than "what did we
    #: say", and only the second is evidence.
    notification_sent: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    remediation: Mapped[str | None] = mapped_column(Text, nullable=True)
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: True for a rehearsal. Kept in the same table as real incidents so a
    #: rehearsal exercises the real code path, and flagged so it can never be
    #: counted in a breach history shown to a customer or a regulator.
    is_rehearsal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    raised_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class CrossBorderTransfer(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One transfer of personal data to a sub-processor. Insert-only.

    Per transfer, not per configuration. A tenant asking "has any of our
    employees' data been sent to a model provider" is asking about events, and
    a settings screen showing the AI assistant is enabled does not answer it.
    """

    __tablename__ = "cross_border_transfers"
    __table_args__ = (
        Index("ix_transfer_when", "tenant_id", "occurred_at"),
        Index("ix_transfer_processor", "tenant_id", "sub_processor_key"),
    )

    sub_processor_key: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose_key: Mapped[str] = mapped_column(String(64), nullable=False)
    data_categories: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    principal_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    #: Denormalised at write time. Whether a transfer crossed a border is a fact
    #: about the day it happened; a sub-processor that later moves to an Indian
    #: region must not rewrite the history of what already left.
    was_cross_border: Mapped[bool] = mapped_column(Boolean, nullable=False)
    initiated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class DpdpConfiguration(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One tenant's DPDP settings.

    `grievance_contact` has no default. Every notice this product renders points
    the reader at a grievance route, and a route that defaults to a placeholder
    is a notice that tells somebody to write to nobody.
    """

    __tablename__ = "dpdp_configurations"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_dpdp_config_per_tenant"),)

    #: The tenant is the Data Fiduciary; we are the Data Processor. This is
    #: their DPO or grievance officer, not ours.
    grievance_contact: Mapped[str] = mapped_column(String(200), nullable=False)
    data_protection_officer_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Their published response periods, per request type. Validated against the
    #: statutory outer bound on write, so a configuration screen cannot commit
    #: the tenant to something unlawful.
    sla_days: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    #: Default language for notices. An Eighth Schedule code or "en".
    default_notice_language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)
    #: Whether this tenant has elected optional AI processing at all. Distinct
    #: from any individual's consent: the tenant switching it off means nobody
    #: is even asked.
    ai_processing_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    configured_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
