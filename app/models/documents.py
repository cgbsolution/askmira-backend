"""Letter templates, issued letters and e-signature (slice 03).

Indian HR teams generate offer, appointment, confirmation, transfer, promotion,
relieving, experience and salary-certificate letters constantly. The absence of
this reads as a toy product in a demo.

Invariants:

1. `LetterTemplate` is versioned. Editing creates a new version; the version an
   issued letter used is never mutated, so a letter can always be re-rendered
   and verified against what was actually sent.
2. `IssuedLetter` is insert-then-status-only. `merged_html` and the stored PDF
   are immutable once written. A letter that went out is a document of record.
3. `reference_number` is unique per tenant. It is allocated inside the same
   transaction as the insert, from the database, never from an application
   counter. Gaps are acceptable; duplicates are not.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class LetterCategory(str, enum.Enum):
    OFFER = "offer"
    APPOINTMENT = "appointment"
    CONFIRMATION = "confirmation"
    TRANSFER = "transfer"
    PROMOTION = "promotion"
    RELIEVING = "relieving"
    EXPERIENCE = "experience"
    SALARY_CERTIFICATE = "salary_certificate"
    WARNING = "warning"
    PIP = "pip"
    CUSTOM = "custom"


class LetterStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SENT = "sent"
    SIGNED = "signed"
    REVOKED = "revoked"


class FieldSource(str, enum.Enum):
    """Where a merge token gets its value.

    MANUAL is what makes the UI able to prompt: the template declares it needs
    a value the system cannot know, and generation refuses without it.
    """

    EMPLOYEE = "employee"
    SALARY = "salary"
    TENANT = "tenant"
    MANUAL = "manual"
    COMPUTED = "computed"


# Short codes used in reference numbers, e.g. CTX/OFF/2026-27/00142.
CATEGORY_CODES: dict[LetterCategory, str] = {
    LetterCategory.OFFER: "OFF",
    LetterCategory.APPOINTMENT: "APT",
    LetterCategory.CONFIRMATION: "CNF",
    LetterCategory.TRANSFER: "TRF",
    LetterCategory.PROMOTION: "PRO",
    LetterCategory.RELIEVING: "REL",
    LetterCategory.EXPERIENCE: "EXP",
    LetterCategory.SALARY_CERTIFICATE: "SAL",
    LetterCategory.WARNING: "WRN",
    LetterCategory.PIP: "PIP",
    LetterCategory.CUSTOM: "GEN",
}


class LetterTemplate(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A versioned letter template.

    `body_html` is authored by a tenant admin and is treated as *trusted* —
    it is their own document design. Values merged into it are treated as
    untrusted and are escaped. See `letter_merge.py`.
    """

    __tablename__ = "letter_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", "version", name="uq_letter_template_version"),
    )

    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[LetterCategory] = mapped_column(
        Enum(LetterCategory, name="letter_category"), nullable=False, index=True
    )

    body_html: Mapped[str] = mapped_column(Text, nullable=False)
    header_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    footer_html: Mapped[str | None] = mapped_column(Text, nullable=True)

    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approver_roles: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), default=list, nullable=False
    )
    requires_signature: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    signatory_role: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Seed templates carry a visible annotation that the client's own HR and
    # legal teams must review them. This flag drives that banner in the UI.
    is_seed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    fields: Mapped[list[LetterTemplateField]] = relationship(
        back_populates="template", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<LetterTemplate {self.code} v{self.version}>"


class LetterTemplateField(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A merge token a template declares it needs.

    Declaring them explicitly is what lets the UI prompt for MANUAL values and
    lets generation refuse *before* producing a letter with a hole in it.
    """

    __tablename__ = "letter_template_fields"
    __table_args__ = (
        UniqueConstraint("tenant_id", "template_id", "token", name="uq_template_field_token"),
    )

    template_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("letter_templates.id", ondelete="CASCADE"), nullable=False
    )

    # The token as it appears in the body, without braces: "employee.first_name".
    token: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[FieldSource] = mapped_column(
        Enum(FieldSource, name="field_source"), nullable=False
    )
    # Dotted path within the source, or the computed handler name.
    source_path: Mapped[str | None] = mapped_column(String(128), nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    template: Mapped[LetterTemplate] = relationship(back_populates="fields")


class IssuedLetter(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A letter that was actually produced.

    `merged_html`, `content_hash` and `pdf_storage_key` are written once and
    never updated. Only `status` and the signature columns move afterwards.
    """

    __tablename__ = "issued_letters"
    __table_args__ = (
        UniqueConstraint("tenant_id", "reference_number", name="uq_issued_letter_reference"),
        # Exactly one recipient. An offer letter goes to a candidate, who is not
        # an employee yet; every other letter goes to an employee. Allowing both
        # or neither would let a letter exist that nobody was sent.
        CheckConstraint(
            "(employee_id IS NULL) <> (candidate_id IS NULL)",
            name="ck_issued_letter_one_recipient",
        ),
    )

    # Nullable since slice 19: an offer letter is issued before there is an
    # employee record. It is still a letter — same numbering series, same
    # content hash, same template versioning, same e-signature path — and a
    # parallel offer-letter renderer would have drifted from this one inside a
    # release.
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="RESTRICT"), nullable=True
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("candidates.id", ondelete="RESTRICT"), nullable=True
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("letter_templates.id", ondelete="RESTRICT"), nullable=False
    )
    # Snapshotted, so an issued letter always names the exact template version
    # it was rendered from even after the template moves on.
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[LetterCategory] = mapped_column(
        Enum(LetterCategory, name="letter_category"), nullable=False, index=True
    )

    reference_number: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    merged_html: Mapped[str] = mapped_column(Text, nullable=False)
    # SHA-256 of the merged HTML. A re-render can be verified against this, so
    # tampering with the stored document is detectable.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    pdf_storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # The MANUAL values supplied at issuance, kept so the letter is
    # reproducible from its inputs.
    manual_values: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # The date the letter's EMPLOYEE and SALARY values were resolved as of.
    as_of_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[LetterStatus] = mapped_column(
        Enum(LetterStatus, name="letter_status"),
        default=LetterStatus.DRAFT,
        nullable=False,
        index=True,
    )

    issued_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── E-signature ─────────────────────────────────────────────────────────
    signature_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    signature_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signed_pdf_storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)

    revoked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<IssuedLetter {self.reference_number} {self.status.value}>"


class LetterSequence(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """The counter behind reference numbers.

    A row per (tenant, category, financial year). Allocation takes a row lock —
    `SELECT ... FOR UPDATE` — so concurrent issuance cannot produce a duplicate.
    An application-level counter would.
    """

    __tablename__ = "letter_sequences"
    __table_args__ = (
        UniqueConstraint("tenant_id", "category", "financial_year", name="uq_letter_sequence_slot"),
    )

    category: Mapped[LetterCategory] = mapped_column(
        Enum(LetterCategory, name="letter_category"), nullable=False
    )
    # Indian financial year as "2026-27".
    financial_year: Mapped[str] = mapped_column(String(9), nullable=False)
    last_value: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class BulkLetterJob(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A bulk issuance run.

    Appraisal letters after an increment cycle go out in the hundreds at once,
    so issuance is a tracked job with per-employee outcomes rather than one
    long request.
    """

    __tablename__ = "bulk_letter_jobs"

    template_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("letter_templates.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    succeeded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # queued | running | completed | failed
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False, index=True)
    # [{"employee_id": "...", "reference_number": "...", "error": null}, ...]
    results: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
