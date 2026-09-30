"""Goals and key results — the source the goal score in an appraisal reads.

Until this module existed the appraisal template had a goal weight and nothing
to weigh: a 70/30 goals-to-competencies split with no goals is a competency
review with a number missing from it.

Three decisions worth stating.

**Progress comes from key results, never from a slider.** A goal's progress is
the weighted progress of its key results, each measured from a start value to a
target. "80% done" typed into a box is an opinion; "closed 40 of 50 accounts"
is a measurement somebody can check.

**Check-ins are insert-only.** Each one records the value before and after, who
recorded it and when. A goal that reads 100% in March with no history of how it
got there cannot be defended in a review conversation.

**Visibility is a column, not a convention.** A development goal about somebody's
confidence in meetings is not company reading. `SELF` is the owner and HR,
`MANAGER_LINE` adds the owner's reporting line, `COMPANY` is everybody.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
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
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class GoalLevel(str, enum.Enum):
    INDIVIDUAL = "individual"
    TEAM = "team"
    COMPANY = "company"


class GoalStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    COMPLETED = "completed"
    #: Stopped. Excluded from the goal score rather than counted as zero —
    #: a goal the business withdrew is not one the person failed.
    CANCELLED = "cancelled"


class GoalVisibility(str, enum.Enum):
    #: The owner and HR.
    SELF = "self"
    #: The owner, everybody above them in the reporting line, and HR.
    MANAGER_LINE = "manager_line"
    #: Everybody in the company.
    COMPANY = "company"


class KeyResultKind(str, enum.Enum):
    NUMBER = "number"
    PERCENT = "percent"
    CURRENCY = "currency"
    #: Done or not done. Progress is 0 or 100.
    MILESTONE = "milestone"


class CheckInConfidence(str, enum.Enum):
    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    OFF_TRACK = "off_track"
    #: The owner says the goal is finished. The check-in completes the goal in
    #: the same transaction, so "done" in the history and a completed goal
    #: cannot disagree.
    DONE = "done"


class Goal(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One goal: an objective with measurable key results."""

    __tablename__ = "goals"
    __table_args__ = (
        CheckConstraint("weight >= 0 AND weight <= 100", name="ck_goal_weight_range"),
        Index("ix_goals_owner", "tenant_id", "owner_employee_id", "status"),
        Index("ix_goals_cycle", "tenant_id", "appraisal_cycle_id"),
    )

    #: Null for a company goal, which belongs to nobody in particular.
    owner_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=True
    )
    level: Mapped[GoalLevel] = mapped_column(
        Enum(GoalLevel, name="goal_level"), default=GoalLevel.INDIVIDUAL, nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Share of the owner's goal score, out of 100 across their goals in a cycle.
    weight: Mapped[float] = mapped_column(Numeric(6, 2), default=0, nullable=False)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)

    status: Mapped[GoalStatus] = mapped_column(
        Enum(GoalStatus, name="goal_status"), default=GoalStatus.ACTIVE, nullable=False
    )
    visibility: Mapped[GoalVisibility] = mapped_column(
        Enum(GoalVisibility, name="goal_visibility"),
        default=GoalVisibility.MANAGER_LINE,
        nullable=False,
    )

    #: The manager's or company goal this one contributes to.
    parent_goal_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("goals.id", ondelete="SET NULL"), nullable=True
    )
    #: The review this goal counts towards. Null means "whichever cycle's period
    #: contains the due date".
    appraisal_cycle_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("appraisal_cycles.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: Computed from the key results on every check-in, in the same transaction.
    #: Stored so a list of two hundred goals does not recompute each one.
    progress_percent: Mapped[float] = mapped_column(Numeric(6, 2), default=0, nullable=False)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KeyResult(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One measurable result, from a start value to a target."""

    __tablename__ = "goal_key_results"
    __table_args__ = (
        CheckConstraint("weight > 0", name="ck_key_result_weight_positive"),
        Index("ix_goal_key_results_goal", "tenant_id", "goal_id"),
    )

    goal_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[KeyResultKind] = mapped_column(
        Enum(KeyResultKind, name="key_result_kind"),
        default=KeyResultKind.NUMBER,
        nullable=False,
    )
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    start_value: Mapped[float] = mapped_column(Numeric(18, 4), default=0, nullable=False)
    target_value: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    current_value: Mapped[float] = mapped_column(Numeric(18, 4), default=0, nullable=False)
    weight: Mapped[float] = mapped_column(Numeric(6, 2), default=1, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class GoalCheckIn(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One progress update. Insert-only: the history is the evidence."""

    __tablename__ = "goal_check_ins"
    __table_args__ = (Index("ix_goal_check_ins_goal", "tenant_id", "goal_id", "recorded_at"),)

    goal_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
    )
    key_result_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("goal_key_results.id", ondelete="SET NULL"),
        nullable=True,
    )
    previous_value: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    new_value: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    #: The goal's progress straight after this check-in.
    progress_after: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    confidence: Mapped[CheckInConfidence | None] = mapped_column(
        Enum(CheckInConfidence, name="goal_check_in_confidence"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GoalCheckInComment(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A reply to one check-in — usually the manager's. Insert-only, like the check-in.

    A comment is attached to the check-in it answers rather than to the goal,
    so "at risk, blocked on the vendor" and "I'll chase the vendor" stay next to
    each other in the history.
    """

    __tablename__ = "goal_check_in_comments"
    __table_args__ = (
        Index("ix_goal_check_in_comments_check_in", "tenant_id", "check_in_id"),
    )

    check_in_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("goal_check_ins.id", ondelete="CASCADE"), nullable=False
    )
    goal_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
    )
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
