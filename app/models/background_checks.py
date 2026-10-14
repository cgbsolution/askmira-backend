"""Background verification: the request, the candidate's consent, the outcome.

A background check reads somebody's past employers, their degree and, for
some roles, court records. Under DPDP that is its own purpose and needs the
candidate's own consent, after being told which agency will do it — so a check
starts as `AWAITING_CONSENT` and nothing is sent to anybody until the
candidate agrees in their portal. The consent (what they were shown, the name
they typed, when, from where) is kept on the row, beside the outcome.

The verification itself is done by an agency. `vendor` names the connector
that hands the case over; "manual" means HR sends it and records what comes
back, which works with any agency today.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

__all__ = ["BackgroundCheck", "BackgroundCheckStatus"]


class BackgroundCheckStatus(str, enum.Enum):
    AWAITING_CONSENT = "awaiting_consent"
    IN_PROGRESS = "in_progress"
    #: Every check came back as stated.
    CLEAR = "clear"
    #: At least one check found something different from what was stated.
    DISCREPANCY = "discrepancy"
    #: Nothing contradicted, but at least one check could not be completed.
    UNABLE_TO_VERIFY = "unable_to_verify"
    #: Withdrawn by HR, or declined by the candidate.
    CANCELLED = "cancelled"


class BackgroundCheck(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "background_checks"
    __table_args__ = (
        Index("ix_background_checks_candidate", "tenant_id", "candidate_id", "created_at"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    offer_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("offers.id", ondelete="SET NULL"), nullable=True
    )
    vendor: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    #: The agency doing the work, named to the candidate before they consent.
    agency_name: Mapped[str] = mapped_column(String(255), nullable=False)
    vendor_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: [{"type": "employment", "status": "pending"|"clear"|..., "note": "..."}]
    checks: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    status: Mapped[BackgroundCheckStatus] = mapped_column(
        Enum(BackgroundCheckStatus, name="background_check_status"),
        default=BackgroundCheckStatus.AWAITING_CONSENT,
        nullable=False,
    )

    #: Exactly what the candidate was asked to agree to.
    consent_notice: Mapped[str] = mapped_column(Text, nullable=False)
    consent_given_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consent_typed_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    consent_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    consent_user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)

    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    report_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)

    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    cancelled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
