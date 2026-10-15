"""shift site and line

A shift may belong to a plant or site and to a line or area within it.

Revision ID: 35567c879698
Revises: 8f1a4ee1b5ed
Create Date: 2026-10-15 10:43:38.058808
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '35567c879698'
down_revision: str | None = '8f1a4ee1b5ed'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('shifts', sa.Column('location_id', sa.UUID(), nullable=True))
    op.add_column('shifts', sa.Column('work_area', sa.String(length=80), nullable=True))
    op.create_foreign_key('fk_shifts_location_id', 'shifts', 'locations', ['location_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_shifts_location_id', 'shifts', type_='foreignkey')
    op.drop_column('shifts', 'work_area')
    op.drop_column('shifts', 'location_id')
