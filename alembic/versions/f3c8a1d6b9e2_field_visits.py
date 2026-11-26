"""Field visits: visit in / visit out with a location and a selfie.

Sales and marketing people record each client visit: who or where, when they
arrived and left, the location at both ends (always required, as accurate as
the workspace's `location_accuracy_metres`) and a selfie at arrival. A visit
is not an attendance punch and never changes a day's status.

One open visit per person, by a partial unique index.

Revision ID: f3c8a1d6b9e2
Revises: e7b3d9a1c5f2
Create Date: 2026-11-26
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f3c8a1d6b9e2"
down_revision = "e7b3d9a1c5f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "field_visits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "employee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_name", sa.String(160), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("start_latitude", sa.Numeric(10, 7), nullable=False),
        sa.Column("start_longitude", sa.Numeric(10, 7), nullable=False),
        sa.Column("start_accuracy_metres", sa.Integer(), nullable=True),
        sa.Column("start_source", sa.String(16), nullable=False),
        sa.Column("start_photo_key", sa.String(512), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_latitude", sa.Numeric(10, 7), nullable=True),
        sa.Column("end_longitude", sa.Numeric(10, 7), nullable=True),
        sa.Column("end_accuracy_metres", sa.Integer(), nullable=True),
        sa.Column("end_source", sa.String(16), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_field_visits_tenant_id", "field_visits", ["tenant_id"])
    op.create_index(
        "ix_field_visits_tenant_employee_started",
        "field_visits",
        ["tenant_id", "employee_id", "started_at"],
    )
    op.create_index(
        "uq_field_visits_one_open",
        "field_visits",
        ["employee_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_field_visits_one_open", table_name="field_visits")
    op.drop_index("ix_field_visits_tenant_employee_started", table_name="field_visits")
    op.drop_index("ix_field_visits_tenant_id", table_name="field_visits")
    op.drop_table("field_visits")
