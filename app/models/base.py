"""Declarative base and the mixins every model is built from."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column


class Base(DeclarativeBase):
    """Shared declarative base. All models inherit from this."""


class UUIDMixin:
    """UUID primary key, generated application-side.

    UUIDs rather than serial ints: tenant data gets exported, merged and
    referenced across services, and a guessable sequential id in a URL is a
    weak spot in a multi-tenant product.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TenantMixin:
    """Adds the tenant scope column plus its index.

    Every tenant-scoped table carries this. The value is always taken from the
    caller's JWT (`tid`), never from a request body — see `core/deps.py`.
    """

    @declared_attr
    def tenant_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805
        return mapped_column(
            PgUUID(as_uuid=True),
            ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )


class SoftDeleteMixin:
    """Marks a row inactive without losing it.

    HR records are referenced by payroll runs and audit trails, so a hard
    delete would orphan history that has to stay reproducible.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None
