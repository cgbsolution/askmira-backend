"""The DB-facing half of culture depth: awards, values, surveys, plans, comms.

The scoring, the anonymity threshold, the anchoring rule, the completion gate
and the acknowledgement hashing all live in the pure modules beside this. An
invariant checks the isolation.

═══════════════════════════════════════════════════════════════════════════════
THE TWO PLACES WHERE THIS LAYER IS THE CONTROL, NOT THE ENGINE
═══════════════════════════════════════════════════════════════════════════════

1. **`jury_view` never loads another juror's unsubmitted score.** The engine
   filters what it is given; if this layer handed it everything and trusted the
   caller to render only part, the anchoring rule would be a UI convention.

2. **`submit_response` writes to `survey_responses`, which has no respondent
   column at all**, and separately marks `survey_participants.responded_at`. The
   two rows are written in one transaction and share nothing but the survey, so
   there is no query that joins an answer back to a person.

**What is and is not wired**, stated once here:

- Announcements are stored and their audience is resolved; **nothing is sent**.
  Slice 05's notification engine is not called from here, so WhatsApp delivery
  to non-desk staff is a plan rather than a fact.
- A winner is recorded in `award_results` and **nothing posts it to the feed**.
- Nothing schedules a re-survey. `movement` is computed when two waves exist.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.culture_depth import (
    ActionPlanItem,
    Announcement,
    AnnouncementAcknowledgement,
    AwardCycle,
    AwardCycleStatusEnum,
    AwardNomination,
    AwardProgramme,
    AwardResult,
    CompanyEvent,
    CompanyValue,
    EventPrivacyPreference,
    JuryScore,
    NominationStatusEnum,
    PlanScopeEnum,
    PlanStatusEnum,
    Survey,
    SurveyActionPlan,
    SurveyParticipant,
    SurveyResponse,
    SurveyStatusEnum,
    TagSourceEnum,
    ValueTag,
)
from app.models.employee import Employee
from app.models.user import User
from app.services import audit
from app.services.culture import actionplans as plan_engine
from app.services.culture import awards as award_engine
from app.services.culture import comms as comms_engine
from app.services.culture import surveys as survey_engine
from app.services.culture import values as values_engine

__all__ = [
    "acknowledge",
    "acknowledgement_register",
    "announce",
    "calendar",
    "close_survey",
    "complete_plan",
    "create_plan",
    "create_survey",
    "culture_posture",
    "declare_winner",
    "define_value",
    "jury_view",
    "loop_report",
    "nominate",
    "open_cycle",
    "score_nomination",
    "set_event_privacy",
    "submit_response",
    "survey_results",
    "tag_value",
    "value_analytics",
]


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


# ══════════════════════════════════════════════════════════════════════════════
# Values
# ══════════════════════════════════════════════════════════════════════════════


async def define_value(
    session: AsyncSession,
    user: User,
    *,
    code: str,
    name: str,
    description: str,
    icon: str | None = None,
    colour: str | None = None,
    request: Request | None = None,
) -> CompanyValue:
    """Define or update a company value.

    `description` is required by the engine and by the column. A one-word value
    on a dropdown gets tagged by whoever is guessing hardest, and the analytics
    then measure the guessing.
    """
    try:
        values_engine.ValueDefinition(
            code=code, name=name, description=description, icon=icon or "",
            colour=colour or "",
        )
    except values_engine.ValuesError as exc:
        raise _bad_request(str(exc)) from exc

    stmt = tenant_scoped(
        select(CompanyValue).where(CompanyValue.code == code), CompanyValue, user
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = CompanyValue(
            tenant_id=user.tenant_id, code=code, name=name, description=description
        )
        session.add(row)
    else:
        row.name = name
        row.description = description
    row.icon = icon
    row.colour = colour

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="company_value",
        entity_id=row.id,
        actor=user,
        summary=f"Company value {code!r} defined as {name!r}.",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def _value_definitions(
    session: AsyncSession, user: User
) -> list[values_engine.ValueDefinition]:
    stmt = tenant_scoped(
        select(CompanyValue).where(CompanyValue.is_active.is_(True)),
        CompanyValue,
        user,
    )
    return [
        values_engine.ValueDefinition(
            code=row.code,
            name=row.name,
            description=row.description,
            icon=row.icon or "",
            colour=row.colour or "",
        )
        for row in (await session.execute(stmt)).scalars()
    ]


async def tag_value(
    session: AsyncSession,
    user: User,
    *,
    source: TagSourceEnum,
    source_id: uuid.UUID,
    value_code: str,
    subject_employee_id: uuid.UUID | None,
    occurred_on: date,
    group_key: str | None = None,
) -> ValueTag:
    """Tag a shoutout, nomination or competency to a value."""
    known = {v.code for v in await _value_definitions(session, user)}
    if value_code not in known:
        raise _bad_request(
            f"{value_code!r} is not one of this tenant's values ({sorted(known)}). "
            "A tally over codes nobody defined is a tally of typos."
        )

    row = ValueTag(
        tenant_id=user.tenant_id,
        source=source,
        source_id=source_id,
        value_code=value_code,
        subject_employee_id=subject_employee_id,
        occurred_on=occurred_on,
        group_key=group_key,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _tagged_items(
    session: AsyncSession, user: User
) -> list[values_engine.TaggedItem]:
    stmt = tenant_scoped(select(ValueTag), ValueTag, user)
    return [
        values_engine.TaggedItem(
            item_id=str(row.source_id),
            source=values_engine.TagSource(row.source.value),
            value_code=row.value_code,
            subject_id=str(row.subject_employee_id) if row.subject_employee_id else "",
            occurred_on=row.occurred_on,
            group_key=row.group_key or "",
        )
        for row in (await session.execute(stmt)).scalars()
    ]


async def value_analytics(
    session: AsyncSession,
    user: User,
    *,
    group_sizes: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Which values are lived, and which are on a wall.

    Never a per-person score: a value tally converted into a rating is a
    popularity metric wearing a value's name.
    """
    definitions = await _value_definitions(session, user)
    if not definitions:
        raise _bad_request(
            "This tenant has no company values defined yet, so there is nothing "
            "for recognition to be measured against."
        )
    items = await _tagged_items(session, user)
    try:
        report = values_engine.coverage(items, values=definitions)
        unlived = values_engine.unlived_values(items, values=definitions)
        groups = (
            values_engine.by_group(
                items, values=definitions, group_sizes=group_sizes
            )
            if group_sizes
            else None
        )
    except values_engine.ValuesError as exc:
        raise _bad_request(str(exc)) from exc

    return {"coverage": report, "unlived": unlived, "by_group": groups}


# ══════════════════════════════════════════════════════════════════════════════
# Awards
# ══════════════════════════════════════════════════════════════════════════════


async def open_cycle(
    session: AsyncSession,
    user: User,
    *,
    programme_id: uuid.UUID,
    label: str,
    nominations_open_on: date,
    nominations_close_on: date,
    juror_user_ids: list[uuid.UUID],
    result_announcement_date: date | None = None,
    request: Request | None = None,
) -> AwardCycle:
    stmt = tenant_scoped(
        select(AwardProgramme).where(AwardProgramme.id == programme_id),
        AwardProgramme,
        user,
    )
    programme = (await session.execute(stmt)).scalar_one_or_none()
    if programme is None:
        raise _not_found("No such award programme in this tenant.")
    if not juror_user_ids:
        raise _bad_request(
            "A cycle with no jury cannot adjudicate anything, and an award "
            "nobody adjudicated is a shoutout with a certificate."
        )
    try:
        award_engine.NominationWindow(
            opens_on=nominations_open_on, closes_on=nominations_close_on
        )
    except award_engine.AwardError as exc:
        raise _bad_request(str(exc)) from exc

    row = AwardCycle(
        tenant_id=user.tenant_id,
        programme_id=programme_id,
        label=label,
        nominations_open_on=nominations_open_on,
        nominations_close_on=nominations_close_on,
        result_announcement_date=result_announcement_date,
        status=AwardCycleStatusEnum.NOMINATIONS_OPEN,
        juror_user_ids=[str(x) for x in juror_user_ids],
    )
    session.add(row)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="award_cycle",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Award cycle {label!r} open "
            f"{nominations_open_on.isoformat()} to "
            f"{nominations_close_on.isoformat()}, {len(juror_user_ids)} juror(s)."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def nominate(
    session: AsyncSession,
    user: User,
    *,
    cycle_id: uuid.UUID,
    category_key: str,
    nominee_employee_id: uuid.UUID,
    nominator_employee_id: uuid.UUID,
    citation: str,
    supporting_evidence: list[dict[str, Any]] | None = None,
    value_codes: list[str] | None = None,
    today: date | None = None,
    request: Request | None = None,
) -> AwardNomination:
    """Put somebody forward, if the window is open.

    The window is checked here against the cycle's stored dates, not against a
    flag somebody set — a status left on `nominations_open` after the closing
    date would otherwise accept late entries and the jury would score a
    different field from the one everybody was told about.
    """
    stmt = tenant_scoped(
        select(AwardCycle).where(AwardCycle.id == cycle_id), AwardCycle, user
    )
    cycle = (await session.execute(stmt)).scalar_one_or_none()
    if cycle is None:
        raise _not_found("No such award cycle in this tenant.")

    window = award_engine.NominationWindow(
        opens_on=cycle.nominations_open_on, closes_on=cycle.nominations_close_on
    )
    try:
        award_engine.check_window_open(window, today=today or date.today())
        award_engine.Nomination(
            nomination_id="pending",
            nominee_id=str(nominee_employee_id),
            nominator_id=str(nominator_employee_id),
            category_key=category_key,
            citation=citation,
        )
    except award_engine.AwardError as exc:
        raise _bad_request(str(exc)) from exc

    programme_stmt = tenant_scoped(
        select(AwardProgramme).where(AwardProgramme.id == cycle.programme_id),
        AwardProgramme,
        user,
    )
    programme = (await session.execute(programme_stmt)).scalar_one_or_none()
    if programme is None:
        raise _not_found("The programme for this cycle is gone.")
    categories = {c.get("key") for c in (programme.categories or [])}
    if categories and category_key not in categories:
        raise _bad_request(
            f"{category_key!r} is not a category of this programme "
            f"({sorted(c for c in categories if c)})."
        )

    row = AwardNomination(
        tenant_id=user.tenant_id,
        cycle_id=cycle_id,
        programme_id=cycle.programme_id,
        category_key=category_key,
        nominee_employee_id=nominee_employee_id,
        nominator_employee_id=nominator_employee_id,
        citation=citation,
        supporting_evidence=list(supporting_evidence or []),
        status=NominationStatusEnum.SUBMITTED,
        submitted_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()

    for code in value_codes or []:
        session.add(
            ValueTag(
                tenant_id=user.tenant_id,
                source=TagSourceEnum.NOMINATION,
                source_id=row.id,
                value_code=code,
                subject_employee_id=nominee_employee_id,
                occurred_on=today or date.today(),
            )
        )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="award_nomination",
        entity_id=row.id,
        actor=user,
        summary=f"Nomination submitted in category {category_key!r}.",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def score_nomination(
    session: AsyncSession,
    user: User,
    *,
    nomination_id: uuid.UUID,
    scores: dict[str, int],
    comments: str | None = None,
    submit: bool = False,
    request: Request | None = None,
) -> JuryScore:
    """Record or submit one juror's score.

    A submitted score is not editable. Reopening it after seeing the panel is
    the anchoring problem arriving by a different door.
    """
    stmt = tenant_scoped(
        select(AwardNomination).where(AwardNomination.id == nomination_id),
        AwardNomination,
        user,
    )
    nomination = (await session.execute(stmt)).scalar_one_or_none()
    if nomination is None:
        raise _not_found("No such nomination in this tenant.")

    cycle = (
        await session.execute(
            tenant_scoped(
                select(AwardCycle).where(AwardCycle.id == nomination.cycle_id),
                AwardCycle,
                user,
            )
        )
    ).scalar_one_or_none()
    if cycle is None:
        raise _not_found("The cycle for this nomination is gone.")
    if str(user.id) not in list(cycle.juror_user_ids or []):
        raise _forbidden(
            "Only a juror on this cycle may score its nominations."
        )

    existing = (
        await session.execute(
            tenant_scoped(
                select(JuryScore)
                .where(JuryScore.nomination_id == nomination_id)
                .where(JuryScore.juror_user_id == user.id),
                JuryScore,
                user,
            )
        )
    ).scalar_one_or_none()

    if existing is not None and existing.is_submitted:
        raise _bad_request(
            "This score is already submitted. Reopening it after reading the "
            "panel is the anchoring problem arriving by a different door - ask "
            "for the cycle to be reopened if the jury needs to reconsider."
        )

    row = existing or JuryScore(
        tenant_id=user.tenant_id,
        nomination_id=nomination_id,
        juror_user_id=user.id,
    )
    row.scores = dict(scores)
    row.comments = comments
    if submit:
        row.is_submitted = True
        row.submitted_at = datetime.now(UTC)
    if existing is None:
        session.add(row)

    await session.commit()
    await session.refresh(row)
    return row


async def jury_view(
    session: AsyncSession,
    user: User,
    *,
    nomination_id: uuid.UUID,
) -> dict[str, Any]:
    """What this juror may read right now.

    The filtering happens before the response is built, not in the UI. A caller
    that received every score and rendered part of it would leave the anchoring
    rule one template change away from being gone.
    """
    stmt = tenant_scoped(
        select(AwardNomination).where(AwardNomination.id == nomination_id),
        AwardNomination,
        user,
    )
    nomination = (await session.execute(stmt)).scalar_one_or_none()
    if nomination is None:
        raise _not_found("No such nomination in this tenant.")

    rows = list(
        (
            await session.execute(
                tenant_scoped(
                    select(JuryScore).where(JuryScore.nomination_id == nomination_id),
                    JuryScore,
                    user,
                )
            )
        ).scalars()
    )
    engine_scores = [
        award_engine.JuryScore(
            nomination_id=str(r.nomination_id),
            juror_id=str(r.juror_user_id),
            scores=dict(r.scores or {}),
            comments=r.comments or "",
            is_submitted=r.is_submitted,
        )
        for r in rows
    ]
    visible = award_engine.visible_scores_for(
        viewer_juror_id=str(user.id), all_scores=engine_scores
    )

    own = next((s for s in engine_scores if s.juror_id == str(user.id)), None)
    return {
        "nomination_id": str(nomination_id),
        "citation": nomination.citation,
        "you_have_submitted": bool(own and own.is_submitted),
        "scores": [
            {
                "juror_id": s.juror_id,
                "scores": s.scores,
                "comments": s.comments,
                "mean": s.mean,
            }
            for s in visible
        ],
        "note": (
            "You see the panel because you have submitted."
            if own and own.is_submitted
            else "Other jurors' scores are hidden until you submit yours. "
            "Reading them first measurably changes what you write."
        ),
    }


async def declare_winner(
    session: AsyncSession,
    user: User,
    *,
    nomination_id: uuid.UUID,
    awarded_on: date,
    request: Request | None = None,
) -> AwardResult:
    """Record a win permanently on the employee's record."""
    stmt = tenant_scoped(
        select(AwardNomination).where(AwardNomination.id == nomination_id),
        AwardNomination,
        user,
    )
    nomination = (await session.execute(stmt)).scalar_one_or_none()
    if nomination is None:
        raise _not_found("No such nomination in this tenant.")

    scores = list(
        (
            await session.execute(
                tenant_scoped(
                    select(JuryScore).where(JuryScore.nomination_id == nomination_id),
                    JuryScore,
                    user,
                )
            )
        ).scalars()
    )
    if not any(s.is_submitted for s in scores):
        raise _bad_request(
            "No juror has submitted a score for this nomination. Declaring it "
            "the winner would make the jury decorative."
        )

    programme = (
        await session.execute(
            tenant_scoped(
                select(AwardProgramme).where(
                    AwardProgramme.id == nomination.programme_id
                ),
                AwardProgramme,
                user,
            )
        )
    ).scalar_one()

    row = AwardResult(
        tenant_id=user.tenant_id,
        cycle_id=nomination.cycle_id,
        nomination_id=nomination.id,
        programme_name=programme.name,
        category_key=nomination.category_key,
        employee_id=nomination.nominee_employee_id,
        citation=nomination.citation,
        awarded_on=awarded_on,
    )
    session.add(row)
    nomination.status = NominationStatusEnum.WON

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE,
        entity_type="award_result",
        entity_id=row.id,
        actor=user,
        summary=(
            f"{programme.name} ({nomination.category_key}) awarded on "
            f"{awarded_on.isoformat()}."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


# ══════════════════════════════════════════════════════════════════════════════
# Surveys
# ══════════════════════════════════════════════════════════════════════════════


async def create_survey(
    session: AsyncSession,
    user: User,
    *,
    name: str,
    kind: Any,
    opens_on: date,
    closes_on: date,
    instrument_key: str | None = None,
    min_responses_to_report: int = survey_engine.MIN_RESPONSES,
    request: Request | None = None,
) -> Survey:
    if min_responses_to_report < survey_engine.MIN_RESPONSES:
        raise _bad_request(
            f"The reporting threshold cannot be set below "
            f"{survey_engine.MIN_RESPONSES}. Below that, a group's answers are "
            "identifiable by elimination and the guarantee the survey rests on "
            "is no longer true."
        )
    if instrument_key is not None:
        try:
            survey_engine.instrument(instrument_key)
        except survey_engine.SurveyError as exc:
            raise _bad_request(str(exc)) from exc

    row = Survey(
        tenant_id=user.tenant_id,
        name=name,
        kind=kind,
        instrument_key=instrument_key,
        opens_on=opens_on,
        closes_on=closes_on,
        status=SurveyStatusEnum.OPEN,
        min_responses_to_report=min_responses_to_report,
    )
    session.add(row)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="survey",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Survey {name!r} open, reporting threshold "
            f"{min_responses_to_report}."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def submit_response(
    session: AsyncSession,
    user: User,
    *,
    survey_id: uuid.UUID,
    employee_id: uuid.UUID,
    answers: dict[str, int],
    free_text: str | None = None,
    group_key: str | None = None,
    submitted_on: date | None = None,
) -> dict[str, Any]:
    """Store the answers and, separately, the fact that this person responded.

    Two rows in one transaction. The answers row has **no respondent column**;
    the participant row has **no answers**. They share nothing but the survey,
    so no query joins an answer back to a person - which is what makes the
    anonymity a property of the schema rather than of a promise.
    """
    survey = (
        await session.execute(
            tenant_scoped(select(Survey).where(Survey.id == survey_id), Survey, user)
        )
    ).scalar_one_or_none()
    if survey is None:
        raise _not_found("No such survey in this tenant.")
    if survey.status is not SurveyStatusEnum.OPEN:
        raise _bad_request(f"This survey is {survey.status.value}.")

    if survey.instrument_key:
        try:
            survey_engine.score_scale([answers], instrument_key=survey.instrument_key)
        except survey_engine.SurveyError as exc:
            raise _bad_request(str(exc)) from exc

    participant = (
        await session.execute(
            tenant_scoped(
                select(SurveyParticipant)
                .where(SurveyParticipant.survey_id == survey_id)
                .where(SurveyParticipant.employee_id == employee_id),
                SurveyParticipant,
                user,
            )
        )
    ).scalar_one_or_none()
    if participant is None:
        participant = SurveyParticipant(
            tenant_id=user.tenant_id, survey_id=survey_id, employee_id=employee_id
        )
        session.add(participant)
    elif participant.responded_at is not None:
        raise _bad_request(
            "This person has already responded. A second submission would "
            "weight one voice twice, and the first cannot be found and replaced "
            "because the answers are not linked to them."
        )
    participant.responded_at = datetime.now(UTC)

    employee = (
        await session.execute(
            tenant_scoped(
                select(Employee).where(Employee.id == employee_id), Employee, user
            )
        )
    ).scalar_one_or_none()

    session.add(
        SurveyResponse(
            tenant_id=user.tenant_id,
            survey_id=survey_id,
            answers=dict(answers),
            free_text=free_text,
            group_key=group_key,
            department_key=(
                str(employee.department_id)
                if employee and employee.department_id
                else None
            ),
            location_key=(
                str(employee.location_id)
                if employee and employee.location_id
                else None
            ),
            submitted_on=submitted_on or date.today(),
        )
    )
    await session.commit()
    return {
        "recorded": True,
        "note": (
            "Your answers are stored without any link to you. The system knows "
            "that you responded so it stops reminding you, and that is all it "
            "can know."
        ),
    }


async def close_survey(
    session: AsyncSession, user: User, *, survey_id: uuid.UUID
) -> Survey:
    survey = (
        await session.execute(
            tenant_scoped(select(Survey).where(Survey.id == survey_id), Survey, user)
        )
    ).scalar_one_or_none()
    if survey is None:
        raise _not_found("No such survey in this tenant.")
    survey.status = SurveyStatusEnum.CLOSED
    await session.commit()
    await session.refresh(survey)
    return survey


async def survey_results(
    session: AsyncSession,
    user: User,
    *,
    survey_id: uuid.UUID,
    group_by: str = "department_key",
) -> dict[str, Any]:
    """Results, with everything below the threshold withheld.

    The threshold applies to the caller whoever they are. There is no "HR can
    see everything" path: the first time a manager works out who said what, the
    instrument is dead for that company, and HR having a bypass is how that
    happens.
    """
    if group_by not in {"department_key", "location_key", "group_key"}:
        raise _bad_request(
            "Results can be grouped by department, location or team. A finer "
            "slice than that is a slice that identifies people."
        )

    survey = (
        await session.execute(
            tenant_scoped(select(Survey).where(Survey.id == survey_id), Survey, user)
        )
    ).scalar_one_or_none()
    if survey is None:
        raise _not_found("No such survey in this tenant.")

    rows = list(
        (
            await session.execute(
                tenant_scoped(
                    select(SurveyResponse).where(SurveyResponse.survey_id == survey_id),
                    SurveyResponse,
                    user,
                )
            )
        ).scalars()
    )

    if survey.instrument_key == "enps":
        scores = [int(r.answers.get("enps", -1)) for r in rows]
        try:
            overall = survey_engine.enps(
                scores, min_responses=survey.min_responses_to_report
            ).as_dict()
        except survey_engine.SurveyError as exc:
            raise _bad_request(str(exc)) from exc
        return {"survey_id": str(survey_id), "instrument": "enps", "overall": overall}

    if not survey.instrument_key:
        raise _bad_request(
            "This survey has no validated instrument, so there is no scale to "
            "score it against. Its answers can be exported; they must not be "
            "reported as an instrument score."
        )

    groups: dict[str, list[dict[str, int]]] = {}
    for row in rows:
        key = getattr(row, group_by) or "unassigned"
        groups.setdefault(key, []).append(dict(row.answers or {}))

    breakdown = survey_engine.breakdown(
        groups,
        instrument_key=survey.instrument_key,
        min_responses=survey.min_responses_to_report,
    )
    overall_scores = (
        survey_engine.score_scale(
            [dict(r.answers or {}) for r in rows],
            instrument_key=survey.instrument_key,
        )
        if len(rows) >= survey.min_responses_to_report
        else {}
    )

    return {
        "survey_id": str(survey_id),
        "instrument": survey.instrument_key,
        "responses": len(rows),
        "min_responses_to_report": survey.min_responses_to_report,
        "overall": overall_scores,
        "groups": [g.as_dict() for g in breakdown],
        "note": (
            "Groups below the threshold are withheld, and where one is withheld "
            "a second goes with it so the first cannot be recovered by "
            "subtraction. This applies to every viewer, including HR."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Action plans
# ══════════════════════════════════════════════════════════════════════════════


async def create_plan(
    session: AsyncSession,
    user: User,
    *,
    survey_id: uuid.UUID,
    scope: PlanScopeEnum,
    scope_key: str,
    theme: str,
    owner_employee_id: uuid.UUID,
    due_date: date,
    items: list[dict[str, Any]],
    request: Request | None = None,
) -> SurveyActionPlan:
    if not items:
        raise _bad_request(
            "A plan with no action items is a theme somebody typed. The whole "
            "reason action planning exists is that the second year of a survey "
            "programme is made of empty plans."
        )

    row = SurveyActionPlan(
        tenant_id=user.tenant_id,
        survey_id=survey_id,
        scope=scope,
        scope_key=scope_key,
        theme=theme,
        owner_employee_id=owner_employee_id,
        due_date=due_date,
        status=PlanStatusEnum.COMMITTED,
        progress_notes=[],
    )
    session.add(row)
    await session.flush()

    for spec in items:
        try:
            plan_engine.ActionItem(
                item_id="pending",
                description=str(spec["description"]),
                owner_id=str(spec["owner_employee_id"]),
                due_date=spec["due_date"],
            )
        except plan_engine.ActionPlanError as exc:
            raise _bad_request(str(exc)) from exc
        session.add(
            ActionPlanItem(
                tenant_id=user.tenant_id,
                plan_id=row.id,
                description=str(spec["description"]),
                owner_employee_id=uuid.UUID(str(spec["owner_employee_id"])),
                due_date=spec["due_date"],
            )
        )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="survey_action_plan",
        entity_id=row.id,
        actor=user,
        summary=f"Action plan for {theme!r} with {len(items)} item(s).",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def _engine_plan(
    session: AsyncSession, user: User, row: SurveyActionPlan
) -> plan_engine.ActionPlan:
    items = list(
        (
            await session.execute(
                tenant_scoped(
                    select(ActionPlanItem).where(ActionPlanItem.plan_id == row.id),
                    ActionPlanItem,
                    user,
                )
            )
        ).scalars()
    )
    return plan_engine.ActionPlan(
        plan_id=str(row.id),
        survey_id=str(row.survey_id),
        scope=plan_engine.PlanScope(row.scope.value),
        scope_key=row.scope_key,
        theme=row.theme,
        owner_id=str(row.owner_employee_id),
        due_date=row.due_date,
        status=plan_engine.PlanStatus(row.status.value),
        items=tuple(
            plan_engine.ActionItem(
                item_id=str(i.id),
                description=i.description,
                owner_id=str(i.owner_employee_id),
                due_date=i.due_date,
                done_on=i.done_on,
                dropped_reason=i.dropped_reason or "",
            )
            for i in items
        ),
        progress_notes=tuple(row.progress_notes or []),
    )


async def complete_plan(
    session: AsyncSession,
    user: User,
    *,
    plan_id: uuid.UUID,
    completed_on: date,
    request: Request | None = None,
) -> SurveyActionPlan:
    """Mark a plan complete, if every item is done or explicitly dropped.

    The gate is the engine's. A plan completed with open items is a checkbox,
    and a checkbox is what the second year of a dying survey programme is made
    of.
    """
    row = (
        await session.execute(
            tenant_scoped(
                select(SurveyActionPlan).where(SurveyActionPlan.id == plan_id),
                SurveyActionPlan,
                user,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _not_found("No such action plan in this tenant.")

    allowed, reason = plan_engine.can_complete(
        await _engine_plan(session, user, row)
    )
    if not allowed:
        raise _bad_request(reason)

    row.status = PlanStatusEnum.COMPLETED
    row.completed_on = completed_on
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="survey_action_plan",
        entity_id=row.id,
        actor=user,
        summary=f"Action plan for {row.theme!r} completed.",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def loop_report(
    session: AsyncSession,
    user: User,
    *,
    as_of: date,
    movement: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Per theme: was anything done, and did the next wave move."""
    rows = list(
        (
            await session.execute(
                tenant_scoped(select(SurveyActionPlan), SurveyActionPlan, user)
            )
        ).scalars()
    )
    plans = [await _engine_plan(session, user, row) for row in rows]
    return plan_engine.loop_closure(
        plans=plans, movement=dict(movement or {}), today=as_of
    )


# ══════════════════════════════════════════════════════════════════════════════
# Announcements
# ══════════════════════════════════════════════════════════════════════════════


async def announce(
    session: AsyncSession,
    user: User,
    *,
    title: str,
    body: str,
    publish_at: datetime,
    audience: dict[str, list[str]] | None = None,
    expires_at: datetime | None = None,
    requires_acknowledgement: bool = False,
    channels: list[str] | None = None,
    request: Request | None = None,
) -> Announcement:
    spec = audience or {}
    engine_audience = comms_engine.Audience(
        department_ids=tuple(spec.get("department_ids", [])),
        location_ids=tuple(spec.get("location_ids", [])),
        grades=tuple(spec.get("grades", [])),
    )
    try:
        engine = comms_engine.Announcement(
            announcement_id="pending",
            title=title,
            body=body,
            audience=engine_audience,
            publish_at=publish_at,
            expires_at=expires_at,
            requires_acknowledgement=requires_acknowledgement,
            channels=tuple(channels or ["in_app"]),
        )
    except comms_engine.AnnouncementError as exc:
        raise _bad_request(str(exc)) from exc

    row = Announcement(
        tenant_id=user.tenant_id,
        title=title,
        body=body,
        audience=spec,
        publish_at=publish_at,
        expires_at=expires_at,
        requires_acknowledgement=requires_acknowledgement,
        channels=list(channels or ["in_app"]),
        content_hash=engine.content_hash,
        published_by_user_id=user.id,
    )
    session.add(row)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="announcement",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Announcement {title!r} published to "
            f"{engine_audience.describe()}"
            + (" (acknowledgement required)." if requires_acknowledgement else ".")
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


def _engine_announcement(row: Announcement) -> comms_engine.Announcement:
    spec = row.audience or {}
    return comms_engine.Announcement(
        announcement_id=str(row.id),
        title=row.title,
        body=row.body,
        audience=comms_engine.Audience(
            department_ids=tuple(spec.get("department_ids", [])),
            location_ids=tuple(spec.get("location_ids", [])),
            grades=tuple(spec.get("grades", [])),
        ),
        publish_at=row.publish_at,
        expires_at=row.expires_at,
        requires_acknowledgement=row.requires_acknowledgement,
        channels=tuple(row.channels or ["in_app"]),
    )


async def acknowledge(
    session: AsyncSession,
    user: User,
    *,
    announcement_id: uuid.UUID,
    employee_id: uuid.UUID,
    channel: str | None = None,
) -> dict[str, Any]:
    """Record that somebody read this version of this text.

    The hash of the text as it stands now is stored on the row. Edit the body
    afterwards and this acknowledgement stops covering it, rather than silently
    attesting to a document the person never saw.
    """
    row = (
        await session.execute(
            tenant_scoped(
                select(Announcement).where(Announcement.id == announcement_id),
                Announcement,
                user,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _not_found("No such announcement in this tenant.")
    if not row.requires_acknowledgement:
        raise _bad_request(
            "This announcement does not ask for an acknowledgement, so "
            "recording one would create an obligation nobody was given."
        )

    current = comms_engine.body_hash(row.title, row.body)
    existing = (
        await session.execute(
            tenant_scoped(
                select(AnnouncementAcknowledgement)
                .where(AnnouncementAcknowledgement.announcement_id == announcement_id)
                .where(AnnouncementAcknowledgement.employee_id == employee_id)
                .where(AnnouncementAcknowledgement.content_hash == current),
                AnnouncementAcknowledgement,
                user,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"recorded": False, "note": "Already acknowledged this version."}

    session.add(
        AnnouncementAcknowledgement(
            tenant_id=user.tenant_id,
            announcement_id=announcement_id,
            employee_id=employee_id,
            acknowledged_at=datetime.now(UTC),
            content_hash=current,
            channel=channel,
        )
    )
    await session.commit()
    return {"recorded": True, "content_hash": current}


async def acknowledgement_register(
    session: AsyncSession,
    user: User,
    *,
    announcement_id: uuid.UUID,
) -> dict[str, Any]:
    """Who has acknowledged the current text, named."""
    row = (
        await session.execute(
            tenant_scoped(
                select(Announcement).where(Announcement.id == announcement_id),
                Announcement,
                user,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _not_found("No such announcement in this tenant.")

    employees = list(
        (
            await session.execute(tenant_scoped(select(Employee), Employee, user))
        ).scalars()
    )
    recipients = [
        comms_engine.Recipient(
            employee_id=str(e.id),
            department_id=str(e.department_id) if e.department_id else None,
            location_id=str(e.location_id) if e.location_id else None,
            grade=e.grade,
        )
        for e in employees
    ]
    acks = [
        comms_engine.Acknowledgement(
            announcement_id=str(a.announcement_id),
            employee_id=str(a.employee_id),
            acknowledged_at=a.acknowledged_at,
            content_hash=a.content_hash,
        )
        for a in (
            await session.execute(
                tenant_scoped(
                    select(AnnouncementAcknowledgement).where(
                        AnnouncementAcknowledgement.announcement_id == announcement_id
                    ),
                    AnnouncementAcknowledgement,
                    user,
                )
            )
        ).scalars()
    ]

    try:
        return comms_engine.acknowledgement_report(
            announcement=_engine_announcement(row),
            recipients=recipients,
            acknowledgements=acks,
        )
    except comms_engine.AnnouncementError as exc:
        raise _bad_request(str(exc)) from exc


# ══════════════════════════════════════════════════════════════════════════════
# Events calendar
# ══════════════════════════════════════════════════════════════════════════════


async def set_event_privacy(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    share_birthday: bool,
    share_work_anniversary: bool,
    birthday_scope: str = "team",
) -> EventPrivacyPreference:
    """Record what this person has agreed to have announced."""
    row = (
        await session.execute(
            tenant_scoped(
                select(EventPrivacyPreference).where(
                    EventPrivacyPreference.employee_id == employee_id
                ),
                EventPrivacyPreference,
                user,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = EventPrivacyPreference(
            tenant_id=user.tenant_id, employee_id=employee_id
        )
        session.add(row)
    row.share_birthday = share_birthday
    row.share_work_anniversary = share_work_anniversary
    row.birthday_scope = birthday_scope
    await session.commit()
    await session.refresh(row)
    return row


async def calendar(
    session: AsyncSession,
    user: User,
    *,
    from_date: date,
    to_date: date,
) -> dict[str, Any]:
    """The events that may be published in a window.

    An employee with no privacy row contributes nothing. The absence of a
    record is a refusal, not a gap to be filled with a default of yes.
    """
    if to_date < from_date:
        raise _bad_request("A calendar window cannot end before it starts.")

    employees = list(
        (
            await session.execute(tenant_scoped(select(Employee), Employee, user))
        ).scalars()
    )
    prefs = {
        str(p.employee_id): comms_engine.EventPrivacy(
            employee_id=str(p.employee_id),
            share_birthday=p.share_birthday,
            share_work_anniversary=p.share_work_anniversary,
            birthday_scope=p.birthday_scope,
        )
        for p in (
            await session.execute(
                tenant_scoped(
                    select(EventPrivacyPreference), EventPrivacyPreference, user
                )
            )
        ).scalars()
    }

    person_events: list[comms_engine.PersonEvent] = []
    for e in employees:
        if e.date_of_birth:
            occasion = e.date_of_birth.replace(year=from_date.year)
            if from_date <= occasion <= to_date:
                person_events.append(
                    comms_engine.PersonEvent(
                        employee_id=str(e.id),
                        kind=comms_engine.EventKind.BIRTHDAY,
                        on_date=occasion,
                        label=f"{e.first_name} {e.last_name}".strip(),
                    )
                )
        if e.date_of_joining:
            occasion = e.date_of_joining.replace(year=from_date.year)
            years = from_date.year - e.date_of_joining.year
            if from_date <= occasion <= to_date and years > 0:
                person_events.append(
                    comms_engine.PersonEvent(
                        employee_id=str(e.id),
                        kind=comms_engine.EventKind.WORK_ANNIVERSARY,
                        on_date=occasion,
                        label=f"{e.first_name} {e.last_name}".strip(),
                        years=years,
                    )
                )

    company = [
        comms_engine.CalendarEvent(
            kind=comms_engine.EventKind(row.kind.value),
            on_date=row.on_date,
            label=row.title,
        )
        for row in (
            await session.execute(
                tenant_scoped(
                    select(CompanyEvent)
                    .where(CompanyEvent.on_date >= from_date)
                    .where(CompanyEvent.on_date <= to_date),
                    CompanyEvent,
                    user,
                )
            )
        ).scalars()
    ]

    events = comms_engine.calendar_events(
        person_events=person_events, privacy=prefs, company_events=company
    )
    return {
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "events": [e.as_dict() for e in events],
        "note": (
            "Birthdays and work anniversaries appear only for people who have "
            "opted in, and the two are separate choices."
        ),
    }


def culture_posture() -> dict[str, Any]:
    """What this slice enforces, and what it does not."""
    return {
        "enforced": [
            "Survey results below the reporting threshold are withheld from "
            "everybody, HR included, and a second group is withheld with them "
            "so the first cannot be recovered by subtraction.",
            "The threshold cannot be configured below five - a database check "
            "constraint, not a convention.",
            "Survey answers are stored in a table with no respondent column.",
            "A juror sees no other score until they submit their own, decided "
            "on the server rather than in the UI.",
            "The nomination window is enforced against the cycle's stored "
            "dates, inclusive at both ends.",
            "An action plan cannot be completed while any item is open.",
            "An acknowledgement stores the hash of the text it attested to, so "
            "an edit does not inherit it.",
            "Birthday and work-anniversary sharing default to off, in the "
            "column, and are separate consents.",
        ],
        "not_built": [
            "Nothing is sent. Announcements are stored and their audience is "
            "resolved; slice 05's notification engine is not called from here, "
            "so WhatsApp delivery to non-desk staff is a plan, not a fact.",
            "Nothing posts an award result to the feed. The win is recorded on "
            "the employee's profile and announced by a person.",
            "No re-survey is scheduled. Movement is computed when two waves of "
            "the same instrument exist and somebody asks.",
            "No external benchmarks - that is slice 37. Comparison is against "
            "this company's own prior periods only.",
            "The validated instruments are composed from published item "
            "structures and have not been psychometrically validated on this "
            "population.",
            "No Swag Store, no Your Voice inbox. Those were assumed by the "
            "brief to exist already and do not exist in this repository.",
        ],
    }
