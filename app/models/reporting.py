"""Saved reports, their schedules, and the record of what left the building.

`ReportRun` and `ReportDelivery` exist for one reason: **a report builder with
scheduled distribution is a data-exfiltration path if nobody is watching it.**
Somebody with report access can build a salary register, schedule it weekly to a
personal address, and nothing about that is unusual to look at unless the sends
are recorded with their recipients. So every run and every send is a row, and the
row names who received what.

`Report.definition` is JSON, but not free-form: it is validated against the
semantic catalogue on save and again on every run. A definition that named a
table would be refused at both ends.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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


class ReportVisibility(str, enum.Enum):
    """Who can open a saved report.

    Visibility governs the *definition*, never the data. A SHARED salary report
    opened by a manager still returns nothing they may not see, because the
    compiler re-checks their role. Sharing a report is sharing a question, not an
    answer.
    """

    PRIVATE = "private"
    #: Explicitly listed users.
    SHARED = "shared"
    #: Everybody in the tenant who can reach reporting at all.
    TENANT = "tenant"


class ChartType(str, enum.Enum):
    TABLE = "table"
    BAR = "bar"
    LINE = "line"
    PIE = "pie"
    STACKED = "stacked"


class ExportFormat(str, enum.Enum):
    CSV = "csv"
    XLSX = "xlsx"


class Cadence(str, enum.Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"


class RunStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    #: The query hit the row cap or the timeout. Distinct from FAILED because
    #: the fix is different: narrow the report, not debug it.
    TOO_LARGE = "too_large"
    FAILED = "failed"


class EmptyBehaviour(str, enum.Enum):
    """What a scheduled report does when there is nothing to report.

    Both are right for different reports. "Employees on notice" arriving empty is
    good news somebody wants confirmed; "pending leave requests" arriving empty
    every Monday trains people to stop opening it.
    """

    SEND_ANYWAY = "send_anyway"
    SKIP = "skip"


class Report(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A saved question."""

    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", "owner_user_id", name="uq_report_name"),
        Index("ix_reports_visibility", "tenant_id", "visibility"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: A catalogue key, never a table name.
    entity_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: The whole definition — selections, filters, grouping, sorting. Validated
    #: against the catalogue on save and again on every run.
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)

    chart_type: Mapped[ChartType] = mapped_column(
        Enum(ChartType, name="report_chart_type"),
        default=ChartType.TABLE, nullable=False,
    )
    # No `index=True` here: the composite index above already leads with
    # tenant_id and covers every lookup this column takes part in. Both would
    # auto-name to `ix_reports_visibility`, and the collision showed up as
    # permanent drift in `alembic check` rather than as an error.
    visibility: Mapped[ReportVisibility] = mapped_column(
        Enum(ReportVisibility, name="report_visibility"),
        default=ReportVisibility.PRIVATE, nullable=False,
    )
    shared_with_user_ids: Mapped[list] = mapped_column(
        JSONB, default=list, nullable=False
    )

    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: The starter this was cloned from, where it was. Kept so "how many clients
    #: use the salary register" is answerable, and so a starter that nobody
    #: clones can be retired.
    created_from_starter: Mapped[str | None] = mapped_column(String(64), nullable=True)

    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ReportSchedule(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A standing instruction to send a report to a list of people."""

    __tablename__ = "report_schedules"
    __table_args__ = (
        CheckConstraint("hour_of_day BETWEEN 0 AND 23", name="ck_schedule_hour"),
    )

    report_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("reports.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    cadence: Mapped[Cadence] = mapped_column(
        Enum(Cadence, name="report_cadence"), nullable=False
    )
    #: Local to `timezone_name`, which is the recipient's, not the server's. A
    #: "Monday 9am" report that arrives at 3:30am because the server is in UTC
    #: is a report people learn to ignore.
    hour_of_day: Mapped[int] = mapped_column(Integer, default=9, nullable=False)
    #: Monday is 0. Only meaningful for WEEKLY.
    day_of_week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Only meaningful for MONTHLY and QUARTERLY.
    day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timezone_name: Mapped[str] = mapped_column(
        String(64), default="Asia/Kolkata", nullable=False
    )

    recipient_user_ids: Mapped[list] = mapped_column(
        JSONB, default=list, nullable=False
    )
    export_format: Mapped[ExportFormat] = mapped_column(
        Enum(ExportFormat, name="report_export_format"),
        default=ExportFormat.CSV, nullable=False,
    )
    on_empty: Mapped[EmptyBehaviour] = mapped_column(
        Enum(EmptyBehaviour, name="report_empty_behaviour"),
        default=EmptyBehaviour.SEND_ANYWAY, nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ReportRun(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One execution. Insert-only.

    Records the role it ran as and the sensitivity of what it returned, because
    "who has been pulling salary data, and how often" is a question that has to
    be answerable without reconstructing it from application logs.
    """

    __tablename__ = "report_runs"
    __table_args__ = (
        Index("ix_report_runs_lookup", "tenant_id", "report_id", "created_at"),
    )

    report_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("reports.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("report_schedules.id", ondelete="SET NULL"),
        nullable=True,
    )
    run_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: The role the query was compiled for. A report is only as restricted as
    #: the person who ran it, so this is what makes a run interpretable later.
    run_as_role: Mapped[str] = mapped_column(String(32), nullable=False)
    row_scope: Mapped[str] = mapped_column(String(32), nullable=False)

    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="report_run_status"), nullable=False, index=True
    )
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: The highest sensitivity actually returned. The column that makes a
    #: "who pulled payroll data this month" query one line rather than a script.
    max_sensitivity: Mapped[str] = mapped_column(
        String(32), default="public", nullable=False, index=True
    )
    #: The definition as it ran, snapshotted. A report edited afterwards must not
    #: rewrite what an old run means.
    definition_snapshot: Mapped[dict] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    export_format: Mapped[ExportFormat | None] = mapped_column(
        Enum(ExportFormat, name="report_export_format", create_type=False), nullable=True
    )


class ReportDelivery(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One recipient of one scheduled send. Insert-only.

    A row per recipient, not a list on the run: "was this person ever sent the
    salary register" is the question, and answering it by parsing a JSON array is
    how it stops being asked.
    """

    __tablename__ = "report_deliveries"
    __table_args__ = (
        Index("ix_report_deliveries_recipient", "tenant_id", "recipient_user_id"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("report_runs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("report_schedules.id", ondelete="SET NULL"),
        nullable=True,
    )
    recipient_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Snapshotted: an address that is deleted later must still be answerable.
    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    export_format: Mapped[ExportFormat] = mapped_column(
        Enum(ExportFormat, name="report_export_format", create_type=False),
        nullable=False,
    )
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_sensitivity: Mapped[str] = mapped_column(
        String(32), default="public", nullable=False, index=True
    )


class DashboardWidget(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A saved report pinned to one person's analytics dashboard.

    The report runs as whoever views the dashboard, so pinning shares nothing
    the viewer could not already see.
    """

    __tablename__ = "dashboard_widgets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "report_id", name="uq_dashboard_widget_report"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    report_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("reports.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
