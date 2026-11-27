"""merge consultant payouts and main heads

Revision ID: 679703c0db59
Revises: 4d2ce8019eda, f7b2d4e8a1c3
Create Date: 2026-11-27 12:28:55.378504
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '679703c0db59'
down_revision: str | Sequence[str] | None = ('4d2ce8019eda', 'f7b2d4e8a1c3')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
