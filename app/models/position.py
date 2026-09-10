"""Positions: approved seats, who is in them, and the budget behind them.

The distinction this module exists to make is between a **seat** and a **person**.
Core HR models employees; a workforce plan needs the thing an employee occupies,
because a freeze, a reorg and a budget are all statements about seats and none of
them can be expressed in headcount numbers alone.

Two consequences run through the whole file.

**An exit vacates a position; it never deletes one.** The seat was approved and
budgeted, and it still exists the day after somebody leaves. Deleting it on exit
is how an organisation loses track of what it is meant to be staffed at, and how
a backfill becomes a new approval nobody remembers granting.

**Occupancy is a series, not a field.** `PositionAssignment` rows are
effective-dated, so "who held this seat last March" is answerable. A single
`current_employee_id` column would make the org chart unanswerable for any date
but today, which defeats most of the reason to have positions.
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


class PositionStatus(str, enum.Enum):
    """Where a seat stands.

    APPROVED and VACANT look identical on a headcount and are completely
    different to a recruiter: one is a new role, the other is a backfill with a
    leaver's handover behind it. FROZEN and ABOLISHED differ the same way — a
    freeze keeps the seat and its budget, an abolition takes both away.
    """

    APPROVED = "approved"
    FILLED = "filled"
    VACANT = "vacant"
    FROZEN = "frozen"
    ABOLISHED = "abolished"


class AssignmentType(str, enum.Enum):
    PRIMARY = "primary"
    ACTING = "acting"
    ADDITIONAL_CHARGE = "additional_charge"


class ScenarioStatus(str, enum.Enum):
    DRAFT = "draft"
    COMMITTED = "committed"
    DISCARDED = "discarded"


class CostCentre(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Where a position's cost lands in finance's books.

    Separate from department because they are not the same thing and clients
    stop pretending they are at about fifty people: a shared services team sits
    in one department and charges three cost centres.
    """

    __tablename__ = "cost_centres"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_cost_centre_code"),
    )

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    owner_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Position(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One approved seat."""

    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_position_code"),
        Index("ix_positions_status", "tenant_id", "status"),
        Index("ix_positions_department", "tenant_id", "department_id"),
        # A seat is at most one whole person and at least some fraction of one.
        # Zero would be a position nobody can be appointed to; above one is two
        # seats wearing one code.
        CheckConstraint("fte > 0 AND fte <= 1", name="ck_position_fte"),
        CheckConstraint(
            "budgeted_ctc_max IS NULL OR budgeted_ctc_min IS NULL "
            "OR budgeted_ctc_max >= budgeted_ctc_min",
            name="ck_position_band",
        ),
    )

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)

    department_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL"), nullable=True
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("locations.id", ondelete="SET NULL"), nullable=True
    )
    cost_centre_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("cost_centres.id", ondelete="SET NULL"),
        nullable=True,
    )
    grade: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    # Position-to-position. This is what survives a manager's exit: the line is
    # between seats, so when somebody leaves their reports are not orphaned — the
    # seat above them is simply empty.
    reports_to_position_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("positions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    budgeted_ctc_min: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    budgeted_ctc_max: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)

    # Decimal, not float, and not an integer. A part-time seat is 0.5 of a
    # position and 0.5 of a budget, and the budget number has to agree with
    # finance's.
    fte: Mapped[float] = mapped_column(
        Numeric(4, 2), default=1, nullable=False
    )

    status: Mapped[PositionStatus] = mapped_column(
        Enum(PositionStatus, name="position_status"),
        default=PositionStatus.APPROVED, nullable=False,
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    #: Set when the seat is abolished. The row survives — a position that stopped
    #: existing is part of the history of what the organisation was.
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    # When the seat last became empty. Vacancy ageing measures from here, not
    # from when a requisition was raised — the gap between those two is usually
    # where the delay actually is.
    vacated_on: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)

    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Why it was frozen or abolished. A decision somebody has to defend later.
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    assignments: Mapped[list[PositionAssignment]] = relationship(
        back_populates="position", cascade="all, delete-orphan", lazy="selectin"
    )


class PositionAssignment(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One person in one seat, over one span of time.

    Effective-dated on purpose. A `current_employee_id` column on the position
    would make "who held this last March" unanswerable, which defeats most of the
    reason to model positions at all.
    """

    __tablename__ = "position_assignments"
    __table_args__ = (
        Index("ix_position_assignments_span", "tenant_id", "position_id", "from_date"),
        Index("ix_position_assignments_employee", "tenant_id", "employee_id"),
        CheckConstraint(
            "to_date IS NULL OR to_date >= from_date", name="ck_assignment_span"
        ),
    )

    position_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("positions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
    )

    from_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: Inclusive. That is how an HR system records "their last day was the 31st",
    #: and treating it as exclusive would show every leaver as gone a day early.
    to_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    assignment_type: Mapped[AssignmentType] = mapped_column(
        Enum(AssignmentType, name="position_assignment_type"),
        default=AssignmentType.PRIMARY, nullable=False, index=True,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    position: Mapped[Position] = relationship(back_populates="assignments")


class HeadcountBudget(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """What was approved for a department for a financial year.

    The financial year is the Indian one, stored as its label ("FY 2026-27")
    because that is what it is called on every document a client will compare
    this against.
    """

    __tablename__ = "headcount_budgets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "financial_year", "department_id", name="uq_headcount_budget"
        ),
        CheckConstraint("approved_positions >= 0", name="ck_budget_positions"),
    )

    financial_year: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"), nullable=True
    )
    cost_centre_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("cost_centres.id", ondelete="SET NULL"),
        nullable=True,
    )

    approved_positions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    approved_cost: Mapped[float] = mapped_column(
        Numeric(16, 2), default=0, nullable=False
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class PositionScenario(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A draft reorg, freeze or growth case.

    Nothing here touches a live position until `commit` is called. The changes
    are a list of intentions; the projection is computed from them in a pure
    engine that has no session and cannot write.
    """

    __tablename__ = "position_scenarios"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_scenario_name"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ScenarioStatus] = mapped_column(
        Enum(ScenarioStatus, name="position_scenario_status"),
        default=ScenarioStatus.DRAFT, nullable=False, index=True,
    )
    #: The date the scenario models. A reorg effective in April is a different
    #: set of numbers from the same reorg today.
    as_of: Mapped[date] = mapped_column(Date, nullable=False)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    committed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    committed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    discarded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    changes: Mapped[list[ScenarioChange]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan", lazy="selectin"
    )


class ScenarioChange(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One intention inside a scenario.

    Ordered, because a scenario that moves a position and then abolishes it is a
    different plan from one that does the reverse, and a commit has to replay
    them in the order somebody built them.
    """

    __tablename__ = "position_scenario_changes"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "scenario_id", "sequence", name="uq_scenario_change_sequence"
        ),
    )

    scenario_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("position_scenarios.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    #: create | move | freeze | unfreeze | abolish
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Null for a create, where the position does not exist yet.
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("positions.id", ondelete="CASCADE"),
        nullable=True,
    )
    values: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Set on commit, so a committed scenario says which real position each
    #: create produced. Without it, "what did this reorg actually do" is a
    #: reconstruction.
    applied_position_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("positions.id", ondelete="SET NULL"),
        nullable=True,
    )

    scenario: Mapped[PositionScenario] = relationship(back_populates="changes")
