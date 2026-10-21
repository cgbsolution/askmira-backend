"""An employee's in-portal acknowledgement of a letter issued to them.

This is not the e-signature path. `IssuedLetter.signature_*` is a signed PDF
coming back from a provider; this is the employee opening the letter in the
portal, typing their name and accepting it — the record most Indian employers
actually need for an appointment or increment letter, and one that works on a
deployment with no signature provider connected.

The row records *what* was accepted, not only that something was: the
letter's content hash at the moment of acceptance. A letter whose stored text
later stopped matching its hash was not the letter this person accepted, and
the mismatch is detectable from this row alone.

One row per letter. HR asking for an acknowledgement creates it with
`requested_at`; the employee accepting fills the acknowledgement columns, once.
They are never cleared — a revoked letter keeps the record that it had been
accepted, because that is what happened.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class LetterAcknowledgement(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "letter_acknowledgements"
    __table_args__ = (
        UniqueConstraint("tenant_id", "letter_id", name="uq_letter_acknowledgement_letter"),
    )

    letter_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("issued_letters.id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ── Asked for ───────────────────────────────────────────────────────────
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ── Given ───────────────────────────────────────────────────────────────
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: The name the employee typed as their confirmation.
    typed_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: The letter's content hash when it was accepted.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
