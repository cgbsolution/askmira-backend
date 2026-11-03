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

A **forecast** is the prospective side: what a signed order is expected to bill
and cost, month by month, before any of it has happened (an order win from Ask
Mira writes one). It is kept apart from the entries on purpose — an entry is
something that happened, so entries still refuse future months — and the P&L
shows the two side by side. A forecast is withdrawn, never deleted: the order
it came from may be undone, and its trail stays.
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

__all__ = [
    "AccountAllocation",
    "AccountEntry",
    "AccountEntryKind",
    "AccountForecast",
    "AccountForecastKind",
    "AccountForecastLine",
    "ClientAccount",
]


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


class AccountForecastKind:
    """What a forecast line is."""

    BILLING = "billing"
    PEOPLE_COST = "people_cost"
    RECRUITMENT = "recruitment"
    OTHER_COST = "other_cost"
    ALL = (BILLING, PEOPLE_COST, RECRUITMENT, OTHER_COST)
    COSTS = (PEOPLE_COST, RECRUITMENT, OTHER_COST)


class AccountForecast(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A prospective P&L for an account: an order's expected billing and cost by month."""

    __tablename__ = "account_forecasts"
    __table_args__ = (
        Index("ix_account_forecasts_account", "tenant_id", "account_id", "start_month"),
        # One live forecast per order reference: a repeated order win finds it.
        Index(
            "uq_account_forecasts_live_reference",
            "tenant_id",
            "reference",
            unique=True,
            postgresql_where="status = 'active'",
        ),
        CheckConstraint("status IN ('active', 'withdrawn')", name="ck_account_forecast_status"),
        CheckConstraint("months BETWEEN 1 AND 120", name="ck_account_forecast_months"),
        CheckConstraint("extract(day from start_month) = 1", name="ck_account_forecast_start"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("client_accounts.id", ondelete="CASCADE", name="fk_account_forecasts_account"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    #: The order's reference, such as the customer's PO number.
    reference: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Where it came from: "order_win" (Ask Mira) or "manual".
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    #: The Ask Mira run, or the CRM deal, it came from.
    source_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: The first month it covers (always the 1st).
    start_month: Mapped[date] = mapped_column(Date, nullable=False)
    months: Mapped[int] = mapped_column(Integer, nullable=False)
    #: "active", or "withdrawn" when the order it came from was undone.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    #: The order's value as the customer's PO states it, when one was given.
    order_value: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_account_forecasts_creator"),
        nullable=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class AccountForecastLine(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One expected amount in one month: billing, people cost, recruitment or another cost."""

    __tablename__ = "account_forecast_lines"
    __table_args__ = (
        Index(
            "ix_account_forecast_lines_period",
            "tenant_id",
            "forecast_id",
            "period_year",
            "period_month",
        ),
        CheckConstraint(
            "kind IN ('billing', 'people_cost', 'recruitment', 'other_cost')",
            name="ck_account_forecast_line_kind",
        ),
        CheckConstraint("period_month BETWEEN 1 AND 12", name="ck_account_forecast_line_month"),
        CheckConstraint("amount > 0", name="ck_account_forecast_line_amount"),
    )

    forecast_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "account_forecasts.id", ondelete="CASCADE", name="fk_account_forecast_lines_forecast"
        ),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    period_year: Mapped[int] = mapped_column(Integer, nullable=False)
    period_month: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    #: What it is: a role ("OutSystems developer x2"), "Recruitment (5 hires)".
    label: Mapped[str] = mapped_column(String(160), nullable=False)
