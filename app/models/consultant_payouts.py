"""Consultant payout runs: approved consultant weeks paid outside payroll.

A run takes every approved consultant week up to its cut-off and makes one
line per consultant: the hours priced at the rate in force on each day worked,
GST on top where the consultant charges it, and TDS under 194J or 194C taken
off. The run's pay date is the TDS date: it picks the financial year, the rule
set and the year-to-date total for every line.

A week belongs to at most one run — `consultant_payout_weeks.timesheet_id` is
unique — so nothing is paid twice. Excluding a line or deleting a draft
deletes its week rows, which releases the weeks for a later run.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin
from app.models.contractors import TdsSection


class PayoutRunStatus(str, enum.Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    PAID = "paid"


class ConsultantPayoutRun(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "consultant_payout_runs"

    #: The company paying, or None for every company in the workspace.
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "legal_entities.id", ondelete="RESTRICT", name="fk_consultant_payout_runs_entity"
        ),
        nullable=True,
    )
    #: Weeks ending on or before this day are taken.
    cut_off: Mapped[date] = mapped_column(Date, nullable=False)
    #: The day the consultants are paid, and the TDS date.
    pay_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[PayoutRunStatus] = mapped_column(
        Enum(PayoutRunStatus, name="consultant_payout_run_status"),
        default=PayoutRunStatus.DRAFT,
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_consultant_payout_runs_created_by"),
        nullable=True,
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_consultant_payout_runs_approved_by"),
        nullable=True,
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ConsultantPayoutLine(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One consultant's pay in a run.

    `excluded` with `problems` and no `excluded_reason`: held back until the
    data is fixed and the run recalculated; the line keeps its weeks.
    `excluded_reason` set: HR took the line out by hand; its weeks are released
    and recalculating never brings it back.
    """

    __tablename__ = "consultant_payout_lines"
    __table_args__ = (
        UniqueConstraint("run_id", "employee_id", name="uq_consultant_payout_line_person"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "consultant_payout_runs.id", ondelete="CASCADE", name="fk_consultant_payout_lines_run"
        ),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="RESTRICT", name="fk_consultant_payout_lines_employee"),
        nullable=False,
        index=True,
    )
    hours: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=0, nullable=False)
    #: Hours times the rate in force each day, before GST: what TDS is charged on.
    gross: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    gst_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0, nullable=False)
    gst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    section: Mapped[TdsSection] = mapped_column(
        Enum(TdsSection, name="tds_section", create_type=False), nullable=False
    )
    #: What TDS was charged on: more than the gross when the year's threshold
    #: was crossed and earlier fees are caught up.
    tds_base: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    tds_rate: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=0, nullable=False)
    tds_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    net_payable: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    tds_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_set_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "payroll_rule_sets.id", ondelete="RESTRICT", name="fk_consultant_payout_lines_rule_set"
        ),
        nullable=True,
    )
    #: What holds the line back, in words HR can act on.
    problems: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    excluded: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    excluded_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The 26Q challan this line's TDS was deposited under.
    challan_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "tds_challans.id", ondelete="SET NULL", name="fk_consultant_payout_lines_challan"
        ),
        nullable=True,
        index=True,
    )


class ConsultantPayoutWeek(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One approved week paid by a line. A week is in at most one run."""

    __tablename__ = "consultant_payout_weeks"

    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "consultant_payout_runs.id", ondelete="CASCADE", name="fk_consultant_payout_weeks_run"
        ),
        nullable=False,
        index=True,
    )
    line_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "consultant_payout_lines.id",
            ondelete="CASCADE",
            name="fk_consultant_payout_weeks_line",
        ),
        nullable=False,
        index=True,
    )
    timesheet_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "timesheets.id", ondelete="RESTRICT", name="fk_consultant_payout_weeks_timesheet"
        ),
        nullable=False,
        unique=True,
    )
    hours: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=0, nullable=False)
    gross: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    #: The week priced by day and project:
    #: [{work_date, project_id, hours, rate, amount}].
    breakdown: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )


class ConsultantTdsOpening(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Fees paid to a consultant earlier in a financial year, before payout runs.

    Feeds the year-to-date threshold for that year and section.
    """

    __tablename__ = "consultant_tds_openings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "employee_id",
            "fy_start",
            "section",
            name="uq_consultant_tds_opening",
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE", name="fk_consultant_tds_openings_employee"),
        nullable=False,
        index=True,
    )
    #: 1 April of the financial year.
    fy_start: Mapped[date] = mapped_column(Date, nullable=False)
    section: Mapped[TdsSection] = mapped_column(
        Enum(TdsSection, name="tds_section", create_type=False), nullable=False
    )
    fees_paid: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    #: How much of `fees_paid` TDS was already charged on.
    taxed_base: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    tds_deducted: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
