"""Awards, values, surveys, action plans and internal comms (slice 32).

Two endpoints carry the weight of the slice:

- `GET /culture/surveys/{id}/results` withholds every group below the reporting
  threshold, **from everybody including HR**, and withholds a second group when
  it withholds one so the first cannot be recovered by subtraction.
- `GET /culture/nominations/{id}/jury` returns only what this juror may read.
  The filtering happens on the server; a response containing every score and a
  UI that renders part of it would leave the anchoring rule one template change
  away from gone.

`GET /culture/posture` says what is enforced and what is not built.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.culture_depth import (
    PlanScopeEnum,
    SurveyKindEnum,
    TagSourceEnum,
)
from app.models.user import UserRole
from app.services.culture import service as culture
from app.services.culture import surveys as survey_engine

router = APIRouter(tags=["culture-depth"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)


# ── Contracts ───────────────────────────────────────────────────────────────


class ValueIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    #: Required. A one-word value on a dropdown gets tagged by whoever is
    #: guessing hardest, and the analytics then measure the guessing.
    description: str = Field(min_length=1, max_length=2000)
    icon: str | None = Field(default=None, max_length=48)
    colour: str | None = Field(default=None, max_length=24)


class TagIn(BaseModel):
    source: TagSourceEnum
    source_id: uuid.UUID
    value_code: str = Field(min_length=1, max_length=64)
    subject_employee_id: uuid.UUID | None = None
    occurred_on: date
    group_key: str | None = Field(default=None, max_length=120)


class CycleIn(BaseModel):
    programme_id: uuid.UUID
    label: str = Field(min_length=1, max_length=80)
    nominations_open_on: date
    nominations_close_on: date
    juror_user_ids: list[uuid.UUID] = Field(min_length=1)
    result_announcement_date: date | None = None


class NominationIn(BaseModel):
    cycle_id: uuid.UUID
    category_key: str = Field(min_length=1, max_length=64)
    nominee_employee_id: uuid.UUID
    nominator_employee_id: uuid.UUID
    citation: str = Field(min_length=1, max_length=4000)
    supporting_evidence: list[dict[str, Any]] = Field(default_factory=list)
    value_codes: list[str] = Field(default_factory=list)
    today: date | None = None


class ScoreIn(BaseModel):
    scores: dict[str, int]
    comments: str | None = Field(default=None, max_length=4000)
    #: Once true, the score is final. Reopening it after reading the panel is
    #: the anchoring problem arriving by a different door.
    submit: bool = False


class WinnerIn(BaseModel):
    nomination_id: uuid.UUID
    awarded_on: date


class SurveyIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    kind: SurveyKindEnum
    opens_on: date
    closes_on: date
    instrument_key: str | None = Field(default=None, max_length=64)
    #: Cannot go below the engine's floor. A threshold an admin can set to one
    #: is not a threshold.
    min_responses_to_report: int = Field(
        default=survey_engine.MIN_RESPONSES, ge=survey_engine.MIN_RESPONSES, le=500
    )


class ResponseIn(BaseModel):
    employee_id: uuid.UUID
    answers: dict[str, int]
    free_text: str | None = Field(default=None, max_length=4000)
    group_key: str | None = Field(default=None, max_length=120)
    submitted_on: date | None = None


class PlanItemIn(BaseModel):
    description: str = Field(min_length=1, max_length=2000)
    owner_employee_id: uuid.UUID
    due_date: date


class PlanIn(BaseModel):
    survey_id: uuid.UUID
    scope: PlanScopeEnum
    scope_key: str = Field(min_length=1, max_length=120)
    theme: str = Field(min_length=1, max_length=80)
    owner_employee_id: uuid.UUID
    due_date: date
    items: list[PlanItemIn] = Field(min_length=1)


class CompletePlanIn(BaseModel):
    completed_on: date


class AnnouncementIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1)
    publish_at: datetime
    audience: dict[str, list[str]] = Field(default_factory=dict)
    expires_at: datetime | None = None
    requires_acknowledgement: bool = False
    channels: list[str] = Field(default_factory=lambda: ["in_app"])


class AcknowledgeIn(BaseModel):
    employee_id: uuid.UUID
    channel: str | None = Field(default=None, max_length=32)


class PrivacyIn(BaseModel):
    employee_id: uuid.UUID
    #: Both default to False. Opt-in means the default is no.
    share_birthday: bool = False
    share_work_anniversary: bool = False
    birthday_scope: str = Field(default="team", pattern="^(team|company)$")


# ── Posture and instruments ─────────────────────────────────────────────────


@router.get("/culture/posture")
async def posture(user: CurrentUser) -> dict[str, Any]:
    """What this slice enforces, and what it does not.

    Not role-gated. An employee is entitled to know that their survey answers
    are stored without a link to them.
    """
    return culture.culture_posture()


@router.get("/culture/instruments")
async def instruments(user: CurrentUser) -> dict[str, Any]:
    """The validated instruments, with the source and the reason for each.

    Composed from published item structures; **not psychometrically validated
    on this population**, which the posture says too.
    """
    return {
        "instruments": [i.as_dict() for i in survey_engine.INSTRUMENTS.values()],
        "min_responses": survey_engine.MIN_RESPONSES,
    }


# ── Values ──────────────────────────────────────────────────────────────────


@router.put("/culture/values")
async def define_value(
    body: ValueIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    row = await culture.define_value(
        session,
        user,
        code=body.code,
        name=body.name,
        description=body.description,
        icon=body.icon,
        colour=body.colour,
        request=request,
    )
    return {"value_id": str(row.id), "code": row.code, "name": row.name}


@router.post("/culture/value-tags", status_code=status.HTTP_201_CREATED)
async def tag_value(
    body: TagIn,
    session: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    permissions.require_role(
        user,
        permissions.MANAGER_ROLES,
        detail=(
            "Values are tagged by managers and HR; colleagues recognise each "
            "other through shoutouts."
        ),
    )
    row = await culture.tag_value(
        session,
        user,
        source=body.source,
        source_id=body.source_id,
        value_code=body.value_code,
        subject_employee_id=body.subject_employee_id,
        occurred_on=body.occurred_on,
        group_key=body.group_key,
    )
    return {"tag_id": str(row.id), "value_code": row.value_code}


@router.get("/culture/values/analytics")
async def value_analytics(
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Which values are lived, and which are on a wall.

    Counted per source, never as a per-person score: recognition converted into
    a rating is a popularity metric wearing a value's name.
    """
    return await culture.value_analytics(session, user)


# ── Awards ──────────────────────────────────────────────────────────────────


@router.post("/culture/award-cycles", status_code=status.HTTP_201_CREATED)
async def open_cycle(
    body: CycleIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    row = await culture.open_cycle(
        session,
        user,
        programme_id=body.programme_id,
        label=body.label,
        nominations_open_on=body.nominations_open_on,
        nominations_close_on=body.nominations_close_on,
        juror_user_ids=body.juror_user_ids,
        result_announcement_date=body.result_announcement_date,
        request=request,
    )
    return {
        "cycle_id": str(row.id),
        "label": row.label,
        "nominations_open_on": row.nominations_open_on.isoformat(),
        "nominations_close_on": row.nominations_close_on.isoformat(),
        "status": row.status.value,
    }


@router.post("/culture/nominations", status_code=status.HTTP_201_CREATED)
async def nominate(
    body: NominationIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """Put somebody forward, if the window is open.

    The window is checked against the cycle's stored dates, not against a status
    flag - a status left open after the closing date would accept late entries
    and the jury would score a different field from the advertised one.
    """
    permissions.require_self_or_hr(
        user, body.nominator_employee_id, detail="You can only nominate in your own name."
    )
    row = await culture.nominate(
        session,
        user,
        cycle_id=body.cycle_id,
        category_key=body.category_key,
        nominee_employee_id=body.nominee_employee_id,
        nominator_employee_id=body.nominator_employee_id,
        citation=body.citation,
        supporting_evidence=body.supporting_evidence,
        value_codes=body.value_codes,
        today=body.today,
        request=request,
    )
    return {
        "nomination_id": str(row.id),
        "category_key": row.category_key,
        "status": row.status.value,
    }


@router.post("/culture/nominations/{nomination_id}/score")
async def score_nomination(
    nomination_id: uuid.UUID,
    body: ScoreIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    row = await culture.score_nomination(
        session,
        user,
        nomination_id=nomination_id,
        scores=body.scores,
        comments=body.comments,
        submit=body.submit,
        request=request,
    )
    return {
        "score_id": str(row.id),
        "is_submitted": row.is_submitted,
        "note": (
            "Submitted. You can now see the rest of the panel."
            if row.is_submitted
            else "Saved as a draft. No other juror can see it, and you cannot "
            "see theirs until you submit."
        ),
    }


@router.get("/culture/nominations/{nomination_id}/jury")
async def jury_view(
    nomination_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """What this juror may read right now.

    Filtered before the response is built. Reading another juror's score before
    writing your own measurably changes what you write, and five opinions that
    anchored on the first are one opinion with four signatures.
    """
    return await culture.jury_view(session, user, nomination_id=nomination_id)


@router.post("/culture/award-results", status_code=status.HTTP_201_CREATED)
async def declare_winner(
    body: WinnerIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Record a win permanently on the employee's record.

    Nothing posts it to the feed; the announcement is made by a person.
    """
    row = await culture.declare_winner(
        session,
        user,
        nomination_id=body.nomination_id,
        awarded_on=body.awarded_on,
        request=request,
    )
    return {
        "award_result_id": str(row.id),
        "employee_id": str(row.employee_id),
        "programme_name": row.programme_name,
        "category_key": row.category_key,
        "awarded_on": row.awarded_on.isoformat(),
    }


# ── Surveys ─────────────────────────────────────────────────────────────────


@router.post("/culture/surveys", status_code=status.HTTP_201_CREATED)
async def create_survey(
    body: SurveyIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    row = await culture.create_survey(
        session,
        user,
        name=body.name,
        kind=body.kind,
        opens_on=body.opens_on,
        closes_on=body.closes_on,
        instrument_key=body.instrument_key,
        min_responses_to_report=body.min_responses_to_report,
        request=request,
    )
    return {
        "survey_id": str(row.id),
        "name": row.name,
        "instrument_key": row.instrument_key,
        "min_responses_to_report": row.min_responses_to_report,
        "status": row.status.value,
    }


@router.post("/culture/surveys/{survey_id}/responses", status_code=201)
async def submit_response(
    survey_id: uuid.UUID,
    body: ResponseIn,
    session: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """Submit answers.

    The answers go to a table with no respondent column; a separate row records
    that this person responded, so reminders stop. The two share nothing but the
    survey.
    """
    permissions.require_self(
        user,
        body.employee_id,
        detail="A survey can only be answered by the person it was sent to.",
    )
    return await culture.submit_response(
        session,
        user,
        survey_id=survey_id,
        employee_id=body.employee_id,
        answers=body.answers,
        free_text=body.free_text,
        group_key=body.group_key,
        submitted_on=body.submitted_on,
    )


@router.post("/culture/surveys/{survey_id}/close")
async def close_survey(
    survey_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    row = await culture.close_survey(session, user, survey_id=survey_id)
    return {"survey_id": str(row.id), "status": row.status.value}


@router.get("/culture/surveys/{survey_id}/results")
async def survey_results(
    survey_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    group_by: str = "department_key",
) -> dict[str, Any]:
    """Results, with every group below the threshold withheld.

    Not gated to HR, and deliberately: there is no privileged view. The
    threshold applies to whoever asks, because a bypass for one role is how a
    manager eventually works out who said what.
    """
    return await culture.survey_results(
        session, user, survey_id=survey_id, group_by=group_by
    )


# ── Action plans ────────────────────────────────────────────────────────────


@router.post("/culture/action-plans", status_code=status.HTTP_201_CREATED)
async def create_plan(
    body: PlanIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    permissions.require_role(
        user,
        permissions.MANAGER_ROLES,
        detail="Action plans are created by managers and HR.",
    )
    row = await culture.create_plan(
        session,
        user,
        survey_id=body.survey_id,
        scope=body.scope,
        scope_key=body.scope_key,
        theme=body.theme,
        owner_employee_id=body.owner_employee_id,
        due_date=body.due_date,
        items=[i.model_dump() for i in body.items],
        request=request,
    )
    return {
        "plan_id": str(row.id),
        "theme": row.theme,
        "status": row.status.value,
        "item_count": len(body.items),
    }


@router.post("/culture/action-plans/{plan_id}/complete")
async def complete_plan(
    plan_id: uuid.UUID,
    body: CompletePlanIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """Complete a plan, if every item is done or explicitly dropped.

    Returns 400 naming what is still open. A plan completed with open items is
    a checkbox, and a checkbox is what the second year of a dying survey
    programme is made of.
    """
    row = await culture.complete_plan(
        session,
        user,
        plan_id=plan_id,
        completed_on=body.completed_on,
        request=request,
    )
    return {
        "plan_id": str(row.id),
        "status": row.status.value,
        "completed_on": row.completed_on.isoformat() if row.completed_on else None,
    }


@router.get("/culture/action-plans/loop")
async def loop_report(
    session: DbSession,
    user: CurrentUser,
    as_of: date | None = None,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Per theme: was anything done, and did the next wave move.

    Either half alone is misleading. Action with no movement is effort in the
    wrong place; movement with no action is something else that happened.
    """
    return await culture.loop_report(session, user, as_of=as_of or date.today())


# ── Announcements ───────────────────────────────────────────────────────────


@router.post("/culture/announcements", status_code=status.HTTP_201_CREATED)
async def announce(
    body: AnnouncementIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Publish an announcement. **Nothing is sent** - it is stored and shown."""
    row = await culture.announce(
        session,
        user,
        title=body.title,
        body=body.body,
        publish_at=body.publish_at,
        audience=body.audience,
        expires_at=body.expires_at,
        requires_acknowledgement=body.requires_acknowledgement,
        channels=body.channels,
        request=request,
    )
    return {
        "announcement_id": str(row.id),
        "content_hash": row.content_hash,
        "requires_acknowledgement": row.requires_acknowledgement,
        "delivery": (
            "Stored and visible in the app. No message is sent by this slice."
        ),
    }


@router.post("/culture/announcements/{announcement_id}/acknowledge")
async def acknowledge(
    announcement_id: uuid.UUID,
    body: AcknowledgeIn,
    session: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """Record that somebody read this version of this text."""
    permissions.require_self(
        user,
        body.employee_id,
        detail="Only the person an announcement was sent to can acknowledge it.",
    )
    return await culture.acknowledge(
        session,
        user,
        announcement_id=announcement_id,
        employee_id=body.employee_id,
        channel=body.channel,
    )


@router.get("/culture/announcements/{announcement_id}/register")
async def acknowledgement_register(
    announcement_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Who has acknowledged the current text, and who has not - by name.

    This is what a POSH or IT-policy audit asks for. An edit to the body
    invalidates the earlier acknowledgements rather than inheriting them, and
    those people appear as outstanding again.
    """
    return await culture.acknowledgement_register(
        session, user, announcement_id=announcement_id
    )


# ── Events calendar ─────────────────────────────────────────────────────────


@router.put("/culture/event-privacy")
async def set_event_privacy(
    body: PrivacyIn,
    session: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """Record what this person has agreed to have announced.

    Both flags default to off. A birthday is personal data and a feed post is a
    disclosure the employee did not choose.
    """
    permissions.require_self_or_hr(
        user, body.employee_id, detail="You can only change your own privacy settings."
    )
    row = await culture.set_event_privacy(
        session,
        user,
        employee_id=body.employee_id,
        share_birthday=body.share_birthday,
        share_work_anniversary=body.share_work_anniversary,
        birthday_scope=body.birthday_scope,
    )
    return {
        "employee_id": str(row.employee_id),
        "share_birthday": row.share_birthday,
        "share_work_anniversary": row.share_work_anniversary,
        "birthday_scope": row.birthday_scope,
    }


@router.get("/culture/calendar")
async def calendar(
    session: DbSession,
    user: CurrentUser,
    from_date: date,
    to_date: date,
) -> dict[str, Any]:
    """Events in a window. Personal ones appear only where somebody opted in."""
    return await culture.calendar(
        session, user, from_date=from_date, to_date=to_date
    )
