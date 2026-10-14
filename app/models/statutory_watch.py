"""Statutory change notices, and which workspaces have been told.

A **notice** is published by the platform (AskMira's own super admin, not a
customer): a change to PF, ESI, Professional Tax, LWF or income tax, with its
jurisdiction, the date it takes effect, the circular or notification it comes
from, and — where the platform's statutory reviewer supplied them — the rules
for a new rule-set version. It is deliberately not tenant-scoped: one change
in the law is one notice, read by every workspace it affects.

Each workspace sees a notice as in place, drafted, or needing action, from its
own rule sets; drafting from a notice inserts a new unapproved version that a
second admin approves, as any rule-set change.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class StatutoryNotice(Base, UUIDMixin, TimestampMixin):
    """One change in the law. **Not tenant-scoped**: it is the same for everyone."""

    __tablename__ = "statutory_notices"

    rule_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    #: "IN" for a national levy, else the state code.
    jurisdiction: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    source_reference: Mapped[str] = mapped_column(String(512), nullable=False)
    #: The rules for a new version, where reviewed; null leaves them to the workspace.
    suggested_rules: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_statutory_notices_published_by"),
        nullable=True,
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StatutoryNoticeAlert(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A workspace was alerted about a notice. One per notice per workspace."""

    __tablename__ = "statutory_notice_alerts"
    __table_args__ = (UniqueConstraint("tenant_id", "notice_id", name="uq_statutory_notice_alert"),)

    notice_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "statutory_notices.id", ondelete="CASCADE", name="fk_statutory_notice_alerts_notice"
        ),
        nullable=False,
    )
