"""Ask Mira: each person's conversations with the workspace's AI companion

Revision ID: d1a7c3e9f5b2
Revises: c8e3a1b5d7f9
Create Date: 2026-10-19 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d1a7c3e9f5b2"
down_revision: str | None = "c8e3a1b5d7f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _stamps() -> list[sa.Column]:
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


def upgrade() -> None:
    op.create_table(
        "companion_conversations",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
        *_stamps(),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_companion_conversations_user", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_companion_conversations_tenant_id"),
        "companion_conversations",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_companion_conversations_owner",
        "companion_conversations",
        ["user_id", "last_message_at"],
        unique=False,
    )

    op.create_table(
        "companion_messages",
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("tool", sa.String(length=48), nullable=True),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cards", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("invocation_id", sa.UUID(), nullable=True),
        *_stamps(),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["companion_conversations.id"],
            name="fk_companion_messages_conversation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_companion_messages_user", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["invocation_id"],
            ["agent_invocations.id"],
            name="fk_companion_messages_inv",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_companion_messages_tenant_id"), "companion_messages", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_companion_messages_user_id"), "companion_messages", ["user_id"], unique=False
    )
    op.create_index(
        "ix_companion_messages_thread",
        "companion_messages",
        ["conversation_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_companion_messages_thread", table_name="companion_messages")
    op.drop_index(op.f("ix_companion_messages_user_id"), table_name="companion_messages")
    op.drop_index(op.f("ix_companion_messages_tenant_id"), table_name="companion_messages")
    op.drop_table("companion_messages")
    op.drop_index("ix_companion_conversations_owner", table_name="companion_conversations")
    op.drop_index(
        op.f("ix_companion_conversations_tenant_id"), table_name="companion_conversations"
    )
    op.drop_table("companion_conversations")
