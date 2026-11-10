"""How payroll posts to the books: one ledger mapping per payslip line code.

A payroll journal is only as good as its ledger names, and a name that does not
exist in the company's books makes Tally or Zoho reject the import — or, worse,
create a stray ledger that somebody reconciles by hand for a year. So the
mapping is the tenant's, line by line, and an export with an unmapped line is
refused rather than filled in with a guess.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

__all__ = ["LedgerMapping", "LedgerNature", "PayrollJournalPosting"]


class LedgerNature(str, enum.Enum):
    #: Pay to the employee: debited to an expense ledger.
    EARNING = "earning"
    #: Taken from the employee: credited to a liability or recovery ledger.
    DEDUCTION = "deduction"
    #: The employer's own cost: debited to an expense, credited to a liability.
    EMPLOYER_CONTRIBUTION = "employer_contribution"
    #: What the employees are owed after deductions: credited to salary payable.
    NET_PAY = "net_pay"


class LedgerMapping(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Which ledgers one payslip line code posts to."""

    __tablename__ = "ledger_mappings"
    __table_args__ = (UniqueConstraint("tenant_id", "line_code", name="uq_ledger_mapping_code"),)

    #: A payslip line code: BASIC, PF_EMPLOYEE, OVERTIME, ADJ_INCENTIVE, or NET_PAY.
    line_code: Mapped[str] = mapped_column(String(64), nullable=False)
    nature: Mapped[LedgerNature] = mapped_column(
        Enum(LedgerNature, name="ledger_nature"), nullable=False
    )
    #: The expense ledger, for earnings and employer contributions.
    debit_ledger: Mapped[str | None] = mapped_column(String(160), nullable=True)
    #: The liability ledger, for deductions, employer contributions and net pay.
    credit_ledger: Mapped[str | None] = mapped_column(String(160), nullable=True)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class PayrollJournalPosting(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """An approved run's journal, posted straight into the company's books.

    One per (run, books): the unique constraint is what stops a second click from
    posting the month's salary twice. Kept with the books' own journal id and
    number so the accountant can find it there.
    """

    __tablename__ = "payroll_journal_postings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "run_id", "target", name="uq_payroll_journal_posting"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("payroll_runs.id", ondelete="CASCADE"), nullable=False
    )
    #: Which books: "books_zoho".
    target: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    external_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Total of the debits (equal to the credits).
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    posted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
