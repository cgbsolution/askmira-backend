"""Ask Mira as a conversational agent, and client-account P&L

Revision ID: b4e8c2a6d0f1
Revises: a9d3e7f1c2b4
Create Date: 2026-10-29 21:00:00.000000

- `companion_messages.transcript`, `.steps`: the turn as the model saw it (tool
  calls and trimmed results), replayed on later turns; the tools a reply used.
- `companion_actions`: what Ask Mira offered to do (apply for leave, approve a
  request…), waiting for the person to confirm.
- `client_accounts`, `account_entries`, `account_allocations`: accounts the
  company bills, what was billed and spent on each per month, and who works on
  which account from when — the inputs of an account's P&L.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b4e8c2a6d0f1"
down_revision: str | None = "a9d3e7f1c2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _base() -> list[sa.Column]:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.add_column(
        "companion_messages",
        sa.Column("transcript", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "companion_messages",
        sa.Column("steps", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    status = postgresql.ENUM(
        "PROPOSED", "DONE", "CANCELLED", "FAILED", name="companion_action_status", create_type=False
    )
    status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "companion_actions",
        *_base(),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "companion_conversations.id",
                ondelete="CASCADE",
                name="fk_companion_actions_conversation",
            ),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_companion_actions_user"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", status, nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_via", sa.String(16), nullable=True),
    )
    op.create_index("ix_companion_actions_tenant_id", "companion_actions", ["tenant_id"])
    op.create_index("ix_companion_actions_user_id", "companion_actions", ["user_id"])
    op.create_index(
        "ix_companion_actions_conversation", "companion_actions", ["conversation_id", "created_at"]
    )

    op.create_table(
        "client_accounts",
        *_base(),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("client_name", sa.String(160), nullable=True),
        sa.Column(
            "legal_entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "legal_entities.id", ondelete="SET NULL", name="fk_client_accounts_entity"
            ),
            nullable=True,
        ),
        sa.Column(
            "owner_employee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="SET NULL", name="fk_client_accounts_owner"),
            nullable=True,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.UniqueConstraint("tenant_id", "code", name="uq_client_account_code"),
    )
    op.create_index("ix_client_accounts_tenant_id", "client_accounts", ["tenant_id"])

    op.create_table(
        "account_entries",
        *_base(),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "client_accounts.id", ondelete="CASCADE", name="fk_account_entries_account"
            ),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("period_year", sa.Integer(), nullable=False),
        sa.Column("period_month", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("reference", sa.String(120), nullable=True),
        sa.Column("entry_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('billing', 'cost')", name="ck_account_entry_kind"),
        sa.CheckConstraint("period_month BETWEEN 1 AND 12", name="ck_account_entry_month"),
        sa.CheckConstraint("amount <> 0", name="ck_account_entry_amount"),
    )
    op.create_index("ix_account_entries_tenant_id", "account_entries", ["tenant_id"])
    op.create_index(
        "ix_account_entries_period",
        "account_entries",
        ["tenant_id", "account_id", "period_year", "period_month"],
    )

    op.create_table(
        "account_allocations",
        *_base(),
        sa.Column(
            "employee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "employees.id", ondelete="CASCADE", name="fk_account_allocations_employee"
            ),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "client_accounts.id", ondelete="CASCADE", name="fk_account_allocations_account"
            ),
            nullable=False,
        ),
        sa.Column("percent", sa.Numeric(5, 2), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.CheckConstraint("percent > 0 AND percent <= 100", name="ck_account_allocation_percent"),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_account_allocation_dates",
        ),
    )
    op.create_index("ix_account_allocations_tenant_id", "account_allocations", ["tenant_id"])
    op.create_index(
        "ix_account_allocations_employee",
        "account_allocations",
        ["tenant_id", "employee_id", "effective_from"],
    )
    op.create_index(
        "ix_account_allocations_account", "account_allocations", ["tenant_id", "account_id"]
    )


def downgrade() -> None:
    op.drop_table("account_allocations")
    op.drop_table("account_entries")
    op.drop_table("client_accounts")
    op.drop_table("companion_actions")
    postgresql.ENUM(name="companion_action_status").drop(op.get_bind(), checkfirst=True)
    op.drop_column("companion_messages", "steps")
    op.drop_column("companion_messages", "transcript")
