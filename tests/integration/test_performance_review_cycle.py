"""The whole performance experience against a real database.

A full cycle — launch through the rules, self review, manager review scored from
measured goals and ratings, calibration lock, release, acknowledgement — plus
the refusals that make it safe: nobody rates, calibrates, recommends or records
a check-in on themselves, a colleague outside the line cannot read a review,
and another tenant gets a 404.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appraisal import (
    AcknowledgementType,
    Appraisal,
    AppraisalStage,
    AppraisalStageEntry,
    AppraisalStatus,
    CycleStatus,
    CycleType,
    StageRole,
)
from app.models.audit import AuditLog
from app.models.calibration import CalibrationMode, CalibrationScope, SessionStatus
from app.models.competency import CompetencyCategory, ScaleType
from app.models.culture import Shoutout
from app.models.culture_depth import CompanyValue
from app.models.employee import Employee, EmploymentStatus, Gender
from app.models.goals import GoalLevel, GoalStatus, GoalVisibility, KeyResultKind
from app.models.notifications import Notification
from app.models.org import Department, LegalEntity, Location
from app.models.performance_flow import EmployeeInputStateEnum, MilestoneStatusEnum
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.goals import GoalCheckInCreate, GoalCreate, GoalUpdate, KeyResultCreate
from app.schemas.performance import (
    AppraisalTemplateCreate,
    BandSpec,
    CompetencyCreate,
    CompetencyTemplateCreate,
    CycleCreate,
    DelegationCreate,
    IndicatorCreate,
    PolicyCreate,
    RuleCreate,
    ScaleCreate,
    ScaleLevelCreate,
    SessionCreate,
    StageCreate,
    TargetSpec,
    TemplateItemCreate,
)
from app.services.appraisal import (
    calibration_service,
    competency_service,
    cycle_config,
    operations,
    review,
)
from app.services.appraisal import service as appraisal_service
from app.services.goals import service as goals_service
from app.services.performance import service as performance
from app.services.performance import views
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db

D = Decimal
TODAY = date.today()


async def _person(
    session: AsyncSession,
    tenant: Tenant,
    entity: LegalEntity,
    location: Location,
    *,
    code: str,
    name: str,
    role: UserRole,
    manager: Employee | None = None,
    department: Department | None = None,
    grade: str = "L3",
    gender: Gender | None = None,
) -> tuple[Employee, User]:
    first, _, last = name.partition(" ")
    employee = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=first,
        last_name=last or None,
        work_email=f"{code.lower()}@acme.test",
        legal_entity_id=entity.id,
        location_id=location.id,
        manager_id=manager.id if manager else None,
        department_id=department.id if department else None,
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2020, 1, 1),
        grade=grade,
        gender=gender,
    )
    session.add(employee)
    await session.flush()
    user = User(
        tenant_id=tenant.id,
        email=f"{code.lower()}@acme.test",
        password_hash="not-a-real-hash",
        full_name=name,
        role=role,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(user)
    await session.commit()
    await session.refresh(employee)
    await session.refresh(user)
    return employee, user


@pytest.fixture
async def org(session: AsyncSession, tenant: Tenant, legal_entity: LegalEntity, location: Location):
    dept = Department(tenant_id=tenant.id, name="Engineering", code="ENG")
    session.add(dept)
    await session.commit()
    hr_emp, hr = await _person(
        session,
        tenant,
        legal_entity,
        location,
        code="HR1",
        name="Priya Ramesh",
        role=UserRole.ADMIN,
    )
    head_emp, head = await _person(
        session,
        tenant,
        legal_entity,
        location,
        code="HEAD",
        name="Kavya Iyer",
        role=UserRole.EMPLOYEE,
        department=dept,
        grade="L7",
    )
    boss_emp, boss = await _person(
        session,
        tenant,
        legal_entity,
        location,
        code="MGR1",
        name="Vikram Shah",
        role=UserRole.EMPLOYEE,
        manager=head_emp,
        department=dept,
        grade="L5",
    )
    meera_emp, meera = await _person(
        session,
        tenant,
        legal_entity,
        location,
        code="EMP1",
        name="Meera Nair",
        role=UserRole.EMPLOYEE,
        manager=boss_emp,
        department=dept,
        gender=Gender.FEMALE,
    )
    ravi_emp, ravi = await _person(
        session,
        tenant,
        legal_entity,
        location,
        code="EMP2",
        name="Ravi Kumar",
        role=UserRole.EMPLOYEE,
        manager=hr_emp,
    )
    return {
        "tenant": tenant,
        "dept": dept,
        "hr": hr,
        "hr_emp": hr_emp,
        "head": head,
        "head_emp": head_emp,
        "boss": boss,
        "boss_emp": boss_emp,
        "meera": meera,
        "meera_emp": meera_emp,
        "ravi": ravi,
        "ravi_emp": ravi_emp,
    }


async def _configured_cycle(session: AsyncSession, org) -> dict:
    """A cycle with a scale, competencies, a template, rules and a policy."""
    hr = org["hr"]
    scale = await competency_service.create_scale(
        session,
        hr,
        ScaleCreate(
            name="Five point",
            scale_type=ScaleType.NUMERIC,
            min_value=D(1),
            max_value=D(5),
            levels=[
                ScaleLevelCreate(value=D(v), label=f"Level {v}", description=f"What {v} means")
                for v in range(1, 6)
            ],
        ),
    )
    session.add(
        CompanyValue(
            tenant_id=org["tenant"].id,
            code="ownership",
            name="Ownership",
            description="We finish what we start.",
        )
    )
    competencies = []
    for code, name, values in (("COLLAB", "Collaboration", ["ownership"]), ("CRAFT", "Craft", [])):
        competencies.append(
            await competency_service.create_competency(
                session,
                hr,
                CompetencyCreate(
                    code=code,
                    name=name,
                    category=CompetencyCategory.BEHAVIOURAL,
                    scale_id=scale["id"],
                    indicators=[
                        IndicatorCreate(scale_value=D(v), behaviour_description=f"{name} at {v}")
                        for v in range(1, 6)
                    ],
                    linked_value_codes=values,
                ),
            )
        )
    template = await competency_service.create_competency_template(
        session,
        hr,
        CompetencyTemplateCreate(
            name="Standard",
            items=[
                TemplateItemCreate(competency_id=competencies[0]["id"], weight=D(50)),
                TemplateItemCreate(
                    competency_id=competencies[1]["id"], weight=D(50), is_mandatory=False
                ),
            ],
        ),
    )
    cycle = await appraisal_service.create_cycle(
        session,
        hr,
        CycleCreate(
            name="FY27 annual review",
            cycle_type=CycleType.ANNUAL,
            period_start=TODAY - timedelta(days=200),
            period_end=TODAY + timedelta(days=100),
            stages=[
                StageCreate(
                    sequence=1,
                    role=StageRole.SELF,
                    label="Self review",
                    due_date=TODAY + timedelta(days=3),
                ),
                StageCreate(
                    sequence=2,
                    role=StageRole.MANAGER,
                    label="Manager review",
                    due_date=TODAY + timedelta(days=10),
                ),
                StageCreate(sequence=3, role=StageRole.HR, label="HR sign-off"),
            ],
        ),
    )
    await session.commit()
    await competency_service.create_appraisal_template(
        session,
        hr,
        AppraisalTemplateCreate(
            cycle_id=cycle.id,
            rating_scale_id=scale["id"],
            competency_template_id=template["id"],
            goal_weight=D(50),
            competency_weight=D(50),
            bands=[
                BandSpec(label="Below", lower=D(0), upper=D(50)),
                BandSpec(label="Meets", lower=D(50), upper=D(80)),
                BandSpec(label="Exceeds", lower=D(80), upper=D(100)),
            ],
        ),
    )
    await cycle_config.create_rule(
        session,
        hr,
        RuleCreate(cycle_id=cycle.id, priority=100, template_id=template["id"], label="Everyone"),
    )
    await calibration_service.create_policy(
        session,
        hr,
        PolicyCreate(
            cycle_id=cycle.id,
            mode=CalibrationMode.ADVISORY,
            scope=CalibrationScope.DEPARTMENT,
            min_group_size=1,
            targets=[
                TargetSpec(rating_value="Exceeds", target_percent=D(20), tolerance_percent=D(20)),
                TargetSpec(rating_value="Meets", target_percent=D(60), tolerance_percent=D(40)),
                TargetSpec(rating_value="Below", target_percent=D(20), tolerance_percent=D(20)),
            ],
        ),
    )
    await session.commit()
    return {"cycle": cycle, "competencies": competencies, "template": template}


async def _goal_for(session, org, cycle, *, current: str) -> object:
    """Meera's one goal: 50 accounts, `current` closed."""
    goal = await goals_service.create_goal(
        session,
        org["meera"],
        GoalCreate(
            title="Close key accounts",
            weight=D(100),
            due_date=TODAY + timedelta(days=60),
            appraisal_cycle_id=cycle.id,
            key_results=[
                KeyResultCreate(title="Accounts closed", target_value=D(50)),
            ],
        ),
    )
    await goals_service.check_in(
        session,
        org["meera"],
        goal.id,
        GoalCheckInCreate(key_result_id=goal.key_results[0].id, value=D(current)),
    )
    return goal


async def _appraisal(session, cycle, employee) -> Appraisal:
    row = await session.scalar(
        select(Appraisal).where(
            Appraisal.cycle_id == cycle.id, Appraisal.employee_id == employee.id
        )
    )
    assert row is not None
    return row


def _ratings(config, collab: str, craft: str | None) -> dict:
    return {
        "competencies": [
            {"competency_id": str(config["competencies"][0]["id"]), "rating": collab},
            {
                "competency_id": str(config["competencies"][1]["id"]),
                "rating": craft,
                "is_applicable": craft is not None,
                "not_applicable_reason": None if craft else "Not part of this role.",
            },
        ]
    }


# ═══════════════════════════════════════════════════════════════════════════
# The full cycle
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_full_cycle_from_launch_to_acknowledgement(session: AsyncSession, org) -> None:
    config = await _configured_cycle(session, org)
    cycle = config["cycle"]
    await _goal_for(session, org, cycle, current="40")  # 80% of the goal

    # Recognition beside the linked competency, as evidence.
    session.add(
        Shoutout(
            tenant_id=org["tenant"].id,
            from_employee_id=org["boss_emp"].id,
            to_employee_id=org["meera_emp"].id,
            message="Owned the migration end to end.",
            value_tag="ownership",
        )
    )
    await session.commit()

    launched = await cycle_config.launch(session, org["hr"], cycle_id=cycle.id)
    await session.commit()
    assert launched["resolved"] == launched["included"] == 5
    await session.refresh(cycle)
    assert cycle.status is CycleStatus.ACTIVE

    appraisal = await _appraisal(session, cycle, org["meera_emp"])
    assert appraisal.competency_template_id == config["template"]["id"]

    # Launch told the people holding the first step.
    launched_notes = await session.scalar(
        select(func.count(Notification.id)).where(
            Notification.event_key == "performance.review.launched",
            Notification.recipient_user_id == org["meera"].id,
        )
    )
    assert launched_notes >= 1

    # ── Self review, by Meera ───────────────────────────────────────────────
    form = await review.review_detail(session, org["meera"], appraisal.id)
    assert form["viewer"]["can_act"] and form["viewer"]["is_subject"]
    assert form["goals"]["score"] == "80.00"
    collab = next(c for c in form["competencies"] if c["code"] == "COLLAB")
    assert collab["indicators"] and collab["evidence"]["recognition_count"] == 1

    await appraisal_service.save_draft(
        session,
        org["meera"],
        appraisal_id=appraisal.id,
        ratings=_ratings(config, "4", None),
        comments="Half done",
    )
    await session.commit()
    form = await review.review_detail(session, org["meera"], appraisal.id)
    assert form["draft"]["comments"] == "Half done"

    await appraisal_service.submit_stage(
        session,
        org["meera"],
        appraisal_id=appraisal.id,
        ratings=_ratings(config, "4", None),
        comments="A strong year.",
    )
    await session.commit()
    stage_open = await session.scalar(
        select(func.count(Notification.id)).where(
            Notification.event_key == "performance.review.stage_open",
            Notification.recipient_user_id == org["boss"].id,
        )
    )
    assert stage_open == 1, "the manager is told the step is theirs"

    # Meera cannot now write her own manager review.
    with pytest.raises(HTTPException) as refused:
        await appraisal_service.submit_stage(
            session, org["meera"], appraisal_id=appraisal.id, ratings=_ratings(config, "5", "5")
        )
    assert refused.value.status_code == 403

    # Before release Meera sees her own entry but not the outcome.
    form = await review.review_detail(session, org["meera"], appraisal.id)
    assert form["outcome"] is None
    assert [e["stage_role"] for e in form["entries"]] == ["self"]

    # ── Manager review, scored from goals and ratings ───────────────────────
    preview = await review.score_preview(
        session, org["boss"], appraisal_id=appraisal.id, ratings=_ratings(config, "4", "3")
    )
    # goals 80 x 50% + competencies ((4+3)/2 / 5 * 100 = 70) x 50% = 75
    assert preview["score"] == "75.00"
    assert preview["rating"]["label"] == "Meets"
    assert preview["goal"]["contributing"][0]["title"] == "Close key accounts"

    # A non-applicable competency is excluded rather than zero-scored.
    excluded = await review.score_preview(
        session, org["boss"], appraisal_id=appraisal.id, ratings=_ratings(config, "4", None)
    )
    assert excluded["score"] == "80.00"  # 80 x 50% + 80 x 50%

    await appraisal_service.submit_stage(
        session,
        org["boss"],
        appraisal_id=appraisal.id,
        ratings=_ratings(config, "4", "3"),
        comments="Solid delivery.",
    )
    await session.commit()
    entry = await session.scalar(
        select(AppraisalStageEntry)
        .join(AppraisalStage, AppraisalStage.id == AppraisalStageEntry.stage_id)
        .where(
            AppraisalStageEntry.appraisal_id == appraisal.id,
            AppraisalStage.role == StageRole.MANAGER,
        )
    )
    assert entry is not None and entry.score == D("75.00")
    assert entry.is_escalated is False

    # ── The goal score flows: more progress, a higher preview ───────────────
    goals = await goals_service.list_goals(session, org["meera"])
    await goals_service.check_in(
        session,
        org["boss"],
        goals[0].id,
        GoalCheckInCreate(key_result_id=goals[0].key_results[0].id, value=D(50)),
    )
    raised = await review.score_preview(
        session, org["hr"], appraisal_id=appraisal.id, ratings=_ratings(config, "4", "3")
    )
    assert raised["score"] == "85.00"

    # ── HR sign-off completes the chain, carrying the manager's score ───────
    await appraisal_service.submit_stage(session, org["hr"], appraisal_id=appraisal.id, ratings={})
    await session.commit()
    await session.refresh(appraisal)
    assert appraisal.status is AppraisalStatus.COMPLETED
    assert appraisal.final_score == D("75.00")
    assert appraisal.final_rating == "Meets"

    # ── Calibration: the head of department cannot calibrate, HR does ───────
    calib = await calibration_service.create_session(
        session,
        org["hr"],
        SessionCreate(
            cycle_id=cycle.id,
            name="Engineering",
            scope=CalibrationScope.DEPARTMENT,
            scope_value=str(org["dept"].id),
            facilitator_user_id=org["head"].id,
            participant_user_ids=[org["boss"].id],
        ),
    )
    await session.commit()

    with pytest.raises(HTTPException) as outsider:
        await calibration_service.session_workspace(session, org["ravi"], calib.id)
    assert outsider.value.status_code == 403

    workspace = await calibration_service.session_workspace(session, org["boss"], calib.id)
    meera_row = next(r for r in workspace["grid"] if r["employee_id"] == str(org["meera_emp"].id))
    assert meera_row["goal_score"] == "100.00"
    assert meera_row["recognition_count"] == 1
    assert workspace["rating_options"] == ["Below", "Meets", "Exceeds"]
    assert workspace["viewer"]["can_change"] is False, "a participant does not change ratings"

    with pytest.raises(HTTPException):
        await calibration_service.apply_change(
            session,
            org["boss"],
            session_id=calib.id,
            appraisal_id=appraisal.id,
            new_rating="Exceeds",
            justification="Participant trying.",
        )

    head_appraisal = await _appraisal(session, cycle, org["head_emp"])
    with pytest.raises(HTTPException) as own:
        await calibration_service.apply_change(
            session,
            org["head"],
            session_id=calib.id,
            appraisal_id=head_appraisal.id,
            new_rating="Exceeds",
            justification="x",
        )
    assert own.value.status_code == 403

    with pytest.raises(HTTPException) as bad_label:
        await calibration_service.apply_change(
            session,
            org["head"],
            session_id=calib.id,
            appraisal_id=appraisal.id,
            new_rating="Superstar",
            justification="Not a rating in this cycle.",
        )
    assert bad_label.value.status_code == 422

    await calibration_service.apply_change(
        session,
        org["head"],
        session_id=calib.id,
        appraisal_id=appraisal.id,
        new_rating="Exceeds",
        new_score=D(82),
        justification="Goal closed out fully after the manager review.",
    )
    await session.commit()

    evidence = await calibration_service.person_evidence(
        session, org["head"], session_id=calib.id, appraisal_id=appraisal.id
    )
    assert evidence["goals"]["score"] == "100.00"
    assert evidence["calibration_history"][0]["to"] == "Exceeds"

    with pytest.raises(HTTPException):
        await calibration_service.lock_session(session, org["head"], session_id=calib.id)
    locked = await calibration_service.lock_session(session, org["hr"], session_id=calib.id)
    await session.commit()
    assert locked["ratings_written"] == 1
    await session.refresh(appraisal)
    assert appraisal.final_rating == "Exceeds"

    reopened = await calibration_service.reopen_session(
        session, org["hr"], session_id=calib.id, reason="One more look."
    )
    assert reopened.status is SessionStatus.IN_PROGRESS
    await calibration_service.lock_session(session, org["hr"], session_id=calib.id)
    await session.commit()
    lock_audits = await session.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.entity_type == "calibration_session", AuditLog.entity_id == calib.id
        )
    )
    assert lock_audits >= 4  # created, locked, reopened, locked again

    # ── Release and acknowledge with an objection ───────────────────────────
    await appraisal_service.release(session, org["hr"], appraisal_id=appraisal.id)
    await session.commit()
    form = await review.review_detail(session, org["meera"], appraisal.id)
    assert form["outcome"]["final_rating"] == "Exceeds"
    assert form["outcome"]["calibrated"] is None, "the policy does not disclose calibration"
    assert form["viewer"]["can_acknowledge"]
    assert {e["stage_role"] for e in form["entries"]} == {"self", "manager", "hr"}

    with pytest.raises(HTTPException) as for_her:
        await appraisal_service.acknowledge(
            session,
            org["boss"],
            appraisal_id=appraisal.id,
            acknowledgement=AcknowledgementType.AGREED,
        )
    assert for_her.value.status_code == 403

    acked = await appraisal_service.acknowledge(
        session,
        org["meera"],
        appraisal_id=appraisal.id,
        acknowledgement=AcknowledgementType.DISAGREED,
        objection="The collaboration rating ignores the Q3 migration.",
    )
    await session.commit()
    assert acked.status is AppraisalStatus.ACKNOWLEDGED
    objections = await session.scalar(
        select(func.count(Notification.id)).where(
            Notification.event_key == "performance.review.objection",
            Notification.recipient_user_id == org["hr"].id,
        )
    )
    assert objections == 1

    mine = await review.my_reviews(session, org["meera"])
    assert mine[0]["final_rating"] == "Exceeds"


# ═══════════════════════════════════════════════════════════════════════════
# Visibility and refusals
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_colleague_cannot_read_a_review_and_another_tenant_gets_404(
    session: AsyncSession, org, other_hr_user: User
) -> None:
    config = await _configured_cycle(session, org)
    await cycle_config.launch(session, org["hr"], cycle_id=config["cycle"].id)
    await session.commit()
    appraisal = await _appraisal(session, config["cycle"], org["meera_emp"])

    with pytest.raises(HTTPException) as colleague:
        await review.review_detail(session, org["ravi"], appraisal.id)
    assert colleague.value.status_code == 403

    # The skip-level manager is in the line and may read it.
    detail = await review.review_detail(session, org["head"], appraisal.id)
    assert detail["appraisal"]["employee_name"] == "Meera Nair"

    with pytest.raises(HTTPException) as foreign:
        await review.review_detail(session, other_hr_user, appraisal.id)
    assert foreign.value.status_code == 404

    listed = await appraisal_service.list_appraisals(session, org["ravi"])
    assert {row["employee_name"] for row in listed} == {"Ravi Kumar"}
    with pytest.raises(HTTPException):
        await appraisal_service.list_appraisals(
            session, org["ravi"], employee_id=org["meera_emp"].id
        )


async def test_self_actions_are_refused_everywhere(session: AsyncSession, org) -> None:
    config = await _configured_cycle(session, org)
    await cycle_config.launch(session, org["hr"], cycle_id=config["cycle"].id)
    await session.commit()

    # HR's own review: HR writes the self review, but not the HR sign-off.
    hr_appraisal = await _appraisal(session, config["cycle"], org["hr_emp"])
    with pytest.raises(HTTPException) as own_manager_step:
        stages = await appraisal_service._stages(session, org["hr"], config["cycle"].id)
        hr_appraisal.current_stage_id = stages[1].id
        await session.commit()
        await appraisal_service.submit_stage(
            session,
            org["hr"],
            appraisal_id=hr_appraisal.id,
            ratings=_ratings(config, "5", "5"),
        )
    assert own_manager_step.value.status_code == 403

    # An increment for yourself.
    cycle = await performance.create_increment_cycle(
        session,
        org["hr"],
        name="FY27 increments",
        effective_from=TODAY,
        budget_total=D(1000000),
        bands=[
            {
                "rating_label": "Meets",
                "min_percentage": D(4),
                "max_percentage": D(8),
                "recommended_percentage": D(6),
            }
        ],
    )
    with pytest.raises(HTTPException) as own_increment:
        await performance.recommend_increment(
            session,
            org["boss"],
            cycle_id=cycle.id,
            employee_id=org["boss_emp"].id,
            rating_label="Meets",
            current_ctc=D(1000000),
            percentage=D(6),
        )
    assert own_increment.value.status_code == 403

    # A PIP on yourself, and a check-in on your own PIP.
    with pytest.raises(HTTPException) as own_pip:
        await performance.open_pip(
            session,
            org["boss"],
            employee_id=org["boss_emp"].id,
            start_date=TODAY,
            end_date=TODAY + timedelta(days=60),
            reason="r",
            expectations="e",
            support_provided="s",
            milestones=[{"description": "d", "target_date": TODAY, "measurable_criteria": "m"}],
        )
    assert own_pip.value.status_code == 403

    plan = await performance.open_pip(
        session,
        org["hr"],
        employee_id=org["boss_emp"].id,
        start_date=TODAY,
        end_date=TODAY + timedelta(days=60),
        reason="Shortfall.",
        expectations="Ship the backlog.",
        support_provided="Weekly coaching.",
        milestones=[
            {
                "description": "Backlog",
                "target_date": TODAY + timedelta(days=30),
                "measurable_criteria": "Zero open P1s",
            }
        ],
    )
    with pytest.raises(HTTPException) as own_check_in:
        await performance.record_check_in(
            session,
            org["boss"],
            pip_id=plan.id,
            scheduled_for=TODAY,
            held_on=TODAY,
            manager_comments="I am doing fine.",
            employee_input_state=EmployeeInputStateEnum.GIVEN,
            employee_comments="Yes.",
        )
    assert own_check_in.value.status_code == 403

    # A manager outside the line cannot record one either; the line above can.
    with pytest.raises(HTTPException):
        await performance.record_check_in(
            session,
            org["ravi"],
            pip_id=plan.id,
            scheduled_for=TODAY,
            held_on=TODAY,
            manager_comments="x",
            employee_input_state=EmployeeInputStateEnum.GIVEN,
            employee_comments="y",
        )
    row = await performance.record_check_in(
        session,
        org["head"],
        pip_id=plan.id,
        scheduled_for=TODAY,
        held_on=TODAY,
        manager_comments="Reviewed the backlog.",
        employee_input_state=EmployeeInputStateEnum.GIVEN,
        employee_comments="I need the staging environment fixed first.",
    )

    # The employee's own statement is theirs alone.
    with pytest.raises(HTTPException):
        await views.add_employee_statement(
            session, org["head"], pip_id=plan.id, statement="Written for him."
        )
    await views.add_employee_statement(
        session,
        org["boss"],
        pip_id=plan.id,
        check_in_id=row.id,
        statement="The staging outage cost me two weeks of the plan.",
    )
    with pytest.raises(HTTPException):
        await views.assess_milestone(
            session,
            org["boss"],
            pip_id=plan.id,
            milestone_id=(await views.pip_detail(session, org["hr"], plan.id))["milestones"][0][
                "id"
            ],
            milestone_status=MilestoneStatusEnum.MET,
            note="I met it.",
        )
    detail = await views.pip_detail(session, org["boss"], plan.id)
    assert detail["viewer"]["can_add_statement"] and not detail["viewer"]["can_record_check_in"]
    assert detail["statements"][0]["statement"].startswith("The staging outage")
    pack = await performance.evidence_pack(session, org["hr"], pip_id=plan.id, today=TODAY)
    assert pack["employee_statements"][0]["statement"].startswith("The staging outage")

    with pytest.raises(HTTPException):
        await views.pip_detail(session, org["ravi"], plan.id)
    assert [p["pip_id"] for p in await views.list_pips(session, org["head"])] == [str(plan.id)]
    assert await views.list_pips(session, org["ravi"]) == []


# ═══════════════════════════════════════════════════════════════════════════
# Team view, dashboard, reminders, escalation, delegation
# ═══════════════════════════════════════════════════════════════════════════


async def test_team_view_dashboard_reminders_and_escalation(session: AsyncSession, org) -> None:
    config = await _configured_cycle(session, org)
    cycle = config["cycle"]
    await _goal_for(session, org, cycle, current="30")
    await cycle_config.launch(session, org["hr"], cycle_id=cycle.id)
    await session.commit()
    meera = await _appraisal(session, cycle, org["meera_emp"])
    await appraisal_service.submit_stage(
        session, org["meera"], appraisal_id=meera.id, ratings=_ratings(config, "3", "3")
    )
    await session.commit()

    team = await review.team_overview(session, org["boss"])
    assert [r["employee_name"] for r in team] == ["Meera Nair"]
    assert team[0]["awaiting_me"] and team[0]["stage_role"] == "manager"
    assert {r["employee_name"] for r in await review.team_overview(session, org["head"])} == {
        "Meera Nair",
        "Vikram Shah",
    }
    assert await review.team_overview(session, org["ravi"]) == []

    progress = await appraisal_service.cycle_progress(session, org["hr"], cycle.id)
    assert progress["total"] == 5
    engineering = next(b for b in progress["by_department"] if b["department"] == "Engineering")
    assert engineering["total"] == 3 and engineering["pending"] == 3
    by_vikram = next(b for b in progress["by_manager"] if b["manager_name"] == "Vikram Shah")
    assert by_vikram["total"] == 1
    line_only = await appraisal_service.cycle_progress(session, org["boss"], cycle.id)
    assert line_only["total"] == 1

    reminded = await operations.send_stage_reminders(session, org["hr"], cycle_id=cycle.id)
    await session.commit()
    assert reminded["appraisals"] == 5
    boss_reminders = await session.scalar(
        select(func.count(Notification.id)).where(
            Notification.event_key == "performance.review.reminder",
            Notification.recipient_user_id == org["boss"].id,
        )
    )
    assert boss_reminders >= 1

    # Overdue past the window: the manager step goes to the head of department,
    # who may then act on it.
    stages = await appraisal_service._stages(session, org["hr"], cycle.id)
    stages[1].due_date = TODAY - timedelta(days=30)
    await session.commit()
    escalated = await operations.escalate_overdue(session, org["hr"], cycle_id=cycle.id)
    await session.commit()
    assert escalated["escalated"] == 1
    await session.refresh(meera)
    assert meera.escalated_to_user_id == org["head"].id
    again = await operations.escalate_overdue(session, org["hr"], cycle_id=cycle.id)
    assert again["escalated"] == 0, "each appraisal escalates once"

    # Delegation: Vikram hands his steps to Ravi's manager (HR's employee user
    # is HR anyway), so use a plain manager outside the line.
    cover_emp, cover = await _person(
        session,
        org["tenant"],
        await session.get(LegalEntity, org["meera_emp"].legal_entity_id),
        await session.get(Location, org["meera_emp"].location_id),
        code="MGR2",
        name="Anil Cover",
        role=UserRole.EMPLOYEE,
    )
    with pytest.raises(HTTPException):
        await appraisal_service.create_delegation(
            session,
            org["boss"],
            DelegationCreate(
                cycle_id=cycle.id,
                from_user_id=org["head"].id,
                to_user_id=cover.id,
                effective_from=TODAY,
                effective_to=TODAY + timedelta(days=5),
            ),
        )
    await appraisal_service.create_delegation(
        session,
        org["boss"],
        DelegationCreate(
            cycle_id=cycle.id,
            to_user_id=cover.id,
            effective_from=TODAY,
            effective_to=TODAY + timedelta(days=5),
            reason="Leave",
        ),
    )
    await session.commit()
    assert [r["employee_name"] for r in await review.team_overview(session, cover)] == [
        "Meera Nair"
    ]
    await appraisal_service.submit_stage(
        session, cover, appraisal_id=meera.id, ratings=_ratings(config, "3", "3")
    )
    await session.commit()
    entry = await session.scalar(
        select(AppraisalStageEntry).where(
            AppraisalStageEntry.appraisal_id == meera.id,
            AppraisalStageEntry.actor_user_id == cover.id,
        )
    )
    assert entry.delegated_from_user_id == org["boss"].id


async def test_scheduled_sweep_and_probation_scheduling(session: AsyncSession, org) -> None:
    org["meera_emp"].probation_end_date = TODAY + timedelta(days=20)
    await session.commit()
    result = await operations.run_performance_sweep(session, org["hr"])
    assert result["probation"]["scheduled"] == 1
    again = await cycle_config.schedule_due_probation_reviews(session, org["hr"])
    assert again["scheduled"] == 0
    probation = await cycle_config.list_probation_reviews(session, org["hr"])
    assert probation[0]["employee_name"] == "Meera Nair"
    due = await session.scalar(
        select(func.count(Notification.id)).where(
            Notification.event_key == "performance.probation.due",
            Notification.recipient_user_id == org["boss"].id,
        )
    )
    assert due == 1
    with pytest.raises(HTTPException):
        await operations.run_performance_sweep(session, org["boss"])


# ═══════════════════════════════════════════════════════════════════════════
# Goals
# ═══════════════════════════════════════════════════════════════════════════


async def test_goal_visibility_alignment_and_weights(session: AsyncSession, org) -> None:
    company = await goals_service.create_goal(
        session,
        org["hr"],
        GoalCreate(
            title="Grow revenue 20%", level=GoalLevel.COMPANY, due_date=TODAY + timedelta(days=90)
        ),
    )
    with pytest.raises(HTTPException):
        await goals_service.create_goal(
            session,
            org["boss"],
            GoalCreate(title="Nope", level=GoalLevel.COMPANY, due_date=TODAY),
        )

    boss_goal = await goals_service.create_goal(
        session,
        org["boss"],
        GoalCreate(
            title="Team ships Q3 roadmap",
            weight=D(60),
            due_date=TODAY + timedelta(days=60),
            parent_goal_id=company.id,
            key_results=[KeyResultCreate(title="Epics shipped", target_value=D(10))],
        ),
    )
    private = await goals_service.create_goal(
        session,
        org["meera"],
        GoalCreate(
            title="Speak up in design reviews",
            weight=D(20),
            due_date=TODAY + timedelta(days=60),
            visibility=GoalVisibility.SELF,
            key_results=[
                KeyResultCreate(title="Done", kind=KeyResultKind.MILESTONE, target_value=D(1))
            ],
        ),
    )
    # The manager sets a goal for Meera, aligned to his own.
    assigned = await goals_service.create_goal(
        session,
        org["boss"],
        GoalCreate(
            title="Deliver the billing epic",
            weight=D(80),
            due_date=TODAY + timedelta(days=60),
            owner_employee_id=org["meera_emp"].id,
            parent_goal_id=boss_goal.id,
            key_results=[KeyResultCreate(title="Stories done", target_value=D(40))],
        ),
    )
    assert assigned.parent_title == "Team ships Q3 roadmap"
    notified = await session.scalar(
        select(func.count(Notification.id)).where(
            Notification.event_key == "performance.goal.assigned",
            Notification.recipient_user_id == org["meera"].id,
        )
    )
    assert notified == 1

    # The weights budget: 80 + 20 already.
    with pytest.raises(HTTPException) as over:
        await goals_service.create_goal(
            session,
            org["meera"],
            GoalCreate(
                title="One more",
                weight=D(1),
                due_date=TODAY + timedelta(days=60),
                key_results=[KeyResultCreate(title="x", target_value=D(1))],
            ),
        )
    assert over.value.status_code == 422

    # Visibility.
    boss_sees = {
        g.title for g in await goals_service.list_goals(session, org["boss"], scope="team")
    }
    assert boss_sees == {"Deliver the billing epic"}, "the private goal is hidden"
    with pytest.raises(HTTPException):
        await goals_service.get_goal(session, org["boss"], private.id)
    with pytest.raises(HTTPException):
        await goals_service.get_goal(session, org["ravi"], assigned.id)
    assert (await goals_service.get_goal(session, org["meera"], boss_goal.id)).title
    assert (await goals_service.get_goal(session, org["hr"], private.id)).title

    # Alignment rules: not to a colleague's goal, not in a loop.
    with pytest.raises(HTTPException):
        await goals_service.create_goal(
            session,
            org["ravi"],
            GoalCreate(
                title="Mine",
                due_date=TODAY,
                parent_goal_id=boss_goal.id,
                key_results=[KeyResultCreate(title="x", target_value=D(1))],
            ),
        )
    with pytest.raises(HTTPException):
        await goals_service.update_goal(
            session, org["boss"], boss_goal.id, GoalUpdate(parent_goal_id=assigned.id)
        )
    alignable = await goals_service.alignable_parents(session, org["meera"])
    assert {g.title for g in alignable} == {"Grow revenue 20%", "Team ships Q3 roadmap"}

    # Progress: a colleague cannot check in; the owner can; cancelling excludes it.
    with pytest.raises(HTTPException):
        await goals_service.check_in(
            session,
            org["ravi"],
            assigned.id,
            GoalCheckInCreate(key_result_id=assigned.key_results[0].id, value=D(40)),
        )
    updated = await goals_service.check_in(
        session,
        org["meera"],
        assigned.id,
        GoalCheckInCreate(key_result_id=assigned.key_results[0].id, value=D(10), note="Sprint 1"),
    )
    assert updated.progress_percent == D("25.00")
    history = await goals_service.list_check_ins(session, org["boss"], assigned.id)
    assert history[0].previous_value == D(0) and history[0].new_value == D(10)

    cancelled = await goals_service.update_goal(
        session, org["boss"], assigned.id, GoalUpdate(status=GoalStatus.CANCELLED)
    )
    assert cancelled.status is GoalStatus.CANCELLED
    with pytest.raises(HTTPException):
        await goals_service.check_in(
            session,
            org["meera"],
            assigned.id,
            GoalCheckInCreate(key_result_id=assigned.key_results[0].id, value=D(20)),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Increments: the salary assignment and the review rating win
# ═══════════════════════════════════════════════════════════════════════════


async def test_increment_candidates_resolve_rating_and_hide_individual_pay(
    session: AsyncSession, org
) -> None:
    config = await _configured_cycle(session, org)
    await cycle_config.launch(session, org["hr"], cycle_id=config["cycle"].id)
    await session.commit()
    meera = await _appraisal(session, config["cycle"], org["meera_emp"])
    meera.final_rating = "Meets"
    await session.commit()

    cycle = await performance.create_increment_cycle(
        session,
        org["hr"],
        name="FY27 increments",
        effective_from=TODAY,
        budget_total=D(1000000),
        linked_appraisal_cycle_id=config["cycle"].id,
        bands=[
            {
                "rating_label": "Meets",
                "min_percentage": D(4),
                "max_percentage": D(8),
                "recommended_percentage": D(6),
            },
            {
                "rating_label": "Exceeds",
                "min_percentage": D(8),
                "max_percentage": D(14),
                "recommended_percentage": D(11),
            },
        ],
    )

    # A manager cannot type a CTC for somebody with no salary assignment.
    with pytest.raises(HTTPException) as typed:
        await performance.recommend_increment(
            session,
            org["boss"],
            cycle_id=cycle.id,
            employee_id=org["meera_emp"].id,
            rating_label=None,
            current_ctc=D(900000),
            percentage=D(6),
        )
    assert typed.value.status_code == 403

    # The rating comes from the review, not the caller.
    with pytest.raises(HTTPException) as wrong:
        await performance.recommend_increment(
            session,
            org["hr"],
            cycle_id=cycle.id,
            employee_id=org["meera_emp"].id,
            rating_label="Exceeds",
            current_ctc=D(900000),
            percentage=D(11),
        )
    assert "review rating is 'Meets'" in wrong.value.detail

    result = await performance.recommend_increment(
        session,
        org["hr"],
        cycle_id=cycle.id,
        employee_id=org["meera_emp"].id,
        rating_label=None,
        current_ctc=D(900000),
        percentage=D(6),
    )
    assert result["proposal"]["rating_label"] == "Meets"

    boss_view = await views.increment_candidates(
        session, await as_caller(session, org["boss"]), cycle.id
    )
    assert [r["name"] for r in boss_view["rows"]] == ["Meera Nair"]
    row = boss_view["rows"][0]
    assert row["rating_label"] == "Meets" and row["band"]["recommended"] == "6.00"
    parity = row["recommendation"]["equity"].get("parity")
    assert parity is None or ("peer_min" not in parity and "peer_max" not in parity)
    assert row["can_decide"] is False

    hr_view = await views.increment_candidates(session, org["hr"], cycle.id)
    assert len(hr_view["rows"]) == 5
    with pytest.raises(HTTPException):
        await views.increment_candidates(session, org["ravi"], cycle.id)

    people = await views.people(session, org["boss"])
    assert {p["name"] for p in people} == {"Vikram Shah", "Meera Nair"}
