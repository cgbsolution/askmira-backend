"""Salary disbursement: bank profiles, batches, and the maker-checker record.

The control this schema exists to support is maker-checker: the person who
prepares a disbursement batch must not be the person who releases it. That is
why `prepared_by_user_id` and `approved_by_user_id` are separate columns and
why `approved_file_hash` is stored alongside them.

The hash is the part that makes the control real rather than decorative. An
approval that does not name *what* was approved can be given on one file and
used to release another — regenerate after approval, and the signature carries
over to a file nobody looked at. Storing the hash means a regenerated file has
a different hash, the approval no longer matches, and release refuses.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class BankFileFormat(str, enum.Enum):
    HDFC = "hdfc"
    ICICI = "icici"
    SBI = "sbi"
    AXIS = "axis"
    KOTAK = "kotak"
    GENERIC_NEFT = "generic_neft"


class VerificationMethod(str, enum.Enum):
    # A one-rupee credit that confirms the account exists and whose name is on
    # it. The only method that actually verifies anything.
    PENNY_DROP = "penny_drop"
    # Someone looked at a cancelled cheque. Better than nothing.
    MANUAL = "manual"
    NONE = "none"


class DisbursementStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    RELEASED = "released"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"


class LineStatus(str, enum.Enum):
    PENDING = "pending"
    CREDITED = "credited"
    FAILED = "failed"
    # Reissued in a supplementary batch after a failure.
    REISSUED = "reissued"


class BankProfile(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """The company account salaries are paid from, and the bank's file format."""

    __tablename__ = "bank_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "legal_entity_id", "account_number",
                         name="uq_bank_profile_account"),
    )

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("legal_entities.id", ondelete="RESTRICT"), nullable=False
    )
    bank_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Encrypted at rest, like every other account number here.
    account_number: Mapped[str] = mapped_column(String(512), nullable=False)
    ifsc: Mapped[str] = mapped_column(String(11), nullable=False)
    corporate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    file_format: Mapped[BankFileFormat] = mapped_column(
        Enum(BankFileFormat, name="bank_file_format"), nullable=False
    )
    # Per-bank knobs the serialiser reads: beneficiary name length, the
    # transaction code this corporate is set up for, whether the debit account
    # repeats on every line. Banks vary these per customer, so they cannot be
    # constants in a serialiser.
    format_config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class EmployeeBankAccount(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Where an employee's salary goes.

    `account_holder_name` is stored separately from the employee's name on
    purpose: they differ legitimately (a maiden name, an initial spelled out)
    and the bank matches on the account holder. Comparing the two is a
    validation check, not a reason to keep only one.
    """

    __tablename__ = "employee_bank_accounts"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    # Encrypted at rest — a payroll database dump should not be a list of every
    # employee's bank account.
    account_number: Mapped[str] = mapped_column(String(512), nullable=False)
    ifsc: Mapped[str] = mapped_column(String(11), nullable=False)
    account_holder_name: Mapped[str] = mapped_column(String(255), nullable=False)
    bank_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_method: Mapped[VerificationMethod] = mapped_column(
        Enum(VerificationMethod, name="verification_method"),
        default=VerificationMethod.NONE,
        nullable=False,
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class DisbursementBatch(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One salary payment file, and who approved it.

    A batch moves DRAFT → PENDING_APPROVAL → APPROVED → RELEASED. The
    interesting transitions are the two that refuse:

    - APPROVED requires a different user from the one who prepared it.
    - RELEASED requires the current file hash to match the approved one.
    """

    __tablename__ = "disbursement_batches"

    payroll_run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("payroll_runs.id", ondelete="RESTRICT"), nullable=False
    )
    bank_profile_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("bank_profiles.id", ondelete="RESTRICT"), nullable=False
    )

    status: Mapped[DisbursementStatus] = mapped_column(
        Enum(DisbursementStatus, name="disbursement_status"),
        default=DisbursementStatus.DRAFT,
        nullable=False,
        index=True,
    )
    # A supplementary batch reissuing failed credits from an earlier one. It
    # exists so a handful of failures does not mean regenerating the whole run.
    supplements_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("disbursement_batches.id", ondelete="SET NULL"),
        nullable=True,
    )

    total_amount: Mapped[float] = mapped_column(Numeric(16, 2), default=0, nullable=False)
    employee_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    value_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # The file itself, encrypted at rest. It is a list of every employee's bank
    # account and salary; a database dump must not be that list in the clear.
    encrypted_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    prepared_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    prepared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # What they actually approved. Without this the approval is transferable to
    # a file nobody looked at, and the whole control is theatre.
    approved_file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    validation_exceptions: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    lines: Mapped[list[DisbursementLine]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )

    @property
    def approval_is_current(self) -> bool:
        """Whether the approval still applies to the file that exists now."""
        return bool(
            self.approved_file_hash
            and self.file_hash
            and self.approved_file_hash == self.file_hash
        )


class DisbursementLine(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One employee's credit.

    The account number and IFSC are snapshotted onto the line rather than read
    through to `EmployeeBankAccount`. An employee changing their account next
    month must not retroactively change what this batch says was paid, and a
    failed credit has to be investigated against the details actually sent.
    """

    __tablename__ = "disbursement_lines"

    batch_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("disbursement_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    payslip_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("payslips.id", ondelete="SET NULL"), nullable=True
    )

    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    account_number: Mapped[str] = mapped_column(String(512), nullable=False)
    ifsc: Mapped[str] = mapped_column(String(11), nullable=False)
    beneficiary_name: Mapped[str] = mapped_column(String(255), nullable=False)

    status: Mapped[LineStatus] = mapped_column(
        Enum(LineStatus, name="disbursement_line_status"),
        default=LineStatus.PENDING,
        nullable=False,
        index=True,
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    bank_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    batch: Mapped[DisbursementBatch] = relationship(back_populates="lines")


class DisbursementDownload(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Every time someone took a copy of a disbursement file.

    A file holding the whole workforce's bank details leaving the system is an
    event worth recording on its own, not a line in a general audit stream.
    Insert-only, like the audit log.
    """

    __tablename__ = "disbursement_downloads"

    batch_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("disbursement_batches.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    downloaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    downloaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # The hash of what they took, so a leaked file can be traced to a download.
    file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
