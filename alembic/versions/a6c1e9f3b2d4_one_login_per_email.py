"""one login per email across the platform

Revision ID: a6c1e9f3b2d4
Revises: 5765c32d4a40
Create Date: 2026-10-15

Sign-in finds a person by email alone, so the same address in two workspaces
made it pick one arbitrarily. Workspaces are now created only by the platform
super admin, and every place a login is made checks the whole platform; this
index makes that hold even under a race.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a6c1e9f3b2d4"
down_revision: str | None = "5765c32d4a40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_users_email_lower", "users", [sa.text("lower(email)")], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_users_email_lower", table_name="users")
