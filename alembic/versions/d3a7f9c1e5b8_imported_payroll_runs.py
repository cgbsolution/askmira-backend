"""Imported payroll runs

Revision ID: d3a7f9c1e5b8
Revises: c6e2a8f4b0d7
Create Date: 2026-10-30 18:00:00.000000

Months paid on the previous payroll system (factoHR) are loaded as locked runs
whose figures are kept exactly as they were paid, never recomputed:

- `payroll_runs.origin`: "computed" (AskMira worked it out) or "imported".
  Every existing run is computed.
- `payroll_runs.import_details`: the source system, files and register totals.
- `payslips.import_details`: the file, sheet, row(s) and the register's own
  cells for each payslip (no identifiers or bank details).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d3a7f9c1e5b8"
down_revision: str | None = "c6e2a8f4b0d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payroll_runs",
        sa.Column("origin", sa.String(length=16), server_default="computed", nullable=False),
    )
    op.create_check_constraint(
        "ck_payroll_runs_origin", "payroll_runs", "origin IN ('computed', 'imported')"
    )
    op.add_column(
        "payroll_runs",
        sa.Column("import_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "payslips",
        sa.Column("import_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("payslips", "import_details")
    op.drop_column("payroll_runs", "import_details")
    op.drop_constraint("ck_payroll_runs_origin", "payroll_runs", type_="check")
    op.drop_column("payroll_runs", "origin")
