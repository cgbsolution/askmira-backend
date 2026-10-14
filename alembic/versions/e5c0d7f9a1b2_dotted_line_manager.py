"""Dotted-line manager

A second, informational manager on the employee record: a project or
functional lead. It carries no approval rights.

Revision ID: e5c0d7f9a1b2
Revises: d4b9c6e8f0a1
Create Date: 2026-10-14 09:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'e5c0d7f9a1b2'
down_revision: str | None = 'd4b9c6e8f0a1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'employees',
        sa.Column('dotted_line_manager_id', postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        'fk_employees_dotted_line_manager_id',
        'employees',
        'employees',
        ['dotted_line_manager_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('fk_employees_dotted_line_manager_id', 'employees', type_='foreignkey')
    op.drop_column('employees', 'dotted_line_manager_id')
