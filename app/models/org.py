"""Organisation structure: legal entity, location, department (FRD 5.1)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
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
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_legal_entities_tenant_code"),)

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
