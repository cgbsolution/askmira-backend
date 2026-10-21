"""Background job runs (cross-cutting gap 4).

One row per job per tenant per run. The admin screen reads "when did this last
run, did it work, and what went wrong" from here — a nightly job whose only
record is a log line on a container that has since been replaced is a job
nobody can vouch for.

`tenant_id` is nullable on purpose: a platform job (purging expired SAML
assertion ids, pruning this table) belongs to no tenant. This is the only
reason the table does not use `TenantMixin`, and every read in the service
filters on `tenant_id` explicitly.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class SchedulerRunStatus(str, enum.Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: The job did not apply to this tenant — its module is switched off.
    SKIPPED = "skipped"


class SchedulerJobRun(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "scheduler_job_runs"
    __table_args__ = (Index("ix_scheduler_run_job_tenant", "job_key", "tenant_id", "started_at"),)

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    job_key: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[SchedulerRunStatus] = mapped_column(
        Enum(SchedulerRunStatus, name="scheduler_run_status"), nullable=False, index=True
    )
    #: What the job reported: counts, the ids it touched. Never personal data —
    #: the handlers return tallies.
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    #: The exception text, verbatim. A summarised error is one nobody can debug.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: "schedule" for the runner, "manual" for an admin's run-now.
    trigger: Mapped[str] = mapped_column(String(16), default="schedule", nullable=False)
    triggered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
