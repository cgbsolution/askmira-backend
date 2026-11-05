"""Punch addresses, and half days from the late-mark rule.

Revision ID: d8f2b6c4a1e9
Revises: e5a9c3d7f1b8
Create Date: 2026-11-05

One row per place (coordinates rounded to four decimals, about 11 metres) per
workspace, looked up once and shared by every punch made there. Punches
themselves are untouched: they stay insert-only.

`attendance_days.late_mark_half_day` marks a present day the workspace's
late-mark rule turned into a half day, so the rule can undo it.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d8f2b6c4a1e9"
down_revision = "e5a9c3d7f1b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "punch_addresses",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("lat_key", sa.Numeric(8, 4), nullable=False),
        sa.Column("lng_key", sa.Numeric(8, 4), nullable=False),
        sa.Column("address", sa.String(512), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "lat_key", "lng_key", name="uq_punch_address_place"),
    )
    op.create_index("ix_punch_addresses_tenant_id", "punch_addresses", ["tenant_id"])
    op.add_column(
        "attendance_days",
        sa.Column("late_mark_half_day", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("attendance_days", "late_mark_half_day")
    op.drop_index("ix_punch_addresses_tenant_id", table_name="punch_addresses")
    op.drop_table("punch_addresses")
