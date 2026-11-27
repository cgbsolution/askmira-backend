"""merge consultant payouts and day-off request types heads

Revision ID: 9c1f5e3a7d20
Revises: 679703c0db59, b7e2d4f9a1c6
Create Date: 2026-11-27 18:00:00.000000
"""

from collections.abc import Sequence


revision: str = '9c1f5e3a7d20'
down_revision: str | Sequence[str] | None = ('679703c0db59', 'b7e2d4f9a1c6')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
