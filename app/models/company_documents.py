"""Company documents: the policy library.

Documents that belong to the company rather than to a person — the leave
policy, the code of conduct, the POSH policy, the employee handbook. HR files
them with a type, a reference number, issue and expiry dates and the soft
copy; the ones marked visible are what an employee finds when they look a
policy up (FRD 5.1, "policy lookup").
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, Integer, String, Text
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
