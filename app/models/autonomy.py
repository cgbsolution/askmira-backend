"""Autonomy configuration, the proposal queue, and reversals.

Three tables, and each shape follows from a question somebody will ask in an
enterprise security review:

- **`agent_autonomy_configs` stores `max_autonomy_at_time`.** The ceiling as it
  stood when the tenant configured it. "What were they allowed to set" is a
  question about that moment, and recomputing it from today's catalogue would
  answer a different one — particularly if we later lower a ceiling.

- **`agent_proposals` carries `previous_state`.** Captured when the proposal is
  made, not when it is applied. By the time somebody wants to revert, the record
  has moved on, and reconstructing "what was it before" from an audit log is a
  reconstruction rather than a revert.

- **`agent_proposals` also carries `reasoning` and `citations`, both non-null.**
  A proposal an approver cannot evaluate gets approved in a batch, and the
  approval step becomes latency with no safety. The database refuses one.

- **`proposal_reversals` is a separate row, not a status flip.** "It was applied
  on the 3rd and reverted on the 5th" is two events, and a single mutable status
  can only hold the second.
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
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class AutonomyLevelEnum(str, enum.Enum):
    SUGGEST_ONLY = "suggest_only"
    PROPOSE_FOR_APPROVAL = "propose_for_approval"
    AUTO_APPLY_WITH_NOTIFY = "auto_apply_with_notify"
    AUTO_APPLY_SILENT = "auto_apply_silent"


class ProposalStatusEnum(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    AUTO_APPLIED = "auto_applied"
    REVERTED = "reverted"


class AgentAutonomyConfig(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One tenant's setting for one agent action.

    A row absent means the platform default applies. Stored rather than seeded
    for every tenant: a table with one row per tenant per action is mostly
    defaults, and "which of these did they actually choose" stops being visible.
    """

    __tablename__ = "agent_autonomy_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "action_key", name="uq_autonomy_per_action_per_tenant"),
        CheckConstraint(
            "threshold_value IS NULL OR threshold_value >= 0",
            name="ck_autonomy_threshold_non_negative",
        ),
    )

    #: `agent_key.action_key` from `services.autonomy.catalogue.ACTIONS`. Not a
    #: foreign key: the catalogue is code, reviewed as code, and a table that
    #: could drift from it would be a second source of truth for a ceiling.
    action_key: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    agent_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    autonomy: Mapped[AutonomyLevelEnum] = mapped_column(
        Enum(AutonomyLevelEnum, name="agent_autonomy_level"), nullable=False
    )
    #: The platform ceiling when this was configured. Stored so "what were they
    #: allowed to set" is a question about that moment — and so lowering a
    #: ceiling later does not rewrite the history of what a tenant chose.
    max_autonomy_at_time: Mapped[AutonomyLevelEnum] = mapped_column(
        Enum(AutonomyLevelEnum, name="agent_autonomy_level", create_type=False),
        nullable=False,
    )

    #: Who may approve proposals from this action. Empty means the default HR
    #: approvers.
    approver_roles: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    #: The magnitude below which the action applies itself. Null where the
    #: action does not support one.
    threshold_value: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    #: The unit, copied from the catalogue at write time. Denormalised so a
    #: stored number is never readable without knowing what it counts.
    threshold_unit: Mapped[str | None] = mapped_column(String(16), nullable=True)

    configured_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    configured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentProposal(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One thing an agent wants to do, in the single consolidated queue.

    There is one queue for every agent. A per-agent inbox looks tidier in each
    module and produces a person with five tabs who checks two of them.
    """

    __tablename__ = "agent_proposals"
    __table_args__ = (
        Index("ix_proposal_queue", "tenant_id", "status", "expires_at"),
        Index("ix_proposal_action", "tenant_id", "action_key", "status"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_proposal_confidence"),
        # Non-empty reasoning and at least a citation array. A proposal an
        # approver cannot evaluate gets approved in a batch of forty.
        CheckConstraint("length(reasoning) > 0", name="ck_proposal_has_reasoning"),
    )

    action_key: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    agent_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)

    proposed_change: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    #: Captured at proposal time. By the time somebody wants to revert, the
    #: record has moved on.
    previous_state: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    #: Verified `[[source:id]]` identifiers from slice 28.
    citations: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    impact: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    reversible: Mapped[bool] = mapped_column(Boolean, nullable=False)

    status: Mapped[ProposalStatusEnum] = mapped_column(
        Enum(ProposalStatusEnum, name="agent_proposal_status"),
        default=ProposalStatusEnum.PENDING,
        nullable=False,
        index=True,
    )
    #: Stored, not computed at read time. Lengthening an expiry window next
    #: month must not retroactively revive proposals that had already lapsed.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Why it routed the way it did, in words. Shown in the queue so "why did
    #: this need me and that one not" is answerable without reading config.
    routing_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Where an approval or rejection became an eval case. The loop back into
    #: slice 27: a proposal type rejected repeatedly is a defective agent, and
    #: the rejections are the candidate regression cases.
    promoted_to_eval_case_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("eval_cases.id", ondelete="SET NULL"),
        nullable=True,
    )


class ProposalReversal(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One undo of one applied proposal. Insert-only.

    A separate row rather than a status flip: "applied on the 3rd, reverted on
    the 5th" is two events, and one mutable status can only hold the second.
    """

    __tablename__ = "proposal_reversals"
    __table_args__ = (
        UniqueConstraint("proposal_id", name="uq_reversal_per_proposal"),
        Index("ix_reversal_when", "tenant_id", "reverted_at"),
    )

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("agent_proposals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: The state restored. Copied from the proposal's `previous_state` at revert
    #: time so the reversal row is self-contained — a reversal that has to read
    #: the proposal to say what it did is one that breaks if the proposal is
    #: ever purged by retention.
    restored_state: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reverted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    reverted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
