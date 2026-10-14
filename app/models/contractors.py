"""Contractors and consultants paid against invoices, with TDS under 194C / 194J.

A contractor is not an employee: no payroll, no PF or ESI. What the company owes
the tax department is TDS on what it pays them — under section 194C for
contract work and 194J for professional or technical fees — deposited and
reported quarterly in Form 26Q.

Each invoice records the TDS rate and amount it was charged, and which
`tds_non_salary` rule-set version those came from, so a later change to rates
never rewrites a deduction already made.
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
from app.models.types import EncryptedText


class ContractorKind(str, enum.Enum):
    INDIVIDUAL = "individual"
    HUF = "huf"
    FIRM = "firm"
    COMPANY = "company"


class TdsSection(str, enum.Enum):
    #: Payment to contractors.
    S194C = "194C"
    #: Fees for professional services.
    S194J_PROFESSIONAL = "194J_professional"
    #: Fees for technical services (and call-centre work).
    S194J_TECHNICAL = "194J_technical"


class InvoiceStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    PAID = "paid"
    CANCELLED = "cancelled"


class Contractor(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "contractors"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[ContractorKind] = mapped_column(
        Enum(ContractorKind, name="contractor_kind"), nullable=False
    )
    pan: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    gstin: Mapped[str | None] = mapped_column(String(15), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    default_section: Mapped[TdsSection] = mapped_column(
        Enum(TdsSection, name="tds_section"), nullable=False
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ContractorInvoice(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "contractor_invoices"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "contractor_id", "invoice_number", name="uq_contractor_invoice_number"
        ),
        CheckConstraint("amount > 0", name="ck_contractor_invoice_amount"),
        CheckConstraint("gst_amount >= 0", name="ck_contractor_invoice_gst"),
        CheckConstraint("tds_amount >= 0", name="ck_contractor_invoice_tds"),
    )

    contractor_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("contractors.id", ondelete="RESTRICT", name="fk_contractor_invoices_contractor"),
        nullable=False,
        index=True,
    )
    invoice_number: Mapped[str] = mapped_column(String(64), nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The value TDS is charged on: the invoice before GST.
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    gst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    section: Mapped[TdsSection] = mapped_column(
        Enum(TdsSection, name="tds_section", create_type=False), nullable=False
    )
    #: The amount TDS was charged on — more than this invoice when the year's
    #: threshold was crossed and earlier invoices are caught up.
    tds_base: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    tds_rate: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=0, nullable=False)
    tds_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    net_payable: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    tds_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_set_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "payroll_rule_sets.id", ondelete="RESTRICT", name="fk_contractor_invoices_rule_set"
        ),
        nullable=True,
    )
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, name="contractor_invoice_status"),
        default=InvoiceStatus.PENDING,
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_contractor_invoices_created_by"),
        nullable=True,
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_contractor_invoices_approved_by"),
        nullable=True,
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
