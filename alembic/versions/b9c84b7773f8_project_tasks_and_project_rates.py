"""project tasks and project rates

Tasks within projects for timesheet entries, and hourly rates that apply to one
project. A downgrade removes project rates: the old key allows one rate a day.

Revision ID: b9c84b7773f8
Revises: 39f210cece2a
Create Date: 2026-10-15 01:37:44.216875
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = 'b9c84b7773f8'
down_revision: str | None = '39f210cece2a'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('project_tasks',
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_project_tasks_project', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'project_id', 'name', name='uq_project_tasks_name')
    )
    op.create_index(op.f('ix_project_tasks_project_id'), 'project_tasks', ['project_id'], unique=False)
    op.create_index(op.f('ix_project_tasks_tenant_id'), 'project_tasks', ['tenant_id'], unique=False)
    op.add_column('hourly_rates', sa.Column('project_id', sa.UUID(), nullable=True))
    op.drop_constraint('uq_hourly_rate_from', 'hourly_rates', type_='unique')
    op.create_index('uq_hourly_rate_from', 'hourly_rates', ['tenant_id', 'employee_id', 'effective_from'], unique=True, postgresql_where=sa.text('project_id IS NULL'))
    op.create_index('uq_hourly_rate_project_from', 'hourly_rates', ['tenant_id', 'employee_id', 'project_id', 'effective_from'], unique=True, postgresql_where=sa.text('project_id IS NOT NULL'))
    op.create_foreign_key('fk_hourly_rates_project', 'hourly_rates', 'projects', ['project_id'], ['id'], ondelete='RESTRICT')
    op.add_column('timesheet_entries', sa.Column('task_id', sa.UUID(), nullable=True))
    op.create_foreign_key('fk_timesheet_entries_task', 'timesheet_entries', 'project_tasks', ['task_id'], ['id'], ondelete='RESTRICT')


def downgrade() -> None:
    op.drop_constraint('fk_timesheet_entries_task', 'timesheet_entries', type_='foreignkey')
    op.drop_column('timesheet_entries', 'task_id')
    op.execute("DELETE FROM hourly_rates WHERE project_id IS NOT NULL")
    op.drop_constraint('fk_hourly_rates_project', 'hourly_rates', type_='foreignkey')
    op.drop_index('uq_hourly_rate_project_from', table_name='hourly_rates', postgresql_where=sa.text('project_id IS NOT NULL'))
    op.drop_index('uq_hourly_rate_from', table_name='hourly_rates', postgresql_where=sa.text('project_id IS NULL'))
    op.create_unique_constraint('uq_hourly_rate_from', 'hourly_rates', ['tenant_id', 'employee_id', 'effective_from'])
    op.drop_column('hourly_rates', 'project_id')
    op.drop_index(op.f('ix_project_tasks_tenant_id'), table_name='project_tasks')
    op.drop_index(op.f('ix_project_tasks_project_id'), table_name='project_tasks')
    op.drop_table('project_tasks')
