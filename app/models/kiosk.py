"""A person's PIN for punching at a shared kiosk.

A kiosk is a tablet at a gate or a reception desk, registered as a device. A
person punches there with their employee code and this PIN. Only an Argon2 hash
is stored. Wrong guesses lock the PIN for a while, so a PIN of four digits
cannot be walked through at the kiosk.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
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
