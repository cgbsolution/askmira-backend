"""Consultant payout runs: who last changed the figures (maker-checker).

Revision ID: f2c9d4b8e1a3
Revises: e4b8c2a7d9f1
Create Date: 2026-11-16
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f2c9d4b8e1a3"
down_revision = "e4b8c2a7d9f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "consultant_payout_runs",
        sa.Column("computed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_consultant_payout_runs_computed_by",
        "consultant_payout_runs",
        "users",
        ["computed_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute("UPDATE consultant_payout_runs SET computed_by_user_id = created_by_user_id")


def downgrade() -> None:
    op.drop_constraint(
        "fk_consultant_payout_runs_computed_by", "consultant_payout_runs", type_="foreignkey"
    )
    op.drop_column("consultant_payout_runs", "computed_by_user_id")
