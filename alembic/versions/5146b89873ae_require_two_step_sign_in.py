"""require two-step sign-in

A workspace may require two-step sign-in of its admins, or of everyone.

Revision ID: 5146b89873ae
Revises: f24b5924784e
Create Date: 2026-10-15 02:08:15.236765
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '5146b89873ae'
down_revision: str | None = 'f24b5924784e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('tenant_session_policies', sa.Column('require_two_step', sa.String(length=16), server_default='off', nullable=False))


def downgrade() -> None:
    op.drop_column('tenant_session_policies', 'require_two_step')
