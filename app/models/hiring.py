"""Hiring / ATS — the re-platformed AnytimeHire module (FRD 5.5).

The point of re-platforming rather than integrating is data continuity: a
Candidate converts into an Employee inside the same schema, so pre-hire signal
(interview scores, assessment results) stays queryable against post-hire
outcomes in the V3 attrition model.

Note on FS-HIR-02: the Fit Score parity defect carried over from AnytimeHire is
a pre-launch blocking gate. `fit_score` is stored alongside the component
scores that produced it precisely so a regression test can assert that Fit
Score varies with AI Round Score rather than clustering identically.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class RequisitionStatus(str, enum.Enum):
    """Where a requisition stands.

    There is deliberately no separate APPROVED state alongside OPEN. An approved
    requisition *is* an open one — it is approved so that recruiting can start —
    and carrying both would mean every report has to know which of the two to
    filter on, with half of them getting it wrong.

    FULFILLED and CLOSED are different, though. FULFILLED means the seats were
    filled; CLOSED means recruiting stopped. "How many requisitions did we
    actually fill" is a question the two states have to be able to answer apart.
    """

    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    OPEN = "open"
    REJECTED = "rejected"
    ON_HOLD = "on_hold"
    FULFILLED = "fulfilled"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class CandidateStage(str, enum.Enum):
    APPLIED = "applied"
    SCREENING = "screening"
    AI_INTERVIEW = "ai_interview"
    HUMAN_INTERVIEW = "human_interview"
    OFFER = "offer"
    HIRED = "hired"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class Requisition(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """An open role.

    `jd_versions` keeps every published version of the description, because
    FS-HIR-01 requires a published JD to be versioned — a candidate applied
    against a specific wording and that wording has to stay retrievable.
    """

    __tablename__ = "requisitions"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_requisitions_tenant_code"),)

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)

    department_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL"), nullable=True
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("locations.id", ondelete="SET NULL"), nullable=True
    )
    hiring_manager_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    recruiter_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )

    openings: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    employment_type: Mapped[str] = mapped_column(String(32), default="full_time", nullable=False)
    experience_min_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    experience_max_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    budget_ctc_min: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    budget_ctc_max: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # [{"version": 1, "body": "...", "published_at": "...", "ai_generated": true}]
    jd_versions: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    #: The current description started from an AI draft. Carried into the JD
    #: version it is published as, so a candidate-facing wording can always be
    #: traced to whether a person wrote it from scratch.
    description_ai_assisted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    skills: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    status: Mapped[RequisitionStatus] = mapped_column(
        Enum(RequisitionStatus, name="requisition_status"),
        default=RequisitionStatus.DRAFT,
        nullable=False,
        index=True,
    )
    target_close_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Slice 19: approval ──────────────────────────────────────────────────
    grade: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # Set when this backfills someone who left. A backfill and a new position
    # are not the same decision and should not need the same signatures.
    replacement_for_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_joining_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    raised_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # The approved seat this requisition fills (slice 23). Null is legitimate
    # and is exactly what makes a requisition off-budget: companies do hire
    # ahead of an approval, and a product that blocked it would be worked
    # around within a week, at which point the flag is gone too.
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("positions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    # Headcount approved for this position by position management (slice 23).
    # Null where that module has not been configured — the budget check then
    # falls back to the CTC band alone rather than silently passing.
    approved_headcount: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Seats currently held by a live offer or a joined employee. A revoked or
    # dropped offer decrements it, or the requisition reads as filled while the
    # seat is empty.
    filled_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    @property
    def open_vacancies(self) -> int:
        return max(0, self.openings - self.filled_count)

    candidates: Mapped[list[Candidate]] = relationship(
        back_populates="requisition", cascade="all, delete-orphan"
    )


class Candidate(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "candidates"
    __table_args__ = (
        # Declared here as well as in the migration so `alembic check` sees the
        # two agreeing. Ingestion narrows candidates by the last ten digits of
        # their phone — the same rule the matching engine applies — and without
        # this the narrowing query reads the whole table on every application.
        Index(
            "ix_candidates_phone_digits",
            "tenant_id",
            text(r"right(regexp_replace(phone, '\D', '', 'g'), 10)"),
        ),
        Index("ix_candidates_tags", "tags", postgresql_using="gin"),
    )

    requisition_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False
    )

    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: The candidate's own WhatsApp opt-in (on the application form or their portal).
    #: Nobody else can set it: a recruiter ticking it would be consent nobody gave.
    whatsapp_opt_in_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    whatsapp_opted_out_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    resume_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Structured output of resume parsing: skills, roles, education, tenure.
    parsed_resume: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    current_company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    total_experience_years: Mapped[float | None] = mapped_column(Numeric(4, 1), nullable=True)
    current_ctc: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    expected_ctc: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    notice_period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    stage: Mapped[CandidateStage] = mapped_column(
        Enum(CandidateStage, name="candidate_stage"),
        default=CandidateStage.APPLIED,
        nullable=False,
        index=True,
    )
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: A recruiter's labels — "java", "campus-2026", "silver-medallist". Lower
    #: case, de-duplicated; cleared with the rest of the person on erasure.
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(48)), default=list, server_default=text("'{}'"), nullable=False
    )

    # Composite 0-100 ranking. Never stored without the component scores in
    # InterviewScore that justify it — a bare number is exactly the defect
    # FS-HIR-02 exists to prevent.
    fit_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)

    # Set on conversion, closing the pre-hire to post-hire loop (FRD 7.2).
    converted_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )

    requisition: Mapped[Requisition] = relationship(back_populates="candidates")
    scores: Mapped[list[InterviewScore]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )


class InterviewScore(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One interview round's outcome.

    `signals` carries the factual basis for the score. Per FS-ANL-02's rule —
    no bare AI score without its contributing factors — an AI-generated score
    is not written without it.
    """

    __tablename__ = "interview_scores"

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )

    # ai_screening | ai_interview | coding | human
    round_type: Mapped[str] = mapped_column(String(32), nullable=False)
    round_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    max_score: Mapped[float] = mapped_column(Numeric(5, 2), default=100, nullable=False)
    # 0-1. Mandatory on every AI-scored output (FS-ANL-02).
    confidence: Mapped[float | None] = mapped_column(Numeric(3, 2), nullable=True)

    is_ai_generated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    interviewer_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )

    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # {"communication": 8, "problem_solving": 6, ...} — what produced the score.
    signals: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # Proctoring observations. Wording must stay factual and
    # non-judgemental — the miscalibrated "AI-Cheating 10/10" badge is a named
    # pre-launch blocker (FS-HIR-02).
    proctoring_notes: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    candidate: Mapped[Candidate] = relationship(back_populates="scores")


class CandidateStageEvent(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Insert-only history of a candidate's pipeline stage.

    `Candidate.stage` says where somebody is; this says how they got there, who
    moved them and why. Without it a rejection has no reason anybody can read
    back, and stage timings (slice 36) have no dates to measure between — the
    only alternative is reconstructing them from the audit log's prose.
    """

    __tablename__ = "candidate_stage_events"
    __table_args__ = (
        Index("ix_candidate_stage_events_candidate", "tenant_id", "candidate_id", "occurred_at"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    requisition_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False
    )
    from_stage: Mapped[CandidateStage | None] = mapped_column(
        Enum(CandidateStage, name="candidate_stage", create_type=False), nullable=True
    )
    to_stage: Mapped[CandidateStage] = mapped_column(
        Enum(CandidateStage, name="candidate_stage", create_type=False),
        nullable=False,
        index=True,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
