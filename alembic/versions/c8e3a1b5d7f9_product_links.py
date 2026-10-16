"""product links: a workspace's link to the interview and expense products

Revision ID: c8e3a1b5d7f9
Revises: b7d2f0a4c3e5
Create Date: 2026-10-16 18:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c8e3a1b5d7f9"
down_revision: str | None = "b7d2f0a4c3e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_links",
        sa.Column("product", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("external_ref", sa.String(length=120), nullable=True),
        sa.Column("linked_by_user_id", sa.UUID(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["linked_by_user_id"],
            ["users.id"],
            name="fk_product_links_linked_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "product", name="uq_product_links_tenant_product"),
    )
    op.create_index(
        op.f("ix_product_links_tenant_id"), "product_links", ["tenant_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_product_links_tenant_id"), table_name="product_links")
    op.drop_table("product_links")
