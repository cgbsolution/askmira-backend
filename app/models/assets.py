"""Asset management: the company's laptops, phones, cards and keys, and who holds them.

Three decisions in this schema are worth stating up front.

**An assignment is a closed record, never edited back.** Handing a laptop to
somebody opens an `AssetAssignment`; getting it back — or learning it is gone —
closes that row with an outcome. Reassigning the laptop opens a new row. The
history of who held what, in which condition, is therefore the rows themselves,
and a partial unique index keeps it to one open row per asset, so an asset can
never be "with" two people at once.

**Nothing is deleted.** An asset leaves the register by being retired or
disposed, with a reason. An exited employee's unreturned laptop is evidence in
their settlement; a hard delete would make that settlement line unexplainable.

**Recovery flows into the exit settlement, not around it.** When somebody
leaves, each open assignment becomes a clearance item on their exit checklist,
linked back by `ClearanceItem.asset_assignment_id`. Marking that item "not
returned" puts the asset's book value on the item, and the settlement's
"Unreturned assets" line already sums clearance recoveries — so the figure is
typed once and read twice, never typed twice.
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
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class AssetStatus(str, enum.Enum):
    IN_STOCK = "in_stock"
    ASSIGNED = "assigned"
    IN_REPAIR = "in_repair"
    LOST = "lost"
    # Out of service but still owned: kept for parts, awaiting disposal.
    RETIRED = "retired"
    # Gone from the company: sold, scrapped, donated, or written off to a leaver.
    DISPOSED = "disposed"


class AssetCondition(str, enum.Enum):
    NEW = "new"
    GOOD = "good"
    FAIR = "fair"
    DAMAGED = "damaged"


class AssignmentOutcome(str, enum.Enum):
    """How an assignment ended. Empty while it is open."""

    RETURNED = "returned"
    # Never came back: reported lost, or not returned at exit.
    LOST = "lost"
    # Not recovered, deliberately: a clearance waiver at exit, with its reason.
    WRITTEN_OFF = "written_off"


class AssetRequestStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    # Approved and an asset handed over against it.
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"


class AssetCategory(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A kind of asset: laptops, phones, access cards.

    The category carries the policy: how long its assets take to depreciate to
    nothing (which is what an unreturned one costs a leaver), whether the holder
    must acknowledge receipt, and who collects them at exit.
    """

    __tablename__ = "asset_categories"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_asset_categories_tenant_code"),
        CheckConstraint(
            "depreciation_months IS NULL OR depreciation_months > 0",
            name="ck_asset_category_depreciation_positive",
        ),
    )

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Who collects these at exit: the named owner of the clearance item. Empty
    # means HR collects.
    custodian_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Straight-line to zero over this many months from purchase. Empty means
    # the asset holds its purchase cost (a key, a card).
    depreciation_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requires_acknowledgement: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )


class Asset(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One physical thing the company owns, tagged."""

    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "asset_tag", name="uq_assets_tenant_tag"),
        CheckConstraint(
            "purchase_cost IS NULL OR purchase_cost >= 0", name="ck_asset_cost_non_negative"
        ),
    )

    category_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("asset_categories.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    asset_tag: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    serial_number: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    make: Mapped[str | None] = mapped_column(String(80), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    specs: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    purchase_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    purchase_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    vendor: Mapped[str | None] = mapped_column(String(160), nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    warranty_until: Mapped[date | None] = mapped_column(Date, nullable=True)

    location_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("locations.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[AssetStatus] = mapped_column(
        Enum(AssetStatus, name="asset_status"),
        default=AssetStatus.IN_STOCK,
        nullable=False,
        index=True,
    )
    condition: Mapped[AssetCondition] = mapped_column(
        Enum(AssetCondition, name="asset_condition"),
        default=AssetCondition.NEW,
        nullable=False,
    )
    # Why the asset is in its current state — "screen cracked, sent to vendor",
    # "stolen from car, FIR filed". Required for every manual status change.
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class AssetAssignment(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One person holding one asset, from hand-over to its end.

    Closed, never edited back: `closed_at` and `outcome` are set once, and the
    next holder is a new row.
    """

    __tablename__ = "asset_assignments"
    __table_args__ = (
        # One open assignment per asset. The register cannot show a laptop as
        # with two people, whatever order two requests arrive in.
        Index(
            "uq_asset_assignment_open",
            "asset_id",
            unique=True,
            postgresql_where=text("closed_at IS NULL"),
        ),
        CheckConstraint(
            "recovery_amount IS NULL OR recovery_amount >= 0",
            name="ck_asset_assignment_recovery_non_negative",
        ),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    assigned_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    expected_return_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    condition_out: Mapped[AssetCondition] = mapped_column(
        Enum(AssetCondition, name="asset_condition"), nullable=False
    )
    assignment_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Recorded by the holder themselves. Nobody acknowledges on their behalf.
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    returned_to_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    condition_in: Mapped[AssetCondition | None] = mapped_column(
        Enum(AssetCondition, name="asset_condition"), nullable=True
    )
    return_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What the holder owes for damage or loss. Recorded here; at exit it is
    # carried onto the clearance item, which is what the settlement reads.
    recovery_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    exit_record_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("exit_records.id", ondelete="SET NULL"), nullable=True
    )

    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[AssignmentOutcome | None] = mapped_column(
        Enum(AssignmentOutcome, name="asset_assignment_outcome"), nullable=True
    )
    return_reminder_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


class AssetRequest(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """An employee asking for an asset of some kind.

    Decided by their line manager or HR — never by themselves — and fulfilled by
    HR handing over an in-stock asset of that category, which closes it.
    """

    __tablename__ = "asset_requests"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("asset_categories.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    needed_by: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[AssetRequestStatus] = mapped_column(
        Enum(AssetRequestStatus, name="asset_request_status"),
        default=AssetRequestStatus.PENDING,
        nullable=False,
        index=True,
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    fulfilled_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
    )
    fulfilled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
