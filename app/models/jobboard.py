"""Job board accounts, postings, applications, and the candidate's own view.

`JobBoardAccount.credentials` is encrypted at rest with the tenant-derived key
from `app.core.crypto`. A board credential is a live recruiter login: with it,
someone can post jobs in the client's name and read their entire resume
database.

`Application` is the normalised landing point for every source — board, careers
page, referral. It exists separately from `Candidate` precisely because a person
applies more than once: the candidate is the person, and the applications are
what they did. Collapsing them is what loses "applied three times, rejected
twice at final round".

`CandidateConsent` and the retention columns are the DPDP surface (slice 25). A
resume is personal data belonging to somebody who never became an employee and
has no account here; keeping it forever by default is the wrong default, so
retention is set per tenant and enforced by a purge that runs on a schedule.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class JobBoard(str, enum.Enum):
    NAUKRI = "naukri"
    LINKEDIN = "linkedin"
    INDEED = "indeed"
    # Our own careers page. A board like any other, so a report of "where do
    # applications come from" does not have to special-case it.
    CAREERS = "careers"
    REFERRAL = "referral"
    # Entered by HR by hand — an agency CV, a walk-in, somebody met at an event.
    # Its own value rather than borrowing one of the above, so source
    # effectiveness never credits a board with a candidate it did not send.
    MANUAL = "manual"


class PostingStatus(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHING = "publishing"
    LIVE = "live"
    # Tried, failed, and waiting for a human. Never a silent absence — the whole
    # reason this state exists is that a dropped posting is invisible.
    PARKED = "parked"
    EXPIRED = "expired"
    CLOSED = "closed"


class ApplicationStatus(str, enum.Enum):
    """What the candidate is told, in the candidate's own words.

    Deliberately coarser than `CandidateStage`. A candidate does not need to
    know they are at "AI interview round 2 pending panel"; they need to know
    somebody is looking. Exposing the internal stage would also leak how many
    people are ahead of them.
    """

    APPLIED = "applied"
    UNDER_REVIEW = "under_review"
    SHORTLISTED = "shortlisted"
    INTERVIEWING = "interviewing"
    OFFERED = "offered"
    CLOSED = "closed"


class ConsentPurpose(str, enum.Enum):
    """What the candidate agreed to, separately.

    Bundled consent is not consent under DPDP. Somebody applying for one job has
    not agreed to be kept on file for two years and contacted about others, and
    a single checkbox covering both cannot be withdrawn from one without the
    other.
    """

    #: Not consent in the DPDP sense: recruitment rests on legitimate use
    #: (s.7(a) — the candidate gave their details for this purpose). The row is
    #: the record that they saw the notice and asked to be considered.
    APPLICATION_PROCESSING = "application_processing"
    TALENT_POOL_RETENTION = "talent_pool_retention"
    MARKETING_CONTACT = "marketing_contact"
    #: An AI model reads the résumé (outside India), with name, email and phone
    #: removed. Optional; without it nobody can run AI screening on them.
    AI_SCREENING = "ai_screening"


class JobBoardAccount(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One tenant's login for one board."""

    __tablename__ = "job_board_accounts"
    __table_args__ = (UniqueConstraint("tenant_id", "board", name="uq_job_board_account_once"),)

    board: Mapped[JobBoard] = mapped_column(
        Enum(JobBoard, name="job_board"), nullable=False, index=True
    )
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Encrypted with the tenant-derived key. A board credential is a live
    # recruiter login: it can post in the client's name and read their resume
    # database.
    credentials_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Non-secret per-board settings: Naukri's recruiter id, LinkedIn's
    # organisation URN, whether Resdex is on the subscription.
    settings: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The board's own last words when something went wrong. Kept on the account
    # so an expired subscription is visible without opening a log.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class JobPosting(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One requisition, on one board."""

    __tablename__ = "job_postings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "requisition_id", "board", name="uq_job_posting_per_board"),
        Index("ix_job_postings_refresh", "tenant_id", "status", "next_refresh_at"),
    )

    requisition_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("requisitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    board: Mapped[JobBoard] = mapped_column(
        Enum(JobBoard, name="job_board", create_type=False), nullable=False, index=True
    )
    external_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    status: Mapped[PostingStatus] = mapped_column(
        Enum(PostingStatus, name="posting_status"),
        default=PostingStatus.DRAFT,
        nullable=False,
        index=True,
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Refresh / boost ─────────────────────────────────────────────────────
    # A Naukri posting decays in listing position within days and clients expect
    # a scheduled boost. Null on boards that have no such concept, which is why
    # this is nullable rather than defaulted.
    refresh_interval_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_refresh_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Failure handling ────────────────────────────────────────────────────
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # The board's own words on the last failure. A parked posting says why.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What the adapter warned about at publish: a job that landed under "Other"
    # gets a fraction of the views, and the recruiter needs that today.
    warnings: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # What the board said it accepted, after its own normalisation.
    accepted_as: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    view_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    application_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Application(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One application, from any source, normalised.

    Separate from `Candidate` because a person applies more than once. The
    candidate is the person; the applications are what they did, and keeping
    them apart is what makes "applied three times, rejected twice at final
    round" a query rather than a memory.
    """

    __tablename__ = "applications"
    __table_args__ = (
        # The same board application must not land twice. Boards re-send on
        # their own schedule and a poll that overlaps a webhook is normal.
        UniqueConstraint(
            "tenant_id",
            "board",
            "external_application_id",
            name="uq_application_external_once",
        ),
        Index("ix_applications_candidate", "tenant_id", "candidate_id", "created_at"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    requisition_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("requisitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    board: Mapped[JobBoard] = mapped_column(
        Enum(JobBoard, name="job_board", create_type=False), nullable=False, index=True
    )
    external_application_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    status: Mapped[ApplicationStatus] = mapped_column(
        Enum(ApplicationStatus, name="application_status"),
        default=ApplicationStatus.APPLIED,
        nullable=False,
        index=True,
    )
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # How this application found its candidate: "new", "email", "phone", or
    # "ambiguous". Kept so a dedup rule change can be audited against what it
    # actually did, rather than guessed at from the result.
    matched_on: Mapped[str | None] = mapped_column(String(16), nullable=True)
    match_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    resume_storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Whatever the board sent that we have no column for. A field nobody mapped
    # is still evidence when a recruiter is asking why a candidate looks wrong.
    raw_payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # ── Retention (DPDP, slice 25) ──────────────────────────────────────────
    # Computed at ingestion from the tenant's policy, so "when does this go" is
    # answerable per row rather than recomputed from a policy that has since
    # changed.
    retention_until: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # What was removed. The row itself survives a purge so hiring statistics do
    # not silently change when personal data is deleted — the count of
    # applications is not personal data, the applicant is.
    purge_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class CandidateConsent(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One purpose, one consent, one row.

    Insert-only. A withdrawal is a new row with `granted` false, not an edit —
    "did they consent on the day we processed this" has to stay answerable after
    they withdraw, because that is exactly the question a regulator asks.
    """

    __tablename__ = "candidate_consents"
    __table_args__ = (
        Index("ix_candidate_consents_lookup", "tenant_id", "candidate_id", "purpose"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    purpose: Mapped[ConsentPurpose] = mapped_column(
        Enum(ConsentPurpose, name="consent_purpose"), nullable=False
    )
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # The exact wording shown. Consent to a notice nobody kept is not evidence
    # of anything.
    notice_text: Mapped[str] = mapped_column(Text, nullable=False)
    notice_version: Mapped[str] = mapped_column(String(32), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    source_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)


class CareersPageConfig(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """The tenant's careers page.

    `culture_enabled` pulls real shoutouts and clubs from the Culture module.
    Real ones or none — an invented testimonial on a careers page is exactly the
    kind of thing a candidate finds out about on day one.
    """

    __tablename__ = "careers_page_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_careers_config_per_tenant"),
        UniqueConstraint("slug", name="uq_careers_slug"),
        CheckConstraint("resume_retention_days > 0", name="ck_careers_retention_positive"),
    )

    # Public URL segment. Globally unique, because the page is served without a
    # tenant in scope — there is nobody logged in to resolve one from.
    slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    hero_heading: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hero_subheading: Mapped[str | None] = mapped_column(Text, nullable=True)
    about_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    logo_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    hero_image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    primary_colour: Mapped[str | None] = mapped_column(String(9), nullable=True)
    accent_colour: Mapped[str | None] = mapped_column(String(9), nullable=True)

    culture_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Applying without an account. Forced signup measurably depresses
    # application rates, and a candidate who cannot finish is not a candidate.
    allow_apply_without_account: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ── DPDP ────────────────────────────────────────────────────────────────
    # The notice a candidate is shown at application, and how long their data is
    # kept. Two years is a common Indian talent-pool period, but it is the
    # client's decision and the notice has to state whatever it is.
    privacy_notice_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    privacy_notice_version: Mapped[str] = mapped_column(String(32), default="1", nullable=False)
    resume_retention_days: Mapped[int] = mapped_column(Integer, default=730, nullable=False)

    seo_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    seo_description: Mapped[str | None] = mapped_column(Text, nullable=True)


class CandidatePortalToken(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """How a candidate sees their own status without an account.

    A long random token in an emailed link. Forcing account creation on someone
    who has applied once is the same mistake as forcing it at application: the
    portal exists to stop candidates being left in silence, and a login wall
    guarantees most of them never see it.

    Scoped to one candidate, expiring, and revocable. The token hash is stored,
    not the token — a leaked database should not hand over every candidate's
    portal.
    """

    __tablename__ = "candidate_portal_tokens"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_candidate_portal_token"),)

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CandidateDocument(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A document the candidate uploaded before joining.

    Feeds pre-onboarding (slice 24). Held under the same retention clock as the
    resume: a PAN card belonging to somebody who never joined is exactly the
    kind of thing that should not sit in a bucket for six years.
    """

    __tablename__ = "candidate_documents"

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    retention_until: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
