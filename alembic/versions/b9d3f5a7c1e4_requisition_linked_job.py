"""Requisitions remember the job they opened in the linked interview tool.

Revision ID: b9d3f5a7c1e4
Revises: a1c5e9b3d7f2
Create Date: 2026-11-05
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b9d3f5a7c1e4"
down_revision = "a1c5e9b3d7f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("requisitions", sa.Column("linked_job_ref", sa.String(64), nullable=True))
    op.add_column(
        "requisitions", sa.Column("linked_job_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("requisitions", sa.Column("linked_job_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("requisitions", "linked_job_error")
    op.drop_column("requisitions", "linked_job_at")
    op.drop_column("requisitions", "linked_job_ref")
