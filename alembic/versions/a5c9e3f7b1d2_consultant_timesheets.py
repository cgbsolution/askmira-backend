"""Consultant timesheets: the route a week took and where its decision came from.

`timesheets.approval_route` is "customer" or "internal" for a consultant's week;
`approval_proof` holds the decision's source (the customer's details, or an
internal approver's note and attachment).

Revision ID: a5c9e3f7b1d2
Revises: e4b8a2d6c1f7
Create Date: 2026-11-16
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a5c9e3f7b1d2"
down_revision = "e4b8a2d6c1f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("timesheets", sa.Column("approval_route", sa.String(length=16), nullable=True))
    op.add_column(
        "timesheets",
        sa.Column("approval_proof", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("timesheets", "approval_proof")
    op.drop_column("timesheets", "approval_route")
