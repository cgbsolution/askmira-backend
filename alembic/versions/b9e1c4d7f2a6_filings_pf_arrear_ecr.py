"""Filings: the PF arrear ECR, a return of its own

Revision ID: b9e1c4d7f2a6
Revises: a7d4f2c8e1b3
Create Date: 2026-10-22 18:00:00.000000

PF arrears paid with a month's salary are remitted on the EPFO's separate
arrear ECR, not on the month's regular one. It is recorded like every other
return, under a return type of its own.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b9e1c4d7f2a6"
down_revision: str | None = "a7d4f2c8e1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Autogenerate does not see an added enum value. ALTER TYPE ... ADD VALUE
    # cannot be used in the transaction that added it, so it runs outside the
    # migration's block.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE return_type ADD VALUE IF NOT EXISTS 'PF_ECR_ARREAR'")


def downgrade() -> None:
    # Postgres cannot drop one value from an enum, and unused it is harmless.
    # A filed return is a statutory record, so a downgrade with arrear ECRs on
    # file refuses rather than deleting them.
    count = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM filing_records WHERE return_type = 'PF_ECR_ARREAR'"))
        .scalar()
    )
    if count:
        raise RuntimeError(
            f"{count} PF arrear ECR record(s) exist. The code before this revision cannot "
            "read them; keep this revision, or move them out deliberately first."
        )
