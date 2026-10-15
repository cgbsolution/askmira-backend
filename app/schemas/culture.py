"""Culture contracts: shoutouts, clubs, values, celebrations, awards, surveys,
action plans and announcements (FRD 5.3, slice 32).

No contract here accepts the caller's own employee id. Who is sending, reacting,
joining, nominating, answering or acknowledging comes from the token; a body
that could name somebody else is a body that lets somebody act as them.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.culture import ShoutoutVisibility
from app.models.culture_depth import (
    AwardCadenceEnum,
    AwardCycleStatusEnum,
    NominationStatusEnum,
    PlanScopeEnum,
    PlanStatusEnum,
    SurveyKindEnum,
    SurveyStatusEnum,
)
from app.services.culture.surveys import MIN_RESPONSES

#: The reactions a shoutout accepts. A closed set, because a free-text emoji
#: field is a free-text field, and the feed is visible to the whole company.
ALLOWED_REACTIONS: tuple[str, ...] = ("👏", "❤️", "🎉", "🙌", "🚀", "😄")

_KEY = r"^[a-z0-9][a-z0-9_-]{0,63}$"


def _unique_keys(rows: list[Any], what: str) -> None:
    keys = [row.key for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError(f"Each {what} needs a distinct key.")


# ── People ──────────────────────────────────────────────────────────────────


class PersonRef(BaseModel):
    employee_id: uuid.UUID
    name: str
    photo_url: str | None
    designation: str | None


# ── Shoutouts ───────────────────────────────────────────────────────────────


class ShoutoutCreate(BaseModel):
    to_employee_id: uuid.UUID
    message: str = Field(min_length=1, max_length=2000)
    #: A company value code. Validated against the tenant's defined values.
    value_tag: str | None = Field(default=None, max_length=64)
    visibility: ShoutoutVisibility = ShoutoutVisibility.COMPANY

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A shoutout needs a message.")
        return value.strip()


class ShoutoutPin(BaseModel):
    is_pinned: bool


class ReactionSet(BaseModel):
    """Set the caller's reaction on or off. Idempotent: the same call twice
    leaves the same state and writes nothing the second time."""

    emoji: str
    active: bool

    @field_validator("emoji")
    @classmethod
    def _allowed(cls, value: str) -> str:
        if value not in ALLOWED_REACTIONS:
            raise ValueError(f"Reactions are limited to {' '.join(ALLOWED_REACTIONS)}.")
        return value


class ReactionSummary(BaseModel):
    emoji: str
    count: int
    #: Whether the caller is one of the people who reacted.
    reacted: bool


class AwardPostRef(BaseModel):
    result_id: uuid.UUID
    programme_name: str
    category_key: str
    category_name: str | None


class ShoutoutRead(BaseModel):
    id: uuid.UUID
    #: "peer" is a colleague's shoutout; "award" is an award result the
    #: company announced, and has no sender.
    kind: Literal["peer", "award", "milestone"]
    #: A work anniversary the company marked: how many years.
    milestone_years: int | None = None
    from_employee_id: uuid.UUID | None
    to_employee_id: uuid.UUID
    sender: PersonRef | None
    recipient: PersonRef
    message: str
    value_tag: str | None
    value_name: str | None
    visibility: ShoutoutVisibility
    is_pinned: bool
    points_awarded: int
    created_at: datetime
    reactions: list[ReactionSummary]
    award: AwardPostRef | None
    can_delete: bool
    can_pin: bool


# ── Clubs ───────────────────────────────────────────────────────────────────


class ClubCreate(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    icon: str | None = Field(default=None, max_length=16)
    is_private: bool = False

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if len(cleaned) < 2:
            raise ValueError("A club needs a name of at least two characters.")
        return cleaned


class ClubRead(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    icon: str | None
    is_private: bool
    member_count: int
    is_member: bool
    #: "member" or "organiser" when the caller belongs; otherwise None.
    my_role: str | None
    created_at: datetime


class ClubMemberRead(BaseModel):
    employee_id: uuid.UUID
    name: str
    photo_url: str | None
    designation: str | None
    role: str
    joined_at: datetime


class ClubParticipationRead(BaseModel):
    """Participation in aggregate. Never who joined what (FRD 5.3)."""

    active_clubs: int
    public_clubs: int
    private_clubs: int
    total_memberships: int
    distinct_members: int
    active_headcount: int
    #: Share of active headcount in at least one club, 0-1, or None with no
    #: headcount. A ratio, not money.
    participation_rate: float | None
    note: str


# ── Values ──────────────────────────────────────────────────────────────────


class CompanyValueRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    description: str
    icon: str | None
    colour: str | None
    display_order: int
    is_active: bool


class CompanyValueUpsert(BaseModel):
    code: str = Field(pattern=_KEY)
    name: str = Field(min_length=1, max_length=120)
    #: Required. A one-word value on a dropdown gets tagged by whoever is
    #: guessing hardest, and the analytics then measure the guessing.
    description: str = Field(min_length=1, max_length=2000)
    icon: str | None = Field(default=None, max_length=48)
    colour: str | None = Field(default=None, max_length=24)
    display_order: int = Field(default=0, ge=0, le=1000)
    is_active: bool = True


class ValueTagCreate(BaseModel):
    source: Literal["shoutout", "nomination", "competency"]
    source_id: uuid.UUID
    value_code: str = Field(min_length=1, max_length=64)
    subject_employee_id: uuid.UUID | None = None
    occurred_on: date
    group_key: str | None = Field(default=None, max_length=120)


# ── Celebrations ────────────────────────────────────────────────────────────


class EventPrivacyRead(BaseModel):
    share_birthday: bool
    share_work_anniversary: bool
    birthday_scope: Literal["team", "company"]
    #: False when no preference has ever been saved - which shares nothing.
    configured: bool


class EventPrivacyUpdate(BaseModel):
    #: Both default to False. Opt-in means the default is no.
    share_birthday: bool = False
    share_work_anniversary: bool = False
    birthday_scope: Literal["team", "company"] = "team"


# ── Awards ──────────────────────────────────────────────────────────────────


class AwardCategory(BaseModel):
    key: str = Field(pattern=_KEY)
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)


class AwardCriterion(BaseModel):
    key: str = Field(pattern=_KEY)
    name: str = Field(min_length=1, max_length=120)
    weight: int = Field(default=1, ge=1, le=10)


class AwardProgrammeCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    description: str | None = Field(default=None, max_length=4000)
    cadence: AwardCadenceEnum
    categories: list[AwardCategory] = Field(min_length=1, max_length=20)
    criteria: list[AwardCriterion] = Field(min_length=1, max_length=10)
    eligibility_rule: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _distinct(self) -> AwardProgrammeCreate:
        _unique_keys(self.categories, "category")
        _unique_keys(self.criteria, "criterion")
        return self


class AwardProgrammeRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    cadence: AwardCadenceEnum
    categories: list[dict[str, Any]]
    criteria: list[dict[str, Any]]
    eligibility_rule: str | None
    is_active: bool
    created_at: datetime


class AwardCycleCreate(BaseModel):
    programme_id: uuid.UUID
    label: str = Field(min_length=1, max_length=80)
    nominations_open_on: date
    nominations_close_on: date
    #: Chosen from the directory and resolved to logins on the server. An
    #: employee with no login cannot sit on a jury, and is refused by name.
    juror_employee_ids: list[uuid.UUID] = Field(min_length=1, max_length=25)
    result_announcement_date: date | None = None

    @model_validator(mode="after")
    def _ordered(self) -> AwardCycleCreate:
        if self.nominations_close_on < self.nominations_open_on:
            raise ValueError("Nominations cannot close before they open.")
        return self


class AwardCycleStatusUpdate(BaseModel):
    status: AwardCycleStatusEnum


class AwardCycleRead(BaseModel):
    id: uuid.UUID
    programme_id: uuid.UUID
    programme_name: str
    label: str
    nominations_open_on: date
    nominations_close_on: date
    result_announcement_date: date | None
    status: AwardCycleStatusEnum
    window_state: Literal["not_open", "open", "closed"]
    categories: list[dict[str, Any]]
    criteria: list[dict[str, Any]]
    is_juror: bool
    juror_count: int
    nomination_count: int


class EvidenceRef(BaseModel):
    """A pointer to a record, never a URL. A link from a nomination rendered on
    a jury screen is a link somebody else chose for the jury to click."""

    kind: Literal["shoutout", "goal", "project"]
    label: str = Field(min_length=1, max_length=200)
    id: uuid.UUID | None = None


class NominationCreate(BaseModel):
    cycle_id: uuid.UUID
    category_key: str = Field(min_length=1, max_length=64)
    nominee_employee_id: uuid.UUID
    #: Not nullable. A name with no case attached is a vote.
    citation: str = Field(min_length=10, max_length=4000)
    supporting_evidence: list[EvidenceRef] = Field(default_factory=list, max_length=10)
    value_codes: list[str] = Field(default_factory=list, max_length=3)


class NominationRead(BaseModel):
    id: uuid.UUID
    cycle_id: uuid.UUID
    category_key: str
    category_name: str | None
    nominee: PersonRef
    #: Only for HR and for the nominator themselves. A jury that knows who
    #: nominated is scoring the nominator's standing as much as the case.
    nominator: PersonRef | None
    citation: str
    supporting_evidence: list[dict[str, Any]]
    status: NominationStatusEnum
    submitted_at: datetime
    #: For a juror on the cycle: where their own score stands.
    my_score: Literal["none", "draft", "submitted"] | None


class JuryScoreCreate(BaseModel):
    scores: dict[str, int] = Field(min_length=1, max_length=10)
    comments: str | None = Field(default=None, max_length=4000)
    #: Once true, the score is final. Reopening it after reading the panel is
    #: the anchoring problem arriving by a different door.
    submit: bool = False

    @field_validator("scores")
    @classmethod
    def _range(cls, value: dict[str, int]) -> dict[str, int]:
        for key, score in value.items():
            if not 1 <= score <= 10:
                raise ValueError(f"Score for {key!r} must be between 1 and 10.")
        return value


class AwardWinnerCreate(BaseModel):
    nomination_id: uuid.UUID
    awarded_on: date | None = None


class AwardResultRead(BaseModel):
    id: uuid.UUID
    cycle_id: uuid.UUID
    programme_name: str
    category_key: str
    category_name: str | None
    employee: PersonRef
    citation: str
    awarded_on: date
    announced_at: datetime | None
    shoutout_id: uuid.UUID | None


# ── Surveys ─────────────────────────────────────────────────────────────────


class SurveyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    kind: SurveyKindEnum
    opens_on: date
    closes_on: date
    instrument_key: str | None = Field(default=None, max_length=64)
    #: Cannot go below the engine's floor. A threshold an admin can set to one
    #: is not a threshold.
    min_responses_to_report: int = Field(default=MIN_RESPONSES, ge=MIN_RESPONSES, le=500)
    #: Days after close that the next wave opens. Null: no re-survey.
    resurvey_interval_days: int | None = Field(default=None, ge=30, le=730)

    @model_validator(mode="after")
    def _ordered(self) -> SurveyCreate:
        if self.closes_on < self.opens_on:
            raise ValueError("A survey cannot close before it opens.")
        return self


class SurveyRead(BaseModel):
    id: uuid.UUID
    name: str
    kind: SurveyKindEnum
    instrument_key: str | None
    instrument_name: str | None
    opens_on: date
    closes_on: date
    status: SurveyStatusEnum
    min_responses_to_report: int
    resurvey_interval_days: int | None
    previous_wave_id: uuid.UUID | None
    #: Counts only. Who responded is never listed.
    invited: int
    responded: int
    created_at: datetime


class MySurveyRead(BaseModel):
    id: uuid.UUID
    name: str
    kind: SurveyKindEnum
    #: The instrument with its items, or None for an ad-hoc survey.
    instrument: dict[str, Any] | None
    opens_on: date
    closes_on: date
    status: SurveyStatusEnum
    is_open_now: bool
    has_responded: bool
    min_responses_to_report: int


class SurveyResponseCreate(BaseModel):
    answers: dict[str, int] = Field(min_length=1, max_length=100)
    free_text: str | None = Field(default=None, max_length=4000)
    #: The respondent agrees that this comment may go into an AI theme summary.
    comment_ai_ok: bool = False


# ── Action plans ────────────────────────────────────────────────────────────


class ActionPlanItemCreate(BaseModel):
    description: str = Field(min_length=1, max_length=2000)
    owner_employee_id: uuid.UUID
    due_date: date


class ActionPlanCreate(BaseModel):
    survey_id: uuid.UUID
    scope: PlanScopeEnum
    scope_key: str = Field(min_length=1, max_length=120)
    theme: str = Field(min_length=1, max_length=80)
    owner_employee_id: uuid.UUID
    due_date: date
    items: list[ActionPlanItemCreate] = Field(min_length=1, max_length=30)


class ActionPlanItemUpdate(BaseModel):
    """Resolve an item: done on a date, or dropped with a reason. One or the
    other - an item that is both is a record nobody can read."""

    done_on: date | None = None
    dropped_reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _exactly_one(self) -> ActionPlanItemUpdate:
        dropped = bool(self.dropped_reason and self.dropped_reason.strip())
        if (self.done_on is None) == (not dropped):
            raise ValueError("Mark the item done on a date, or drop it with a reason.")
        return self


class ProgressNoteCreate(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class ActionPlanComplete(BaseModel):
    completed_on: date | None = None


class ActionPlanItemRead(BaseModel):
    id: uuid.UUID
    description: str
    owner: PersonRef
    due_date: date
    done_on: date | None
    dropped_reason: str | None


class ActionPlanRead(BaseModel):
    id: uuid.UUID
    survey_id: uuid.UUID
    survey_name: str | None
    scope: PlanScopeEnum
    scope_key: str
    theme: str
    owner: PersonRef
    due_date: date
    status: PlanStatusEnum
    completed_on: date | None
    progress_notes: list[dict[str, Any]]
    items: list[ActionPlanItemRead]
    can_edit: bool


# ── Announcements ───────────────────────────────────────────────────────────


class AnnouncementAudience(BaseModel):
    """All empty means everybody. The dimensions are and-ed."""

    department_ids: list[str] = Field(default_factory=list, max_length=100)
    location_ids: list[str] = Field(default_factory=list, max_length=100)
    grades: list[str] = Field(default_factory=list, max_length=50)


class AnnouncementCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20000)
    #: Omitted means now.
    publish_at: datetime | None = None
    audience: AnnouncementAudience = Field(default_factory=AnnouncementAudience)
    expires_at: datetime | None = None
    requires_acknowledgement: bool = False
    #: Recorded as the author's intent. Which channels actually carry it is each
    #: recipient's notification preference, decided by the notification engine.
    channels: list[Literal["in_app", "email", "whatsapp", "push"]] = Field(
        default_factory=lambda: ["in_app"], min_length=1
    )


class AnnouncementRead(BaseModel):
    id: uuid.UUID
    title: str
    body: str
    publish_at: datetime
    expires_at: datetime | None
    requires_acknowledgement: bool
    #: Against the current text. An edit makes an earlier acknowledgement stale.
    acknowledged: bool
    acknowledged_at: datetime | None


class AnnouncementManageRead(BaseModel):
    id: uuid.UUID
    title: str
    body: str
    publish_at: datetime
    expires_at: datetime | None
    requires_acknowledgement: bool
    channels: list[str]
    audience: dict[str, Any]
    audience_description: str
    delivered_at: datetime | None
    delivered_count: int
    is_live: bool
    created_at: datetime


class AcknowledgeCreate(BaseModel):
    channel: Literal["in_app", "email", "whatsapp", "push"] | None = "in_app"


def slugify(name: str) -> str:
    """A URL-safe club slug from a display name."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:56] or "club"
