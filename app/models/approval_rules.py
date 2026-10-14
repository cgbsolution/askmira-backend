"""Configurable approval chains, and approvers who are away.

An `ApprovalRule` says who approves a leave request or an expense claim, in
order, when its conditions hold — "claims over ₹50,000 in Sales: line manager,
then the regional head, then finance". The first active rule that matches, by
priority, decides; with none, the built-in single step (the line manager)
stands. The chain is resolved and snapshotted at submission, so a reorg next
week does not change who was asked.

An `ApprovalDelegation` lets an approver hand their approvals to somebody else
for a period — leave, travel — so a request does not wait on an empty desk.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date

from sqlalchemy import Boolean, Date, Enum, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

__all__ = ["ApprovalDelegation", "ApprovalProcess", "ApprovalRule"]


class ApprovalProcess(str, enum.Enum):
    LEAVE = "leave"
    EXPENSE = "expense"


class ApprovalRule(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "approval_rules"
    __table_args__ = (Index("ix_approval_rules_process", "tenant_id", "process", "priority"),)

    process: Mapped[ApprovalProcess] = mapped_column(
        Enum(ApprovalProcess, name="approval_process"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Lower runs first; the first matching active rule decides.
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Conditions. Each one left empty matches everything.
    min_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    max_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    min_days: Mapped[float | None] = mapped_column(Numeric(6, 1), nullable=True)
    leave_type_ids: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    expense_categories: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    department_ids: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    location_ids: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    #: [{"kind": "line_manager" | "skip_level" | "user" | "hr", "user_id": "..."}]
    steps: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    #: A step waiting longer than this goes up a level (the approver's manager, else HR).
    escalate_after_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class ApprovalDelegation(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "approval_delegations"
    __table_args__ = (
        Index("ix_approval_delegations_from", "tenant_id", "from_user_id", "starts_on"),
    )

    from_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    to_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
