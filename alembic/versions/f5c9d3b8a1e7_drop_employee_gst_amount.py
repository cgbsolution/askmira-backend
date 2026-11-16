"""Drop employees.gst_amount: consultant GST is worked out per payout line.

The old fixed monthly GST typed on a consultant's record is no longer used.
(payout lines and contractor invoices keep their own gst_amount.)

Revision ID: f5c9d3b8a1e7
Revises: b3d7f1a9c2e5
Create Date: 2026-11-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f5c9d3b8a1e7"
down_revision = "b3d7f1a9c2e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("employees", "gst_amount")


def downgrade() -> None:
    op.add_column("employees", sa.Column("gst_amount", sa.Numeric(14, 2), nullable=True))
