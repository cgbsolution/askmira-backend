"""Awards, values, surveys, action plans and internal comms (slice 32).

Separate from `culture.py`, which holds shoutouts and clubs, because these are a
different kind of object: adjudicated, periodic and recorded permanently, rather
than instant and peer-to-peer. Same module in the product, different tables.

═══════════════════════════════════════════════════════════════════════════════
`survey_responses` HAS NO RESPONDENT COLUMN, AND THAT IS THE POINT
═══════════════════════════════════════════════════════════════════════════════

An anonymous survey whose answers table carries an `employee_id` is anonymous by
policy, and a policy is one `SELECT` away from not being anonymous. So the
answers are stored with only the group keys they will be reported by, and the
column that would identify a respondent does not exist. An invariant fails the
build if one is added.

Who has responded is recorded in `survey_participants` instead — that table has
the employee and the timestamp and **no answers**, which is exactly what a
reminder needs and nothing more. Joining the two tells you nothing: the
participant rows and the response rows have no key in common beyond the survey.

═══════════════════════════════════════════════════════════════════════════════
AN ACKNOWLEDGEMENT STORES THE HASH OF WHAT WAS ACKNOWLEDGED
═══════════════════════════════════════════════════════════════════════════════

Edit a POSH policy after two hundred people have acknowledged it and, without
the hash, those two hundred acknowledgements attest to a document none of them
saw. `announcement_acknowledgements.content_hash` is what makes the register
survive an edit honestly.

═══════════════════════════════════════════════════════════════════════════════
`event_privacy_preferences` DEFAULTS TO FALSE, IN THE COLUMN
═══════════════════════════════════════════════════════════════════════════════

Opt-in is a schema default, not an application convention, because an
application convention is one careless `or True` away from broadcasting
somebody's date of birth to the company.
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


class AwardCadenceEnum(str, enum.Enum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    HALF_YEARLY = "half_yearly"
    ANNUAL = "annual"


class AwardCycleStatusEnum(str, enum.Enum):
    DRAFT = "draft"
    NOMINATIONS_OPEN = "nominations_open"
    JURY_SCORING = "jury_scoring"
    DECIDED = "decided"
    ANNOUNCED = "announced"
    CANCELLED = "cancelled"


class NominationStatusEnum(str, enum.Enum):
    SUBMITTED = "submitted"
    #: Screened out before the jury saw it - ineligible, duplicate, withdrawn.
    WITHDRAWN = "withdrawn"
    SHORTLISTED = "shortlisted"
    WON = "won"
    NOT_SELECTED = "not_selected"


class TagSourceEnum(str, enum.Enum):
    """What was tagged to a value.

    Kept as an enum rather than a free string because the analytics count each
    source separately, and a typo would create a fourth source that reports as
    zero everywhere.
    """

    SHOUTOUT = "shoutout"
    NOMINATION = "nomination"
    COMPETENCY = "competency"


class SurveyKindEnum(str, enum.Enum):
    PULSE = "pulse"
    ENPS = "enps"
    ENGAGEMENT = "engagement"
    WELLBEING = "wellbeing"
    INCLUSION = "inclusion"
    CUSTOM = "custom"


class SurveyStatusEnum(str, enum.Enum):
    DRAFT = "draft"
    OPEN = "open"
    CLOSED = "closed"


class PlanScopeEnum(str, enum.Enum):
    COMPANY = "company"
    DEPARTMENT = "department"
    TEAM = "team"


class PlanStatusEnum(str, enum.Enum):
    DRAFT = "draft"
    COMMITTED = "committed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    DROPPED = "dropped"


class EventKindEnum(str, enum.Enum):
    BIRTHDAY = "birthday"
    WORK_ANNIVERSARY = "work_anniversary"
    HOLIDAY = "holiday"
    COMPANY_EVENT = "company_event"


# ══════════════════════════════════════════════════════════════════════════════
# Values
# ══════════════════════════════════════════════════════════════════════════════


class CompanyValue(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One company value. `code` is what `competency_value_links` references."""

    __tablename__ = "company_values"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_company_value_code"),
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Not nullable. A one-word value on a dropdown gets tagged by whoever is
    #: guessing hardest, and the analytics then measure the guessing.
    description: Mapped[str] = mapped_column(Text, nullable=False)
    icon: Mapped[str | None] = mapped_column(String(48), nullable=True)
    colour: Mapped[str | None] = mapped_column(String(24), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ValueTag(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One shoutout, nomination or competency tagged to one value.

    A join table rather than a column on each taggable thing, because the
    analytics question is "which values are lived", and answering it from three
    different columns on three tables means every new taggable object is a
    change to the query rather than a row here.
    """

    __tablename__ = "value_tags"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source",
            "source_id",
            "value_code",
            name="uq_value_tag_once",
        ),
        Index("ix_value_tags_value", "tenant_id", "value_code"),
        Index("ix_value_tags_subject", "tenant_id", "subject_employee_id"),
    )

    source: Mapped[TagSourceEnum] = mapped_column(
        Enum(TagSourceEnum, name="value_tag_source"), nullable=False
    )
    #: The shoutout, nomination or competency-rating row.
    source_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    value_code: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Who the recognition was about, for the per-person evidence view.
    subject_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=True,
    )
    #: Denormalised at tagging time so a coverage report for "Engineering, Q2"
    #: does not move when somebody transfers in Q3.
    group_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)


# ══════════════════════════════════════════════════════════════════════════════
# Awards
# ══════════════════════════════════════════════════════════════════════════════


class AwardProgramme(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A recurring award: its categories, its jury, and who may be nominated."""

    __tablename__ = "award_programmes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_award_programme_name"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cadence: Mapped[AwardCadenceEnum] = mapped_column(
        Enum(AwardCadenceEnum, name="award_cadence"), nullable=False
    )
    #: `[{key, name, description}]`.
    categories: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    #: `[{key, name, weight}]` - what the jury scores each nomination on.
    criteria: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    #: Free-form rule, evaluated by a human. Deliberately not executable: an
    #: eligibility rule nobody can read is one nobody can challenge.
    eligibility_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Roles permitted to sit on a jury.
    jury_roles: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class AwardCycle(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One run of a programme, with its window and its announcement date."""

    __tablename__ = "award_cycles"
    __table_args__ = (
        UniqueConstraint(
            "programme_id", "label", name="uq_award_cycle_label"
        ),
        CheckConstraint(
            "nominations_close_on >= nominations_open_on",
            name="ck_award_window_ordered",
        ),
    )

    programme_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("award_programmes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    #: Both ends inclusive. A window advertised as closing on the 20th and
    #: closing at 00:00 on the 20th loses every nomination made on the day
    #: people were told they had.
    nominations_open_on: Mapped[date] = mapped_column(Date, nullable=False)
    nominations_close_on: Mapped[date] = mapped_column(Date, nullable=False)
    result_announcement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[AwardCycleStatusEnum] = mapped_column(
        Enum(AwardCycleStatusEnum, name="award_cycle_status"),
        default=AwardCycleStatusEnum.DRAFT,
        nullable=False,
    )
    #: Jurors for this cycle. A juror on last quarter's panel is not
    #: automatically on this one.
    juror_user_ids: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)


class AwardNomination(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One person put forward, with the case for them."""

    __tablename__ = "award_nominations"
    __table_args__ = (
        UniqueConstraint(
            "cycle_id",
            "nominee_employee_id",
            "nominator_employee_id",
            "category_key",
            name="uq_one_nomination_per_pair_per_category",
        ),
        CheckConstraint(
            "nominee_employee_id <> nominator_employee_id",
            name="ck_no_self_nomination",
        ),
        Index("ix_nominations_cycle", "tenant_id", "cycle_id", "category_key"),
    )

    cycle_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("award_cycles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    programme_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("award_programmes.id", ondelete="CASCADE"),
        nullable=False,
    )
    category_key: Mapped[str] = mapped_column(String(64), nullable=False)
    nominee_employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    nominator_employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Not nullable. A name with no case attached is a vote, and a vote is a
    #: popularity contest with a jury watching.
    citation: Mapped[str] = mapped_column(Text, nullable=False)
    #: `[{kind, id, label}]` pointing at shoutouts, goals or project records.
    supporting_evidence: Mapped[list] = mapped_column(
        JSONB, default=list, nullable=False
    )
    status: Mapped[NominationStatusEnum] = mapped_column(
        Enum(NominationStatusEnum, name="award_nomination_status"),
        default=NominationStatusEnum.SUBMITTED,
        nullable=False,
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class JuryScore(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One juror's assessment of one nomination.

    `is_submitted` is the anchoring gate. While it is false, no other juror may
    read this row — the server does not send it, which is the control. Hiding it
    in the UI is not.
    """

    __tablename__ = "jury_scores"
    __table_args__ = (
        UniqueConstraint(
            "nomination_id", "juror_user_id", name="uq_one_score_per_juror"
        ),
    )

    nomination_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("award_nominations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    juror_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: Criterion key to integer score.
    scores: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_submitted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AwardResult(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A win, recorded permanently rather than scrolling off a feed.

    This is what distinguishes an award from a shoutout at the record level: it
    is on the employee's profile in five years, when the feed post is gone.
    """

    __tablename__ = "award_results"
    __table_args__ = (
        UniqueConstraint(
            "cycle_id", "category_key", "employee_id", name="uq_one_win_per_category"
        ),
        Index("ix_award_results_employee", "tenant_id", "employee_id"),
    )

    cycle_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("award_cycles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    nomination_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("award_nominations.id", ondelete="SET NULL"),
        nullable=True,
    )
    programme_name: Mapped[str] = mapped_column(String(160), nullable=False)
    category_key: Mapped[str] = mapped_column(String(64), nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
    )
    citation: Mapped[str] = mapped_column(Text, nullable=False)
    awarded_on: Mapped[date] = mapped_column(Date, nullable=False)
    announced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# ══════════════════════════════════════════════════════════════════════════════
# Surveys
# ══════════════════════════════════════════════════════════════════════════════


class Survey(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One run of one instrument."""

    __tablename__ = "surveys"
    __table_args__ = (
        CheckConstraint(
            "min_responses_to_report >= 5", name="ck_survey_anonymity_floor"
        ),
        CheckConstraint("closes_on >= opens_on", name="ck_survey_window_ordered"),
        CheckConstraint(
            "resurvey_interval_days IS NULL OR resurvey_interval_days >= 30",
            name="ck_survey_resurvey_interval",
        ),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[SurveyKindEnum] = mapped_column(
        Enum(SurveyKindEnum, name="survey_kind"), nullable=False
    )
    #: Key into `services.culture.surveys.INSTRUMENTS`, or null for an ad-hoc
    #: question set - which is fine, and must never be reported as a validated
    #: instrument.
    instrument_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: For an ad-hoc survey: `[{key, text, reverse_scored, theme}]`.
    custom_items: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    opens_on: Mapped[date] = mapped_column(Date, nullable=False)
    closes_on: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[SurveyStatusEnum] = mapped_column(
        Enum(SurveyStatusEnum, name="survey_status"),
        default=SurveyStatusEnum.DRAFT,
        nullable=False,
    )
    #: A check constraint holds the floor at five. A tenant may raise it; the
    #: database will not let anybody lower it, because a threshold an admin can
    #: set to one is not a threshold.
    min_responses_to_report: Mapped[int] = mapped_column(
        Integer, default=5, nullable=False
    )
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Days after this wave closes that the next wave opens. Null means no
    #: re-survey is scheduled. `schedule_due_resurveys` reads it; nothing else
    #: decides when to ask again.
    resurvey_interval_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The wave this one re-surveys, so movement is computed between the two.
    previous_wave_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("surveys.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


class SurveyParticipant(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Who was invited and whether they have responded. **No answers.**

    This is the half a reminder needs. It carries the employee and the moment
    and nothing about what they said, and it shares no key with
    `survey_responses` beyond the survey itself — so joining the two tells you
    who answered and still not what they answered.
    """

    __tablename__ = "survey_participants"
    __table_args__ = (
        UniqueConstraint(
            "survey_id", "employee_id", name="uq_one_participant_row"
        ),
    )

    survey_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("surveys.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reminded_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class SurveyResponse(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One set of answers, with **no column that identifies who gave them**.

    Deliberately. An anonymous survey whose answers table carries an
    `employee_id` is anonymous by policy, and a policy is one SELECT away from
    not being anonymous. An invariant fails the build if a respondent column
    appears here.

    The group keys are the reporting dimensions and are denormalised at
    submission: a department reorganisation in November must not retrospectively
    move September's answers into a different team, and resolving them live
    would require the employee link this table exists not to have.
    """

    __tablename__ = "survey_responses"
    __table_args__ = (
        Index("ix_survey_responses_survey", "tenant_id", "survey_id"),
        Index("ix_survey_responses_group", "tenant_id", "survey_id", "group_key"),
    )

    survey_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("surveys.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Item key to integer answer.
    answers: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    free_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The team or department this response is reported under, captured at
    #: submission. Coarse by construction.
    group_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    department_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    location_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    submitted_on: Mapped[date] = mapped_column(Date, nullable=False)


# ══════════════════════════════════════════════════════════════════════════════
# Action plans
# ══════════════════════════════════════════════════════════════════════════════


class SurveyActionPlan(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """What one group committed to after seeing its results."""

    __tablename__ = "survey_action_plans"
    __table_args__ = (
        Index("ix_action_plans_scope", "tenant_id", "scope", "scope_key"),
    )

    survey_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("surveys.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scope: Mapped[PlanScopeEnum] = mapped_column(
        Enum(PlanScopeEnum, name="action_plan_scope"), nullable=False
    )
    scope_key: Mapped[str] = mapped_column(String(120), nullable=False)
    theme: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    #: A named person. "The team" owns nothing, and an item owned by nobody is
    #: the one still open next year.
    owner_employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[PlanStatusEnum] = mapped_column(
        Enum(PlanStatusEnum, name="action_plan_status"),
        default=PlanStatusEnum.DRAFT,
        nullable=False,
    )
    #: Append-only. A progress note rewritten is a progress note nobody trusts.
    progress_notes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    completed_on: Mapped[date | None] = mapped_column(Date, nullable=True)


class ActionPlanItem(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One thing somebody said they would do."""

    __tablename__ = "action_plan_items"
    __table_args__ = (
        Index("ix_action_items_owner", "tenant_id", "owner_employee_id"),
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("survey_action_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    owner_employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    done_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: An item that will not happen is dropped **with a reason**, which people
    #: can accept. An item left open while the plan is called done is what they
    #: cannot.
    dropped_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


# ══════════════════════════════════════════════════════════════════════════════
# Announcements
# ══════════════════════════════════════════════════════════════════════════════


class Announcement(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One published communication."""

    __tablename__ = "announcements"
    __table_args__ = (
        Index("ix_announcements_publish", "tenant_id", "publish_at"),
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: `{department_ids, location_ids, grades}`. All empty means everybody -
    #: unconfigured and nobody have to be different states.
    audience: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    publish_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    #: Null where acknowledgement is required: a register that disappears is not
    #: a register, and the audit question is asked months later.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    requires_acknowledgement: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    channels: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    #: SHA-256 of the title and body as published. An acknowledgement stores the
    #: hash it attested to, so an edit does not silently inherit signatures.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: When the audience was notified through the notification engine. Null
    #: until `publish_at` has passed and delivery ran - which is what stops a
    #: scheduled announcement being sent twice.
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: How many people with a login the delivery reached.
    delivered_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )


class AnnouncementAcknowledgement(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One person confirming they read one version of one announcement.

    The row is per version, not per announcement: `content_hash` records what
    was actually on screen. An edit invalidates it rather than inheriting it,
    which is the difference between a register and a claim.
    """

    __tablename__ = "announcement_acknowledgements"
    __table_args__ = (
        UniqueConstraint(
            "announcement_id",
            "employee_id",
            "content_hash",
            name="uq_ack_per_version",
        ),
        Index("ix_acks_announcement", "tenant_id", "announcement_id"),
    )

    announcement_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("announcements.id", ondelete="CASCADE"),
        nullable=False,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    acknowledged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Kept for an audit that asks how, not only whether.
    channel: Mapped[str | None] = mapped_column(String(32), nullable=True)


# ══════════════════════════════════════════════════════════════════════════════
# Events calendar
# ══════════════════════════════════════════════════════════════════════════════


class EventPrivacyPreference(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """What one employee has agreed to have announced.

    Both flags default to **False in the column**. Opt-in as a schema default
    rather than an application convention, because a convention is one careless
    `or True` away from broadcasting somebody's date of birth to the company.
    """

    __tablename__ = "event_privacy_preferences"
    __table_args__ = (
        UniqueConstraint("tenant_id", "employee_id", name="uq_event_privacy_person"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    share_birthday: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    share_work_anniversary: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    #: `team` or `company`. Some people are happy for their team to know and not
    #: the whole organisation.
    birthday_scope: Mapped[str] = mapped_column(
        String(16), default="team", nullable=False
    )


class CompanyEvent(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A holiday or company event on the shared calendar. Nobody's private data."""

    __tablename__ = "company_events"
    __table_args__ = (
        Index("ix_company_events_date", "tenant_id", "on_date"),
    )

    kind: Mapped[EventKindEnum] = mapped_column(
        Enum(EventKindEnum, name="calendar_event_kind"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    on_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: Same shape as an announcement audience. Empty means everybody.
    audience: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("locations.id", ondelete="SET NULL"),
        nullable=True,
    )
