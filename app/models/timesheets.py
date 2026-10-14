"""Projects, weekly timesheets, and hourly pay rates.

People log hours against projects, a week at a time, and submit the week; their
line manager or HR approves it. Approved hours are the record behind project
reports and, for people paid by the hour, behind their pay.

An **hourly rate** is effective-dated and never edited in place: a raise is a
new row from its date, and a week is priced at the rate in force on each day
worked — the same rule payroll rule sets follow. When an hourly-paid person's
week is approved, its pay becomes a payroll adjustment that an admin approves
before it reaches a run.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class Project(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_projects_code"),)

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    client: Mapped[str | None] = mapped_column(String(160), nullable=True)
    billable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class TimesheetStatus(str, enum.Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"


class Timesheet(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "timesheets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "employee_id", "week_start", name="uq_timesheet_week"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE", name="fk_timesheets_employee_id"),
        nullable=False,
        index=True,
    )
    #: The Monday the week starts on.
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[TimesheetStatus] = mapped_column(
        Enum(TimesheetStatus, name="timesheet_status"),
        default=TimesheetStatus.DRAFT,
        nullable=False,
        index=True,
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_timesheets_decided_by"),
        nullable=True,
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    payroll_adjustment_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "payroll_adjustments.id", ondelete="SET NULL", name="fk_timesheets_payroll_adjustment"
        ),
        nullable=True,
    )


class TimesheetEntry(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "timesheet_entries"
    __table_args__ = (
        CheckConstraint("hours > 0 AND hours <= 24", name="ck_timesheet_entry_hours"),
    )

    timesheet_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("timesheets.id", ondelete="CASCADE", name="fk_timesheet_entries_timesheet"),
        nullable=False,
        index=True,
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT", name="fk_timesheet_entries_project"),
        nullable=True,
        index=True,
    )
    hours: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)


class HourlyRate(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """What one person is paid an hour, from a date. Never updated in place."""

    __tablename__ = "hourly_rates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "employee_id", "effective_from", name="uq_hourly_rate_from"),
        CheckConstraint("rate > 0", name="ck_hourly_rate_positive"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE", name="fk_hourly_rates_employee_id"),
        nullable=False,
        index=True,
    )
    rate: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
