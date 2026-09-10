"""The appraisal chain: cycles, stages, and what happens at each one.

360 feedback and the appraisal chain are different things, and this is the
second one. Feedback is input; the chain is the decision process — self, manager,
reviewer, HR — and it is what Indian companies actually run.

Three decisions in this schema are worth stating.

**A return is first class, not an exception path.** A manager sending a
self-appraisal back for more detail is ordinary, happens constantly, and a
workflow that only moves forward forces people to work around it. So
`AppraisalStageEntry` records `returned_from_stage` and a mandatory reason, and
returns are part of the normal history rather than an error.

**Delegation keeps the original owner.** When a manager on leave delegates,
`delegated_from_user_id` records who it should have been. An audit trail that
shows only who acted cannot answer "why did someone outside this reporting line
rate me", which is exactly the question an appraisal dispute turns on.

**Acknowledgement is not agreement.** An employee can acknowledge *with a
written objection*, and the cycle still closes. Forcing agreement as the only
way to close is how an appraisal dispute becomes an employment dispute — the
objection routes to HR and stays attached to the record.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
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
from app.models.cycle_config import AutoAdvanceBehaviour


class CycleType(str, enum.Enum):
    ANNUAL = "annual"
    HALF_YEARLY = "half_yearly"
    QUARTERLY = "quarterly"
    PROBATION = "probation"
    OFF_CYCLE = "off_cycle"


class CycleStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    IN_CALIBRATION = "in_calibration"
    CLOSED = "closed"


class StageRole(str, enum.Enum):
    SELF = "self"
    MANAGER = "manager"
    REVIEWER = "reviewer"
    HR = "hr"
    NORMALISATION = "normalisation"


class AppraisalStatus(str, enum.Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    # Sent back to an earlier stage. Ordinary, not exceptional.
    RETURNED = "returned"
    COMPLETED = "completed"
    # Released to the employee, awaiting their acknowledgement.
    RELEASED = "released"
    ACKNOWLEDGED = "acknowledged"


class AcknowledgementType(str, enum.Enum):
    AGREED = "agreed"
    # Acknowledged, with a written objection. The cycle still closes.
    DISAGREED = "disagreed"


class AppraisalCycle(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One review round."""

    __tablename__ = "appraisal_cycles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", "period_start", name="uq_appraisal_cycle"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    cycle_type: Mapped[CycleType] = mapped_column(
        Enum(CycleType, name="appraisal_cycle_type"), nullable=False
    )
    status: Mapped[CycleStatus] = mapped_column(
        Enum(CycleStatus, name="appraisal_cycle_status"),
        default=CycleStatus.DRAFT,
        nullable=False,
        index=True,
    )

    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)

    # {"grades": [...], "locations": [...], "min_service_months": 6,
    #  "exclude_employee_ids": [...]} — who is in the cycle. Held as data so
    #  HR can change it without an engineering change.
    eligibility_rule: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # Days past a stage due date before the stage escalates to the next level
    # up. Without this a cycle stalls on one absent manager and HR chases
    # people by hand.
    escalation_after_days: Mapped[int] = mapped_column(Integer, default=7, nullable=False)

    launched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    stages: Mapped[list[AppraisalStage]] = relationship(
        back_populates="cycle", cascade="all, delete-orphan"
    )


class AppraisalStage(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One step in the chain, and what it can see."""

    __tablename__ = "appraisal_stages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "cycle_id", "sequence", name="uq_appraisal_stage_seq"),
    )

    cycle_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("appraisal_cycles.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[StageRole] = mapped_column(
        Enum(StageRole, name="appraisal_stage_role"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    is_mandatory: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    can_reopen: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ── The stage window (slice 18) ─────────────────────────────────────────
    # `due_date` is what a reminder counts against; `open_date` and
    # `close_date` bound when the stage can actually be filled in.
    open_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    close_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    grace_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # What happens to an appraisal still sitting here when the window closes.
    # ESCALATE by default rather than ADVANCE: a cycle that stalls on one
    # absent manager is a visible failure somebody chases, whereas one that
    # silently skipped the manager review for forty people is not noticed
    # until the ratings look wrong.
    auto_advance: Mapped[AutoAdvanceBehaviour] = mapped_column(
        Enum(AutoAdvanceBehaviour, name="auto_advance_behaviour"),
        default=AutoAdvanceBehaviour.ESCALATE,
        nullable=False,
    )

    # ── Visibility, per stage ───────────────────────────────────────────────
    # Whether this stage's owner sees what earlier stages wrote. A manager
    # usually sees the self-appraisal before rating; a reviewer usually sees
    # both. These are defaults a tenant overrides, not rules.
    sees_previous_entries: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    # Whether the employee can see this stage's comments before release.
    # Usually false — manager comments visible mid-cycle change what managers
    # write, which is not the same as changing what they think.
    visible_to_employee_before_release: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    cycle: Mapped[AppraisalCycle] = relationship(back_populates="stages")


class Appraisal(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One employee's appraisal in one cycle."""

    __tablename__ = "appraisals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "cycle_id", "employee_id", name="uq_appraisal_subject"),
    )

    cycle_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("appraisal_cycles.id", ondelete="CASCADE"),
        nullable=False,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )

    current_stage_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("appraisal_stages.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[AppraisalStatus] = mapped_column(
        Enum(AppraisalStatus, name="appraisal_status"),
        default=AppraisalStatus.NOT_STARTED,
        nullable=False,
        index=True,
    )

    final_rating: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Acknowledgement ─────────────────────────────────────────────────────
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acknowledgement_type: Mapped[AcknowledgementType | None] = mapped_column(
        Enum(AcknowledgementType, name="acknowledgement_type"), nullable=True
    )
    # The employee's written objection, where they disagreed. Kept on the
    # record permanently — it is the employee's account of the year, and it is
    # what a later dispute rests on.
    objection: Mapped[str | None] = mapped_column(Text, nullable=True)
    objection_routed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Reopening ───────────────────────────────────────────────────────────
    reopened_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reopen_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What reopening this would disturb — an increment already granted on the
    # old rating, say. Recorded at reopen time because after the fact nobody
    # can reconstruct what the rating had already fed.
    downstream_impact: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    entries: Mapped[list[AppraisalStageEntry]] = relationship(
        back_populates="appraisal", cascade="all, delete-orphan"
    )


class AppraisalStageEntry(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """What one person wrote at one stage.

    Insert-only in spirit: a return creates a new entry rather than editing the
    old one, so the history shows the round trip. An appraisal that reads as
    though it went straight through when it went back twice is not a record of
    what happened.
    """

    __tablename__ = "appraisal_stage_entries"

    appraisal_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("appraisals.id", ondelete="CASCADE"), nullable=False
    )
    stage_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("appraisal_stages.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Who actually filled it in.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Who it should have been, where the stage was delegated. An audit trail
    # showing only who acted cannot answer "why did someone outside my
    # reporting line rate me".
    delegated_from_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # True when the stage escalated past its owner because they were overdue.
    is_escalated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    ratings: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Returns ─────────────────────────────────────────────────────────────
    # Set when this entry sent the appraisal back. Both the target and the
    # reason, because "returned" without a reason tells the recipient nothing
    # about what to change.
    returned_to_stage_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("appraisal_stages.id", ondelete="SET NULL"),
        nullable=True,
    )
    returned_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    appraisal: Mapped[Appraisal] = relationship(back_populates="entries")


class StageDelegation(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A stage owner handing their stage to someone else while away.

    Explicit and time-boxed. A standing delegation with no end date is how a
    manager who returned from leave three months ago still is not rating
    anyone.
    """

    __tablename__ = "stage_delegations"

    cycle_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("appraisal_cycles.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    to_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
