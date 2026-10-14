"""What points can be spent on: the company's rewards catalogue.

Each item has a fixed points price and, optionally, a stock. Redeeming an item
spends that many points through the same ledger as any redemption — valued at
the rate of the points consumed — and takes one from stock in the same
transaction, so two people cannot both take the last one.
"""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class RewardCatalogueItem(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "reward_catalogue_items"
    __table_args__ = (
        UniqueConstraint("tenant_id", "sku", name="uq_reward_catalogue_sku"),
        CheckConstraint("points_cost > 0", name="ck_reward_catalogue_cost"),
        CheckConstraint("stock IS NULL OR stock >= 0", name="ck_reward_catalogue_stock"),
    )

    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    points_cost: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Left in stock. Null means unlimited (a voucher, a donation).
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
