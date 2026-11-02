"""Letters printed on the workspace's letterhead

Revision ID: e5b9d2f7a1c3
Revises: d3a7f9c1e5b8
Create Date: 2026-11-02 12:00:00.000000

- `letter_templates.use_letterhead`: whether letters from this template are
  printed on the letterhead (Letters -> Letterhead). On for every template.
- `issued_letters.letterhead`: the letterhead a letter was printed on, kept so
  it re-renders the same after the colours or contact lines change. Existing
  letters have none and keep re-rendering on the plain header they used.

The letterhead settings themselves live in `tenants.settings["letterhead"]`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e5b9d2f7a1c3"
down_revision: str | None = "d3a7f9c1e5b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "letter_templates",
        sa.Column("use_letterhead", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column(
        "issued_letters",
        sa.Column("letterhead", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("issued_letters", "letterhead")
    op.drop_column("letter_templates", "use_letterhead")
