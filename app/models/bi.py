"""BI connections, export audit, and the benchmark tables (slice 37).

═══════════════════════════════════════════════════════════════════════════════
`bi_export_runs` IS THE POINT OF THIS FILE
═══════════════════════════════════════════════════════════════════════════════

A BI connector is the largest quantity of personal data that leaves this system,
on a schedule, unattended. The question asked after an incident is never "how
many rows" — it is **"did salary leave the building"** — so the row carries the
field list, the field count, the destination, the role, the schema version and a
`carries_payroll_data` boolean, and it is written from the same `ExportPlan`
object the extract was projected onto. The count and the fields therefore cannot
disagree, because they come from the same place.

═══════════════════════════════════════════════════════════════════════════════
`benchmark_datasets` HAS NO `tenant_id`, AND THAT IS THE WHOLE DESIGN
═══════════════════════════════════════════════════════════════════════════════

It is the published aggregate. Every contributing client sees the same cell —
that is what makes it a benchmark rather than a report. A `tenant_id` on this
table would mean either "the tenant this cell is about", which is precisely the
identification we are preventing, or a per-tenant copy of an identical row.

It is the only table in the product besides `tenants` with no tenant column, so
it is read through exactly one module (`services/bi/published.py`) and that
module is allowed to read nothing else. The narrowness is enforced by an
invariant rather than by intent.

The contributions that *feed* it (`benchmark_contributions`) are ordinary
tenant-scoped rows: one company's value for one metric in one cell, written by
that company's own tenant-scoped service.

═══════════════════════════════════════════════════════════════════════════════
CONSENT IS A ROW WITH A REVOCATION COLUMN, NOT A BOOLEAN ON `tenants`
═══════════════════════════════════════════════════════════════════════════════

"Explicit opt-in per client, in the contract and in the product." So the row
names the contract reference, the human who accepted, and when — and revoking
sets `revoked_at` rather than deleting, because "was this client contributing in
March" is a question somebody will eventually ask under an audit, and a deleted
row answers it with silence.
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
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class BiDestinationKind(str, enum.Enum):
    """Both are offered because different clients genuinely want different ones.

    A client with a warehouse team wants a role they can point dbt at; a client
    without one wants a file in their bucket every morning.
    """

    #: A read-only Postgres role scoped by views over the exposed schema.
    POSTGRES_ROLE = "postgres_role"
    #: Scheduled Parquet or CSV to the client's own object storage.
    OBJECT_STORAGE = "object_storage"


class BiExportKind(str, enum.Enum):
    INCREMENTAL = "incremental"
    #: Genuinely needed when a client rebuilds a warehouse, and recorded as
    #: itself so "the nightly delta" and "somebody took everything" do not look
    #: identical in the audit afterwards.
    FULL = "full"


class BiExportStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class BenchmarkSourceEnum(str, enum.Enum):
    """Always shown, never inferred.

    Our own client base is a specific and possibly unrepresentative population;
    a licensed dataset has its own methodology and its own limitations.
    Presenting them identically hides both.
    """

    OWN_CLIENT_BASE = "own_client_base"
    LICENSED = "licensed"


class BiConnection(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One configured egress path: an exposed entity to a destination.

    `last_watermark` is the change key the next incremental export starts from.
    It is stored per connection rather than derived, because "what did we
    already send" is a fact about this pipe and nothing else knows it.
    """

    __tablename__ = "bi_connections"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: A name from the exposed schema (`dim_employee`, `fact_payslip`, ...),
    #: never a table name. Validated against the catalogue on write.
    entity_name: Mapped[str] = mapped_column(String(64), nullable=False)
    destination: Mapped[BiDestinationKind] = mapped_column(
        Enum(BiDestinationKind, name="bi_destination_kind"), nullable=False
    )
    #: The role name or the bucket URI. "To where" is half of what the audit
    #: row is for, so a connection cannot exist without it.
    destination_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    #: The role whose field-level sensitivity the export is evaluated against.
    #: A connection is a credential, and a credential has a role.
    export_role: Mapped[str] = mapped_column(String(50), nullable=False)
    #: Null means "every field this role may see". A list narrows it further.
    field_names: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    last_watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_bi_connections_tenant_name"),
        CheckConstraint(
            "length(trim(destination_ref)) > 0",
            name="ck_bi_connections_destination_ref_present",
        ),
        Index("ix_bi_connections_tenant_active", "tenant_id", "is_active"),
    )


class BiExportRun(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """What left, when, to where, and at whose instruction.

    Insert-only in practice: the row is created when the export starts and
    completed in the same transaction as the extract. There is no update path
    that rewrites the field list, because a field list that can be edited
    afterwards is not evidence.
    """

    __tablename__ = "bi_export_runs"

    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("bi_connections.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    entity_name: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[BiExportKind] = mapped_column(
        Enum(BiExportKind, name="bi_export_kind"), nullable=False
    )
    destination: Mapped[BiDestinationKind] = mapped_column(
        Enum(BiDestinationKind, name="bi_destination_kind"), nullable=False
    )
    destination_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    export_role: Mapped[str] = mapped_column(String(50), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: The names that left. Not a summary, not a count - the list.
    field_names: Mapped[list] = mapped_column(JSONB, nullable=False)
    field_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Requested, and refused because the role may not see them. Recorded so a
    #: client asking "why is this column empty" gets an answer.
    withheld_fields: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    #: The question actually asked after an incident, answered by a boolean at
    #: the top of the row rather than by reading forty field names.
    carries_payroll_data: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[BiExportStatus] = mapped_column(
        Enum(BiExportStatus, name="bi_export_status"), nullable=False
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    initiated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("row_count >= 0", name="ck_bi_export_runs_row_count"),
        # An export of zero fields is a scheduled job producing an empty file
        # every night, which looks like it is working.
        CheckConstraint("field_count > 0", name="ck_bi_export_runs_has_fields"),
        Index("ix_bi_export_runs_tenant_started", "tenant_id", "started_at"),
        Index("ix_bi_export_runs_tenant_payroll", "tenant_id", "carries_payroll_data"),
    )


class BenchmarkConsent(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One client's opt-in to contribute anonymised aggregates.

    Explicit, in the product as well as the contract, and reversible. Revoking
    sets `revoked_at` rather than deleting the row: "was this client
    contributing in March" is a question an audit asks, and a deleted row
    answers it with silence.
    """

    __tablename__ = "benchmark_consents"

    is_contributor: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: The contract clause this was agreed under. Consent that exists only as a
    #: checkbox is consent nobody can evidence.
    contract_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    accepted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_benchmark_consents_tenant"),
        # Contributing without an acceptance timestamp is consent that cannot
        # be evidenced, which is the same as not having it.
        CheckConstraint(
            "NOT is_contributor OR accepted_at IS NOT NULL",
            name="ck_benchmark_consents_accepted_when_contributing",
        ),
        CheckConstraint(
            "NOT is_contributor OR revoked_at IS NULL",
            name="ck_benchmark_consents_not_contributing_after_revocation",
        ),
    )


class BenchmarkContribution(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One company's value for one metric in one cell, for one period.

    Tenant-scoped like any other row this client owns, and deletable on request
    — withdrawing consent removes the contributions, and the next aggregation
    run produces cells without them.

    `employee_count` is how many people stand behind the number. It is not used
    to weight the distribution (one value per company enters it); it is used to
    *refuse* a cell one company dominates by headcount.
    """

    __tablename__ = "benchmark_contributions"

    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The cell this value belongs to: industry, city, size band, grade,
    #: canonicalised into one key so a cell is comparable across periods.
    dimension_key: Mapped[str] = mapped_column(String(200), nullable=False)
    period: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    employee_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "metric",
            "dimension_key",
            "period",
            name="uq_benchmark_contributions_cell",
        ),
        CheckConstraint("employee_count > 0", name="ck_benchmark_contributions_employee_count"),
        Index(
            "ix_benchmark_contributions_cell",
            "metric",
            "dimension_key",
            "period",
        ),
    )


class BenchmarkDataset(Base, UUIDMixin, TimestampMixin):
    """One published benchmark cell. **Deliberately not tenant-scoped.**

    Every contributing client sees the same cell — that is what makes it a
    benchmark rather than a report. A `tenant_id` here would mean either "the
    tenant this cell is about", which is exactly the identification the
    suppression rules exist to prevent, or a per-tenant copy of an identical
    row.

    A cell below the contributor threshold is stored `published=False` with the
    reason, rather than not stored at all: "why is there no benchmark for my
    segment" is then answerable without re-running the aggregation.
    """

    __tablename__ = "benchmark_datasets"

    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    dimension_key: Mapped[str] = mapped_column(String(200), nullable=False)
    period: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[BenchmarkSourceEnum] = mapped_column(
        Enum(BenchmarkSourceEnum, name="benchmark_source"), nullable=False
    )
    #: Contributing **companies**, not employees. A cell drawn from one
    #: company's four hundred people is that company's pay band with a market
    #: label on it.
    contributors: Mapped[int] = mapped_column(Integer, nullable=False)
    published: Mapped[bool] = mapped_column(Boolean, nullable=False)
    p25: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    p50: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    p75: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "metric",
            "dimension_key",
            "period",
            "source",
            name="uq_benchmark_datasets_cell",
        ),
        # An unpublished cell holds no quartiles. Storing them "just in case"
        # is storing the suppressed number next to the reason it was
        # suppressed, and the next query forgets to check the flag.
        CheckConstraint(
            "published OR (p25 IS NULL AND p50 IS NULL AND p75 IS NULL)",
            name="ck_benchmark_datasets_suppressed_cells_are_empty",
        ),
        CheckConstraint(
            "NOT published OR (p25 IS NOT NULL AND p50 IS NOT NULL AND p75 IS NOT NULL)",
            name="ck_benchmark_datasets_published_cells_are_complete",
        ),
        CheckConstraint("contributors >= 0", name="ck_benchmark_datasets_contributors"),
        Index("ix_benchmark_datasets_lookup", "metric", "dimension_key", "period"),
    )
