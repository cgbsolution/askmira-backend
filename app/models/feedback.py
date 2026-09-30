"""360 feedback requests, and private feedback between colleagues.

The appraisal chain (`app.models.appraisal`) is the decision — self, manager,
reviewer, HR — and it deliberately has no peer stage: a peer does not decide
anybody's rating. This is the input that sits beside it. A request names one
person, the colleagues asked about them, and optionally the review cycle it
feeds; each colleague answers once.

Three decisions worth stating.

**Who sees what is fixed when the request is made, and the reviewers are told.**
`employee_visibility` is chosen up front and shown on the form. A reviewer who
was promised anonymity and then named is a reviewer who never writes anything
useful again.

**Anonymity has a floor.** The person the feedback is about sees an anonymised
summary only once `anonymity_threshold` colleagues (three, at least) have
answered. With two answers "anonymous" means "one of these two", and the
comments identify the writer.

**The form is a snapshot.** The cycle's competencies and rating scale are
copied onto the request when it is made, so a template edited mid-cycle does
not change the questions under somebody who has already answered.

Private feedback (`private_feedback`) is the other half: a note from one
colleague to another, visible to the recipient, their manager and the writer.
Public praise is the culture module's shoutouts; this is the part that is not
for the feed.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class FeedbackRequestStatus(str, enum.Enum):
    OPEN = "open"
    #: Stopped collecting. Answers already given stay.
    CLOSED = "closed"


class FeedbackVisibility(str, enum.Enum):
    #: The person sees an anonymised summary once enough colleagues answered.
    ANONYMISED = "anonymised"
    #: The person sees each answer with the colleague's name.
    NAMED = "named"
    #: Only the manager line and HR see the answers.
    MANAGER_ONLY = "manager_only"


class FeedbackReviewerStatus(str, enum.Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    DECLINED = "declined"


class PrivateFeedbackKind(str, enum.Enum):
    APPRECIATION = "appreciation"
    SUGGESTION = "suggestion"


class FeedbackRequest(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A request for colleagues' feedback about one person."""

    __tablename__ = "feedback_requests"
    __table_args__ = (
        CheckConstraint("anonymity_threshold >= 3", name="ck_feedback_anonymity_floor"),
        Index("ix_feedback_requests_subject", "tenant_id", "subject_employee_id", "status"),
    )

    subject_employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: The review this feedback informs. Null for an ad hoc request.
    appraisal_cycle_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("appraisal_cycles.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: Context for the reviewers: what to think about, which project.
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_on: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[FeedbackRequestStatus] = mapped_column(
        Enum(FeedbackRequestStatus, name="feedback_request_status"),
        default=FeedbackRequestStatus.OPEN,
        nullable=False,
    )
    employee_visibility: Mapped[FeedbackVisibility] = mapped_column(
        Enum(FeedbackVisibility, name="feedback_visibility"),
        default=FeedbackVisibility.ANONYMISED,
        nullable=False,
    )
    anonymity_threshold: Mapped[int] = mapped_column(Integer, default=3, nullable=False)

    #: [{"competency_id", "code", "name", "description"}] — the cycle's
    #: competencies when the request was made. Empty for the simple form.
    questions: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    #: {"min", "max", "levels": [{"value", "label"}]} for the competency ratings.
    scale: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FeedbackReviewer(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One colleague asked for feedback, and their answer."""

    __tablename__ = "feedback_reviewers"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "request_id", "reviewer_employee_id", name="uq_feedback_reviewer"
        ),
        CheckConstraint(
            "overall_rating IS NULL OR (overall_rating >= 1 AND overall_rating <= 5)",
            name="ck_feedback_overall_rating_range",
        ),
        Index("ix_feedback_reviewers_reviewer", "tenant_id", "reviewer_employee_id", "status"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("feedback_requests.id", ondelete="CASCADE"),
        nullable=False,
    )
    reviewer_employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[FeedbackReviewerStatus] = mapped_column(
        Enum(FeedbackReviewerStatus, name="feedback_reviewer_status"),
        default=FeedbackReviewerStatus.PENDING,
        nullable=False,
    )
    #: [{"competency_id", "rating", "comment"}] against the request's snapshot.
    competency_ratings: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    strengths: Mapped[str | None] = mapped_column(Text, nullable=True)
    improvements: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Optional, 1 to 5.
    overall_rating: Mapped[float | None] = mapped_column(Numeric(4, 2), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    declined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decline_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_reminded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reminder_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class PrivateFeedback(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A note from one colleague to another. Not on any feed. Insert-only."""

    __tablename__ = "private_feedback"
    __table_args__ = (
        CheckConstraint("from_employee_id <> to_employee_id", name="ck_private_feedback_not_self"),
        Index("ix_private_feedback_to", "tenant_id", "to_employee_id", "created_at"),
        Index("ix_private_feedback_from", "tenant_id", "from_employee_id", "created_at"),
    )

    from_employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    to_employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[PrivateFeedbackKind] = mapped_column(
        Enum(PrivateFeedbackKind, name="private_feedback_kind"),
        default=PrivateFeedbackKind.APPRECIATION,
        nullable=False,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
