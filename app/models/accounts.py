"""Client accounts and their P&L: what was billed, and the cost mapped to each.

An account is a client engagement the company bills. Its P&L for a month is

    billed - (people cost mapped to it + its other direct costs)

- **Billed** is entered by HR or finance (one row per invoice, or one per month),
  typed or uploaded from Excel. AskMira does not invoice; it records what the
  invoicing system raised.
- **People cost** is what payroll actually paid: each payslip's cost to company
  (`payslips.employer_cost`), shared out by the person's mapping. A mapping says
  "this person works N% on this account from this date"; a month is shared by
  the days each mapping covers, so a mid-month move is split, not rounded.
- **Other direct costs** (travel, software, a subcontractor) are entered per
  month like billing.

A mapping is never edited in place: a change of share ends the old row and
starts a new one, so a past month always reads the share that was true then.
Money is Numeric throughout.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

__all__ = ["AccountAllocation", "AccountEntry", "AccountEntryKind", "ClientAccount"]


class ClientAccount(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "client_accounts"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_client_account_code"),)

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    #: The customer billed, when it differs from the account's name.
    client_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    #: The company in the group that bills it, if the workspace has several.
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("legal_entities.id", ondelete="SET NULL", name="fk_client_accounts_entity"),
        nullable=True,
    )
    #: Who looks after the account (an account manager or delivery head).
    owner_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="SET NULL", name="fk_client_accounts_owner"),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class AccountEntryKind:
    """What an entry is. Kept as text, so the upload's own words map onto it."""

    BILLING = "billing"
    COST = "cost"
    ALL = (BILLING, COST)


class AccountEntry(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One amount for one account in one month: an invoice billed, or a direct cost."""

    __tablename__ = "account_entries"
    __table_args__ = (
        Index(
            "ix_account_entries_period", "tenant_id", "account_id", "period_year", "period_month"
        ),
        CheckConstraint("kind IN ('billing', 'cost')", name="ck_account_entry_kind"),
        CheckConstraint("period_month BETWEEN 1 AND 12", name="ck_account_entry_month"),
        # A credit note is a negative billing; nothing is ever zero.
        CheckConstraint("amount <> 0", name="ck_account_entry_amount"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("client_accounts.id", ondelete="CASCADE", name="fk_account_entries_account"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    period_year: Mapped[int] = mapped_column(Integer, nullable=False)
    period_month: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    #: For a billing: the invoice number. For a cost: what it was (Travel, Software…).
    reference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    entry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class AccountAllocation(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A person working a share of their time on an account, from a date."""

    __tablename__ = "account_allocations"
    __table_args__ = (
        Index("ix_account_allocations_employee", "tenant_id", "employee_id", "effective_from"),
        Index("ix_account_allocations_account", "tenant_id", "account_id"),
        CheckConstraint("percent > 0 AND percent <= 100", name="ck_account_allocation_percent"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_account_allocation_dates",
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE", name="fk_account_allocations_employee"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("client_accounts.id", ondelete="CASCADE", name="fk_account_allocations_account"),
        nullable=False,
    )
    percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    #: Last day of the mapping; open-ended while null.
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
