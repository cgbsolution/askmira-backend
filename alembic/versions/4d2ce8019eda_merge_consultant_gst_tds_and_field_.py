"""merge consultant gst tds and field visits heads

Revision ID: 4d2ce8019eda
Revises: d3a7f1c9e5b2, f3c8a1d6b9e2
Create Date: 2026-11-27 12:27:09.998482
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '4d2ce8019eda'
down_revision: str | Sequence[str] | None = ('d3a7f1c9e5b2', 'f3c8a1d6b9e2')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
