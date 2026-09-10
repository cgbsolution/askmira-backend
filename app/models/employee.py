"""Employee master record and document vault (FRD 5.1, FS-CHR-01/02)."""

from __future__ import annotations

import enum
import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TenantMixin, TimestampMixin, UUIDMixin

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
    pan: Mapped[str | None] = mapped_column(String(10), nullable=True)
    aadhaar_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    uan: Mapped[str | None] = mapped_column(String(12), nullable=True)
    pf_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    esi_number: Mapped[str | None] = mapped_column(String(20), nullable=True)

    bank_account_number: Mapped[str | None] = mapped_column(String(34), nullable=True)
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

    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_employee_visible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    employee: Mapped[Employee] = relationship(back_populates="documents")


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
