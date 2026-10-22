"""Tenants: deleted by the platform

Revision ID: d017d0fe5b8e
Revises: c4a8e2f6b1d9
Create Date: 2026-10-22 22:00:00.000000

The super admin can delete a workspace. It is closed for good — every login
and credential ended, its people's emails and its address freed — but the row
stays, because its audit trail is insert-only and its payroll and statutory
records must be kept.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d017d0fe5b8e"
down_revision: str | None = "c4a8e2f6b1d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_tenants_deleted_at"), "tenants", ["deleted_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_tenants_deleted_at"), table_name="tenants")
    op.drop_column("tenants", "deleted_at")
