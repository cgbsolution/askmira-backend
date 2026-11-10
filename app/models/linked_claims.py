"""Expense Genie claims paid with salary in AskMiraa payroll.

With Expense Genie linked, approved claims there are paid in AskMiraa's payroll
run like AskMiraa's own claims are. One row per claim is the double-pay guard:
a claim reference is unique per workspace, written when the run that pays it is
approved, so no second run can pay it again. `marked_at` is when Expense Genie
recorded the payment on its side; until then the scheduler keeps telling it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class LinkedClaimPayment(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "linked_claim_payments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "product", "reference", name="uq_linked_claim_payment"),
    )

    #: The linked product the claim lives in ("expense": Expense Genie).
    product: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The claim's own reference there.
    reference: Mapped[str] = mapped_column(String(128), nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("payroll_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: When the product recorded the payment on its side.
    marked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Why the product could not be told yet (retried by the scheduler).
    mark_error: Mapped[str | None] = mapped_column(Text, nullable=True)
