"""weekly overtime

Overtime past a weekly threshold (the Factories Act's 48 hours) becomes its own
entry kind, dated the week's last day. A downgrade removes weekly entries,
because the old unique key allows one entry per employee-day.

Revision ID: 39f210cece2a
Revises: ce2d8461c459
Create Date: 2026-10-15 01:31:43.452269
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '39f210cece2a'
down_revision: str | None = 'ce2d8461c459'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('overtime_entries', sa.Column('kind', sa.String(length=8), server_default='daily', nullable=False))
    op.drop_constraint('uq_overtime_entry_day', 'overtime_entries', type_='unique')
    op.create_unique_constraint('uq_overtime_entry_day', 'overtime_entries', ['tenant_id', 'employee_id', 'attendance_date', 'kind'])
    op.add_column('overtime_policies', sa.Column('weekly_threshold_minutes', sa.Integer(), nullable=True))
    op.add_column('overtime_policies', sa.Column('week_starts_on', sa.Integer(), server_default='6', nullable=False))


def downgrade() -> None:
    op.drop_column('overtime_policies', 'week_starts_on')
    op.drop_column('overtime_policies', 'weekly_threshold_minutes')
    op.execute("DELETE FROM overtime_entries WHERE kind = 'weekly'")
    op.drop_constraint('uq_overtime_entry_day', 'overtime_entries', type_='unique')
    op.create_unique_constraint('uq_overtime_entry_day', 'overtime_entries', ['tenant_id', 'employee_id', 'attendance_date'])
    op.drop_column('overtime_entries', 'kind')
