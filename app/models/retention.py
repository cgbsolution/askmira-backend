"""Retention: policies, holds, executed actions and the suppression list.

Four tables, and the shape of each follows from something that has to be
answerable after the fact:

- **`retention_policies`** stores `statutory_minimum_months` alongside the
  configured period. Denormalised on purpose: the floor as it stood when the
  policy was written is the evidence that the tenant was told, and recomputing
  it later from a catalogue that has since been revised would answer a different
  question.

- **`legal_holds`** has `released_at`, not a boolean. "Is this held" and "was
  this held on 3 March" are different questions and a flag only answers the
  first — which is exactly the question nobody asks until litigation.

- **`retention_action_logs`** is insert-only and stores **hashed** sample ids.
  A log of what was deleted that contains readable identifiers of the deleted is
  a copy of the thing that was deleted.

- **`erasure_suppressions`** exists because deleting from the live system does
  not reach backups already written. It is the list a restore has to apply.
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


class RetentionAnchor(str, enum.Enum):
    RECORD_CREATED = "record_created"
    EMPLOYMENT_ENDED = "employment_ended"
    FINANCIAL_YEAR_END = "financial_year_end"
    LAST_ACCESSED = "last_accessed"


class RetentionActionKind(str, enum.Enum):
    DELETE = "delete"
    ANONYMISE = "anonymise"
    ARCHIVE = "archive"


class RetentionRunStatus(str, enum.Enum):
    #: Planned and reported; nothing touched.
    DRY_RUN = "dry_run"
    #: Over the volume threshold, waiting for a person.
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    EXECUTED = "executed"
    #: Somebody looked at the plan and said no.
    REJECTED = "rejected"
    FAILED = "failed"


class RetentionPolicyRow(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One tenant's retention rule for one data category.

    `statutory_minimum_months` is stored, not derived at read time. The floor as
    it stood when the policy was written is what the tenant was shown and agreed
    to; recomputing it later from a revised catalogue would silently rewrite
    what they were told.
    """

    __tablename__ = "retention_policies"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "data_category_key", name="uq_retention_policy_per_category"
        ),
        CheckConstraint(
            "retention_period_months >= statutory_minimum_months",
            name="ck_retention_above_floor",
        ),
        CheckConstraint(
            "retention_period_months >= 1", name="ck_retention_positive"
        ),
    )

    #: A key from `dpdp.categories.CATEGORIES`. Not a foreign key: the catalogue
    #: is code and gets reviewed as code.
    data_category_key: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    retention_period_months: Mapped[int] = mapped_column(Integer, nullable=False)
    anchor_event: Mapped[RetentionAnchor] = mapped_column(
        Enum(RetentionAnchor, name="retention_anchor"), nullable=False
    )
    action: Mapped[RetentionActionKind] = mapped_column(
        Enum(RetentionActionKind, name="retention_action_kind"), nullable=False
    )
    #: The floor. A database constraint enforces the period is not below it, so
    #: even a direct SQL update cannot produce an unlawful policy.
    statutory_minimum_months: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    legal_basis: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    configured_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class LegalHold(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A freeze on retention for a scope of records.

    `released_at` rather than an `is_active` boolean. "Was this record held on
    the day the job ran" is the question that matters in a dispute, and a flag
    that was flipped last month cannot answer it.
    """

    __tablename__ = "legal_holds"
    __table_args__ = (
        Index("ix_legal_hold_open", "tenant_id", "released_at"),
    )

    reason: Mapped[str] = mapped_column(Text, nullable=False)
    #: Empty means all. A hold with every dimension empty freezes the whole
    #: tenant, which is a legitimate instruction from a regulator and is treated
    #: as the broadest hold rather than as no hold.
    employee_ids: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    departments: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    from_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    to_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    placed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    released_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    release_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def is_active(self) -> bool:
        return self.released_at is None


class RetentionRun(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One evaluation of the policies: what would happen, and whether it did."""

    __tablename__ = "retention_runs"
    __table_args__ = (
        Index("ix_retention_run_when", "tenant_id", "evaluated_on"),
        CheckConstraint("records_to_act >= 0", name="ck_retention_run_counts"),
    )

    evaluated_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[RetentionRunStatus] = mapped_column(
        Enum(RetentionRunStatus, name="retention_run_status"),
        default=RetentionRunStatus.DRY_RUN,
        nullable=False,
        index=True,
    )
    records_evaluated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_to_act: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_held: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_blocked: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    counts_by_action: Mapped[dict] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    approval_threshold: Mapped[int] = mapped_column(Integer, nullable=False)

    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rejected_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: The plan as it was shown to the approver. Snapshotted, so "what did they
    #: agree to" is a question about that moment rather than about what the
    #: policies would produce today.
    plan_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class RetentionActionLog(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One executed action. Insert-only, and the sample ids are hashed.

    A log of what was deleted that contains readable identifiers of the deleted
    is a copy of the thing that was deleted — the record survives its own
    erasure inside the evidence that it was erased. So `sample_record_hashes`
    holds HMACs: enough to prove a specific record was in a run when somebody
    asks, not enough to reconstruct the run.
    """

    __tablename__ = "retention_action_logs"
    __table_args__ = (
        Index("ix_retention_log_run", "tenant_id", "run_id"),
        CheckConstraint("record_count >= 0", name="ck_retention_log_count"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("retention_runs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    data_category_key: Mapped[str] = mapped_column(String(64), nullable=False)
    record_type: Mapped[str] = mapped_column(String(120), nullable=False)
    action: Mapped[RetentionActionKind] = mapped_column(
        Enum(RetentionActionKind, name="retention_action_kind", create_type=False),
        nullable=False,
    )
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    #: Who authorised it, where authorisation was needed. Null below the
    #: threshold, and the run row records what the threshold was.
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Hashed. See the class docstring.
    sample_record_hashes: Mapped[list] = mapped_column(
        JSONB, default=list, nullable=False
    )
    policy_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class ErasureSuppression(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """An identifier that must not come back from a backup restore.

    Deleting from the live system does not reach a backup already written. This
    is the list a restore applies so an honoured erasure stays honoured.

    **Nothing reads this yet.** There is no restore procedure in this codebase,
    so the list is written and never applied — stated here and in the Trust
    Center rather than left for somebody to discover.
    """

    __tablename__ = "erasure_suppressions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "record_table",
            "record_id",
            name="uq_erasure_suppression_record",
        ),
    )

    record_table: Mapped[str] = mapped_column(String(120), nullable=False)
    record_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True, index=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    suppressed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    #: The day this can be dropped: once every backup written before the
    #: deletion has aged out, the suppression has nothing left to suppress.
    expires_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    #: Which rights request, where the suppression came from one.
    source_request_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("data_principal_requests.id", ondelete="SET NULL"),
        nullable=True,
    )
