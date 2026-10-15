"""custom profile tabs

A custom field or table may sit on a profile tab the admin names.

Revision ID: f24b5924784e
Revises: c5f1dff9e87f
Create Date: 2026-10-15 02:04:28.012202
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = 'f24b5924784e'
down_revision: str | None = 'c5f1dff9e87f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('custom_field_definitions', sa.Column('tab', sa.String(length=40), nullable=True))
    op.add_column('custom_table_definitions', sa.Column('tab', sa.String(length=40), nullable=True))


def downgrade() -> None:
    op.drop_column('custom_table_definitions', 'tab')
    op.drop_column('custom_field_definitions', 'tab')
