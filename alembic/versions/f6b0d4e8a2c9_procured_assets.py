"""Equipment delivered through ProcureFlow, added to an asset register from AskMira.

Revision ID: f6b0d4e8a2c9
Revises: e5a9c3d7f1b8
Create Date: 2026-11-05
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f6b0d4e8a2c9"
down_revision = "e5a9c3d7f1b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "procured_assets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("source_ref", sa.String(160), nullable=False),
        sa.Column("register", sa.String(16), nullable=False),
        sa.Column("asset_ref", sa.String(64), nullable=False),
        sa.Column("asset_code", sa.String(64), nullable=False),
        sa.Column("added_by_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["added_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "source_ref", name="uq_procured_asset_source"),
    )
    op.create_index("ix_procured_assets_tenant_id", "procured_assets", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_procured_assets_tenant_id", table_name="procured_assets")
    op.drop_table("procured_assets")
