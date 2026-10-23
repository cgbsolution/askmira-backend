"""Salary components: a wage group for the Salary master

Revision ID: f3c7e1a9b5d2
Revises: e5a1c9d3f7b2
Create Date: 2026-10-23 18:00:00.000000

Regular earning, variable earning, reimbursement, non-statutory deduction,
company contribution or provision: where a wage type is listed and totalled on
the Salary master. Null keeps the kind's usual group, so every existing
component reads exactly as before.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3c7e1a9b5d2"
down_revision: str | None = "e5a1c9d3f7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("salary_components", sa.Column("wage_group", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("salary_components", "wage_group")
