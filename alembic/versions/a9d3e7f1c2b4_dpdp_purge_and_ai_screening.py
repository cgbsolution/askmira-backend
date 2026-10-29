"""DPDP: workspace purge dates and the candidate AI-screening consent

Revision ID: a9d3e7f1c2b4
Revises: f2b8d4a6c0e3
Create Date: 2026-10-29 18:00:00.000000

- `tenants.purge_after`, `tenants.purged_at`: a deleted workspace is kept for a
  grace period so its data can be handed back, then its rows and files are
  purged (DPDP s.8(7)(b)). Workspaces already deleted get the same 90 days,
  counted from this migration.
- `consent_purpose` gains AI_SCREENING: a candidate's optional agreement to an
  AI model reading their résumé.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a9d3e7f1c2b4"
down_revision: str | None = "f2b8d4a6c0e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("purge_after", sa.Date(), nullable=True))
    op.add_column("tenants", sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        "UPDATE tenants SET purge_after = CURRENT_DATE + 90 "
        "WHERE deleted_at IS NOT NULL AND purge_after IS NULL"
    )
    # ALTER TYPE ... ADD VALUE cannot share a transaction with a statement that
    # uses the value, so it runs on its own.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE consent_purpose ADD VALUE IF NOT EXISTS 'AI_SCREENING'")


def downgrade() -> None:
    # Postgres cannot drop an enum value; AI_SCREENING stays, unused.
    op.drop_column("tenants", "purged_at")
    op.drop_column("tenants", "purge_after")
