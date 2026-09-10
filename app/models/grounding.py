"""Agent invocation logs and injection attempts.

`AgentInvocation` is what the AI Trust Center publishes from, and its shape
follows from one requirement: **the answer has to be reconstructible**. Six weeks
after a manager acted on something an agent said, the questions are what was
retrieved, what the prompt version was, what the model returned, whether the
citations verified, and what the confidence was. A log missing any one of those
cannot answer "why did it say that", which is the only question anybody asks.

Two columns are worth their own note:

- **`rendered_output` is separate from `raw_output`.** When verification fails,
  the raw output is stored and the rendered one is the withheld message. Storing
  only the raw text would leave no record of what the user actually saw; storing
  only the rendered text would lose the evidence of what the model produced.

- **`retrieved_record_ids` is the set, not a count.** "It cited a record that was
  not retrieved" is only checkable after the fact if the retrieved set survives.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class InvocationOutcome(str, enum.Enum):
    """What happened to one agent call."""

    #: Answered, citations verified.
    ANSWERED = "answered"
    #: Answered in part; the advisory or out-of-scope half was declined.
    PARTIALLY_ANSWERED = "partially_answered"
    #: Declined outright.
    REFUSED = "refused"
    #: Produced an answer that failed verification, so the user saw the
    #: withheld message instead. Counted separately from a refusal because the
    #: two mean opposite things about the agent.
    WITHHELD = "withheld"
    #: The agent or the retrieval raised.
    FAILED = "failed"


class AgentInvocation(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One agent call, with everything needed to reconstruct the answer."""

    __tablename__ = "agent_invocations"
    __table_args__ = (
        Index("ix_invocation_agent", "tenant_id", "agent_key", "created_at"),
        Index("ix_invocation_outcome", "tenant_id", "outcome"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_invocation_confidence_range",
        ),
    )

    agent_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: Which prompt produced this. A hash rather than a hand-set version: "which
    #: prompt was live when it said that" has to be answerable without trusting
    #: somebody to have bumped a number.
    prompt_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)

    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: The scope that was applied, resolved. Stored so "could this user have
    #: reached that record" is answerable against the scope as it was, not as
    #: the role table says it would be today.
    scope_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_employee_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )

    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The identifiers of everything retrieved. The set, not a count — "it cited
    #: something that was never retrieved" is only checkable if this survives.
    retrieved_record_ids: Mapped[list] = mapped_column(
        JSONB, default=list, nullable=False
    )
    refused_sources: Mapped[list] = mapped_column(
        JSONB, default=list, nullable=False
    )

    outcome: Mapped[InvocationOutcome] = mapped_column(
        Enum(InvocationOutcome, name="invocation_outcome"),
        nullable=False,
        index=True,
    )
    #: What the model produced.
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: What the user was actually shown. Differs from `raw_output` exactly when
    #: verification failed, and keeping both is what lets somebody see that the
    #: model fabricated something and the system caught it.
    rendered_output: Mapped[str | None] = mapped_column(Text, nullable=True)

    citations: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    citation_failures: Mapped[list] = mapped_column(
        JSONB, default=list, nullable=False
    )
    citations_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    refusal_reasons: Mapped[list] = mapped_column(
        JSONB, default=list, nullable=False
    )
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    confidence_band: Mapped[str | None] = mapped_column(String(16), nullable=True)

    latency_ms: Mapped[float] = mapped_column(
        Numeric(12, 2), default=0, nullable=False
    )
    tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Which sub-processor saw the payload, where one did. Links this log to
    #: slice 25's cross-border transfer register.
    sub_processor_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PromptInjectionAttempt(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One flagged injection attempt in employee- or candidate-supplied text.

    Recorded rather than blocked. A resume containing "ignore previous
    instructions" is not a reason to reject an application — the candidate may
    not have written it, and refusing outright would let anybody get a rival
    rejected by editing a shared document. The text is neutralised by the prompt
    fencing either way; this row is what makes the attempt visible to a person.
    """

    __tablename__ = "prompt_injection_attempts"
    __table_args__ = (
        Index("ix_injection_when", "tenant_id", "created_at"),
        Index("ix_injection_field", "tenant_id", "source_field"),
    )

    #: `resume`, `self_appraisal`, `expense_note`, `shoutout`.
    source_field: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Which record the text came from, so somebody can go and look at it.
    source_table: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source_record_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The matched span only, not the whole document. Enough to see what was
    #: attempted without copying a candidate's entire CV into a security log.
    matched_text: Mapped[str] = mapped_column(String(400), nullable=False)
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    invocation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("agent_invocations.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: Whether a person has looked at it. An unreviewed queue that nobody can
    #: separate from a reviewed one stops being read.
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
