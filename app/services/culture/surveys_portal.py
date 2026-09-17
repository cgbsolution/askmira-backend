"""Surveys and action plans as people use them (slice 32).

A survey is sent to a person by a participant row. Only that person may answer
it, only while it is open, and only once. Results are aggregates suppressed
below the reporting floor (`service.survey_results`); nothing in this module
lists who responded.

`schedule_due_resurveys` is the scheduler-callable half: it opens the next wave
of every survey whose configured interval has passed. No scheduler exists in
this deployment, so HR calls it from the admin screen or an external cron calls
the endpoint.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.culture_depth import (
    ActionPlanItem,
    PlanStatusEnum,
    Survey,
    SurveyActionPlan,
    SurveyParticipant,
    SurveyStatusEnum,
)
from app.models.employee import Employee
from app.models.user import User
from app.schemas.culture import (
    ActionPlanComplete,
    ActionPlanCreate,
    ActionPlanItemRead,
    ActionPlanItemUpdate,
    ActionPlanRead,
    MySurveyRead,
    ProgressNoteCreate,
    SurveyCreate,
    SurveyRead,
    SurveyResponseCreate,
)
from app.services import audit
from app.services.culture import service as culture
from app.services.culture import surveys as survey_engine
from app.services.culture.feed import person_refs

__all__ = [
    "add_note",
    "close_survey",
    "complete_plan",
    "create_plan",
    "create_survey",
    "list_my_surveys",
    "list_plans",
    "list_surveys",
    "respond",
    "schedule_due_resurveys",
    "update_item",
]


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _instrument(key: str | None) -> dict[str, Any] | None:
    if not key:
        return None
    try:
        return survey_engine.instrument(key).as_dict()
    except survey_engine.SurveyError:
        return None


def _is_open_now(survey: Survey, today: date) -> bool:
    return (
        survey.status is SurveyStatusEnum.OPEN
        and survey.opens_on <= today <= survey.closes_on
    )


# ── Answering ───────────────────────────────────────────────────────────────


async def list_my_surveys(session: AsyncSession, user: User) -> list[MySurveyRead]:
    """Surveys sent to the caller, with whether they have answered."""
    me = permissions.own_employee_id(user)
    rows = (
        await session.execute(
            tenant_scoped(
                select(Survey, SurveyParticipant.responded_at)
                .join(SurveyParticipant, SurveyParticipant.survey_id == Survey.id)
                .where(
                    SurveyParticipant.employee_id == me,
                    Survey.status != SurveyStatusEnum.DRAFT,
                )
                .order_by(Survey.closes_on.desc()),
                Survey,
                user,
            )
        )
    ).all()
    today = date.today()
    return [
        MySurveyRead(
            id=survey.id,
            name=survey.name,
            kind=survey.kind,
            instrument=_instrument(survey.instrument_key),
            opens_on=survey.opens_on,
            closes_on=survey.closes_on,
            status=survey.status,
            is_open_now=_is_open_now(survey, today),
            has_responded=responded_at is not None,
            min_responses_to_report=survey.min_responses_to_report,
        )
        for survey, responded_at in rows
    ]


async def respond(
    session: AsyncSession, user: User, survey_id: uuid.UUID, payload: SurveyResponseCreate
) -> dict[str, Any]:
    """Answer a survey sent to you, once, while it is open.

    Nobody answers on anybody's behalf - HR included. The answers are stored
    without a link to the person (`service.submit_response`).
    """
    me = permissions.own_employee_id(user)
    survey = await session.scalar(
        tenant_scoped(select(Survey).where(Survey.id == survey_id), Survey, user)
    )
    if survey is None:
        raise _not_found("No such survey.")
    participant = await session.scalar(
        tenant_scoped(
            select(SurveyParticipant).where(
                SurveyParticipant.survey_id == survey.id,
                SurveyParticipant.employee_id == me,
            ),
            SurveyParticipant,
            user,
        )
    )
    if participant is None:
        raise permissions.forbidden(
            "This survey was not sent to you. A survey can only be answered by the "
            "people it was sent to."
        )
    if participant.responded_at is not None:
        raise _bad_request("You have already responded to this survey.")
    today = date.today()
    if not _is_open_now(survey, today):
        raise _bad_request(
            f"This survey is not open for responses. It runs from "
            f"{survey.opens_on.isoformat()} to {survey.closes_on.isoformat()}."
        )
    if not survey.instrument_key:
        raise _bad_request("This survey has no questions to answer.")

    keys = {item.key for item in survey_engine.instrument(survey.instrument_key).items}
    unknown = sorted(set(payload.answers) - keys)
    if unknown:
        raise _bad_request(f"{unknown} are not questions in this survey.")
    missing = sorted(keys - set(payload.answers))
    if missing:
        raise _bad_request("Answer every question before submitting.")

    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == me), Employee, user)
    )
    return await culture.submit_response(
        session,
        user,
        survey_id=survey.id,
        employee_id=me,
        answers=dict(payload.answers),
        free_text=(payload.free_text or "").strip() or None,
        # The team is the manager's reporting group. Coarse by construction,
        # and suppressed below the floor like every other group.
        group_key=str(employee.manager_id) if employee and employee.manager_id else None,
    )


# ── Running the programme (HR) ──────────────────────────────────────────────


def _survey_read(survey: Survey, invited: int, responded: int) -> SurveyRead:
    instrument = _instrument(survey.instrument_key)
    return SurveyRead(
        id=survey.id,
        name=survey.name,
        kind=survey.kind,
        instrument_key=survey.instrument_key,
        instrument_name=instrument["name"] if instrument else None,
        opens_on=survey.opens_on,
        closes_on=survey.closes_on,
        status=survey.status,
        min_responses_to_report=survey.min_responses_to_report,
        resurvey_interval_days=survey.resurvey_interval_days,
        previous_wave_id=survey.previous_wave_id,
        invited=invited,
        responded=responded,
        created_at=survey.created_at,
    )


async def _counts(
    session: AsyncSession, user: User, ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, int]]:
    if not ids:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(
                select(
                    SurveyParticipant.survey_id,
                    func.count(SurveyParticipant.id),
                    func.count(SurveyParticipant.responded_at),
                )
                .where(SurveyParticipant.survey_id.in_(ids))
                .group_by(SurveyParticipant.survey_id),
                SurveyParticipant,
                user,
            )
        )
    ).all()
    return {survey_id: (invited, responded) for survey_id, invited, responded in rows}


async def list_surveys(session: AsyncSession, user: User) -> list[SurveyRead]:
    permissions.require_hr(user, detail="The survey programme is run by HR.")
    surveys = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(Survey).order_by(Survey.opens_on.desc(), Survey.name), Survey, user
                )
            )
        ).all()
    )
    counts = await _counts(session, user, [s.id for s in surveys])
    return [_survey_read(s, *counts.get(s.id, (0, 0))) for s in surveys]


async def create_survey(
    session: AsyncSession, user: User, payload: SurveyCreate, *, request: Request | None = None
) -> SurveyRead:
    permissions.require_hr(user, detail="Surveys are created by HR.")
    row = await culture.create_survey(
        session,
        user,
        name=payload.name.strip(),
        kind=payload.kind,
        opens_on=payload.opens_on,
        closes_on=payload.closes_on,
        instrument_key=payload.instrument_key,
        min_responses_to_report=payload.min_responses_to_report,
        resurvey_interval_days=payload.resurvey_interval_days,
        request=request,
    )
    counts = await _counts(session, user, [row.id])
    return _survey_read(row, *counts.get(row.id, (0, 0)))


async def close_survey(
    session: AsyncSession, user: User, survey_id: uuid.UUID, *, request: Request | None = None
) -> SurveyRead:
    permissions.require_hr(user, detail="Surveys are closed by HR.")
    row = await culture.close_survey(session, user, survey_id=survey_id, request=request)
    counts = await _counts(session, user, [row.id])
    return _survey_read(row, *counts.get(row.id, (0, 0)))


async def schedule_due_resurveys(
    session: AsyncSession, tenant_id: uuid.UUID, *, today: date | None = None
) -> dict[str, Any]:
    """Scheduler-callable: open the next wave of every survey that is due.

    Due means the wave has closed, it has a `resurvey_interval_days`, the
    interval has passed since it closed, and no later wave exists yet. The new
    wave has the same instrument, threshold and interval, runs for as long as
    the last one did, links back through `previous_wave_id`, and is sent to
    everybody currently employed. Idempotent: a wave with a successor is never
    re-surveyed twice.
    """
    actor = culture.SystemActor(tenant_id=tenant_id)
    as_of = today or date.today()
    candidates = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(Survey).where(
                        Survey.resurvey_interval_days.is_not(None), Survey.closes_on < as_of
                    ),
                    Survey,
                    actor,
                )
            )
        ).all()
    )
    followed = set()
    if candidates:
        followed = set(
            (
                await session.scalars(
                    tenant_scoped(
                        select(Survey.previous_wave_id).where(
                            Survey.previous_wave_id.in_([c.id for c in candidates])
                        ),
                        Survey,
                        actor,
                    )
                )
            ).all()
        )

    created: list[str] = []
    for survey in candidates:
        interval = survey.resurvey_interval_days or 0
        if survey.id in followed or survey.closes_on + timedelta(days=interval) > as_of:
            continue
        if survey.status is SurveyStatusEnum.OPEN:
            survey.status = SurveyStatusEnum.CLOSED
            await audit.record(
                session,
                tenant_id=tenant_id,
                action=AuditAction.UPDATE,
                entity_type="survey",
                entity_id=survey.id,
                actor=None,
                actor_type="system",
                summary=f"Survey {survey.name!r} closed on schedule ahead of its re-survey.",
            )
        length = survey.closes_on - survey.opens_on
        wave = await culture.create_survey(
            session,
            actor,
            name=f"{survey.name} - re-survey {as_of:%b %Y}"[:160],
            kind=survey.kind,
            opens_on=as_of,
            closes_on=as_of + length,
            instrument_key=survey.instrument_key,
            min_responses_to_report=survey.min_responses_to_report,
            resurvey_interval_days=survey.resurvey_interval_days,
            previous_wave_id=survey.id,
        )
        created.append(str(wave.id))

    await session.commit()
    return {
        "as_of": as_of.isoformat(),
        "created": len(created),
        "survey_ids": created,
        "note": (
            f"{len(created)} re-survey wave(s) opened."
            if created
            else "No survey was due for a re-survey."
        ),
    }


# ── Action plans ────────────────────────────────────────────────────────────


async def _plan_reads(
    session: AsyncSession, user: User, plans: list[SurveyActionPlan]
) -> list[ActionPlanRead]:
    if not plans:
        return []
    items = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(ActionPlanItem)
                    .where(ActionPlanItem.plan_id.in_([p.id for p in plans]))
                    .order_by(ActionPlanItem.due_date, ActionPlanItem.created_at),
                    ActionPlanItem,
                    user,
                )
            )
        ).all()
    )
    survey_names = {
        survey_id: name
        for survey_id, name in (
            await session.execute(
                tenant_scoped(
                    select(Survey.id, Survey.name).where(
                        Survey.id.in_({p.survey_id for p in plans})
                    ),
                    Survey,
                    user,
                )
            )
        ).all()
    }
    people = await person_refs(
        session,
        user,
        {p.owner_employee_id for p in plans} | {i.owner_employee_id for i in items},
    )
    by_plan: dict[uuid.UUID, list[ActionPlanItem]] = {}
    for item in items:
        by_plan.setdefault(item.plan_id, []).append(item)
    is_hr = permissions.is_hr(user)
    me = user.employee_id
    return [
        ActionPlanRead(
            id=plan.id,
            survey_id=plan.survey_id,
            survey_name=survey_names.get(plan.survey_id),
            scope=plan.scope,
            scope_key=plan.scope_key,
            theme=plan.theme,
            owner=people[plan.owner_employee_id],
            due_date=plan.due_date,
            status=plan.status,
            completed_on=plan.completed_on,
            progress_notes=list(plan.progress_notes or []),
            items=[
                ActionPlanItemRead(
                    id=item.id,
                    description=item.description,
                    owner=people[item.owner_employee_id],
                    due_date=item.due_date,
                    done_on=item.done_on,
                    dropped_reason=item.dropped_reason,
                )
                for item in by_plan.get(plan.id, [])
            ],
            can_edit=is_hr or (me is not None and plan.owner_employee_id == me),
        )
        for plan in plans
    ]


async def list_plans(session: AsyncSession, user: User) -> list[ActionPlanRead]:
    """Every plan for HR; otherwise plans you own or hold an item in."""
    stmt = select(SurveyActionPlan).order_by(SurveyActionPlan.due_date)
    if not permissions.is_hr(user):
        if user.employee_id is None:
            return []
        holding = tenant_scoped(
            select(ActionPlanItem.plan_id).where(
                ActionPlanItem.owner_employee_id == user.employee_id
            ),
            ActionPlanItem,
            user,
        )
        stmt = stmt.where(
            or_(
                SurveyActionPlan.owner_employee_id == user.employee_id,
                SurveyActionPlan.id.in_(holding),
            )
        )
    plans = list((await session.scalars(tenant_scoped(stmt, SurveyActionPlan, user))).all())
    return await _plan_reads(session, user, plans)


async def create_plan(
    session: AsyncSession, user: User, payload: ActionPlanCreate, *, request: Request | None = None
) -> ActionPlanRead:
    permissions.require_role(
        user, permissions.MANAGER_ROLES, detail="Action plans are created by managers and HR."
    )
    survey = await session.scalar(
        tenant_scoped(select(Survey).where(Survey.id == payload.survey_id), Survey, user)
    )
    if survey is None:
        raise _not_found("No such survey.")
    owners = {payload.owner_employee_id} | {i.owner_employee_id for i in payload.items}
    found = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(Employee.id).where(
                        Employee.id.in_(owners), Employee.deleted_at.is_(None)
                    ),
                    Employee,
                    user,
                )
            )
        ).all()
    )
    if owners - found:
        raise _not_found("An owner named in this plan is not a current colleague.")

    row = await culture.create_plan(
        session,
        user,
        survey_id=survey.id,
        scope=payload.scope,
        scope_key=payload.scope_key.strip(),
        theme=payload.theme.strip(),
        owner_employee_id=payload.owner_employee_id,
        due_date=payload.due_date,
        items=[i.model_dump() for i in payload.items],
        request=request,
    )
    return (await _plan_reads(session, user, [row]))[0]


async def _plan(session: AsyncSession, user: User, plan_id: uuid.UUID) -> SurveyActionPlan:
    plan = await session.scalar(
        tenant_scoped(
            select(SurveyActionPlan).where(SurveyActionPlan.id == plan_id),
            SurveyActionPlan,
            user,
        )
    )
    if plan is None:
        raise _not_found("No such action plan.")
    return plan


async def update_item(
    session: AsyncSession,
    user: User,
    item_id: uuid.UUID,
    payload: ActionPlanItemUpdate,
    *,
    request: Request | None = None,
) -> ActionPlanRead:
    """Resolve an item: done on a date, or dropped with a reason."""
    item = await session.scalar(
        tenant_scoped(
            select(ActionPlanItem).where(ActionPlanItem.id == item_id), ActionPlanItem, user
        )
    )
    if item is None:
        raise _not_found("No such action item.")
    plan = await _plan(session, user, item.plan_id)
    owns = user.employee_id is not None and user.employee_id in (
        plan.owner_employee_id,
        item.owner_employee_id,
    )
    if not (permissions.is_hr(user) or owns):
        raise permissions.forbidden(
            "Only the item's owner, the plan's owner or HR can update this item."
        )
    if plan.status in (PlanStatusEnum.COMPLETED, PlanStatusEnum.DROPPED):
        raise _bad_request(f"This plan is {plan.status.value}; its items are closed.")

    dropped = (payload.dropped_reason or "").strip() or None
    item.done_on = payload.done_on
    item.dropped_reason = dropped
    if plan.status is PlanStatusEnum.COMMITTED:
        plan.status = PlanStatusEnum.IN_PROGRESS
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="action_plan_item",
        entity_id=item.id,
        actor=user,
        summary=(
            f"Action item dropped: {dropped}"
            if dropped
            else f"Action item done on {payload.done_on.isoformat() if payload.done_on else ''}."
        ),
        request=request,
    )
    await session.commit()
    return (await _plan_reads(session, user, [plan]))[0]


async def add_note(
    session: AsyncSession,
    user: User,
    plan_id: uuid.UUID,
    payload: ProgressNoteCreate,
    *,
    request: Request | None = None,
) -> ActionPlanRead:
    """Append a progress note. Notes are append-only; nothing edits one."""
    plan = await _plan(session, user, plan_id)
    if not (
        permissions.is_hr(user)
        or (user.employee_id is not None and plan.owner_employee_id == user.employee_id)
    ):
        raise permissions.forbidden("Only the plan's owner or HR can add a progress note.")
    plan.progress_notes = [
        *(plan.progress_notes or []),
        {
            "at": datetime.now(UTC).isoformat(),
            "by": user.full_name,
            "note": payload.note.strip(),
        },
    ]
    if plan.status is PlanStatusEnum.COMMITTED:
        plan.status = PlanStatusEnum.IN_PROGRESS
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="survey_action_plan",
        entity_id=plan.id,
        actor=user,
        summary=f"Progress note added to the {plan.theme!r} plan.",
        request=request,
    )
    await session.commit()
    return (await _plan_reads(session, user, [plan]))[0]


async def complete_plan(
    session: AsyncSession,
    user: User,
    plan_id: uuid.UUID,
    payload: ActionPlanComplete,
    *,
    request: Request | None = None,
) -> ActionPlanRead:
    plan = await _plan(session, user, plan_id)
    if not (
        permissions.is_hr(user)
        or (user.employee_id is not None and plan.owner_employee_id == user.employee_id)
    ):
        raise permissions.forbidden("Only the plan's owner or HR can mark it complete.")
    row = await culture.complete_plan(
        session,
        user,
        plan_id=plan_id,
        completed_on=payload.completed_on or date.today(),
        request=request,
    )
    return (await _plan_reads(session, user, [row]))[0]
