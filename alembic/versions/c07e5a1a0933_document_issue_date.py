"""Document vault: issue date

`employee_documents.issued_on`, beside the existing `valid_until`, so HR's
document entry records both when a document was issued and when it expires.

Revision ID: c07e5a1a0933
Revises: c07e5a1a0932
Create Date: 2026-09-30 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c07e5a1a0933"
down_revision: str | None = "c07e5a1a0932"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("employee_documents", sa.Column("issued_on", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("employee_documents", "issued_on")
