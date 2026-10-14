"""Custom tables on the employee record: repeating rows an admin designs.

A custom field holds one value per person; a custom table holds a list — the
certifications someone holds, the languages they speak, the vehicles they
are allowed to drive. An admin defines the table and its columns (each typed
like a custom field); HR adds rows for a person, and the person adds their own
when the table allows it. A sensitive table is never shown to the person it
is about. Every change is audited.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class CustomTableDefinition(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "custom_table_definitions"
    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_custom_table_key"),)

    key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    #: [{"key", "label", "field_type", "options", "is_required"}], in display order.
    columns: Mapped[list] = mapped_column(JSONB, nullable=False)
    #: Hidden from the person the record is about.
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: The person may add and change their own rows.
    employee_editable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class CustomTableRow(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "custom_table_rows"

    table_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "custom_table_definitions.id", ondelete="CASCADE", name="fk_custom_table_rows_table"
        ),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE", name="fk_custom_table_rows_employee"),
        nullable=False,
        index=True,
    )
    values: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
