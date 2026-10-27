"""Demo requests from the marketing website.

A prospect fills in the "Book a demo" form on the website; the website's
server posts it to `POST /api/v1/public/demo-requests` and it lands here, for
the platform team to follow up from the platform console.

**Platform-level, not tenant data.** A demo request comes from someone who is
not a customer yet, so it belongs to no workspace and carries no `tenant_id`.
Only the platform's super admin can read or update these rows; there is no
tenant-scoped path to them at all.

**Minimal.** Only what the person typed is kept, plus the page they sent it
from and when they agreed to be contacted. The visitor's network address is
used for the rate limit and never stored.
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

__all__ = ["DemoRequest", "DemoRequestStatus"]


class DemoRequestStatus(str, enum.Enum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    CLOSED = "closed"


class DemoRequest(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "demo_requests"
    __table_args__ = (Index("ix_demo_requests_status_created", "status", "created_at"),)

    full_name: Mapped[str] = mapped_column(String(100), nullable=False)
    work_email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    company: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    company_size: Mapped[str] = mapped_column(String(20), nullable=False)
    role: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: e.g. ["payroll", "attendance"].
    interests: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: demo | pricing | partnership | other
    topic: Mapped[str] = mapped_column(String(20), nullable=False, default="demo")
    #: The website page the form was sent from.
    source_path: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: When the person agreed to be contacted. The form cannot be sent without it.
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[DemoRequestStatus] = mapped_column(
        Enum(DemoRequestStatus, name="demo_request_status"),
        default=DemoRequestStatus.NEW,
        nullable=False,
    )
    #: The platform team's own notes. Never shown to the prospect.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    handled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_demo_requests_handled_by"),
        nullable=True,
    )
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
