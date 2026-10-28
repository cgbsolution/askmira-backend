"""Organisation structure: legal entity, location, department (FRD 5.1)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.tenant import Tenant


class LegalEntity(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A registered company under the tenant.

    Group companies file separately, so PAN/TAN/PF/ESI registration numbers
    live here rather than on the tenant — payroll and statutory filings are
    per legal entity, not per customer.
    """

    __tablename__ = "legal_entities"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_legal_entities_tenant_code"),
        CheckConstraint(
            "salary_days_basis IN ('calendar', 'fixed', 'working_days')",
            name="ck_legal_entities_salary_days_basis",
        ),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)

    pan: Mapped[str | None] = mapped_column(String(10), nullable=True)
    tan: Mapped[str | None] = mapped_column(String(10), nullable=True)
    gstin: Mapped[str | None] = mapped_column(String(15), nullable=True)
    pf_registration_no: Mapped[str | None] = mapped_column(String(32), nullable=True)
    esi_registration_no: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Professional Tax is registered with each state separately, so one column
    # cannot hold it: an entity operating in Karnataka and Maharashtra has two
    # registrations and files two challans. Keyed by ISO 3166-2:IN state code.
    pt_registrations: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    registered_address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    #: The work week (a shift and its weekly offs) for this company's people
    #: who have no shift or roster of their own. Before the workspace default.
    #: One company in a group works six days, another five, another alternate
    #: Saturdays — this is where that is said once.
    default_shift_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "shifts.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_legal_entities_default_shift_id",
        ),
        nullable=True,
    )

    #: How a month's salary is divided into days, for loss of pay and part
    #: months. `calendar`: the days in the month (28-31), weekly offs and
    #: holidays paid. `fixed`: `salary_fixed_days` every month (26 or 30).
    #: `working_days`: the days the person was scheduled to work that month —
    #: the month less their weekly offs, so it follows a five-day, six-day or
    #: alternate-Saturday week. Holidays count as working days (they are paid).
    salary_days_basis: Mapped[str] = mapped_column(
        String(16), default="calendar", server_default=text("'calendar'"), nullable=False
    )
    salary_fixed_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="legal_entities")
    locations: Mapped[list[Location]] = relationship(
        back_populates="legal_entity", cascade="all, delete-orphan"
    )


class Location(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A work location.

    `state_code` is load-bearing, not descriptive: Professional Tax slabs,
    holiday calendars and several labour-law variants are all state-specific
    (FRD 5.2). Payroll reads it on every run.
    """

    __tablename__ = "locations"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_locations_tenant_code"),)

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("legal_entities.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)

    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # ISO 3166-2:IN subdivision code, e.g. "KA", "MH", "TN".
    state_code: Mapped[str] = mapped_column(String(4), nullable=False, index=True)
    country_code: Mapped[str] = mapped_column(String(2), default="IN", nullable=False)
    pincode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata", nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    legal_entity: Mapped[LegalEntity] = relationship(back_populates="locations")


class Department(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A department. Self-referencing, so the tree can be any depth."""

    __tablename__ = "departments"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_departments_tenant_code"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL"), nullable=True
    )
    # `departments.head_employee_id -> employees.id` closes a cycle with
    # `employees.department_id -> departments.id`. `use_alter` defers this
    # constraint to a named ALTER after both tables exist, which is what lets
    # CREATE, DROP and Alembic order the two tables at all.
    head_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "employees.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_departments_head_employee_id",
        ),
        nullable=True,
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    parent: Mapped[Department | None] = relationship(remote_side="Department.id")
