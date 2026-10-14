"""Form 16 Part A as TRACES issued it, kept to be joined to our Part B.

Part A — the TDS deposited against the employee's PAN, quarter by quarter — is
generated only by the Income Tax department's TRACES portal; an employer
downloads it and cannot produce it. HR uploads those PDFs here (one at a time,
or the ZIP TRACES gives, matched to people by the PAN in each file name), and
the Form 16 an employee downloads is Part A followed by Part B in one file.
The file is kept in private storage; only its key is here.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class Form16PartA(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "form16_part_a"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "employee_id", "financial_year", name="uq_form16_part_a_year"
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE", name="fk_form16_part_a_employee_id"),
        nullable=False,
        index=True,
    )
    #: "2025-26".
    financial_year: Mapped[str] = mapped_column(String(7), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_form16_part_a_uploaded_by"),
        nullable=True,
    )
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
