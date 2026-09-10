"""Exit workflow and full & final settlement.

The most cross-cutting computation in the product: salary, attendance, leave,
gratuity, loans, assets, notice period and tax all land on one statement that
the employee will read line by line on the day they stop being able to ask
anyone about it.

Two decisions in this schema are worth stating up front.

**Net settlement can be negative, and the schema says so.** An employee who
resigned without notice, has an outstanding loan and did not return a laptop can
owe the company money. Flooring at zero would be quietly writing off a debt,
and — worse — it would tell the employee they were owed nothing when in fact a
demand was due. `SettlementLine` carries earnings and recoveries with their
signs intact and `net_amount` is allowed to be below zero.

**Nothing is deleted on exit.** The `User` is deactivated and access revoked;
the `Employee` row, payroll history, attendance and audit trail stay. Statutory
retention runs to years after employment ends, and a deleted employee makes
every past return and payslip unexplainable.

    ┌─────────────────────────────────────────────────────────────────────┐
    │ INTERSECTION WITH SLICE 26 (data retention & deletion policies)     │
    │                                                                     │
    │ Exit is where the retention clock *starts*, not where deletion      │
    │ happens. Slice 26 owns the schedule and the erasure. Anything here  │
    │ that deletes an employee record is a bug in both slices at once:    │
    │ it destroys evidence this slice needs and pre-empts a decision      │
    │ that one has not made yet.                                          │
    └─────────────────────────────────────────────────────────────────────┘
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


class ExitType(str, enum.Enum):
    RESIGNATION = "resignation"
    TERMINATION = "termination"
    RETIREMENT = "retirement"
    # Left without notice and without contact. The settlement still has to be
    # computed — often it is a demand — and the record still has to exist.
    ABSCONDING = "absconding"
    DEATH = "death"
    END_OF_CONTRACT = "end_of_contract"


class ExitStatus(str, enum.Enum):
    INITIATED = "initiated"
    NOTICE_PERIOD = "notice_period"
    CLEARANCE_PENDING = "clearance_pending"
    SETTLEMENT_PENDING = "settlement_pending"
    SETTLED = "settled"
    # Computed but deliberately not paid — a dispute, an unreturned asset, a
    # pending investigation. The reason is recorded.
    ON_HOLD = "on_hold"
    CANCELLED = "cancelled"


class ClearanceStatus(str, enum.Enum):
    PENDING = "pending"
    CLEARED = "cleared"
    # Cleared with money owed: an unreturned laptop, an unsettled advance.
    CLEARED_WITH_RECOVERY = "cleared_with_recovery"
    # Explicitly skipped, with a reason. Not the same as cleared.
    WAIVED = "waived"


class SettlementStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    ON_HOLD = "on_hold"
    PAID = "paid"
    # A negative settlement that has been issued to the employee as a demand.
    DEMAND_RAISED = "demand_raised"


class ExitRecord(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One employee leaving."""

    __tablename__ = "exit_records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "employee_id", "resignation_date",
                         name="uq_exit_employee_date"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    exit_type: Mapped[ExitType] = mapped_column(
        Enum(ExitType, name="exit_type_workflow"), nullable=False
    )
    status: Mapped[ExitStatus] = mapped_column(
        Enum(ExitStatus, name="exit_status"),
        default=ExitStatus.INITIATED,
        nullable=False,
        index=True,
    )

    resignation_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    requested_last_working_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    # What HR agreed to, which is what everything downstream computes against.
    approved_last_working_day: Mapped[date | None] = mapped_column(Date, nullable=True)

    notice_period_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notice_shortfall_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notice_waived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notice_waived_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    notice_waiver_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_rehire_eligible: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Set when the User is deactivated. The Employee row is never deleted —
    # see the module docstring and the slice 26 intersection.
    access_revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    clearance_items: Mapped[list[ClearanceItem]] = relationship(
        back_populates="exit_record", cascade="all, delete-orphan"
    )


class ClearanceItem(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One department signing off, or naming what is owed.

    Recovery flows from here into the settlement: an unreturned laptop is an IT
    clearance item with a recovery amount, and that amount appears as a
    settlement line rather than being typed in twice.
    """

    __tablename__ = "clearance_items"

    exit_record_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("exit_records.id", ondelete="CASCADE"), nullable=False
    )
    department: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False)

    # A mandatory item blocks settlement until it is cleared or waived. Which
    # items are mandatory is per tenant — a company with no library does not
    # need a library clearance.
    is_mandatory: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    status: Mapped[ClearanceStatus] = mapped_column(
        Enum(ClearanceStatus, name="clearance_status"),
        default=ClearanceStatus.PENDING,
        nullable=False,
        index=True,
    )
    recovery_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)

    cleared_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Required when the status is WAIVED. Waiving a mandatory clearance without
    # saying why is how an unreturned laptop becomes nobody's problem.
    waiver_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    exit_record: Mapped[ExitRecord] = relationship(back_populates="clearance_items")


class ClearanceTemplate(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """The tenant's clearance checklist, instantiated on each exit."""

    __tablename__ = "clearance_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "department", "description",
                         name="uq_clearance_template_item"),
    )

    department: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Settlement(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """The full and final computation.

    `net_amount` may be negative. That is not an error state to be floored
    away — an employee who resigned without notice with an outstanding loan and
    an unreturned laptop owes the company, and the product has to be able to
    say so and raise a demand.
    """

    __tablename__ = "settlements"
    __table_args__ = (
        UniqueConstraint("tenant_id", "exit_record_id", name="uq_settlement_exit"),
    )

    exit_record_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("exit_records.id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )

    status: Mapped[SettlementStatus] = mapped_column(
        Enum(SettlementStatus, name="settlement_status"),
        default=SettlementStatus.DRAFT,
        nullable=False,
        index=True,
    )

    total_earnings: Mapped[float] = mapped_column(Numeric(16, 2), default=0, nullable=False)
    total_recoveries: Mapped[float] = mapped_column(Numeric(16, 2), default=0, nullable=False)
    total_tax: Mapped[float] = mapped_column(Numeric(16, 2), default=0, nullable=False)
    # Earnings less recoveries less tax. Negative is a real answer.
    net_amount: Mapped[float] = mapped_column(Numeric(16, 2), default=0, nullable=False)

    # The exit TDS recompute frequently produces a refund or a large final
    # deduction. Kept so the statement can explain it rather than showing an
    # unexplained number.
    tax_working: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # A hold is computed-but-not-paid. The reason is required, because "why
    # has my settlement not been paid" is the question that follows.
    hold_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    held_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    held_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    statement_letter_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("issued_letters.id", ondelete="SET NULL"), nullable=True
    )

    lines: Mapped[list[SettlementLine]] = relationship(
        back_populates="settlement", cascade="all, delete-orphan"
    )

    @property
    def is_demand(self) -> bool:
        """The company is owed money rather than owing it."""
        return self.net_amount is not None and self.net_amount < 0


class SettlementLine(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One line of the statement, with the basis it was computed on.

    `basis` is not decoration. The employee reads this on the day they lose
    access to anyone who could explain it, and "Notice shortfall recovery
    -45,000" without "15 days at 3,000 per day on basic + DA" is a line they
    can only dispute, not check.
    """

    __tablename__ = "settlement_lines"

    settlement_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("settlements.id", ondelete="CASCADE"), nullable=False
    )

    # earning | recovery | tax
    line_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    # Positive for earnings, positive for recoveries — the sign is carried by
    # `line_type`, so a reader is never working out what a negative earning
    # means.
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)

    basis: Mapped[str] = mapped_column(Text, nullable=False)
    # Which slice produced it, so a disputed figure can be traced to the
    # computation that made it.
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    settlement: Mapped[Settlement] = relationship(back_populates="lines")
