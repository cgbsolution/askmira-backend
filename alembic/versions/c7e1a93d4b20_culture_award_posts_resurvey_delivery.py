"""culture: award posts on the feed, re-survey scheduling, announcement delivery

Revision ID: c7e1a93d4b20
Revises: b62b0a40ce25
Create Date: 2026-09-16 10:00:00.000000

Three columns the slice-32 gaps could not be closed without:

- `shoutouts.award_result_id`, with `from_employee_id` made nullable and a check
  that one of the two is set. An award announced on the feed is the company's
  post; attributing it to whichever HR person declared the winner would be a
  post in somebody's name that they did not write.
- `surveys.resurvey_interval_days` and `surveys.previous_wave_id`, so the next
  wave is scheduled from configuration and movement is measured between waves.
- `announcements.delivered_at` and `delivered_count`, so a scheduled
  announcement is sent exactly once when its publish time arrives.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c7e1a93d4b20"
down_revision: str | None = "b62b0a40ce25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── shoutouts: award announcements ──────────────────────────────────────
    op.alter_column(
        "shoutouts",
        "from_employee_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.add_column(
        "shoutouts",
        sa.Column("award_result_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "shoutouts_award_result_id_fkey",
        "shoutouts",
        "award_results",
        ["award_result_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "shoutouts_award_result_id_key", "shoutouts", ["award_result_id"]
    )
    op.create_check_constraint(
        "ck_shoutout_has_author",
        "shoutouts",
        "from_employee_id IS NOT NULL OR award_result_id IS NOT NULL",
    )

    # ── surveys: re-survey waves ────────────────────────────────────────────
    op.add_column(
        "surveys", sa.Column("resurvey_interval_days", sa.Integer(), nullable=True)
    )
    op.add_column(
        "surveys",
        sa.Column("previous_wave_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "surveys_previous_wave_id_fkey",
        "surveys",
        "surveys",
        ["previous_wave_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_surveys_previous_wave_id", "surveys", ["previous_wave_id"], unique=False
    )
    op.create_check_constraint(
        "ck_survey_resurvey_interval",
        "surveys",
        "resurvey_interval_days IS NULL OR resurvey_interval_days >= 30",
    )

    # ── announcements: delivery ─────────────────────────────────────────────
    op.add_column(
        "announcements",
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "announcements",
        sa.Column(
            "delivered_count", sa.Integer(), server_default="0", nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("announcements", "delivered_count")
    op.drop_column("announcements", "delivered_at")

    op.drop_constraint("ck_survey_resurvey_interval", "surveys", type_="check")
    op.drop_index("ix_surveys_previous_wave_id", table_name="surveys")
    op.drop_constraint("surveys_previous_wave_id_fkey", "surveys", type_="foreignkey")
    op.drop_column("surveys", "previous_wave_id")
    op.drop_column("surveys", "resurvey_interval_days")

    # Award announcements have no sender and cannot survive the NOT NULL. The
    # award itself stays in award_results; only the feed post goes.
    op.execute("DELETE FROM shoutouts WHERE from_employee_id IS NULL")
    op.drop_constraint("ck_shoutout_has_author", "shoutouts", type_="check")
    op.drop_constraint("shoutouts_award_result_id_key", "shoutouts", type_="unique")
    op.drop_constraint("shoutouts_award_result_id_fkey", "shoutouts", type_="foreignkey")
    op.drop_column("shoutouts", "award_result_id")
    op.alter_column(
        "shoutouts",
        "from_employee_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
