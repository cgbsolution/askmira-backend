"""A person's PIN for punching at a shared kiosk.

A kiosk is a tablet at a gate or a reception desk, registered as a device. A
person punches there with their employee code and this PIN. Only an Argon2 hash
is stored. Wrong guesses lock the PIN for a while, so a PIN of four digits
cannot be walked through at the kiosk.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class KioskPin(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "kiosk_pins"
    __table_args__ = (
        UniqueConstraint("tenant_id", "employee_id", name="uq_kiosk_pin_employee"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE", name="fk_kiosk_pins_employee_id"),
        nullable=False,
    )
    #: Argon2id, as for passwords. Never the PIN.
    pin_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EmployeeBadge(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A card issued to a person — an RFID or barcode badge — that punches at a kiosk.

    A kiosk's card reader types the card's number like a keyboard. Only a hash of
    the number is kept, with its last four digits to tell cards apart. A lost
    card is revoked, never deleted, so a punch made with it can still be traced.
    """

    __tablename__ = "employee_badges"
    __table_args__ = (
        Index(
            "uq_employee_badges_active",
            "tenant_id",
            "badge_hash",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE", name="fk_employee_badges_employee_id"),
        nullable=False,
        index=True,
    )
    #: SHA-256 of the workspace and the card number. Never the number.
    badge_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    last_four: Mapped[str] = mapped_column(String(4), nullable=False)
    issued_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_employee_badges_issued_by"),
        nullable=True,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
