"""Ask Mira: each person's conversations with their workspace's AI companion.

A conversation belongs to one person in one workspace and to nobody else:
nobody, not HR, not an admin, reads another person's chat. Messages are only
ever appended. A person may delete a whole conversation; what the companion
did along the way stays in the audit log, which is insert-only (FS-AIT-01).

A message from the companion keeps three things apart:

- `text` is what the model wrote, shown to the person;
- `cards` are what AskMira looked up and showed, as this person, from its own
  tables — the record behind every figure the model states;
- `transcript` is the turn as the model saw it: which tools it called, with
  what, and the trimmed view of each result it read (companion/views.py —
  never a statutory identifier, bank detail, contact detail or leave reason).
  It is sent back on later turns so the conversation can build on itself.

An action the companion proposes (apply for leave, approve a request) is a
`CompanionAction`. Nothing is done until the person confirms it — a button, or
a "yes" in a later message — and then it runs through the module's own service,
as them, with that module's checks and audit row.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

__all__ = [
    "CompanionAction",
    "CompanionActionStatus",
    "CompanionConversation",
    "CompanionMessage",
]


class CompanionConversation(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "companion_conversations"
    __table_args__ = (Index("ix_companion_conversations_owner", "user_id", "last_message_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE", name="fk_companion_conversations_user"),
        nullable=False,
    )
    #: The first question, shortened; the person can see their conversations by it.
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CompanionMessage(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "companion_messages"
    __table_args__ = (Index("ix_companion_messages_thread", "conversation_id", "created_at"),)

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "companion_conversations.id",
            ondelete="CASCADE",
            name="fk_companion_messages_conversation",
        ),
        nullable=False,
    )
    #: The person the conversation belongs to, on every row, so a query for
    #: someone's messages never has to trust a join.
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE", name="fk_companion_messages_user"),
        nullable=False,
        index=True,
    )
    #: "user" or "assistant".
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The person's words, or what the model wrote. Never a looked-up record.
    text: Mapped[str] = mapped_column(Text, nullable=False)
    #: The tool the companion used for this answer, and with what.
    tool: Mapped[str | None] = mapped_column(String(48), nullable=True)
    arguments: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    #: What AskMira showed: tables, a policy answer, a report. Display only.
    cards: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    #: The model call that planned this answer (agent_invocations), when there was one.
    invocation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("agent_invocations.id", ondelete="SET NULL", name="fk_companion_messages_inv"),
        nullable=True,
    )
    #: The turn as the model saw it, replayed on later turns: for the person's
    #: message, the content sent; for a reply, the tool calls, the trimmed
    #: results and the answer, in order. Null on turns answered without a model.
    transcript: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    #: The tools a reply used, in order: key, title and the inputs it ran with.
    steps: Mapped[list | None] = mapped_column(JSONB, nullable=True)


class CompanionActionStatus(str, enum.Enum):
    PROPOSED = "proposed"
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"


class CompanionAction(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Something Ask Mira offered to do for the person, waiting on their say.

    `payload` is what will be done, as the module's own service reads it;
    `summary` is what the person was shown. Confirming runs the service as the
    person; the action row is marked done in the same transaction as the
    service's own change and audit row.
    """

    __tablename__ = "companion_actions"
    __table_args__ = (Index("ix_companion_actions_conversation", "conversation_id", "created_at"),)

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "companion_conversations.id",
            ondelete="CASCADE",
            name="fk_companion_actions_conversation",
        ),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE", name="fk_companion_actions_user"),
        nullable=False,
        index=True,
    )
    #: leave_request, leave_cancel, regularisation, approval_decision, hr_ticket.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[CompanionActionStatus] = mapped_column(
        Enum(CompanionActionStatus, name="companion_action_status"),
        default=CompanionActionStatus.PROPOSED,
        nullable=False,
    )
    #: What came of it: the request raised, its link, or why it failed.
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: "button" or "message": how the person said yes (or no).
    decided_via: Mapped[str | None] = mapped_column(String(16), nullable=True)
