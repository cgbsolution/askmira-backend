"""Performance conversations (check-in replies, 1:1s, 360, private feedback) and
in-portal letter acknowledgement

Revision ID: e4f009300b08
Revises: ae8b1d4d874e
Create Date: 2026-09-30 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e4f009300b08"
down_revision: str | None = "ae8b1d4d874e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Created once with checkfirst and dropped explicitly in downgrade(), so the
# migration round-trips. SQLAlchemy stores enum member NAMES, as elsewhere.
_MEETING_STATUS = postgresql.ENUM(
    "SCHEDULED", "COMPLETED", "CANCELLED", name="one_on_one_status"
)
_ITEM_KIND = postgresql.ENUM("TALKING_POINT", "ACTION_ITEM", name="one_on_one_item_kind")
_REQUEST_STATUS = postgresql.ENUM("OPEN", "CLOSED", name="feedback_request_status")
_VISIBILITY = postgresql.ENUM(
    "ANONYMISED", "NAMED", "MANAGER_ONLY", name="feedback_visibility"
)
_REVIEWER_STATUS = postgresql.ENUM(
    "PENDING", "SUBMITTED", "DECLINED", name="feedback_reviewer_status"
)
_PRIVATE_KIND = postgresql.ENUM("APPRECIATION", "SUGGESTION", name="private_feedback_kind")

_ENUMS = (
    _MEETING_STATUS,
    _ITEM_KIND,
    _REQUEST_STATUS,
    _VISIBILITY,
    _REVIEWER_STATUS,
    _PRIVATE_KIND,
)


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, create_type=False)


def _base() -> list[sa.Column]:
    return [
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def _tenant(table: str) -> None:
    op.create_index(op.f(f"ix_{table}_tenant_id"), table, ["tenant_id"])


def upgrade() -> None:
    bind = op.get_bind()

    # A check-in can now say the goal is done. Postgres cannot drop an enum
    # value, so downgrade leaves it in place (unused rows are the only effect).
    op.execute("ALTER TYPE goal_check_in_confidence ADD VALUE IF NOT EXISTS 'DONE'")

    for enum in _ENUMS:
        enum.create(bind, checkfirst=True)

    # ── Replies to goal check-ins ───────────────────────────────────────────
    op.create_table(
        "goal_check_in_comments",
        sa.Column("check_in_id", sa.UUID(), nullable=False),
        sa.Column("goal_id", sa.UUID(), nullable=False),
        sa.Column("author_user_id", sa.UUID(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        *_base(),
        sa.ForeignKeyConstraint(["check_in_id"], ["goal_check_ins.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _tenant("goal_check_in_comments")
    op.create_index(
        "ix_goal_check_in_comments_check_in",
        "goal_check_in_comments",
        ["tenant_id", "check_in_id"],
    )

    # ── 1:1s ────────────────────────────────────────────────────────────────
    op.create_table(
        "one_on_ones",
        sa.Column("manager_employee_id", sa.UUID(), nullable=False),
        sa.Column("report_employee_id", sa.UUID(), nullable=False),
        sa.Column("scheduled_on", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("status", _enum("one_on_one_status"), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by_user_id", sa.UUID(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        *_base(),
        sa.CheckConstraint(
            "manager_employee_id <> report_employee_id", name="ck_one_on_one_two_people"
        ),
        sa.CheckConstraint(
            "duration_minutes > 0 AND duration_minutes <= 480",
            name="ck_one_on_one_duration_range",
        ),
        sa.ForeignKeyConstraint(["manager_employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["report_employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["completed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _tenant("one_on_ones")
    op.create_index(
        "ix_one_on_ones_manager",
        "one_on_ones",
        ["tenant_id", "manager_employee_id", "scheduled_on"],
    )
    op.create_index(
        "ix_one_on_ones_report",
        "one_on_ones",
        ["tenant_id", "report_employee_id", "scheduled_on"],
    )

    op.create_table(
        "one_on_one_items",
        sa.Column("one_on_one_id", sa.UUID(), nullable=False),
        sa.Column("kind", _enum("one_on_one_item_kind"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("added_by_user_id", sa.UUID(), nullable=True),
        sa.Column("owner_employee_id", sa.UUID(), nullable=True),
        sa.Column("due_on", sa.Date(), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("done_by_user_id", sa.UUID(), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False),
        *_base(),
        sa.ForeignKeyConstraint(["one_on_one_id"], ["one_on_ones.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["added_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["owner_employee_id"], ["employees.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["done_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _tenant("one_on_one_items")
    op.create_index(
        "ix_one_on_one_items_meeting",
        "one_on_one_items",
        ["tenant_id", "one_on_one_id", "display_order"],
    )
    op.create_index(
        "ix_one_on_one_items_owner",
        "one_on_one_items",
        ["tenant_id", "owner_employee_id", "done_at"],
    )

    op.create_table(
        "one_on_one_private_notes",
        sa.Column("one_on_one_id", sa.UUID(), nullable=False),
        sa.Column("author_user_id", sa.UUID(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        *_base(),
        sa.ForeignKeyConstraint(["one_on_one_id"], ["one_on_ones.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "one_on_one_id", "author_user_id", name="uq_one_on_one_note_author"
        ),
    )
    _tenant("one_on_one_private_notes")

    # ── 360 feedback ────────────────────────────────────────────────────────
    op.create_table(
        "feedback_requests",
        sa.Column("subject_employee_id", sa.UUID(), nullable=False),
        sa.Column("requested_by_user_id", sa.UUID(), nullable=True),
        sa.Column("appraisal_cycle_id", sa.UUID(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("due_on", sa.Date(), nullable=False),
        sa.Column("status", _enum("feedback_request_status"), nullable=False),
        sa.Column("employee_visibility", _enum("feedback_visibility"), nullable=False),
        sa.Column("anonymity_threshold", sa.Integer(), nullable=False),
        sa.Column("questions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scale", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        *_base(),
        sa.CheckConstraint("anonymity_threshold >= 3", name="ck_feedback_anonymity_floor"),
        sa.ForeignKeyConstraint(["subject_employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["appraisal_cycle_id"], ["appraisal_cycles.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _tenant("feedback_requests")
    op.create_index(
        "ix_feedback_requests_subject",
        "feedback_requests",
        ["tenant_id", "subject_employee_id", "status"],
    )

    op.create_table(
        "feedback_reviewers",
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("reviewer_employee_id", sa.UUID(), nullable=False),
        sa.Column("status", _enum("feedback_reviewer_status"), nullable=False),
        sa.Column(
            "competency_ratings", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("strengths", sa.Text(), nullable=True),
        sa.Column("improvements", sa.Text(), nullable=True),
        sa.Column("overall_rating", sa.Numeric(4, 2), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("declined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decline_reason", sa.Text(), nullable=True),
        sa.Column("last_reminded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminder_count", sa.Integer(), nullable=False),
        *_base(),
        sa.CheckConstraint(
            "overall_rating IS NULL OR (overall_rating >= 1 AND overall_rating <= 5)",
            name="ck_feedback_overall_rating_range",
        ),
        sa.ForeignKeyConstraint(["request_id"], ["feedback_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["reviewer_employee_id"], ["employees.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "request_id", "reviewer_employee_id", name="uq_feedback_reviewer"
        ),
    )
    _tenant("feedback_reviewers")
    op.create_index(
        "ix_feedback_reviewers_reviewer",
        "feedback_reviewers",
        ["tenant_id", "reviewer_employee_id", "status"],
    )

    op.create_table(
        "private_feedback",
        sa.Column("from_employee_id", sa.UUID(), nullable=False),
        sa.Column("to_employee_id", sa.UUID(), nullable=False),
        sa.Column("kind", _enum("private_feedback_kind"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        *_base(),
        sa.CheckConstraint(
            "from_employee_id <> to_employee_id", name="ck_private_feedback_not_self"
        ),
        sa.ForeignKeyConstraint(["from_employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _tenant("private_feedback")
    op.create_index(
        "ix_private_feedback_to", "private_feedback", ["tenant_id", "to_employee_id", "created_at"]
    )
    op.create_index(
        "ix_private_feedback_from",
        "private_feedback",
        ["tenant_id", "from_employee_id", "created_at"],
    )

    # ── Letter acknowledgement ──────────────────────────────────────────────
    op.create_table(
        "letter_acknowledgements",
        sa.Column("letter_id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_by_user_id", sa.UUID(), nullable=True),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by_user_id", sa.UUID(), nullable=True),
        sa.Column("typed_name", sa.String(length=255), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        *_base(),
        sa.ForeignKeyConstraint(["letter_id"], ["issued_letters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["acknowledged_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "letter_id", name="uq_letter_acknowledgement_letter"),
    )
    _tenant("letter_acknowledgements")
    op.create_index(
        op.f("ix_letter_acknowledgements_employee_id"),
        "letter_acknowledgements",
        ["employee_id"],
    )


def downgrade() -> None:
    for table in (
        "letter_acknowledgements",
        "private_feedback",
        "feedback_reviewers",
        "feedback_requests",
        "one_on_one_private_notes",
        "one_on_one_items",
        "one_on_ones",
        "goal_check_in_comments",
    ):
        op.drop_table(table)
    bind = op.get_bind()
    for enum in reversed(_ENUMS):
        enum.drop(bind, checkfirst=True)
    # 'DONE' stays on goal_check_in_confidence: Postgres cannot remove an enum
    # value. Check-ins recorded as done keep reading back.
