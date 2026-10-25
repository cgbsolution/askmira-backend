"""Salary master: MONTHLY GROSS as typed

Revision ID: a6c2e8f4b1d3
Revises: f3c7e1a9b5d2
Create Date: 2026-10-24 10:00:00.000000

HR types MONTHLY GROSS on the Salary master like every other amount, in any Pay
Basis. Payroll still pays the wage types; this keeps the figure HR entered.
Null on every existing revision: their gross is the earnings' sum, as before.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a6c2e8f4b1d3"
down_revision: str | None = "f3c7e1a9b5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("employee_salaries", sa.Column("gross_amount", sa.Numeric(14, 2), nullable=True))
    op.add_column("employee_salaries", sa.Column("gross_basis", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("employee_salaries", "gross_basis")
    op.drop_column("employee_salaries", "gross_amount")
