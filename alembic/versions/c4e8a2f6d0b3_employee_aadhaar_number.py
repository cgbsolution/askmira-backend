"""Employee: full Aadhaar number, encrypted

Revision ID: c4e8a2f6d0b3
Revises: b7d3f9a1c5e2
Create Date: 2026-10-27 16:00:00.000000

Admin and HR now see the full Aadhaar number (EPF KYC and UAN seeding need
it). The column holds ciphertext (EncryptedText). Nothing is backfilled:
records saved earlier kept only the last four, so the full number appears
once HR enters it again.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4e8a2f6d0b3"
down_revision: str | None = "b7d3f9a1c5e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("aadhaar_number", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("employees", "aadhaar_number")
