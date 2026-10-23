"""Salary master: typed component amounts, remarks and a file per revision

Revision ID: e5a1c9d3f7b2
Revises: d017d0fe5b8e
Create Date: 2026-10-23 12:00:00.000000

HR can now type an amount for any wage type on one employee's salary, note why,
and attach the letter or approval behind a revision. Existing rows are all
structure-calculated, so `is_manual` starts false for every one of them.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e5a1c9d3f7b2"
down_revision: str | None = "d017d0fe5b8e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "employee_salary_components",
        sa.Column("is_manual", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "employee_salary_components", sa.Column("remarks", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "employee_salary_components",
        sa.Column(
            "pay_basis", sa.String(length=8), server_default=sa.text("'monthly'"), nullable=False
        ),
    )
    op.add_column(
        "employee_salaries", sa.Column("attachment_key", sa.String(length=1024), nullable=True)
    )
    op.add_column(
        "employee_salaries", sa.Column("attachment_name", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "employee_salaries", sa.Column("attachment_mime", sa.String(length=128), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("employee_salaries", "attachment_mime")
    op.drop_column("employee_salaries", "attachment_name")
    op.drop_column("employee_salaries", "attachment_key")
    op.drop_column("employee_salary_components", "pay_basis")
    op.drop_column("employee_salary_components", "remarks")
    op.drop_column("employee_salary_components", "is_manual")
