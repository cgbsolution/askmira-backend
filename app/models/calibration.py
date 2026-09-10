"""Calibration: policies, sessions, and the record of every rating change.

═══════════════════════════════════════════════════════════════════════════════
DEFENSIBILITY IS THE POINT OF THIS SCHEMA
═══════════════════════════════════════════════════════════════════════════════

A calibration session changes people's ratings — often downward, in a room they
were not in, by people who do not manage them. Months later one of those ratings
turns into a smaller increment, or no promotion, or a performance conversation,
and someone asks how it was arrived at.

`CalibrationChange` answers that: previous value, new value, who changed it,
when, and **why, in their own words**. Nothing is anonymous and nothing is
optional. A justification column that permits null is a column that will be
null exactly when it matters.

Changes are insert-only. A revised justification is a new row, so the history
shows the revision rather than replacing what was originally said.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
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


class CalibrationMode(str, enum.Enum):
    NONE = "none"
    # Show the gap, allow closure anyway.
    ADVISORY = "advisory"
    # Block closure while a scope is outside tolerance.
    ENFORCED = "enforced"


class CalibrationScope(str, enum.Enum):
    COMPANY = "company"
    DEPARTMENT = "department"
    MANAGER = "manager"
    GRADE = "grade"


class SessionStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class CalibrationPolicy(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """How calibration runs for one cycle.

    All three modes are first class. Clients differ strongly on whether a
    forced distribution is good practice, they decided before they bought the
    software, and the product does not take a view.
    """

    __tablename__ = "calibration_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "cycle_id", name="uq_calibration_policy_cycle"),
    )

    cycle_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("appraisal_cycles.id", ondelete="CASCADE"),
        nullable=False,
    )
    mode: Mapped[CalibrationMode] = mapped_column(
        Enum(CalibrationMode, name="calibration_mode"),
        default=CalibrationMode.ADVISORY,
        nullable=False,
    )
    scope: Mapped[CalibrationScope] = mapped_column(
        Enum(CalibrationScope, name="calibration_scope"),
        default=CalibrationScope.DEPARTMENT,
        nullable=False,
    )

    # Below this, a group is reported but not enforced. A manager with three
    # reports cannot express a 10% target, and forcing it means rating someone
    # by arithmetic rather than performance.
    min_group_size: Mapped[int] = mapped_column(Integer, default=8, nullable=False)

    # Whether the employee is told their rating was calibrated. A tenant
    # choice: some orgs are transparent about it, some treat the session as
    # internal. The final rating is always visible either way.
    disclose_calibration_to_employee: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    targets: Mapped[list[DistributionTarget]] = relationship(
        back_populates="policy", cascade="all, delete-orphan"
    )


class DistributionTarget(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """The share of a group expected at one rating, and the allowed drift."""

    __tablename__ = "distribution_targets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "policy_id", "rating_value", name="uq_distribution_target"
        ),
    )

    policy_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("calibration_policies.id", ondelete="CASCADE"),
        nullable=False,
    )
    rating_value: Mapped[str] = mapped_column(String(64), nullable=False)
    target_percent: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    tolerance_percent: Mapped[float] = mapped_column(
        Numeric(6, 2), default=0, nullable=False
    )

    policy: Mapped[CalibrationPolicy] = relationship(back_populates="targets")


class CalibrationSession(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One live session, over one scope."""

    __tablename__ = "calibration_sessions"

    cycle_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("appraisal_cycles.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    scope: Mapped[CalibrationScope] = mapped_column(
        Enum(CalibrationScope, name="calibration_scope"), nullable=False
    )
    # The department name, manager id, or grade this session covers.
    scope_value: Mapped[str] = mapped_column(String(128), nullable=False)

    facilitator_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Who was in the room. Recorded because "who was present when my rating
    # changed" is part of the same question as "who changed it".
    participant_user_ids: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, name="calibration_session_status"),
        default=SessionStatus.SCHEDULED,
        nullable=False,
        index=True,
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Locking writes the final ratings back to the appraisals.
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # The distribution as it stood when the session locked, so the shape that
    # was agreed is recoverable even after later reopenings.
    final_distribution: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    changes: Mapped[list[CalibrationChange]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class CalibrationChange(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One rating change, with who made it and why.

    Insert-only. A revised justification is a new row: replacing what was
    originally written would destroy exactly the evidence this table exists
    to hold.

    `justification` is NOT NULL deliberately. A nullable justification column
    is null precisely when someone later needs to read it.
    """

    __tablename__ = "calibration_changes"

    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("calibration_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    appraisal_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("appraisals.id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )

    previous_rating: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_rating: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    new_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)

    # Never anonymous. If a rating is disputed, the org has to be able to say
    # who changed it.
    changed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    justification: Mapped[str] = mapped_column(Text, nullable=False)

    # True where the change was applied when the session locked rather than
    # during the live discussion.
    applied_on_lock: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    session: Mapped[CalibrationSession] = relationship(back_populates="changes")
