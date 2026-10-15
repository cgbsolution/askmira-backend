"""survey comment ai choice

Each survey response records whether its writer let AI read the comment.

Revision ID: 8f1a4ee1b5ed
Revises: 5146b89873ae
Create Date: 2026-10-15 10:32:42.486102
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '8f1a4ee1b5ed'
down_revision: str | None = '5146b89873ae'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('survey_responses', sa.Column('comment_ai_ok', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    op.drop_column('survey_responses', 'comment_ai_ok')
