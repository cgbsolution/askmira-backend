"""hiring: candidate stage events and the manual application source

Revision ID: d3f1a9c47e21
Revises: b62b0a40ce25
Create Date: 2026-09-16 10:00:00.000000

Two things the web portal's hiring screens need that the schema did not have:

- `candidate_stage_events`, the insert-only history behind a stage move. A
  rejection needs a reason somebody can read back, and stage timings need dates.
- `MANUAL` on `job_board`, so a candidate HR enters by hand is not credited to a
  board that never sent them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d3f1a9c47e21"
down_revision: str | None = "b62b0a40ce25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Existing type, created by the initial schema. Referenced, never re-created.
_CANDIDATE_STAGE = postgresql.ENUM(name="candidate_stage", create_type=False)


def upgrade() -> None:
    # PostgreSQL 12+ allows this inside the migration transaction; the new value
    # is not used until a later transaction, so nothing here trips over it.
    op.execute("ALTER TYPE job_board ADD VALUE IF NOT EXISTS 'MANUAL'")

    op.create_table(
        "candidate_stage_events",
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("requisition_id", sa.UUID(), nullable=False),
        sa.Column("from_stage", _CANDIDATE_STAGE, nullable=True),
        sa.Column("to_stage", _CANDIDATE_STAGE, nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requisition_id"], ["requisitions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_candidate_stage_events_candidate",
        "candidate_stage_events",
        ["tenant_id", "candidate_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_candidate_stage_events_tenant_id"),
        "candidate_stage_events",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_candidate_stage_events_to_stage"),
        "candidate_stage_events",
        ["to_stage"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_candidate_stage_events_to_stage"), table_name="candidate_stage_events"
    )
    op.drop_index(
        op.f("ix_candidate_stage_events_tenant_id"), table_name="candidate_stage_events"
    )
    op.drop_index("ix_candidate_stage_events_candidate", table_name="candidate_stage_events")
    op.drop_table("candidate_stage_events")
    # PostgreSQL cannot remove a value from an enum type. 'MANUAL' stays on
    # job_board after a downgrade; nothing older writes it, so it is inert.
