"""approval chains for corrections timesheets and lifecycle

Attendance corrections, timesheets and lifecycle events carry an approval chain
resolved from the tenant's approval rules, which gain those three processes.
Enum values cannot be dropped in Postgres, so a downgrade removes the rules that
use them and leaves the values in place.

Revision ID: c5f1dff9e87f
Revises: b9c84b7773f8
Create Date: 2026-10-15 01:48:26.475804
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'c5f1dff9e87f'
down_revision: str | None = 'b9c84b7773f8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADD VALUE cannot run inside the migration's transaction block.
    with op.get_context().autocommit_block():
        # SQLAlchemy stores this enum by member name, so the labels are upper case.
        for value in ("REGULARISATION", "TIMESHEET", "LIFECYCLE"):
            op.execute(f"ALTER TYPE approval_process ADD VALUE IF NOT EXISTS '{value}'")
    op.add_column('attendance_regularisations', sa.Column('approval_chain', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False))
    op.add_column('lifecycle_events', sa.Column('approval_chain', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False))
    op.add_column('timesheets', sa.Column('approval_chain', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False))


def downgrade() -> None:
    op.execute(
        "DELETE FROM approval_rules WHERE process::text IN "
        "('REGULARISATION', 'TIMESHEET', 'LIFECYCLE')"
    )
    op.drop_column('timesheets', 'approval_chain')
    op.drop_column('lifecycle_events', 'approval_chain')
    op.drop_column('attendance_regularisations', 'approval_chain')
