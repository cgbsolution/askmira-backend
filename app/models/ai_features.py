"""What the AI features keep: the assistant's sources, and outputs worth reviewing.

Model output that people will act on is stored, not regenerated on each view:
a survey's comment themes and a resume screening are read by people who make
decisions with them, and "it said something different yesterday" is not a
property a decision record can have. Each row names the model that produced it
and when, so a later reader knows what they are looking at.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
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


class AssistantArticle(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A piece of company policy HR wrote for the assistant to answer from.

    Only published articles are retrieved. The assistant cites the article it
    used, so an employee can open the words HR actually wrote.
    """

    __tablename__ = "assistant_articles"
    __table_args__ = (Index("ix_assistant_articles_published", "tenant_id", "is_published"),)

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class SurveyTopicSummary(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Themes found in one survey's free-text comments.

    Paraphrased themes with counts, never quotes: the comments are stored with
    no link to who wrote them, and a verbatim sentence is the easiest way to
    recognise a colleague's voice. A theme mentioned by fewer people than the
    survey's reporting floor is folded into "Other".
    """

    __tablename__ = "survey_topic_summaries"
    __table_args__ = (
        Index("ix_survey_topic_summaries_survey", "tenant_id", "survey_id", "created_at"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_survey_topic_confidence"),
    )

    survey_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("surveys.id", ondelete="CASCADE"), nullable=False
    )
    comment_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: [{"theme", "summary", "mentions", "sentiment"}]
    topics: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    generated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class CandidateScreening(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A resume read against a role's requirements, requirement by requirement.

    Advisory. Each requirement carries the resume's own words as evidence, or
    says none was found; the coverage figure is counted from those rows by code,
    not produced by the model, and the confidence is shown beside it. Nothing
    here moves a candidate — a person reads it and decides.
    """

    __tablename__ = "candidate_screenings"
    __table_args__ = (
        Index("ix_candidate_screenings_candidate", "tenant_id", "candidate_id", "created_at"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_candidate_screening_confidence"
        ),
        CheckConstraint(
            "requirements_met >= 0 AND requirements_met <= requirements_total",
            name="ck_candidate_screening_counts",
        ),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    requisition_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False
    )
    #: [{"requirement", "assessment": met|partial|not_evident, "evidence"}]
    requirements: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    requirements_total: Mapped[int] = mapped_column(Integer, nullable=False)
    requirements_met: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    #: What was taken out of the resume before it was sent.
    redactions: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    generated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
