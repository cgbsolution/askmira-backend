"""Employee master record and document vault (FRD 5.1, FS-CHR-01/02)."""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TenantMixin, TimestampMixin, UUIDMixin
from app.models.types import EncryptedText

if TYPE_CHECKING:
    from app.models.user import User


class EmploymentStatus(str, enum.Enum):
    ACTIVE = "active"
    ON_NOTICE = "on_notice"
    ON_LEAVE = "on_leave"
    EXITED = "exited"
    PRE_JOINING = "pre_joining"


class EmploymentType(str, enum.Enum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERN = "intern"
    CONSULTANT = "consultant"


class Gender(str, enum.Enum):
    FEMALE = "female"
    MALE = "male"
    NON_BINARY = "non_binary"
    UNDISCLOSED = "undisclosed"


class Employee(Base, UUIDMixin, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """The employee master record.

    Soft-deleted rather than removed: payroll runs, audit trails and analytics
    all reference employees historically, and those have to stay reproducible
    after someone leaves.
    """

    __tablename__ = "employees"
    __table_args__ = (
        UniqueConstraint("tenant_id", "employee_code", name="uq_employees_tenant_code"),
    )

    employee_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # ── Identity ────────────────────────────────────────────────────────────
    first_name: Mapped[str] = mapped_column(String(128), nullable=False)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    work_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    personal_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    gender: Mapped[Gender | None] = mapped_column(Enum(Gender, name="gender"), nullable=True)
    photo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # ── Placement ───────────────────────────────────────────────────────────
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("legal_entities.id", ondelete="SET NULL"), nullable=True
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("locations.id", ondelete="SET NULL"), nullable=True
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL"), nullable=True
    )
    manager_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    #: A second, dotted-line manager: a project or functional lead the person
    #: also works for. Shown on the profile and the chart; it carries no
    #: approval rights — leave, expenses and reviews still go to `manager_id`.
    dotted_line_manager_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="SET NULL", name="fk_employees_dotted_line_manager_id"),
        nullable=True,
    )

    designation: Mapped[str | None] = mapped_column(String(255), nullable=True)
    grade: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    # ── Employment ──────────────────────────────────────────────────────────
    employment_type: Mapped[EmploymentType] = mapped_column(
        Enum(EmploymentType, name="employment_type"),
        default=EmploymentType.FULL_TIME,
        nullable=False,
    )
    status: Mapped[EmploymentStatus] = mapped_column(
        Enum(EmploymentStatus, name="employment_status"),
        default=EmploymentStatus.ACTIVE,
        nullable=False,
        index=True,
    )
    date_of_joining: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_of_exit: Mapped[date | None] = mapped_column(Date, nullable=True)
    probation_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    notice_period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── Statutory identifiers (India) ───────────────────────────────────────
    # Encrypted at rest by the column type; plaintext to the application.
    pan: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    aadhaar_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    uan: Mapped[str | None] = mapped_column(String(12), nullable=True)
    pf_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    esi_number: Mapped[str | None] = mapped_column(String(20), nullable=True)

    bank_account_number: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    bank_ifsc: Mapped[str | None] = mapped_column(String(11), nullable=True)

    # ── Tenant-defined custom fields (FS-CHR-01) ────────────────────────────
    # Values keyed by the field definitions in `custom_field_definitions`.
    # JSONB rather than an EAV table: reads stay a single row fetch, and HR
    # admins add fields without an engineering change or a migration.
    custom_fields: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user: Mapped[User | None] = relationship(
        back_populates="employee",
        primaryjoin="Employee.id == User.employee_id",
        uselist=False,
        viewonly=True,
    )
    manager: Mapped[Employee | None] = relationship(
        remote_side="Employee.id", foreign_keys=[manager_id]
    )
    dotted_line_manager: Mapped[Employee | None] = relationship(
        remote_side="Employee.id", foreign_keys=[dotted_line_manager_id], viewonly=True
    )
    documents: Mapped[list[EmployeeDocument]] = relationship(
        back_populates="employee", cascade="all, delete-orphan"
    )

    @property
    def full_name(self) -> str:
        return self.display_name or " ".join(filter(None, [self.first_name, self.last_name]))

    def __repr__(self) -> str:
        return f"<Employee {self.employee_code} {self.full_name}>"


class CustomFieldDefinition(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Tenant-configurable field definitions for the employee record.

    Every change here is audit-logged, and a required field blocks an
    incomplete record from saving (FS-CHR-01).
    """

    __tablename__ = "custom_field_definitions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "entity", "key", name="uq_custom_field_tenant_entity_key"),
    )

    entity: Mapped[str] = mapped_column(String(64), default="employee", nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    # text | number | date | select | multiselect | boolean
    field_type: Mapped[str] = mapped_column(String(32), nullable=False)
    options: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    is_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Sensitive fields stay hidden from the employee's own self-service view.
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: The profile tab the field is shown on, named by the admin ("Safety",
    #: "Uniform"). Null: the "More details" card on the Employment tab.
    tab: Mapped[str | None] = mapped_column(String(40), nullable=True)


class EmployeeDocument(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Document vault entry (FRD 5.1)."""

    __tablename__ = "employee_documents"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )

    # offer_letter | id_proof | education | payslip | policy_ack | other
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    file_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # The number on the document (a passport or PAN number, a cheque number).
    document_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when the document belongs to one of the employee's dependants (a
    # child's birth certificate for insurance) rather than to the employee.
    dependant_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # When the document was issued (a passport's date of issue, a degree's
    # award date); `valid_until` is when it expires.
    issued_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_employee_visible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # ── Verification (self-uploaded documents) ─────────────────────────────
    # `not_required` for what HR put in the vault; `pending` for what the
    # employee uploaded themselves, until HR verifies or rejects it. A
    # self-uploaded degree certificate is a claim until somebody has looked.
    verification_status: Mapped[str] = mapped_column(
        String(16),
        default="not_required",
        server_default="not_required",
        nullable=False,
        index=True,
    )
    verified_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Required on a rejection: "rejected" alone tells the employee nothing to fix.
    verification_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 0 = no expiry notice sent, 1 = "expires soon" sent, 2 = "expired" sent.
    expiry_reminder_stage: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    employee: Mapped[Employee] = relationship(back_populates="documents")


class DocumentVerification(str, enum.Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class EmployeeProfile(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """The extended employee record: one row per employee, one JSONB column per section.

    Why JSONB per section rather than columns or child tables. Each section is
    read and written as a unit — an employee saves "my education", never one
    degree's end year — and every write is validated by a typed Pydantic model
    (`app.schemas.employee_profile`) before it lands, so the shape is enforced
    at the only door in. None of it is queried across employees: nobody asks
    "everyone whose second emergency contact is a sibling". Six child tables
    would buy joins nobody runs and a migration for every field a customer asks
    for.

    What stays a column elsewhere, and why: PAN, UAN, the Aadhaar last four, PF
    and ESI numbers remain on `employees`, because payroll, tax and the
    statutory returns read them there; the salary account stays in
    `employee_bank_accounts`, encrypted, because disbursement reads it there.

    `hiring_snapshot` is what the offer carried in (source, CTC at offer, fit
    score). HR only — it is never in a self-service or directory read, which is
    why it is here and not in `employees.custom_fields`.
    """

    __tablename__ = "employee_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "employee_id", name="uq_employee_profile_employee"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    personal: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    addresses: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    emergency_contacts: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    family_members: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    nominees: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    education: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    previous_employment: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )

    #: A generated storage key (never a URL a person typed). The public
    #: `employees.photo_url` points at the API route that serves it.
    photo_storage_key: Mapped[str | None] = mapped_column(String(160), nullable=True)

    hiring_snapshot: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )


class ProfileChangeStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class ProfileChangeRequest(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """An employee asking HR to change a sensitive part of their own record.

    Legal name, date of birth, statutory identifiers, the salary account,
    addresses and nominees are changed this way: each one either reaches a
    statutory return or decides where money goes. The request keeps what was
    on the record when it was raised (`current_snapshot`) so the reviewer sees
    the change, not just the new value — and so an approval refuses if the
    record has moved underneath it.

    Approval applies the change in the same transaction as the status change
    and its audit row. Rows are never deleted; a withdrawn or rejected request
    stays as history.
    """

    __tablename__ = "profile_change_requests"
    __table_args__ = (
        # One open request per section per person. Two pending PAN changes
        # would leave the reviewer choosing between them blind.
        Index(
            "uq_profile_change_one_pending",
            "tenant_id",
            "employee_id",
            "section",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        CheckConstraint(
            "status = 'PENDING' OR decided_at IS NOT NULL",
            name="ck_profile_change_decided_has_time",
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    section: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[ProfileChangeStatus] = mapped_column(
        Enum(ProfileChangeStatus, name="profile_change_status"),
        default=ProfileChangeStatus.PENDING,
        nullable=False,
        index=True,
    )
    proposed: Mapped[dict] = mapped_column(JSONB, nullable=False)
    current_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class EmployeeWorkLocation(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Where an employee worked, and from when.

    `Employee.location_id` says where someone works *now*, which is enough for
    a directory and not enough for payroll. Professional Tax and the Labour
    Welfare Fund are state levies, so a transfer from Karnataka to Maharashtra
    in the middle of a month changes which state's slab applies and which
    state's return the amount belongs in.

    Without this series, payroll reading the current location would apply the
    new state's rules to the whole period, and the earlier state's return would
    quietly be short. With it, payroll can see the change — and, per slice 09,
    flags the period for a human rather than choosing a state itself.

    Rows are inserted, never updated: a correction to a past transfer is a new
    row closing the old one.
    """

    __tablename__ = "employee_work_locations"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    location_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
