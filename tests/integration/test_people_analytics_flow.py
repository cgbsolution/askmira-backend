"""People analytics: attrition is a rate, signals carry their evidence, scope holds.

What these hold:

- attrition divides exits by *average* headcount, and reports voluntary exits
  apart from the total;
- the absence signals name the rule and the facts behind each flag;
- a line manager sees only their reporting line; an employee with no reports
  sees nothing but themselves.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.models.attendance import AttendanceDay, AttendanceStatus
from app.models.employee import Employee, EmploymentStatus
from app.models.exit_workflow import ExitRecord, ExitStatus, ExitType
from app.models.user import User, UserRole
from app.services.reporting import analytics
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db

TODAY = date(2026, 10, 13)


def _person(tenant, entity, location, code, *, joined, left=None, manager=None) -> Employee:
    return Employee(
        tenant_id=tenant.id, employee_code=code, first_name=code, last_name="Test",
        work_email=f"{code.lower()}@acme.test", legal_entity_id=entity.id,
        location_id=location.id, manager_id=manager,
        status=EmploymentStatus.EXITED if left else EmploymentStatus.ACTIVE,
        date_of_joining=joined, date_of_exit=left,
    )


async def test_attrition_is_exits_over_average_headcount(
    session, tenant, hr_user, legal_entity, location
) -> None:
    people = [
        _person(tenant, legal_entity, location, f"E{i:02d}", joined=date(2024, 1, 1))
        for i in range(8)
    ]
    leaver = _person(
        tenant, legal_entity, location, "QUIT", joined=date(2024, 1, 1), left=date(2026, 9, 15)
    )
    fired = _person(
        tenant, legal_entity, location, "FIRE", joined=date(2024, 1, 1), left=date(2026, 9, 20)
    )
    session.add_all([*people, leaver, fired])
    await session.flush()
    session.add_all(
        [
            ExitRecord(
                tenant_id=tenant.id, employee_id=leaver.id, exit_type=ExitType.RESIGNATION,
                status=ExitStatus.SETTLED, resignation_date=date(2026, 8, 15),
            ),
            ExitRecord(
                tenant_id=tenant.id, employee_id=fired.id, exit_type=ExitType.TERMINATION,
                status=ExitStatus.SETTLED, resignation_date=date(2026, 9, 20),
            ),
        ]
    )
    await session.commit()

    result = await analytics.attrition(session, hr_user, today=TODAY, months=1)
    # October alone: nobody left. The window is one month.
    assert result["exits"] == 0

    september = await analytics.attrition(session, hr_user, today=date(2026, 9, 30), months=1)
    # Opened September with 10, closed with 8: average 9; two exits = 22.2%.
    assert september["average_headcount"] == "9.0"
    assert september["exits"] == 2 and september["voluntary_exits"] == 1
    assert september["attrition_percent"] == "22.2"
    assert september["voluntary_attrition_percent"] == "11.1"
    assert "average" in september["basis"]


async def test_a_manager_sees_only_their_line(
    session, tenant, hr_user, legal_entity, location
) -> None:
    boss = _person(tenant, legal_entity, location, "BOSS", joined=date(2024, 1, 1))
    session.add(boss)
    await session.flush()
    report = _person(tenant, legal_entity, location, "RPT", joined=date(2024, 1, 1),
                     manager=boss.id)
    other = _person(tenant, legal_entity, location, "OTH", joined=date(2024, 1, 1))
    session.add_all([report, other])
    await session.flush()
    login = User(
        tenant_id=tenant.id, email="boss@acme.test", password_hash="x", full_name="Boss",
        role=UserRole.EMPLOYEE, is_active=True, employee_id=boss.id,
    )
    session.add(login)
    await session.commit()
    login = await as_caller(session, login)

    trend = await analytics.headcount_trend(session, login, today=TODAY, months=1)
    assert trend[0]["headcount"] == 2, "the manager and their report, not the whole company"


async def test_signals_name_their_rule_and_evidence(
    session, tenant, hr_user, legal_entity, location
) -> None:
    worker = _person(tenant, legal_entity, location, "BUSY", joined=date(2025, 1, 1))
    session.add(worker)
    await session.flush()
    for offset in range(1, 21):
        day = TODAY - timedelta(days=offset)
        session.add(
            AttendanceDay(
                tenant_id=tenant.id, employee_id=worker.id, attendance_date=day,
                status=AttendanceStatus.PRESENT, payable_fraction=Decimal(1),
                worked_minutes=600, overtime_minutes=120, computed_at=datetime.now(UTC),
            )
        )
    await session.commit()

    signals = await analytics.absence_signals(session, hr_user, today=TODAY)
    mine = next(s for s in signals if s["employee_code"] == "BUSY")
    kinds = {s["kind"] for s in mine["signals"]}
    assert "burnout_risk" in kinds
    burnout = next(s for s in mine["signals"] if s["kind"] == "burnout_risk")
    assert burnout["rule"] and len(burnout["evidence"]) == 2
    assert "40.0 hours of overtime" in burnout["evidence"][1]


async def _days(session, tenant, employee, statuses: dict[date, AttendanceStatus]) -> None:
    for day, status in statuses.items():
        session.add(
            AttendanceDay(
                tenant_id=tenant.id, employee_id=employee.id, attendance_date=day,
                status=status,
                payable_fraction=Decimal(1) if status is AttendanceStatus.PRESENT else Decimal(0),
                worked_minutes=480 if status is AttendanceStatus.PRESENT else 0,
                computed_at=datetime.now(UTC),
            )
        )


async def test_the_bradford_factor_is_shown_with_its_inputs(
    session, tenant, hr_user, legal_entity, location
) -> None:
    worker = _person(tenant, legal_entity, location, "OFTEN", joined=date(2025, 1, 1))
    session.add(worker)
    await session.flush()
    # Five one-day absences each followed by a worked day, then one two-day absence.
    statuses: dict[date, AttendanceStatus] = {}
    start = TODAY - timedelta(days=60)
    for n in range(5):
        statuses[start + timedelta(days=n * 2)] = AttendanceStatus.ABSENT
        statuses[start + timedelta(days=n * 2 + 1)] = AttendanceStatus.PRESENT
    statuses[start + timedelta(days=20)] = AttendanceStatus.LOP
    statuses[start + timedelta(days=21)] = AttendanceStatus.LOP
    await _days(session, tenant, worker, statuses)
    await session.commit()

    scores = await analytics.absence_scores(session, hr_user, today=TODAY)
    row = next(r for r in scores["people"] if r["employee_code"] == "OFTEN")
    # Six spells, seven days: 6 x 6 x 7 = 252.
    assert (row["spells"], row["days"], row["bradford_factor"]) == (6, "7", 252)
    assert row["at_or_above_review"] is True
    assert "spells squared times days" in scores["method"]

    signals = await analytics.absence_signals(session, hr_user, today=TODAY)
    mine = next(s for s in signals if s["employee_code"] == "OFTEN")
    frequent = next(s for s in mine["signals"] if s["kind"] == "frequent_absence")
    assert "= 252" in frequent["evidence"][-1]


async def test_the_pay_signal_is_for_admins_only(
    session, tenant, hr_user, legal_entity, location
) -> None:
    long_server = _person(tenant, legal_entity, location, "STILL", joined=date(2024, 1, 10))
    session.add(long_server)
    sub_admin = User(
        tenant_id=tenant.id, email="ops@acme.test", password_hash="x", full_name="Ops",
        role=UserRole.SUB_ADMIN, is_active=True,
    )
    session.add(sub_admin)
    await session.commit()

    admin_view = await analytics.absence_signals(session, hr_user, today=TODAY)
    kinds = {s["kind"] for p in admin_view if p["employee_code"] == "STILL" for s in p["signals"]}
    assert "pay_revision" in kinds

    sub_admin = await as_caller(session, sub_admin)
    ops_view = await analytics.absence_signals(session, sub_admin, today=TODAY)
    kinds = {s["kind"] for p in ops_view if p["employee_code"] == "STILL" for s in p["signals"]}
    assert "pay_revision" not in kinds, "when pay last changed is payroll data"


async def test_the_digest_reaches_hr_and_the_flagged_persons_manager(
    session, tenant, hr_user, legal_entity, location
) -> None:
    boss = _person(tenant, legal_entity, location, "LEAD", joined=date(2024, 1, 1))
    session.add(boss)
    await session.flush()
    worker = _person(tenant, legal_entity, location, "TIRED", joined=date(2025, 1, 1),
                     manager=boss.id)
    session.add(worker)
    session.add(
        User(
            tenant_id=tenant.id, email="lead@acme.test", password_hash="x", full_name="Lead",
            role=UserRole.EMPLOYEE, is_active=True, employee_id=boss.id,
        )
    )
    await session.flush()
    monday = date(2026, 10, 12)
    for offset in range(1, 21):
        session.add(
            AttendanceDay(
                tenant_id=tenant.id, employee_id=worker.id,
                attendance_date=monday - timedelta(days=offset),
                status=AttendanceStatus.PRESENT, payable_fraction=Decimal(1),
                worked_minutes=600, overtime_minutes=120, computed_at=datetime.now(UTC),
            )
        )
    await session.commit()

    assert await analytics.send_signal_digest(session, hr_user, today=TODAY) == {"skipped": 1}
    outcome = await analytics.send_signal_digest(session, hr_user, today=monday)
    assert outcome["flagged"] >= 1
    assert outcome["notified"] == 2, "one HR user, and the tired person's own manager"


async def test_performance_overview_shows_trend_goals_and_rater_spread(
    session, tenant, hr_user, legal_entity, location
) -> None:
    from app.models.appraisal import (
        Appraisal,
        AppraisalCycle,
        AppraisalStage,
        AppraisalStageEntry,
        AppraisalStatus,
        CycleStatus,
        CycleType,
        StageRole,
    )
    from app.models.goals import Goal, GoalStatus
    from app.services.reporting import performance_analytics

    cycle = AppraisalCycle(
        tenant_id=tenant.id, name="FY26 annual", cycle_type=CycleType.ANNUAL,
        status=CycleStatus.CLOSED, period_start=date(2025, 4, 1), period_end=date(2026, 3, 31),
    )
    session.add(cycle)
    await session.flush()
    stage = AppraisalStage(
        tenant_id=tenant.id, cycle_id=cycle.id, sequence=1, role=StageRole.MANAGER,
        label="Manager",
    )
    session.add(stage)
    raters = []
    for name in ("Generous", "Tough", "Middle"):
        rater = User(
            tenant_id=tenant.id, email=f"{name.lower()}@acme.test", password_hash="x",
            full_name=name, role=UserRole.EMPLOYEE, is_active=True,
        )
        session.add(rater)
        raters.append(rater)
    await session.flush()

    scores = {"Generous": Decimal("4.5"), "Tough": Decimal("2.5"), "Middle": Decimal("3.5")}
    labels = {"Generous": "Exceeds", "Tough": "Below", "Middle": "Meets"}
    n = 0
    for rater in raters:
        for _ in range(5):
            person = _person(tenant, legal_entity, location, f"P{n:02d}", joined=date(2024, 1, 1))
            session.add(person)
            await session.flush()
            appraisal = Appraisal(
                tenant_id=tenant.id, cycle_id=cycle.id, employee_id=person.id,
                status=AppraisalStatus.COMPLETED if n % 5 else AppraisalStatus.IN_PROGRESS,
                final_rating=labels[rater.full_name], final_score=scores[rater.full_name],
            )
            session.add(appraisal)
            await session.flush()
            session.add(
                AppraisalStageEntry(
                    tenant_id=tenant.id, appraisal_id=appraisal.id, stage_id=stage.id,
                    actor_user_id=rater.id, score=scores[rater.full_name],
                    submitted_at=datetime.now(UTC),
                )
            )
            session.add(
                Goal(
                    tenant_id=tenant.id, owner_employee_id=person.id, title="Ship it",
                    due_date=date(2026, 3, 31), appraisal_cycle_id=cycle.id,
                    status=GoalStatus.COMPLETED if n % 3 == 0 else GoalStatus.ACTIVE,
                    progress_percent=Decimal(100) if n % 3 == 0 else Decimal(40),
                )
            )
            n += 1
    await session.commit()

    overview = await performance_analytics.performance_overview(session, hr_user)
    trend = overview["rating_trend"]
    assert trend["ratings"] == ["Exceeds", "Meets", "Below"], "ordered by the scores behind them"
    only = trend["cycles"][0]
    assert only["appraisal_count"] == 15 and only["completed"] == 12
    assert only["completion_percent"] == "80.0"
    assert only["mean_final_score"] == "3.50"

    goals = overview["goal_attainment"]["cycles"][0]
    assert goals["goals"] == 15 and goals["completed"] == 5
    assert goals["completed_percent"] == "33.3"

    spread = overview["rater_spread"]
    assert spread["overall_mean"] == "3.50"
    by_name = {r["rater"]: r for r in spread["raters"]}
    assert by_name["Generous"]["flagged"] and by_name["Generous"]["direction"] == "higher"
    assert by_name["Tough"]["flagged"] and by_name["Tough"]["direction"] == "lower"
    assert not by_name["Middle"]["flagged"]
