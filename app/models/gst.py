"""GST input tax credit: the register, the rules, and GSTR-2B.

═══════════════════════════════════════════════════════════════════════════════
`gst_input_credits` STORES THE VERDICT AND THE REASON, NOT JUST THE AMOUNT
═══════════════════════════════════════════════════════════════════════════════

A line marked ineligible with no explanation is one finance cannot challenge and
will eventually stop trusting — and once they stop trusting it they export
everything to a spreadsheet, which is the state this module exists to replace.
So `eligibility_reason` and `eligibility_reference` are not nullable.

The verdict is written **at capture** and not recomputed. Section 17(5) is not
going to change quietly, but a tenant's blocked-category configuration might,
and a line that read as eligible in September must not silently become
ineligible in March after somebody edited a rule.

═══════════════════════════════════════════════════════════════════════════════
UNCERTAIN IS A STORED STATE WITH A REVIEW ROW
═══════════════════════════════════════════════════════════════════════════════

`GstCreditReview` records who decided an uncertain line and why. Without it,
"reviewed by finance" is a status somebody set, and in an assessment the
question is who decided and on what basis.

═══════════════════════════════════════════════════════════════════════════════
GSTR-2B IS IMPORTED AS RECEIVED
═══════════════════════════════════════════════════════════════════════════════

`gstr2b_lines` holds the portal's own numbers, unmerged. Reconciliation compares
two independent records; folding the portal's figures into ours would leave
nothing to compare and the whole exercise would confirm itself.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
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


class GstEligibilityEnum(str, enum.Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    #: Nobody could decide from the invoice. Goes to a person, never guessed.
    UNCERTAIN = "uncertain"


class GstTaxSplitEnum(str, enum.Enum):
    CGST_SGST = "cgst_sgst"
    IGST = "igst"
    #: Intra-state in a state the entity is not registered in. Real tax, no
    #: credit - the hotel-bill case.
    CGST_SGST_OTHER_STATE = "cgst_sgst_other_state"


class GstinPortalStatusEnum(str, enum.Enum):
    NOT_CHECKED = "not_checked"
    ACTIVE = "active"
    CANCELLED = "cancelled"
    NOT_FOUND = "not_found"


class Gstr2bMatchEnum(str, enum.Enum):
    NOT_RECONCILED = "not_reconciled"
    MATCHED = "matched"
    MISSING_IN_2B = "missing_in_2b"
    VALUE_MISMATCH = "value_mismatch"


class GstEntityRegistration(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One legal entity's GST registration in one state.

    A company registered in three states has three rows, and which one is the
    recipient decides whether a given invoice's CGST and SGST are claimable at
    all. Storing a single tenant-level GSTIN is the shortcut that produces the
    hotel-bill error at scale.
    """

    __tablename__ = "gst_entity_registrations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "legal_entity_id", "state_code", name="uq_gst_reg_per_state"
        ),
        CheckConstraint("length(gstin) = 15", name="ck_gst_reg_gstin_length"),
        CheckConstraint("length(state_code) = 2", name="ck_gst_reg_state_length"),
    )

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("legal_entities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    gstin: Mapped[str] = mapped_column(String(15), nullable=False)
    state_code: Mapped[str] = mapped_column(String(2), nullable=False)
    state_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class GstBlockedCategory(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One category blocked or restricted under Section 17(5), as configured.

    `has_exceptions` decides whether a line in this category is refused outright
    or routed to a person. A blanket "ineligible" on motor vehicles costs a
    logistics company real money; a blanket "eligible" claims credit nobody is
    entitled to.

    `reviewed` defaults to **False**. The shipped reading is a starting point
    for a conversation with the tenant's advisor, not an answer.
    """

    __tablename__ = "gst_blocked_categories"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "category_key", name="uq_gst_blocked_category"
        ),
        CheckConstraint(
            "(reviewed = false) OR (reviewed_by IS NOT NULL)",
            name="ck_gst_rule_reviewed_names_reviewer",
        ),
    )

    category_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Not nullable. A line marked ineligible with no explanation is one
    #: finance cannot challenge.
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reference: Mapped[str] = mapped_column(
        String(120), default="Section 17(5), CGST Act", nullable=False
    )
    has_exceptions: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    exception_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    reviewed_on: Mapped[date | None] = mapped_column(Date, nullable=True)


class GstInputCredit(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One captured invoice line and what it is worth as input credit.

    The eligibility verdict is written at capture and not recomputed: a line
    that read as eligible in September must not silently become ineligible in
    March because somebody edited a rule.
    """

    __tablename__ = "gst_input_credits"
    __table_args__ = (
        CheckConstraint("taxable_value >= 0", name="ck_gst_taxable_nonneg"),
        CheckConstraint("cgst >= 0 AND sgst >= 0", name="ck_gst_state_tax_nonneg"),
        CheckConstraint("igst >= 0 AND cess >= 0", name="ck_gst_igst_cess_nonneg"),
        Index(
            "ix_gst_credits_invoice",
            "tenant_id",
            "vendor_gstin",
            "normalised_invoice_number",
        ),
        Index("ix_gst_credits_period", "tenant_id", "invoice_date"),
        Index("ix_gst_credits_eligibility", "tenant_id", "eligibility"),
    )

    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("legal_entities.id", ondelete="SET NULL"),
        nullable=True,
    )
    expense_claim_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("expense_claims.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
    )

    vendor_gstin: Mapped[str] = mapped_column(String(15), nullable=False)
    gstin_is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    gstin_problem: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gstin_portal_status: Mapped[GstinPortalStatusEnum] = mapped_column(
        Enum(GstinPortalStatusEnum, name="gstin_portal_status"),
        default=GstinPortalStatusEnum.NOT_CHECKED,
        nullable=False,
    )

    invoice_number: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Case-folded with separators stripped, so an OCR reading of `INV/2026/42`
    #: matches a portal download of `inv-2026-42`. Stored rather than computed
    #: at query time so the match is indexable.
    normalised_invoice_number: Mapped[str] = mapped_column(
        String(120), nullable=False
    )
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)

    category_key: Mapped[str] = mapped_column(String(64), nullable=False)
    taxable_value: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    cgst: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    sgst: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    igst: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    cess: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    hsn_sac: Mapped[str | None] = mapped_column(String(16), nullable=True)

    supplier_state_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    place_of_supply_state_code: Mapped[str | None] = mapped_column(
        String(2), nullable=True
    )
    recipient_state_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    tax_split: Mapped[GstTaxSplitEnum | None] = mapped_column(
        Enum(GstTaxSplitEnum, name="gst_tax_split"), nullable=True
    )

    eligibility: Mapped[GstEligibilityEnum] = mapped_column(
        Enum(GstEligibilityEnum, name="gst_eligibility"), nullable=False
    )
    #: Not nullable, either of them.
    eligibility_reason: Mapped[str] = mapped_column(Text, nullable=False)
    eligibility_reference: Mapped[str] = mapped_column(String(120), nullable=False)
    claimable_amount: Mapped[float] = mapped_column(
        Numeric(14, 2), default=0, nullable=False
    )
    #: What is in question on an uncertain line, so a review queue can be
    #: prioritised by money rather than by date.
    amount_at_stake: Mapped[float] = mapped_column(
        Numeric(14, 2), default=0, nullable=False
    )

    reconciliation_status: Mapped[Gstr2bMatchEnum] = mapped_column(
        Enum(Gstr2bMatchEnum, name="gstr2b_match_state"),
        default=Gstr2bMatchEnum.NOT_RECONCILED,
        nullable=False,
    )
    gstr2b_matched_at: Mapped[date | None] = mapped_column(Date, nullable=True)


class GstCreditReview(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A person's decision on an uncertain line.

    Without this row, "reviewed by finance" is a status somebody set. In an
    assessment the question is who decided and on what basis, and a status
    column answers neither.
    """

    __tablename__ = "gst_credit_reviews"
    __table_args__ = (
        UniqueConstraint("credit_id", name="uq_gst_review_per_credit"),
    )

    credit_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("gst_input_credits.id", ondelete="CASCADE"),
        nullable=False,
    )
    decision: Mapped[GstEligibilityEnum] = mapped_column(
        Enum(GstEligibilityEnum, name="gst_eligibility"), nullable=False
    )
    #: Not nullable. The basis is the whole point of the row.
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_on: Mapped[date] = mapped_column(Date, nullable=False)


class Gstr2bImport(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One GSTR-2B download, as imported."""

    __tablename__ = "gstr2b_imports"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "legal_entity_id", "period_label", name="uq_2b_per_period"
        ),
    )

    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("legal_entities.id", ondelete="SET NULL"),
        nullable=True,
    )
    period_label: Mapped[str] = mapped_column(String(16), nullable=False)
    line_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tax: Mapped[float] = mapped_column(
        Numeric(16, 2), default=0, nullable=False
    )
    imported_on: Mapped[date] = mapped_column(Date, nullable=False)
    imported_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    source_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Gstr2bLine(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One line as the portal reports it, stored unmerged.

    Reconciliation compares two independent records. Folding the portal's
    figures into our own would leave nothing to compare, and the exercise would
    confirm itself.
    """

    __tablename__ = "gstr2b_lines"
    __table_args__ = (
        Index(
            "ix_2b_lines_invoice",
            "tenant_id",
            "vendor_gstin",
            "normalised_invoice_number",
        ),
    )

    import_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("gstr2b_imports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vendor_gstin: Mapped[str] = mapped_column(String(15), nullable=False)
    vendor_name: Mapped[str | None] = mapped_column(String(240), nullable=True)
    invoice_number: Mapped[str] = mapped_column(String(120), nullable=False)
    normalised_invoice_number: Mapped[str] = mapped_column(
        String(120), nullable=False
    )
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    taxable_value: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    cgst: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    sgst: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    igst: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    cess: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)


class GstReconciliation(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One period's match against GSTR-2B, kept whatever it found.

    Kept even when everything matched: a clean period with no record is
    indistinguishable from one nobody reconciled, and the value of this feature
    is that it runs every month.
    """

    __tablename__ = "gst_reconciliations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "legal_entity_id",
            "period_label",
            name="uq_gst_reconciliation_period",
        ),
    )

    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("legal_entities.id", ondelete="SET NULL"),
        nullable=True,
    )
    period_label: Mapped[str] = mapped_column(String(16), nullable=False)
    captured_lines: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    portal_lines: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    matched_tax: Mapped[float] = mapped_column(
        Numeric(16, 2), default=0, nullable=False
    )
    missing_in_2b_tax: Mapped[float] = mapped_column(
        Numeric(16, 2), default=0, nullable=False
    )
    missing_in_register_tax: Mapped[float] = mapped_column(
        Numeric(16, 2), default=0, nullable=False
    )
    #: The headline. Eligible credit captured here and absent from the portal,
    #: which means the vendor has not filed and the money is recoverable by
    #: chasing them.
    at_risk_tax: Mapped[float] = mapped_column(
        Numeric(16, 2), default=0, nullable=False
    )
    counts: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    lines: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    run_on: Mapped[date] = mapped_column(Date, nullable=False)
    run_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
