"""Lifecycle events: approved, effective-dated, and applied as a whole.

A transfer, a promotion and a confirmation are not field edits. They are
decisions with an approver, an effective date, a letter, a payroll consequence
and a place in somebody's employment history — and modelling them as edits loses
every one of those.

Two columns carry most of the weight.

**`effective_from` is when it happens, not when it was approved.** A promotion
approved in March effective April must not change the employee record in March,
and must change it on 1 April whether or not anybody remembers. `EFFECTED` is a
separate status from `APPROVED` for exactly that reason, and `effected_at`
records when the change actually landed.

**`previous_values` is recorded, not derived.** "What was their grade before the
promotion" has to be answerable from the event itself, because the employee
record has moved on and reconstructing it from an audit log is a reconstruction.
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
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class LifecycleEventType(str, enum.Enum):
    CONFIRMATION = "confirmation"
    PROBATION_EXTENSION = "probation_extension"
    TRANSFER = "transfer"
    PROMOTION = "promotion"
    DEMOTION = "demotion"
    REDESIGNATION = "redesignation"
    DEPUTATION = "deputation"
    GRADE_CHANGE = "grade_change"
    DEPARTMENT_CHANGE = "department_change"
    LOCATION_CHANGE = "location_change"
    MANAGER_CHANGE = "manager_change"
    EMPLOYMENT_TYPE_CHANGE = "employment_type_change"


class LifecycleStatus(str, enum.Enum):
    """Where an event stands.

    APPROVED and EFFECTED are deliberately different. An approved future-dated
    promotion is a decision that has been made and has not happened yet, and
    collapsing the two would either apply it early or leave it needing somebody
    to remember.
    """

    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    EFFECTED = "effected"
    CANCELLED = "cancelled"
    #: The effect job ran and could not complete the cascade. The whole cascade
    #: was rolled back — this is a row saying so, because a half-applied
    #: promotion that nobody knows failed is much worse than a visible failure.
    FAILED = "failed"


class SilencePolicy(str, enum.Enum):
    """What a tenant does when a probation review passes with no decision.

    No default. Auto-confirming and requiring a decision lead to opposite
    outcomes for a real person, and a product that picked one silently would be
    making an employment decision on a client's behalf.
    """

    AUTO_CONFIRM = "auto_confirm"
    REQUIRE_DECISION = "require_decision"
    AUTO_EXTEND_ONCE = "auto_extend_once"


class LifecycleEvent(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One approved, effective-dated change to somebody's employment."""

    __tablename__ = "lifecycle_events"
    __table_args__ = (
        Index("ix_lifecycle_effective", "tenant_id", "status", "effective_from"),
        Index("ix_lifecycle_employee", "tenant_id", "employee_id", "effective_from"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    event_type: Mapped[LifecycleEventType] = mapped_column(
        Enum(LifecycleEventType, name="lifecycle_event_type"),
        nullable=False, index=True,
    )

    #: When it takes effect. Not when it was approved.
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    #: The employee record as it stood. Recorded rather than derived: the record
    #: has moved on, and reconstructing it from an audit log is a reconstruction.
    previous_values: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    new_values: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[LifecycleStatus] = mapped_column(
        Enum(LifecycleStatus, name="lifecycle_status"),
        default=LifecycleStatus.DRAFT, nullable=False, index=True,
    )

    #: What the approver was shown. Snapshotted, so "did they know PT would
    #: change" is answerable about the moment they signed rather than about what
    #: the code would say today.
    consequences_shown: Mapped[list] = mapped_column(
        JSONB, default=list, nullable=False
    )

    # ── What the cascade produced ───────────────────────────────────────────
    letter_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("issued_letters.id", ondelete="SET NULL"),
        nullable=True,
    )
    salary_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employee_salaries.id", ondelete="SET NULL"),
        nullable=True,
    )
    position_assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("position_assignments.id", ondelete="SET NULL"),
        nullable=True,
    )
    new_ctc_annual: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)

    # ── People and timing ───────────────────────────────────────────────────
    raised_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: When the change actually landed on the record. Separate from
    #: `approved_at`, and the difference is the whole point of future dating.
    effected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Why the cascade failed, where it did. A half-applied promotion nobody
    #: knows about is much worse than a visible failure.
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Groups a bulk raise. An increment cycle is one decision producing four
    #: hundred events, and "show me that cycle" has to be one query.
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("lifecycle_batches.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    batch: Mapped[LifecycleBatch | None] = relationship(back_populates="events")


class LifecycleBatch(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One bulk raise: an increment cycle, a reorg, a mass transfer.

    Per-employee results live on the events themselves; this row is the handle
    for the whole thing and the record of who raised four hundred changes at
    once.
    """

    __tablename__ = "lifecycle_batches"

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    event_type: Mapped[LifecycleEventType] = mapped_column(
        Enum(LifecycleEventType, name="lifecycle_event_type", create_type=False),
        nullable=False,
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    #: Whether each event needs its own approval or the batch is approved once.
    #: Per tenant configuration: approving four hundred promotions individually
    #: is theatre, and approving a mass termination in one click is not.
    approve_individually: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    raised_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    events: Mapped[list[LifecycleEvent]] = relationship(
        back_populates="batch", lazy="selectin"
    )


class ProbationPolicy(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One tenant's probation configuration.

    `silence_policy` has no column default on purpose. A tenant that has not
    configured this gets an error when the sweep runs, not a behaviour — the two
    live options lead to opposite outcomes for a real person.
    """

    __tablename__ = "probation_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_probation_policy_per_tenant"),
        CheckConstraint("grace_days >= 0", name="ck_probation_grace"),
    )

    silence_policy: Mapped[SilencePolicy] = mapped_column(
        Enum(SilencePolicy, name="probation_silence_policy"), nullable=False
    )
    #: Days past the review date before the silence policy fires. A weekend and
    #: a public holiday should not auto-confirm somebody.
    grace_days: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    #: Whether a confirmation carries a salary revision by default. Common in
    #: the Indian mid-market and absent elsewhere.
    confirmation_raises_salary: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    #: Who to escalate to when a manager and their manager both stay silent.
    hr_escalation_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    configured_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ProbationReminder(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One reminder that was sent about one review. Insert-only.

    Recorded so a sweep run twice does not send twice, and so "how many times
    was this manager asked" is answerable when somebody drifted past their date
    anyway.
    """

    __tablename__ = "probation_reminders"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "review_id", "step", name="uq_probation_reminder_once"
        ),
    )

    review_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("probation_reviews.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    #: manager_first | manager_second | skip_level | hr
    step: Mapped[str] = mapped_column(String(32), nullable=False)
    recipient_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    days_until_due: Mapped[int] = mapped_column(Integer, nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
