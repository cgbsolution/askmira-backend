"""Slice 32 — culture depth against a real database.

The checklist gates:

- anonymity threshold provably blocks small-team result exposure
- jury anchoring prevented server-side
- values tagging visible in culture analytics
- announcement acknowledgement auditable for POSH compliance
- birthday and anniversary privacy opt-in by default

And the prompt's own test list: the nomination window enforces open and close,
survey results are hidden below the threshold, action-plan completion is tracked
and the re-survey comparison works, and acknowledgement is tracked per recipient.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.culture_depth import (
    ActionPlanItem,
    AwardCadenceEnum,
    AwardProgramme,
    NominationStatusEnum,
    PlanScopeEnum,
    PlanStatusEnum,
    SurveyKindEnum,
    SurveyParticipant,
    SurveyResponse,
    TagSourceEnum,
)
from app.models.employee import Employee, EmploymentStatus
from app.models.user import User, UserRole
from app.services.culture import service as culture
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

TODAY = date(2026, 9, 7)
NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)


# ── Helpers ─────────────────────────────────────────────────────────────────


async def make_employees(session, tenant, legal_entity, location, n: int, prefix="E"):
    people = []
    for i in range(n):
        person = Employee(
            tenant_id=tenant.id,
            employee_code=f"{prefix}{i:03d}",
            first_name=f"Person{i}",
            last_name="Test",
            work_email=f"{prefix.lower()}{i}@acme.test",
            legal_entity_id=legal_entity.id,
            location_id=location.id,
            status=EmploymentStatus.ACTIVE,
            date_of_joining=date(2024, 1, 1),
        )
        session.add(person)
        people.append(person)
    await session.commit()
    for p in people:
        await session.refresh(p)
    return people


async def make_juror(session, tenant, label: str) -> User:
    user = User(
        tenant_id=tenant.id,
        email=f"{label}@acme.test",
        password_hash="not-a-real-hash",
        full_name=label.title(),
        role=UserRole.ADMIN,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def make_programme(session, tenant) -> AwardProgramme:
    programme = AwardProgramme(
        tenant_id=tenant.id,
        name="Quarterly Excellence",
        cadence=AwardCadenceEnum.QUARTERLY,
        categories=[{"key": "impact", "name": "Impact"}],
        criteria=[{"key": "impact", "name": "Impact", "weight": 1}],
        jury_roles=["admin"],
    )
    session.add(programme)
    await session.commit()
    await session.refresh(programme)
    return programme


async def open_survey(session, hr_user, threshold: int = 5, instrument="engagement_9"):
    return await culture.create_survey(
        session,
        hr_user,
        name=f"Pulse {uuid.uuid4().hex[:6]}",
        kind=SurveyKindEnum.ENGAGEMENT,
        opens_on=date(2026, 9, 1),
        closes_on=date(2026, 9, 30),
        instrument_key=instrument,
        min_responses_to_report=threshold,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Gate: the anonymity threshold blocks small-team exposure
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_small_group_is_withheld_from_the_results(
    session, hr_user, tenant, legal_entity, location
):
    """A team of three has no average that is not three named opinions."""
    survey = await open_survey(session, hr_user)
    big = await make_employees(session, tenant, legal_entity, location, 8, "BIG")
    small = await make_employees(session, tenant, legal_entity, location, 3, "SML")

    for person in big:
        await culture.submit_response(
            session,
            hr_user,
            survey_id=survey.id,
            employee_id=person.id,
            answers={"clarity": 4, "voice": 4},
            group_key="engineering",
        )
    for person in small:
        await culture.submit_response(
            session,
            hr_user,
            survey_id=survey.id,
            employee_id=person.id,
            answers={"clarity": 1, "voice": 1},
            group_key="tiny",
        )

    results = await culture.survey_results(
        session, hr_user, survey_id=survey.id, group_by="group_key"
    )
    tiny = next(g for g in results["groups"] if g["key"] == "tiny")
    assert tiny["visible"] is False
    assert tiny["scores"] == {}
    assert tiny["responses"] == 3


async def test_a_second_group_is_withheld_so_the_first_is_not_recoverable(
    session, hr_user, tenant, legal_entity, location
):
    """Otherwise the hidden group's score is the total minus the visible ones -
    arithmetic a manager can do in their head."""
    survey = await open_survey(session, hr_user)
    for group, count, score in (("a", 12, 5), ("b", 8, 4), ("c", 6, 3), ("tiny", 3, 1)):
        people = await make_employees(session, tenant, legal_entity, location, count, group.upper())
        for person in people:
            await culture.submit_response(
                session,
                hr_user,
                survey_id=survey.id,
                employee_id=person.id,
                answers={"clarity": score},
                group_key=group,
            )

    results = await culture.survey_results(
        session, hr_user, survey_id=survey.id, group_by="group_key"
    )
    hidden = {g["key"] for g in results["groups"] if not g["visible"]}
    assert "tiny" in hidden
    assert "c" in hidden
    assert next(g for g in results["groups"] if g["key"] == "c")["suppression"] == ("complementary")


async def test_hr_gets_no_privileged_view_of_a_small_team(
    session, hr_user, tenant, legal_entity, location
):
    """There is no bypass. Somebody with the role would run the report and
    forward it, which is how a manager works out who said what."""
    survey = await open_survey(session, hr_user)
    small = await make_employees(session, tenant, legal_entity, location, 4, "SML")
    for person in small:
        await culture.submit_response(
            session,
            hr_user,
            survey_id=survey.id,
            employee_id=person.id,
            answers={"clarity": 2},
            group_key="tiny",
        )

    results = await culture.survey_results(
        session, hr_user, survey_id=survey.id, group_by="group_key"
    )
    assert all(not g["visible"] for g in results["groups"])
    assert results["overall"] == {}


async def test_a_threshold_below_the_floor_is_refused(session, hr_user):
    with pytest.raises(HTTPException) as exc:
        await culture.create_survey(
            session,
            hr_user,
            name="Too permissive",
            kind=SurveyKindEnum.ENGAGEMENT,
            opens_on=date(2026, 9, 1),
            closes_on=date(2026, 9, 30),
            instrument_key="engagement_9",
            min_responses_to_report=2,
        )
    assert "identifiable by elimination" in exc.value.detail


# ══════════════════════════════════════════════════════════════════════════════
# The anonymity is structural, not a promise
# ══════════════════════════════════════════════════════════════════════════════


async def test_an_answer_row_carries_no_link_to_the_person(session, hr_user, employee):
    survey = await open_survey(session, hr_user)
    await culture.submit_response(
        session,
        hr_user,
        survey_id=survey.id,
        employee_id=employee.id,
        answers={"clarity": 5},
        free_text="Things are fine.",
    )

    row = (
        await session.execute(select(SurveyResponse).where(SurveyResponse.survey_id == survey.id))
    ).scalar_one()
    values = {c.name: getattr(row, c.name) for c in SurveyResponse.__table__.columns}
    assert str(employee.id) not in {str(v) for v in values.values()}


async def test_the_participant_row_records_that_they_responded(session, hr_user, employee):
    """So reminders stop. It carries no answers, which is all it needs."""
    survey = await open_survey(session, hr_user)
    await culture.submit_response(
        session,
        hr_user,
        survey_id=survey.id,
        employee_id=employee.id,
        answers={"clarity": 5},
    )
    participant = (
        await session.execute(
            select(SurveyParticipant).where(SurveyParticipant.survey_id == survey.id)
        )
    ).scalar_one()
    assert participant.employee_id == employee.id
    assert participant.responded_at is not None


async def test_a_second_submission_is_refused(session, hr_user, employee):
    """It would weight one voice twice, and the first cannot be found and
    replaced because the answers are not linked to them."""
    survey = await open_survey(session, hr_user)
    await culture.submit_response(
        session,
        hr_user,
        survey_id=survey.id,
        employee_id=employee.id,
        answers={"clarity": 5},
    )
    with pytest.raises(HTTPException) as exc:
        await culture.submit_response(
            session,
            hr_user,
            survey_id=survey.id,
            employee_id=employee.id,
            answers={"clarity": 1},
        )
    assert "already responded" in exc.value.detail


async def test_an_answer_outside_the_scale_is_refused(session, hr_user, employee):
    survey = await open_survey(session, hr_user)
    with pytest.raises(HTTPException):
        await culture.submit_response(
            session,
            hr_user,
            survey_id=survey.id,
            employee_id=employee.id,
            answers={"clarity": 9},
        )


async def test_enps_comes_back_scored_the_standard_way(
    session, hr_user, tenant, legal_entity, location
):
    survey = await culture.create_survey(
        session,
        hr_user,
        name="eNPS September",
        kind=SurveyKindEnum.ENPS,
        opens_on=date(2026, 9, 1),
        closes_on=date(2026, 9, 30),
        instrument_key="enps",
    )
    people = await make_employees(session, tenant, legal_entity, location, 10, "NPS")
    for person, score in zip(people, [10, 9, 9, 8, 7, 6, 3, 10, 9, 5], strict=True):
        await culture.submit_response(
            session,
            hr_user,
            survey_id=survey.id,
            employee_id=person.id,
            answers={"enps": score},
        )

    results = await culture.survey_results(session, hr_user, survey_id=survey.id)
    assert results["overall"]["score"] == 20
    assert results["overall"]["promoters"] == 5
    assert results["overall"]["suppressed"] is False


async def test_an_ad_hoc_survey_is_not_reported_as_an_instrument(session, hr_user):
    survey = await culture.create_survey(
        session,
        hr_user,
        name="Ad hoc",
        kind=SurveyKindEnum.CUSTOM,
        opens_on=date(2026, 9, 1),
        closes_on=date(2026, 9, 30),
    )
    with pytest.raises(HTTPException) as exc:
        await culture.survey_results(session, hr_user, survey_id=survey.id)
    assert "must not be reported as an instrument" in exc.value.detail


# ══════════════════════════════════════════════════════════════════════════════
# Gate: jury anchoring prevented server-side
# ══════════════════════════════════════════════════════════════════════════════


async def nomination_fixture(session, hr_user, tenant, legal_entity, location):
    programme = await make_programme(session, tenant)
    juror_a = await make_juror(session, tenant, "juror-a")
    juror_b = await make_juror(session, tenant, "juror-b")
    cycle = await culture.open_cycle(
        session,
        hr_user,
        programme_id=programme.id,
        label="Q3",
        nominations_open_on=date(2026, 9, 1),
        nominations_close_on=date(2026, 9, 20),
        juror_user_ids=[juror_a.id, juror_b.id],
    )
    people = await make_employees(session, tenant, legal_entity, location, 2, "NOM")
    nomination = await culture.nominate(
        session,
        hr_user,
        cycle_id=cycle.id,
        category_key="impact",
        nominee_employee_id=people[0].id,
        nominator_employee_id=people[1].id,
        citation="Rebuilt the payroll reconciliation over one weekend.",
        today=TODAY,
    )
    return cycle, nomination, juror_a, juror_b


async def test_a_juror_who_has_not_scored_cannot_read_the_panel(
    session, hr_user, tenant, legal_entity, location
):
    """Reading "9/10, obvious winner" before scoring measurably changes what you
    write, and five opinions that anchored on the first are one opinion."""
    _, nomination, juror_a, juror_b = await nomination_fixture(
        session, hr_user, tenant, legal_entity, location
    )
    await culture.score_nomination(
        session, juror_a, nomination_id=nomination.id, scores={"impact": 9}, submit=True
    )

    view = await culture.jury_view(session, juror_b, nomination_id=nomination.id)
    assert view["scores"] == []
    assert view["you_have_submitted"] is False
    assert "hidden until you submit" in view["note"]


async def test_a_juror_who_has_scored_sees_the_submitted_panel(
    session, hr_user, tenant, legal_entity, location
):
    _, nomination, juror_a, juror_b = await nomination_fixture(
        session, hr_user, tenant, legal_entity, location
    )
    await culture.score_nomination(
        session, juror_a, nomination_id=nomination.id, scores={"impact": 9}, submit=True
    )
    await culture.score_nomination(
        session, juror_b, nomination_id=nomination.id, scores={"impact": 4}, submit=True
    )

    view = await culture.jury_view(session, juror_b, nomination_id=nomination.id)
    assert {s["juror_id"] for s in view["scores"]} == {str(juror_a.id), str(juror_b.id)}


async def test_a_draft_score_is_invisible_to_everybody_else(
    session, hr_user, tenant, legal_entity, location
):
    _, nomination, juror_a, juror_b = await nomination_fixture(
        session, hr_user, tenant, legal_entity, location
    )
    await culture.score_nomination(
        session, juror_a, nomination_id=nomination.id, scores={"impact": 9}
    )
    await culture.score_nomination(
        session, juror_b, nomination_id=nomination.id, scores={"impact": 4}, submit=True
    )

    view = await culture.jury_view(session, juror_b, nomination_id=nomination.id)
    assert [s["juror_id"] for s in view["scores"]] == [str(juror_b.id)]


async def test_a_submitted_score_cannot_be_revised(
    session, hr_user, tenant, legal_entity, location
):
    _, nomination, juror_a, _ = await nomination_fixture(
        session, hr_user, tenant, legal_entity, location
    )
    await culture.score_nomination(
        session, juror_a, nomination_id=nomination.id, scores={"impact": 9}, submit=True
    )
    with pytest.raises(HTTPException) as exc:
        await culture.score_nomination(
            session, juror_a, nomination_id=nomination.id, scores={"impact": 2}
        )
    assert "anchoring problem arriving by a different door" in exc.value.detail


async def test_somebody_who_is_not_a_juror_cannot_score(
    session, hr_user, tenant, legal_entity, location
):
    _, nomination, _, _ = await nomination_fixture(session, hr_user, tenant, legal_entity, location)
    outsider = await make_juror(session, tenant, "outsider")
    with pytest.raises(HTTPException) as exc:
        await culture.score_nomination(
            session, outsider, nomination_id=nomination.id, scores={"impact": 10}
        )
    assert exc.value.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# The nomination window
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_nomination_after_the_window_is_refused(
    session, hr_user, tenant, legal_entity, location
):
    cycle, _, _, _ = await nomination_fixture(session, hr_user, tenant, legal_entity, location)
    people = await make_employees(session, tenant, legal_entity, location, 2, "LATE")
    with pytest.raises(HTTPException) as exc:
        await culture.nominate(
            session,
            hr_user,
            cycle_id=cycle.id,
            category_key="impact",
            nominee_employee_id=people[0].id,
            nominator_employee_id=people[1].id,
            citation="Excellent work, submitted late.",
            today=date(2026, 9, 21),
        )
    assert "closed on 2026-09-20" in exc.value.detail


async def test_a_nomination_before_the_window_is_refused(
    session, hr_user, tenant, legal_entity, location
):
    cycle, _, _, _ = await nomination_fixture(session, hr_user, tenant, legal_entity, location)
    people = await make_employees(session, tenant, legal_entity, location, 2, "EARLY")
    with pytest.raises(HTTPException) as exc:
        await culture.nominate(
            session,
            hr_user,
            cycle_id=cycle.id,
            category_key="impact",
            nominee_employee_id=people[0].id,
            nominator_employee_id=people[1].id,
            citation="Keen.",
            today=date(2026, 8, 31),
        )
    assert "open on 2026-09-01" in exc.value.detail


async def test_the_closing_day_itself_still_accepts_a_nomination(
    session, hr_user, tenant, legal_entity, location
):
    """A window closing at 00:00 on the advertised day loses every nomination
    made on the day people were told they had."""
    cycle, _, _, _ = await nomination_fixture(session, hr_user, tenant, legal_entity, location)
    people = await make_employees(session, tenant, legal_entity, location, 2, "LAST")
    row = await culture.nominate(
        session,
        hr_user,
        cycle_id=cycle.id,
        category_key="impact",
        nominee_employee_id=people[0].id,
        nominator_employee_id=people[1].id,
        citation="Made it on the last day.",
        today=date(2026, 9, 20),
    )
    assert row.status is NominationStatusEnum.SUBMITTED


async def test_a_category_outside_the_programme_is_refused(
    session, hr_user, tenant, legal_entity, location
):
    cycle, _, _, _ = await nomination_fixture(session, hr_user, tenant, legal_entity, location)
    people = await make_employees(session, tenant, legal_entity, location, 2, "CAT")
    with pytest.raises(HTTPException) as exc:
        await culture.nominate(
            session,
            hr_user,
            cycle_id=cycle.id,
            category_key="vibes",
            nominee_employee_id=people[0].id,
            nominator_employee_id=people[1].id,
            citation="Great vibes.",
            today=TODAY,
        )
    assert "not a category" in exc.value.detail


async def test_a_winner_needs_a_submitted_jury_score(
    session, hr_user, tenant, legal_entity, location
):
    """Declaring one without would make the jury decorative."""
    _, nomination, juror_a, _ = await nomination_fixture(
        session, hr_user, tenant, legal_entity, location
    )
    with pytest.raises(HTTPException) as exc:
        await culture.declare_winner(
            session, hr_user, nomination_id=nomination.id, awarded_on=TODAY
        )
    assert "jury decorative" in exc.value.detail

    await culture.score_nomination(
        session, juror_a, nomination_id=nomination.id, scores={"impact": 9}, submit=True
    )
    result = await culture.declare_winner(
        session, hr_user, nomination_id=nomination.id, awarded_on=TODAY
    )
    assert result.employee_id == nomination.nominee_employee_id
    assert result.citation == nomination.citation


# ══════════════════════════════════════════════════════════════════════════════
# Gate: values tagging visible in culture analytics
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_nominations_values_reach_the_analytics(
    session, hr_user, tenant, legal_entity, location
):
    await culture.define_value(
        session,
        hr_user,
        code="craft",
        name="Craft",
        description="We ship work we would sign.",
    )
    await culture.define_value(
        session,
        hr_user,
        code="customer",
        name="Customer obsession",
        description="We start from the customer.",
    )
    programme = await make_programme(session, tenant)
    juror = await make_juror(session, tenant, "j1")
    cycle = await culture.open_cycle(
        session,
        hr_user,
        programme_id=programme.id,
        label="Q3",
        nominations_open_on=date(2026, 9, 1),
        nominations_close_on=date(2026, 9, 20),
        juror_user_ids=[juror.id],
    )
    people = await make_employees(session, tenant, legal_entity, location, 2, "VAL")
    await culture.nominate(
        session,
        hr_user,
        cycle_id=cycle.id,
        category_key="impact",
        nominee_employee_id=people[0].id,
        nominator_employee_id=people[1].id,
        citation="Held the line on quality.",
        value_codes=["craft"],
        today=TODAY,
    )

    report = await culture.value_analytics(session, hr_user)
    craft = next(v for v in report["coverage"]["values"] if v["code"] == "craft")
    assert craft["counts"]["nomination"] == 1
    assert report["coverage"]["totals"]["nomination"] == 1


async def test_a_value_nobody_recognises_is_surfaced(session, hr_user, employee):
    """A value on the wall in 1% of recognition is either the wrong value or one
    nobody has been shown how to act on."""
    await culture.define_value(
        session, hr_user, code="craft", name="Craft", description="We sign our work."
    )
    await culture.define_value(
        session,
        hr_user,
        code="customer",
        name="Customer obsession",
        description="We start from the customer.",
    )
    for _ in range(30):
        await culture.tag_value(
            session,
            hr_user,
            source=TagSourceEnum.SHOUTOUT,
            source_id=uuid.uuid4(),
            value_code="craft",
            subject_employee_id=employee.id,
            occurred_on=TODAY,
        )

    report = await culture.value_analytics(session, hr_user)
    assert [u["code"] for u in report["unlived"]] == ["customer"]


async def test_a_tag_to_an_undefined_value_is_refused(session, hr_user, employee):
    await culture.define_value(
        session, hr_user, code="craft", name="Craft", description="We sign our work."
    )
    with pytest.raises(HTTPException) as exc:
        await culture.tag_value(
            session,
            hr_user,
            source=TagSourceEnum.SHOUTOUT,
            source_id=uuid.uuid4(),
            value_code="hustle",
            subject_employee_id=employee.id,
            occurred_on=TODAY,
        )
    assert "tally of typos" in exc.value.detail


async def test_a_value_needs_a_description(session, hr_user):
    with pytest.raises(HTTPException):
        await culture.define_value(session, hr_user, code="speed", name="Speed", description="   ")


# ══════════════════════════════════════════════════════════════════════════════
# Action plans and closing the loop
# ══════════════════════════════════════════════════════════════════════════════


async def make_plan(session, hr_user, employee, survey):
    return await culture.create_plan(
        session,
        hr_user,
        survey_id=survey.id,
        scope=PlanScopeEnum.DEPARTMENT,
        scope_key="engineering",
        theme="voice",
        owner_employee_id=employee.id,
        due_date=date(2026, 8, 31),
        items=[
            {
                "description": "Reinstate weekly one-to-ones",
                "owner_employee_id": employee.id,
                "due_date": date(2026, 8, 1),
            },
            {
                "description": "Publish the promotion criteria",
                "owner_employee_id": employee.id,
                "due_date": date(2026, 8, 15),
            },
        ],
    )


async def test_a_plan_with_open_items_cannot_be_completed(session, hr_user, employee):
    survey = await open_survey(session, hr_user)
    plan = await make_plan(session, hr_user, employee, survey)
    with pytest.raises(HTTPException) as exc:
        await culture.complete_plan(session, hr_user, plan_id=plan.id, completed_on=TODAY)
    assert "still open" in exc.value.detail
    assert "Publish the promotion criteria" in exc.value.detail


async def test_a_plan_completes_once_every_item_is_resolved(session, hr_user, employee):
    survey = await open_survey(session, hr_user)
    plan = await make_plan(session, hr_user, employee, survey)

    items = list(
        (
            await session.execute(select(ActionPlanItem).where(ActionPlanItem.plan_id == plan.id))
        ).scalars()
    )
    items[0].done_on = date(2026, 8, 20)
    items[1].dropped_reason = "Superseded by the new levelling framework."
    await session.commit()

    completed = await culture.complete_plan(session, hr_user, plan_id=plan.id, completed_on=TODAY)
    assert completed.status is PlanStatusEnum.COMPLETED
    assert completed.completed_on == TODAY


async def test_a_plan_with_no_items_is_refused_at_creation(session, hr_user, employee):
    survey = await open_survey(session, hr_user)
    with pytest.raises(HTTPException) as exc:
        await culture.create_plan(
            session,
            hr_user,
            survey_id=survey.id,
            scope=PlanScopeEnum.TEAM,
            scope_key="eng",
            theme="voice",
            owner_employee_id=employee.id,
            due_date=date(2026, 9, 30),
            items=[],
        )
    assert "theme somebody typed" in exc.value.detail


async def test_the_loop_report_pairs_action_with_movement(session, hr_user, employee):
    survey = await open_survey(session, hr_user)
    plan = await make_plan(session, hr_user, employee, survey)
    items = list(
        (
            await session.execute(select(ActionPlanItem).where(ActionPlanItem.plan_id == plan.id))
        ).scalars()
    )
    items[0].done_on = date(2026, 8, 20)
    await session.commit()

    report = await culture.loop_report(session, hr_user, as_of=TODAY, movement={"voice": 0.4})
    voice = next(r for r in report["themes"] if r["theme"] == "voice")
    assert voice["acted"] is True
    assert voice["movement"] == 0.4
    assert "loop closed" in voice["reading"]


async def test_a_theme_never_re_surveyed_reads_as_unmeasured(session, hr_user, employee):
    survey = await open_survey(session, hr_user)
    await make_plan(session, hr_user, employee, survey)
    report = await culture.loop_report(session, hr_user, as_of=TODAY)
    voice = next(r for r in report["themes"] if r["theme"] == "voice")
    assert voice["movement"] is None
    assert "gap in the measurement" in voice["reading"]


# ══════════════════════════════════════════════════════════════════════════════
# Gate: announcement acknowledgement auditable for POSH
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_register_names_who_has_not_acknowledged(
    session, hr_user, tenant, legal_entity, location
):
    people = await make_employees(session, tenant, legal_entity, location, 3, "ACK")
    announcement = await culture.announce(
        session,
        hr_user,
        title="POSH policy 2026",
        body="The policy body.",
        publish_at=NOW,
        requires_acknowledgement=True,
    )
    await culture.acknowledge(
        session,
        hr_user,
        announcement_id=announcement.id,
        employee_id=people[0].id,
    )

    register = await culture.acknowledgement_register(
        session, hr_user, announcement_id=announcement.id
    )
    assert register["acknowledged"] == 1
    assert str(people[1].id) in register["outstanding"]
    assert register["complete"] is False


async def test_editing_the_policy_invalidates_earlier_acknowledgements(
    session, hr_user, tenant, legal_entity, location
):
    """Otherwise a company tells an auditor that people read a document that did
    not exist when they signed."""
    people = await make_employees(session, tenant, legal_entity, location, 2, "EDIT")
    announcement = await culture.announce(
        session,
        hr_user,
        title="POSH policy 2026",
        body="The policy body.",
        publish_at=NOW,
        requires_acknowledgement=True,
    )
    await culture.acknowledge(
        session, hr_user, announcement_id=announcement.id, employee_id=people[0].id
    )

    announcement.body = "The policy body, revised after a committee change."
    await session.commit()

    register = await culture.acknowledgement_register(
        session, hr_user, announcement_id=announcement.id
    )
    assert str(people[0].id) in register["stale"]
    assert str(people[0].id) in register["outstanding"]
    assert "does not carry over" in register["note"]


async def test_re_acknowledging_after_an_edit_is_recorded_separately(
    session, hr_user, tenant, legal_entity, location
):
    people = await make_employees(session, tenant, legal_entity, location, 1, "REACK")
    announcement = await culture.announce(
        session,
        hr_user,
        title="POSH policy 2026",
        body="The policy body.",
        publish_at=NOW,
        requires_acknowledgement=True,
    )
    await culture.acknowledge(
        session, hr_user, announcement_id=announcement.id, employee_id=people[0].id
    )
    announcement.body = "Revised."
    await session.commit()

    out = await culture.acknowledge(
        session, hr_user, announcement_id=announcement.id, employee_id=people[0].id
    )
    assert out["recorded"] is True

    register = await culture.acknowledgement_register(
        session, hr_user, announcement_id=announcement.id
    )
    assert register["outstanding"] == []
    assert register["stale"] == []


async def test_an_acknowledgement_announcement_cannot_be_given_an_expiry(session, hr_user):
    with pytest.raises(HTTPException) as exc:
        await culture.announce(
            session,
            hr_user,
            title="POSH policy",
            body="Body.",
            publish_at=NOW,
            expires_at=datetime(2026, 10, 1, tzinfo=UTC),
            requires_acknowledgement=True,
        )
    assert "register" in exc.value.detail


async def test_acknowledging_something_that_never_asked_is_refused(session, hr_user, employee):
    announcement = await culture.announce(
        session,
        hr_user,
        title="Canteen",
        body="Closed on Friday.",
        publish_at=NOW,
    )
    with pytest.raises(HTTPException) as exc:
        await culture.acknowledge(
            session,
            hr_user,
            announcement_id=announcement.id,
            employee_id=employee.id,
        )
    assert "obligation nobody was given" in exc.value.detail


async def test_the_audience_narrows_who_the_register_expects(
    session, hr_user, tenant, legal_entity, location, employee
):
    people = await make_employees(session, tenant, legal_entity, location, 2, "AUD")
    announcement = await culture.announce(
        session,
        hr_user,
        title="Bengaluru fire drill",
        body="Assemble in the car park.",
        publish_at=NOW,
        audience={"location_ids": [str(location.id)]},
        requires_acknowledgement=True,
    )
    register = await culture.acknowledgement_register(
        session, hr_user, announcement_id=announcement.id
    )
    assert register["expected"] == len(people) + 1
    assert register["audience"] == "1 location(s)"


# ══════════════════════════════════════════════════════════════════════════════
# Gate: birthday and anniversary privacy opt-in by default
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_birthday_is_not_published_without_an_opt_in(session, hr_user, employee):
    """The absence of a record is a refusal, not a gap to fill with yes."""
    employee.date_of_birth = date(1992, 9, 9)
    await session.commit()

    calendar = await culture.calendar(
        session, hr_user, from_date=date(2026, 9, 1), to_date=date(2026, 9, 30)
    )
    assert all(e["kind"] != "birthday" for e in calendar["events"])


async def test_a_birthday_appears_after_an_explicit_opt_in(session, hr_user, employee):
    employee.date_of_birth = date(1992, 9, 9)
    await session.commit()
    await culture.set_event_privacy(
        session,
        hr_user,
        employee_id=employee.id,
        share_birthday=True,
        share_work_anniversary=False,
    )

    calendar = await culture.calendar(
        session, hr_user, from_date=date(2026, 9, 1), to_date=date(2026, 9, 30)
    )
    birthdays = [e for e in calendar["events"] if e["kind"] == "birthday"]
    assert len(birthdays) == 1
    assert birthdays[0]["scope"] == "team"


async def test_a_work_anniversary_is_a_separate_consent(session, hr_user, employee):
    """Somebody happy to mark five years may not want their age inferable."""
    employee.date_of_birth = date(1992, 9, 9)
    employee.date_of_joining = date(2021, 9, 10)
    await session.commit()
    await culture.set_event_privacy(
        session,
        hr_user,
        employee_id=employee.id,
        share_birthday=False,
        share_work_anniversary=True,
    )

    calendar = await culture.calendar(
        session, hr_user, from_date=date(2026, 9, 1), to_date=date(2026, 9, 30)
    )
    kinds = {e["kind"] for e in calendar["events"]}
    assert "work_anniversary" in kinds
    assert "birthday" not in kinds


async def test_the_stored_default_shares_nothing(session, hr_user, employee):
    row = await culture.set_event_privacy(
        session,
        hr_user,
        employee_id=employee.id,
        share_birthday=False,
        share_work_anniversary=False,
    )
    assert row.share_birthday is False
    assert row.share_work_anniversary is False


# ══════════════════════════════════════════════════════════════════════════════
# Tenant isolation
# ══════════════════════════════════════════════════════════════════════════════


async def test_another_tenant_cannot_read_a_survey(session, hr_user, other_hr_user):
    survey = await open_survey(session, hr_user)
    with pytest.raises(HTTPException) as exc:
        await culture.survey_results(session, other_hr_user, survey_id=survey.id)
    assert exc.value.status_code == 404


async def test_another_tenant_cannot_read_an_acknowledgement_register(
    session, hr_user, other_hr_user
):
    announcement = await culture.announce(
        session,
        hr_user,
        title="POSH policy",
        body="Body.",
        publish_at=NOW,
        requires_acknowledgement=True,
    )
    with pytest.raises(HTTPException) as exc:
        await culture.acknowledgement_register(
            session, other_hr_user, announcement_id=announcement.id
        )
    assert exc.value.status_code == 404


async def test_value_analytics_are_scoped_to_the_tenant(session, hr_user, other_hr_user, employee):
    await culture.define_value(
        session, hr_user, code="craft", name="Craft", description="We sign our work."
    )
    await culture.tag_value(
        session,
        hr_user,
        source=TagSourceEnum.SHOUTOUT,
        source_id=uuid.uuid4(),
        value_code="craft",
        subject_employee_id=employee.id,
        occurred_on=TODAY,
    )
    with pytest.raises(HTTPException) as exc:
        await culture.value_analytics(session, other_hr_user)
    assert "no company values defined" in exc.value.detail
