"""Showcase links: the exhibition QR code and each visitor's story.

Revision ID: b2c6e8a4d0f7
Revises: a7d3f1c9e5b2
Create Date: 2026-11-13
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b2c6e8a4d0f7"
down_revision = "a7d3f1c9e5b2"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "showcase_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("code_encrypted", sa.Text(), nullable=False),
        sa.Column(
            "brand_tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="SET NULL", name="fk_showcase_links_brand_tenant"),
            nullable=True,
        ),
        sa.Column("client_name", sa.String(80), nullable=False),
        sa.Column("role_title", sa.String(60), nullable=False),
        sa.Column("headcount", sa.Integer(), nullable=False),
        sa.Column("months", sa.Integer(), nullable=False),
        sa.Column("order_value", sa.Numeric(14, 2), nullable=False),
        sa.Column("team_budget", sa.Numeric(14, 2), nullable=False),
        sa.Column("laptop_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("payment_days", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("visits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_visit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_showcase_links_created_by"),
            nullable=True,
        ),
        *_timestamps(),
        sa.UniqueConstraint("code_hash", name="uq_showcase_links_code_hash"),
    )
    op.create_table(
        "showcase_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "link_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("showcase_links.id", ondelete="CASCADE", name="fk_showcase_runs_link"),
            nullable=False,
        ),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("turns", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
    )
    op.create_index("ix_showcase_runs_link_updated", "showcase_runs", ["link_id", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_showcase_runs_link_updated", table_name="showcase_runs")
    op.drop_table("showcase_runs")
    op.drop_table("showcase_links")
