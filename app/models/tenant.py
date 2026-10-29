"""Tenant — the top of the ownership tree (FRD 7.2)."""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, DateTime, Enum, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.org import LegalEntity
    from app.models.user import User


class TenantPlan(str, enum.Enum):
    PILOT = "pilot"
    GROWTH = "growth"
    ENTERPRISE = "enterprise"


class Tenant(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    """A customer company. Everything else in the schema hangs off this.

    `deleted_at` is set when the platform deletes the company. Its data is
    kept until `purge_after` so it can be exported and handed back, and then
    purged (`services/tenant_purge.py`) — every row and every file. The tenant
    row itself and its audit trail stay: the audit log is insert-only and is
    the record that the workspace existed and was purged.
    """

    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(63), unique=True, index=True, nullable=False)

    plan: Mapped[TenantPlan] = mapped_column(
        Enum(TenantPlan, name="tenant_plan"), default=TenantPlan.PILOT, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # India-hosted by default; stored per tenant so the architecture never
    # hard-codes a single region (FRD 6 — Data residency).
    data_region: Mapped[str] = mapped_column(String(32), default="ap-south-1", nullable=False)

    # Which modules this tenant has switched on, e.g. {"payroll": true}.
    # Nav and API authorisation both read from here.
    enabled_modules: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # Tenant-level defaults: fiscal year start, week start, currency, and so on.
    settings: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # The company's logo in file storage, shown on its own sign-in page and in
    # the portal. Public by design: it is on the company's website already.
    logo_key: Mapped[str | None] = mapped_column(String(512), nullable=True)

    #: For a deleted workspace: the day its data is purged, and when it was.
    purge_after: Mapped[date | None] = mapped_column(Date, nullable=True)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    users: Mapped[list[User]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    legal_entities: Mapped[list[LegalEntity]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )

    @property
    def linked_products(self) -> dict[str, bool]:
        """Our separate products that replace a module here, e.g. {"interview": True}.

        A mirror of the active `product_links`, kept in settings so the session
        payload needs no extra query. Only linking and unlinking write it.
        """
        linked = (self.settings or {}).get("linked_products") or {}
        return {key: bool(on) for key, on in linked.items() if on}

    def __repr__(self) -> str:
        return f"<Tenant {self.slug}>"
