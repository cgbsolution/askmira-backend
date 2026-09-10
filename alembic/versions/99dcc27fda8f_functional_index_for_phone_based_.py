"""functional index for phone-based candidate dedup

Revision ID: 99dcc27fda8f
Revises: dc4dd528e013
Create Date: 2026-09-07 01:18:36.505160
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '99dcc27fda8f'
down_revision: str | None = 'dc4dd528e013'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Match the dedup query, so it does not seq-scan every candidate.

    Ingestion narrows candidates by the last ten digits of their phone, applying
    the same normalisation the matching rule does. Without an index on that
    expression the narrowing query reads the whole candidate table on every
    application — and ingestion runs on a poll, so a tenant with forty thousand
    candidates pays for it every few minutes.

    Both functions are IMMUTABLE, which is what makes the expression indexable.
    """
    op.execute(
        r"""
        CREATE INDEX ix_candidates_phone_digits
        ON candidates (tenant_id, right(regexp_replace(phone, '\D', '', 'g'), 10))
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_candidates_phone_digits")
