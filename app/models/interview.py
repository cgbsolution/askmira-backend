"""Interview stages, scheduled interviews, panels and structured feedback.

`InterviewFeedback` is deliberately one row per panelist per interview, with a
unique constraint. Two rows would mean somebody submitted twice and nobody knows
which is current; an update-in-place would lose the first opinion, which is
precisely what a later question about a hiring decision needs.

`Interview.external_event_id` is what makes a reschedule a move rather than a
duplicate. An orphan calendar event is an interviewer who turns up and a
candidate who joins a call with nobody in it — so the id is stored on the
interview and the reschedule path patches it.

`CalendarConnection.access_token_encrypted` reads somebody's entire diary. It is
encrypted with the tenant-derived key and decrypted only in the calendar
registry, at the point of use.
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
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class InterviewStageType(str, enum.Enum):
    AI_SCREEN = "ai_screen"
    TECHNICAL = "technical"
    MANAGERIAL = "managerial"
    HR = "hr"
    PANEL = "panel"


class InterviewMode(str, enum.Enum):
    IN_PERSON = "in_person"
    VIDEO = "video"
    PHONE = "phone"


class InterviewStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    RESCHEDULED = "rescheduled"
    COMPLETED = "completed"
    # Both sides are worth measuring separately: a candidate no-show is a
    # sourcing signal, a panelist no-show is an internal one, and lumping them
    # together answers neither question.
    CANDIDATE_NO_SHOW = "candidate_no_show"
    PANEL_NO_SHOW = "panel_no_show"
    CANCELLED = "cancelled"


class PanelistRole(str, enum.Enum):
    INTERVIEWER = "interviewer"
    OBSERVER = "observer"
    CHAIR = "chair"


class ResponseStatus(str, enum.Enum):
    NEEDS_ACTION = "needs_action"
    ACCEPTED = "accepted"
    TENTATIVE = "tentative"
    DECLINED = "declined"


class Recommendation(str, enum.Enum):
    """Four values, no middle. A neutral centre collects neutrals."""

    STRONG_YES = "strong_yes"
    YES = "yes"
    NO = "no"
    STRONG_NO = "strong_no"


class CalendarProviderKind(str, enum.Enum):
    GOOGLE = "google"
    MICROSOFT = "microsoft"


class InterviewStage(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One round in a requisition's interview process."""

    __tablename__ = "interview_stages"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "requisition_id", "sequence", name="uq_interview_stage_sequence"
        ),
        CheckConstraint("duration_minutes > 0", name="ck_interview_stage_duration"),
    )

    requisition_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("requisitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    stage_type: Mapped[InterviewStageType] = mapped_column(
        Enum(InterviewStageType, name="interview_stage_type"), nullable=False
    )
    duration_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    mode: Mapped[InterviewMode] = mapped_column(
        Enum(InterviewMode, name="interview_mode"),
        default=InterviewMode.VIDEO,
        nullable=False,
    )

    # The competency template (slice 16) this stage's feedback is structured
    # against. Structured rather than free text is what makes two interviewers
    # comparable instead of two essays.
    feedback_template_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("competency_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Competency codes every panelist must rate.
    required_competencies: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # A failure here ends the process rather than moving to the next round.
    is_eliminating: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Whether feedback blocks advancement. Defaults to True: unrecorded verbal
    # feedback is how a hiring decision becomes undefendable.
    feedback_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    preparation_notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Interview(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One scheduled round for one candidate."""

    __tablename__ = "interviews"
    __table_args__ = (
        Index("ix_interviews_schedule", "tenant_id", "scheduled_start", "status"),
        CheckConstraint("scheduled_end > scheduled_start", name="ck_interview_window"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stage_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("interview_stages.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Always UTC. Timezones exist at the edges — an interview stored in somebody's
    # local time is an interview at 3am for the other side.
    scheduled_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    scheduled_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The candidate's own zone, for rendering. Stored so a confirmation sent
    # three weeks later still shows the time they agreed to.
    candidate_timezone: Mapped[str] = mapped_column(
        String(64), default="Asia/Kolkata", nullable=False
    )

    mode: Mapped[InterviewMode] = mapped_column(
        Enum(InterviewMode, name="interview_mode", create_type=False),
        default=InterviewMode.VIDEO,
        nullable=False,
    )
    location_or_link: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    status: Mapped[InterviewStatus] = mapped_column(
        Enum(InterviewStatus, name="interview_status"),
        default=InterviewStatus.SCHEDULED,
        nullable=False,
        index=True,
    )
    reschedule_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ── Calendar ────────────────────────────────────────────────────────────
    # The provider's event id. This column is what makes a reschedule a move
    # rather than a duplicate; without it, the only way to change the time is to
    # create a second event and leave the first as an orphan.
    external_event_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    calendar_provider: Mapped[CalendarProviderKind | None] = mapped_column(
        Enum(CalendarProviderKind, name="calendar_provider"), nullable=True
    )
    join_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Anything the provider warned about — an event created without a Meet link,
    # an attendee it would not accept. Kept where a recruiter will see it.
    calendar_warnings: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    cancelled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scheduled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    panelists: Mapped[list[InterviewPanelist]] = relationship(
        back_populates="interview", cascade="all, delete-orphan", lazy="selectin"
    )


class InterviewPanelist(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One person on one interview."""

    __tablename__ = "interview_panelists"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "interview_id", "user_id", name="uq_panelist_once_per_interview"
        ),
    )

    interview_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("interviews.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[PanelistRole] = mapped_column(
        Enum(PanelistRole, name="panelist_role"),
        default=PanelistRole.INTERVIEWER,
        nullable=False,
    )

    response_status: Mapped[ResponseStatus] = mapped_column(
        Enum(ResponseStatus, name="panelist_response_status"),
        default=ResponseStatus.NEEDS_ACTION,
        nullable=False,
        index=True,
    )
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when the recruiter has been told about a decline. A decline nobody was
    # told about is a decline discovered at the interview.
    decline_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    feedback_submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attended: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    interview: Mapped[Interview] = relationship(back_populates="panelists")


class InterviewFeedback(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One panelist's written view of one interview.

    One row per panelist per interview, enforced. Two rows means somebody
    submitted twice and nobody knows which is current.
    """

    __tablename__ = "interview_feedback"
    __table_args__ = (
        UniqueConstraint("tenant_id", "interview_id", "panelist_id", name="uq_feedback_once"),
    )

    interview_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("interviews.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    panelist_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("interview_panelists.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    recommendation: Mapped[Recommendation] = mapped_column(
        Enum(Recommendation, name="interview_recommendation"), nullable=False
    )
    # {competency_code: score}, against the slice 16 framework.
    ratings: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    strengths: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Required where the recommendation is negative: "no" with nothing behind it
    # cannot be explained to the candidate or examined for bias.
    concerns: Mapped[str | None] = mapped_column(Text, nullable=True)

    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class SelfScheduleLink(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Recruiter-approved slots a candidate picks from.

    Removes the back-and-forth email thread, which is where most recruiter
    scheduling time actually goes. The token is hashed, not stored — a leaked
    database should not hand over every candidate's booking link.
    """

    __tablename__ = "self_schedule_links"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_self_schedule_token"),)

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stage_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("interview_stages.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # [{"start": iso, "end": iso, "panelist_user_ids": [...]}] — snapshotted, so
    # the candidate is offered exactly what the recruiter approved even if a
    # calendar changes underneath.
    offered_slots: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    candidate_timezone: Mapped[str] = mapped_column(
        String(64), default="Asia/Kolkata", nullable=False
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    booked_interview_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("interviews.id", ondelete="SET NULL"),
        nullable=True,
    )
    booked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CalendarConnection(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One interviewer's OAuth connection to their calendar.

    Per interviewer, not per tenant: reading free/busy needs that person's own
    consent, and a service account impersonating the whole company is both a
    harder sell and a much larger blast radius.
    """

    __tablename__ = "calendar_connections"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "provider", name="uq_calendar_connection_once"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[CalendarProviderKind] = mapped_column(
        Enum(CalendarProviderKind, name="calendar_provider", create_type=False),
        nullable=False,
    )
    account_email: Mapped[str] = mapped_column(String(320), nullable=False)

    # Encrypted with the tenant-derived key, decrypted only in the calendar
    # registry. This token reads somebody's entire diary.
    access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
