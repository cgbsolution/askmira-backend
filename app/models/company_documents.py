"""Company documents: the policy library.

Documents that belong to the company rather than to a person — the leave
policy, the code of conduct, the POSH policy, the employee handbook. HR files
them with a type, a reference number, issue and expiry dates and the soft
copy; the ones marked visible are what an employee finds when they look a
policy up (FRD 5.1, "policy lookup").
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class CompanyDocument(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "company_documents"

    #: A free label HR chooses ("Leave policy", "IT policy"); the portal offers
    #: the usual ones. Grouped on screen by this.
    document_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    document_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)

    file_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)

    issued_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: Published to employees (policy lookup); HR always sees every row.
    is_employee_visible: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )

    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: Every employee must read and acknowledge this one (a POSH policy, the
    #: code of conduct). Acknowledgements bind to `content_sha256`.
    requires_acknowledgement: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: SHA-256 of the stored file. An acknowledgement of a file is worthless if
    #: the file can change under it, so each one records the hash it saw.
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 0 = no expiry notice sent, 1 = "expires soon" sent, 2 = "expired" sent.
    expiry_reminder_stage: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )


class CompanyDocumentAcknowledgement(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One employee's acknowledgement of one version of a company document."""

    __tablename__ = "company_document_acknowledgements"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "employee_id", "content_sha256", name="uq_company_document_ack"
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("company_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: The name the employee typed. Must be their own.
    typed_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    acknowledged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
