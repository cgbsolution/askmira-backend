"""Agent evaluation: suites, cases, runs and results.

The shape follows from what a run has to be able to answer six weeks later:

- **`EvalRunRow.agent_fingerprint`** is a hash of the implementation, not a
  version string somebody remembers to bump. "The suite passed" is not useful
  without knowing what it passed against, and a manually-maintained version is
  wrong on exactly the release where it matters.

- **`EvalResultRow.measurements`** stores the numbers behind each verdict, not
  just pass or fail. A discrimination spread sliding from 34 to 21 while the
  assertion still passes is invisible in a pass rate, and it is the shape of
  every slow degradation.

- **`EvalCaseRow.source_incident_id`** is what keeps the suite from decaying.
  A case tagged to a real defect is a regression case, and a failed regression
  case blocks a merge outright.

- **`tokens` and `cost_inr` sit on the result row**, beside the quality
  verdicts, so a prompt change that improved the score and tripled the cost is
  one query rather than two dashboards.
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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class EvalGateOutcome(str, enum.Enum):
    PASS = "pass"
    WARN = "warn"
    BLOCK = "block"


class EvalTrigger(str, enum.Enum):
    MANUAL = "manual"
    #: A prompt or logic change. The one that gates a merge.
    CI = "ci"
    SCHEDULED = "scheduled"
    #: Sampled live output, re-checked asynchronously against the same
    #: assertions. Never gates anything; feeds drift detection.
    PRODUCTION_SAMPLE = "production_sample"


class EvalSuiteRow(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One suite for one agent.

    Tenant-scoped even though the canonical suites are code, because a tenant
    with an unusual population will want cases of their own — a 4,000-person
    factory's attendance data breaks assumptions a 200-person startup's does
    not, and their cases belong to them.
    """

    __tablename__ = "eval_suites"
    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_key", name="uq_eval_suite_per_agent"),
        CheckConstraint("version >= 1", name="ck_eval_suite_version"),
    )

    agent_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: Whether this agent produces a number somebody acts on. Forces a
    #: discrimination assertion — the service refuses a scoring suite without
    #: one rather than warning about it.
    is_scoring_agent: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    cases: Mapped[list[EvalCaseRow]] = relationship(
        back_populates="suite", lazy="selectin", cascade="all, delete-orphan"
    )


class EvalCaseRow(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One case: inputs, and what has to be true of the outputs."""

    __tablename__ = "eval_cases"
    __table_args__ = (
        UniqueConstraint("suite_id", "name", name="uq_eval_case_name_per_suite"),
        Index("ix_eval_case_regression", "tenant_id", "is_regression_case"),
    )

    suite_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("eval_suites.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: A list, even for a single-input case. The assertions that matter most
    #: compare across inputs, and a shape that made one input the default would
    #: make those awkward to write.
    inputs: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    assertions: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    tags: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    #: Set where this case came from a real defect. That makes it a regression
    #: case, and a failed regression case blocks a merge outright.
    is_regression_case: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    source_incident_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )

    suite: Mapped[EvalSuiteRow] = relationship(back_populates="cases")


class EvalRunRow(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One execution of one suite against one agent version."""

    __tablename__ = "eval_runs"
    __table_args__ = (
        Index("ix_eval_run_suite", "tenant_id", "suite_id", "started_at"),
        CheckConstraint("pass_count >= 0", name="ck_eval_run_pass_count"),
        CheckConstraint("fail_count >= 0", name="ck_eval_run_fail_count"),
    )

    suite_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("eval_suites.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    agent_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: A hash of the implementation, not a version somebody bumps by hand.
    agent_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    suite_version: Mapped[int] = mapped_column(Integer, nullable=False)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    triggered_by: Mapped[EvalTrigger] = mapped_column(
        Enum(EvalTrigger, name="eval_trigger"),
        default=EvalTrigger.MANUAL,
        nullable=False,
    )

    pass_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fail_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Cases that could not be evaluated — a crashing agent or a malformed
    #: suite. Counted separately from failures because the two need different
    #: responses, and both block.
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    gate_outcome: Mapped[EvalGateOutcome] = mapped_column(
        Enum(EvalGateOutcome, name="eval_gate_outcome"),
        default=EvalGateOutcome.PASS,
        nullable=False,
        index=True,
    )
    blocking_reasons: Mapped[list] = mapped_column(
        JSONB, default=list, nullable=False
    )
    warnings: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    #: Every numeric measurement in the run, flattened. What `compare` diffs.
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    #: The diff against the previous run for this agent, where there was one.
    diff_from_previous: Mapped[dict] = mapped_column(
        JSONB, default=dict, nullable=False
    )

    total_latency_ms: Mapped[float] = mapped_column(
        Numeric(12, 2), default=0, nullable=False
    )
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cost_inr: Mapped[float] = mapped_column(
        Numeric(12, 4), default=0, nullable=False
    )

    results: Mapped[list[EvalResultRow]] = relationship(
        back_populates="run", lazy="selectin", cascade="all, delete-orphan"
    )


class EvalResultRow(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One case's outcome within a run. Insert-only.

    `measurements` is the column that makes a slow degradation visible. A row
    that stored only `passed` would let a discrimination spread halve across
    four releases with every run green.
    """

    __tablename__ = "eval_results"
    __table_args__ = (
        Index("ix_eval_result_run", "tenant_id", "run_id"),
        Index("ix_eval_result_failed", "tenant_id", "passed"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("eval_runs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    case_name: Mapped[str] = mapped_column(String(160), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_regression_case: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    source_incident_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: What the agent actually returned. Kept so a failure can be diagnosed
    #: without re-running against a version that may have moved on.
    actual_output: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdicts: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    measurements: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    latency_ms: Mapped[float] = mapped_column(
        Numeric(12, 2), default=0, nullable=False
    )
    tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_inr: Mapped[float] = mapped_column(
        Numeric(12, 4), default=0, nullable=False
    )

    run: Mapped[EvalRunRow] = relationship(back_populates="results")


class AgentFeedback(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A person's verdict on one live agent output.

    The route by which the eval set stops being what somebody imagined could go
    wrong and becomes what actually did. `promoted_to_case_id` records where a
    piece of feedback turned into a case, so "did we act on this" is answerable
    rather than a matter of faith.
    """

    __tablename__ = "agent_feedback"
    __table_args__ = (
        Index("ix_agent_feedback_agent", "tenant_id", "agent_key", "created_at"),
    )

    agent_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: What the agent was given and what it returned, so the feedback can become
    #: a case without anybody reconstructing the input from memory.
    agent_input: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    agent_output: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    was_useful: Mapped[bool] = mapped_column(Boolean, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    promoted_to_case_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("eval_cases.id", ondelete="SET NULL"),
        nullable=True,
    )
