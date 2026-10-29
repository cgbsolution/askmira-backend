"""Legal entity logo

Revision ID: f2b8d4a6c0e3
Revises: e7a3c9f1b5d2
Create Date: 2026-10-29 12:00:00.000000

- `legal_entities.logo_key`: each company of a group (a sister company, a
  client the group staffs) has its own logo. After sign-in its people see it
  at the top of the portal and on the mobile app's Home. Null keeps the
  workspace's logo, which is what everybody saw before.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f2b8d4a6c0e3"
down_revision: str | None = "e7a3c9f1b5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("legal_entities", sa.Column("logo_key", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("legal_entities", "logo_key")
