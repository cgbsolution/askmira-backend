"""Encrypt employee identifiers

PAN and bank account number on the employee row were plaintext. They are now
encrypted at rest by their column type (`EncryptedText`); this widens the
columns for ciphertext and encrypts every value already stored. The downgrade
decrypts them back into the old widths.

Revision ID: e8b4c9d1f2a5
Revises: d7a3b8c0e1f4
Create Date: 2026-10-13 22:05:12.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.crypto import decrypt_field, encrypt_field

revision: str = 'e8b4c9d1f2a5'
down_revision: str | None = 'd7a3b8c0e1f4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (("pan", 10), ("bank_account_number", 34))


def _rewrite(column: str, transform) -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(f"SELECT id, {column} FROM employees WHERE {column} IS NOT NULL AND {column} <> ''")
    ).all()
    for row_id, value in rows:
        bind.execute(
            sa.text(f"UPDATE employees SET {column} = :value WHERE id = :id"),
            {"value": transform(value), "id": row_id},
        )


def upgrade() -> None:
    for column, _width in COLUMNS:
        op.alter_column("employees", column, type_=sa.Text(), existing_nullable=True)
        _rewrite(column, encrypt_field)


def downgrade() -> None:
    for column, width in COLUMNS:
        _rewrite(column, decrypt_field)
        op.alter_column(
            "employees", column, type_=sa.String(length=width), existing_nullable=True
        )
