"""Ask Mira: each person's conversations with their workspace's AI companion.

A conversation belongs to one person in one workspace and to nobody else:
nobody, not HR, not an admin, reads another person's chat. Messages are only
ever appended. A person may delete a whole conversation; what the companion
did along the way stays in the audit log, which is insert-only (FS-AIT-01).

A message from the companion keeps two things apart:

- `text` is what the model wrote, and the only part of the conversation that is
  shown to the model again on the next turn;
- `cards` are what AskMira looked up and showed, as this person, from its own
  tables. They may name colleagues, so they are never sent to the model.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

__all__ = ["CompanionConversation", "CompanionMessage"]


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
