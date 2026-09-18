"""Awards, values, surveys, action plans, announcements and celebrations (slice 32).

Two endpoints carry the weight of the slice:

- `GET /culture/surveys/{id}/results` withholds every group below the reporting
  threshold, **from everybody including HR**, and withholds a second group when
  it withholds one so the first cannot be recovered by subtraction.
- `GET /culture/nominations/{id}/jury` returns only what this juror may read.
  The filtering happens on the server; a response containing every score and a
  UI that renders part of it would leave the anchoring rule one template change
  away from gone.

Thin: parse, gate, delegate to `app.services.culture`, serialise. No body names
the caller's own employee record - the nominator, the respondent, the person
acknowledging and the person setting their privacy all come from the token.

`GET /culture/posture` says what is enforced and what is not built.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request, status

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.culture_depth import TagSourceEnum
from app.models.user import UserRole
from app.schemas.culture import (
    AcknowledgeCreate,
    ActionPlanComplete,
    ActionPlanCreate,
    ActionPlanItemUpdate,
    ActionPlanRead,
    AnnouncementCreate,
    AnnouncementManageRead,
    AnnouncementRead,
    AwardCycleCreate,
    AwardCycleRead,
    AwardCycleStatusUpdate,
    AwardProgrammeCreate,
    AwardProgrammeRead,
    AwardResultRead,
    AwardWinnerCreate,
    CompanyValueRead,
    CompanyValueUpsert,
    EventPrivacyRead,
    EventPrivacyUpdate,
    JuryScoreCreate,
    MySurveyRead,
    NominationCreate,
    NominationRead,
    ProgressNoteCreate,
    SurveyCreate,
    SurveyRead,
    SurveyResponseCreate,
    ValueTagCreate,
)
from app.services.culture import awards_portal, comms_portal, surveys_portal
from app.services.culture import service as culture
from app.services.culture import surveys as survey_engine

router = APIRouter(tags=["culture-depth"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)


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
    """The validated instruments, with the source and the reason for each."""
    return {
        "instruments": [i.as_dict() for i in survey_engine.INSTRUMENTS.values()],
        "min_responses": survey_engine.MIN_RESPONSES,
    }


# ── Values ──────────────────────────────────────────────────────────────────


@router.put("/culture/values", response_model=CompanyValueRead)
async def define_value(
    body: CompanyValueUpsert,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> CompanyValueRead:
    row = await culture.define_value(
        session,
        user,
        code=body.code,
        name=body.name.strip(),
        description=body.description.strip(),
        icon=body.icon,
        colour=body.colour,
        display_order=body.display_order,
        is_active=body.is_active,
        request=request,
    )
    return CompanyValueRead.model_validate(row)


@router.post("/culture/value-tags", status_code=status.HTTP_201_CREATED)
async def tag_value(
    body: ValueTagCreate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
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
        source=TagSourceEnum(body.source),
        source_id=body.source_id,
        value_code=body.value_code,
        subject_employee_id=body.subject_employee_id,
        occurred_on=body.occurred_on,
        group_key=body.group_key,
        request=request,
    )
    return {"tag_id": str(row.id), "value_code": row.value_code}


@router.get("/culture/values/analytics")
async def value_analytics(
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Which values are lived, counted per source - never per person."""
    return await culture.value_analytics(session, user)


# ── Awards ──────────────────────────────────────────────────────────────────


@router.get("/culture/award-programmes", response_model=list[AwardProgrammeRead])
async def list_programmes(session: DbSession, user: CurrentUser) -> list[AwardProgrammeRead]:
    return await awards_portal.list_programmes(session, user)


@router.post(
    "/culture/award-programmes",
    response_model=AwardProgrammeRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_programme(
    body: AwardProgrammeCreate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> AwardProgrammeRead:
    return await awards_portal.create_programme(session, user, body, request=request)


@router.get("/culture/award-cycles", response_model=list[AwardCycleRead])
async def list_cycles(session: DbSession, user: CurrentUser) -> list[AwardCycleRead]:
    return await awards_portal.list_cycles(session, user)


@router.post(
    "/culture/award-cycles",
    response_model=AwardCycleRead,
    status_code=status.HTTP_201_CREATED,
)
async def open_cycle(
    body: AwardCycleCreate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> AwardCycleRead:
    return await awards_portal.open_cycle(session, user, body, request=request)


@router.put("/culture/award-cycles/{cycle_id}/status", response_model=AwardCycleRead)
async def set_cycle_status(
    cycle_id: uuid.UUID,
    body: AwardCycleStatusUpdate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> AwardCycleRead:
    return await awards_portal.set_cycle_status(
        session, user, cycle_id, body.status, request=request
    )


@router.get("/culture/award-cycles/{cycle_id}/nominations", response_model=list[NominationRead])
async def list_nominations(
    cycle_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> list[NominationRead]:
    """Every nomination for the cycle's jurors and HR; your own otherwise."""
    return await awards_portal.list_nominations(session, user, cycle_id)


@router.get("/culture/award-cycles/{cycle_id}/standings")
async def standings(
    cycle_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    return await awards_portal.standings(session, user, cycle_id)


@router.post(
    "/culture/nominations", response_model=NominationRead, status_code=status.HTTP_201_CREATED
)
async def nominate(
    body: NominationCreate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> NominationRead:
    """Nominate a colleague in your own name, if the window is open today."""
    return await awards_portal.nominate(session, user, body, request=request)


@router.post("/culture/nominations/{nomination_id}/score")
async def score_nomination(
    nomination_id: uuid.UUID,
    body: JuryScoreCreate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    row = await culture.score_nomination(
        session,
        user,
        nomination_id=nomination_id,
        scores=body.scores,
        comments=(body.comments or "").strip() or None,
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
    """What this juror may read right now - filtered before it is built."""
    return await culture.jury_view(session, user, nomination_id=nomination_id)


@router.post(
    "/culture/award-results",
    response_model=AwardResultRead,
    status_code=status.HTTP_201_CREATED,
)
async def declare_winner(
    body: AwardWinnerCreate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> AwardResultRead:
    """Record a win permanently and announce it on the feed, in one transaction."""
    return await awards_portal.declare_winner(session, user, body, request=request)


@router.get("/culture/award-results", response_model=list[AwardResultRead])
async def list_results(session: DbSession, user: CurrentUser) -> list[AwardResultRead]:
    return await awards_portal.list_results(session, user)


# ── Surveys ─────────────────────────────────────────────────────────────────


@router.get("/culture/surveys/mine", response_model=list[MySurveyRead])
async def my_surveys(session: DbSession, user: CurrentUser) -> list[MySurveyRead]:
    return await surveys_portal.list_my_surveys(session, user)


@router.get("/culture/surveys", response_model=list[SurveyRead])
async def list_surveys(
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> list[SurveyRead]:
    return await surveys_portal.list_surveys(session, user)


@router.post("/culture/surveys", response_model=SurveyRead, status_code=status.HTTP_201_CREATED)
async def create_survey(
    body: SurveyCreate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> SurveyRead:
    return await surveys_portal.create_survey(session, user, body, request=request)


@router.post("/culture/surveys/resurvey-run")
async def run_resurveys(
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Open every re-survey wave that is due. The same function a scheduler
    would call; no scheduler runs in this deployment."""
    return await surveys_portal.schedule_due_resurveys(session, user.tenant_id)


@router.post("/culture/surveys/{survey_id}/responses", status_code=status.HTTP_201_CREATED)
async def submit_response(
    survey_id: uuid.UUID,
    body: SurveyResponseCreate,
    session: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """Answer a survey sent to you. The answers are stored with no link to you."""
    return await surveys_portal.respond(session, user, survey_id, body)


@router.post("/culture/surveys/{survey_id}/close", response_model=SurveyRead)
async def close_survey(
    survey_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> SurveyRead:
    return await surveys_portal.close_survey(session, user, survey_id, request=request)


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


@router.get("/culture/action-plans", response_model=list[ActionPlanRead])
async def list_plans(session: DbSession, user: CurrentUser) -> list[ActionPlanRead]:
    return await surveys_portal.list_plans(session, user)


@router.post(
    "/culture/action-plans", response_model=ActionPlanRead, status_code=status.HTTP_201_CREATED
)
async def create_plan(
    body: ActionPlanCreate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> ActionPlanRead:
    return await surveys_portal.create_plan(session, user, body, request=request)


@router.get("/culture/action-plans/loop")
async def loop_report(
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Per theme: was anything done, and did the next wave move."""
    return await culture.loop_report(session, user, as_of=date.today())


@router.patch("/culture/action-plan-items/{item_id}", response_model=ActionPlanRead)
async def update_item(
    item_id: uuid.UUID,
    body: ActionPlanItemUpdate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> ActionPlanRead:
    return await surveys_portal.update_item(session, user, item_id, body, request=request)


@router.post("/culture/action-plans/{plan_id}/notes", response_model=ActionPlanRead)
async def add_note(
    plan_id: uuid.UUID,
    body: ProgressNoteCreate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> ActionPlanRead:
    return await surveys_portal.add_note(session, user, plan_id, body, request=request)


@router.post("/culture/action-plans/{plan_id}/complete", response_model=ActionPlanRead)
async def complete_plan(
    plan_id: uuid.UUID,
    body: ActionPlanComplete,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> ActionPlanRead:
    """Complete a plan, if every item is done or explicitly dropped."""
    return await surveys_portal.complete_plan(session, user, plan_id, body, request=request)


# ── Announcements ───────────────────────────────────────────────────────────


@router.get("/culture/announcements", response_model=list[AnnouncementRead])
async def my_announcements(session: DbSession, user: CurrentUser) -> list[AnnouncementRead]:
    return await comms_portal.list_my_announcements(session, user)


@router.get("/culture/announcements/manage", response_model=list[AnnouncementManageRead])
async def managed_announcements(
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> list[AnnouncementManageRead]:
    return await comms_portal.list_managed(session, user)


@router.post(
    "/culture/announcements",
    response_model=AnnouncementManageRead,
    status_code=status.HTTP_201_CREATED,
)
async def announce(
    body: AnnouncementCreate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> AnnouncementManageRead:
    """Publish an announcement. Delivered to the audience now, or when its
    publish time arrives."""
    return await comms_portal.create_announcement(session, user, body, request=request)


@router.post("/culture/announcements/deliver-due")
async def deliver_due(
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Deliver scheduled announcements whose time has come. The same function
    a scheduler would call; no scheduler runs in this deployment."""
    return await culture.deliver_due_announcements(session, user.tenant_id)


@router.post(
    "/culture/announcements/{announcement_id}/acknowledge", response_model=AnnouncementRead
)
async def acknowledge(
    announcement_id: uuid.UUID,
    body: AcknowledgeCreate,
    session: DbSession,
    user: CurrentUser,
) -> AnnouncementRead:
    """Record that you read this version of this text."""
    return await comms_portal.acknowledge(session, user, announcement_id, body)


@router.get("/culture/announcements/{announcement_id}/register")
async def acknowledgement_register(
    announcement_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Who has acknowledged the current text, and who has not - by name."""
    return await comms_portal.acknowledgement_register(session, user, announcement_id)


# ── Celebrations ────────────────────────────────────────────────────────────


@router.get("/culture/event-privacy", response_model=EventPrivacyRead)
async def get_event_privacy(session: DbSession, user: CurrentUser) -> EventPrivacyRead:
    return await comms_portal.get_my_event_privacy(session, user)


@router.put("/culture/event-privacy", response_model=EventPrivacyRead)
async def set_event_privacy(
    body: EventPrivacyUpdate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> EventPrivacyRead:
    """Your own celebration sharing. Both default to off."""
    return await comms_portal.set_my_event_privacy(session, user, body, request=request)


@router.get("/culture/calendar")
async def calendar(
    session: DbSession,
    user: CurrentUser,
    from_date: date,
    to_date: date,
) -> dict[str, Any]:
    """Events in a window. Personal ones appear only where somebody opted in."""
    return await culture.calendar(session, user, from_date=from_date, to_date=to_date)
