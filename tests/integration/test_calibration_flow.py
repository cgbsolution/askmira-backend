"""Slice 17 gates that need a real database.

The distribution arithmetic is proved in `tests/appraisal/`. What only a
database settles: that a justification is refused at the service layer (not just
the screen), that locking writes ratings back, that reopening needs an HR
override, and that the workspace stays accurate at scale.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appraisal import (
    Appraisal,
    AppraisalCycle,
    AppraisalStatus,
    CycleType,
)
from app.models.calibration import (
    CalibrationChange,
    CalibrationMode,
    CalibrationPolicy,
    CalibrationScope,
    CalibrationSession,
    DistributionTarget,
    SessionStatus,
)
from app.models.employee import Employee
from app.models.org import Department, LegalEntity
from app.models.tenant import Tenant
from app.models.user import User
from app.services.appraisal import calibration_service
from tests.integration.conftest import requires_db

pytestmark = requires_db

RATINGS = ["Outstanding", "Exceeds", "Meets", "Below"]


async def _cycle(
    session: AsyncSession, tenant: Tenant, *, name: str = "FY26 review"
) -> AppraisalCycle:
    # The name is a parameter because (tenant, name, period_start) is unique —
    # a second cycle has to be created with its own name, not renamed after.
    cycle = AppraisalCycle(
        tenant_id=tenant.id, name=name, cycle_type=CycleType.ANNUAL,
        period_start=date(2026, 4, 1), period_end=date(2027, 3, 31),
    )
    session.add(cycle)
    await session.commit()
    await session.refresh(cycle)
    return cycle


async def _policy(
    session: AsyncSession,
    tenant: Tenant,
    cycle: AppraisalCycle,
    *,
    mode: CalibrationMode = CalibrationMode.ENFORCED,
    min_group_size: int = 8,
) -> CalibrationPolicy:
    policy = CalibrationPolicy(
        tenant_id=tenant.id, cycle_id=cycle.id, mode=mode,
        scope=CalibrationScope.DEPARTMENT, min_group_size=min_group_size,
    )
    session.add(policy)
    await session.flush()
    for rating, target, tolerance in [
        ("Outstanding", 10, 2), ("Exceeds", 20, 5),
        ("Meets", 55, 10), ("Below", 15, 5),
    ]:
        session.add(
            DistributionTarget(
                tenant_id=tenant.id, policy_id=policy.id, rating_value=rating,
                target_percent=Decimal(target), tolerance_percent=Decimal(tolerance),
            )
        )
    await session.commit()
    await session.refresh(policy)
    return policy


async def _department(
    session: AsyncSession, tenant: Tenant, legal_entity: LegalEntity, name: str
) -> Department:
    dept = Department(tenant_id=tenant.id, name=name, code=name[:8].upper())
    session.add(dept)
    await session.commit()
    await session.refresh(dept)
    return dept


async def _population(
    session: AsyncSession,
    tenant: Tenant,
    cycle: AppraisalCycle,
    legal_entity: LegalEntity,
    department: Department,
    *,
    ratings: list[str],
    prefix: str = "CAL",
) -> list[Appraisal]:
    """One employee and one rated appraisal per entry."""
    out: list[Appraisal] = []
    for index, rating in enumerate(ratings):
        employee = Employee(
            tenant_id=tenant.id, employee_code=f"{prefix}{index:03d}",
            first_name="Person", last_name=str(index),
            work_email=f"{prefix.lower()}{index}@acme.test",
            legal_entity_id=legal_entity.id, department_id=department.id,
            grade="L3",
        )
        session.add(employee)
        await session.flush()
        appraisal = Appraisal(
            tenant_id=tenant.id, cycle_id=cycle.id, employee_id=employee.id,
            status=AppraisalStatus.COMPLETED, final_rating=rating,
            final_score=Decimal(70),
        )
        session.add(appraisal)
        out.append(appraisal)
    await session.commit()
    return out


async def _session(
    session: AsyncSession,
    tenant: Tenant,
    cycle: AppraisalCycle,
    department: Department,
    hr_user: User,
) -> CalibrationSession:
    row = CalibrationSession(
        tenant_id=tenant.id, cycle_id=cycle.id, name="Engineering calibration",
        scope=CalibrationScope.DEPARTMENT, scope_value=str(department.id),
        facilitator_user_id=hr_user.id,
        participant_user_ids=[str(hr_user.id)],
        scheduled_at=datetime.now(UTC),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


def _shape(outstanding=0, exceeds=0, meets=0, below=0) -> list[str]:
    return (
        ["Outstanding"] * outstanding + ["Exceeds"] * exceeds
        + ["Meets"] * meets + ["Below"] * below
    )


# ═══════════════════════════════════════════════════════════════════════════
# Distribution across a real cycle
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_compliant_department_can_close(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    cycle = await _cycle(session, tenant)
    await _policy(session, tenant, cycle)
    dept = await _department(session, tenant, legal_entity, "Engineering")
    await _population(
        session, tenant, cycle, legal_entity, dept,
        ratings=_shape(outstanding=1, exceeds=2, meets=6, below=1),
    )

    result = await calibration_service.cycle_distribution(session, hr_user, cycle.id)

    assert result["mode"] == "enforced"
    assert result["can_close"] is True
    assert result["blockers"] == []


async def test_enforced_mode_blocks_and_names_the_department(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate.

    A facilitator needs to know which scope and by how much, not just that
    something is wrong.
    """
    cycle = await _cycle(session, tenant)
    await _policy(session, tenant, cycle)
    dept = await _department(session, tenant, legal_entity, "Engineering")
    await _population(
        session, tenant, cycle, legal_entity, dept,
        ratings=_shape(outstanding=5, exceeds=2, meets=2, below=1),
    )

    result = await calibration_service.cycle_distribution(session, hr_user, cycle.id)

    assert result["can_close"] is False
    assert result["blockers"]

    # 5 of 10 Outstanding and 2 of 10 Meets both breach, so the blocker under
    # test is found by rating rather than by position.
    outstanding = next(
        b for b in result["blockers"] if b["rating"] == "Outstanding"
    )
    assert outstanding["scope_value"] == str(dept.id)
    assert outstanding["count"] == 5
    assert "points above" in outstanding["message"]


async def test_advisory_mode_shows_the_gap_but_permits_closure(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate: all three modes switchable per cycle."""
    cycle = await _cycle(session, tenant)
    await _policy(session, tenant, cycle, mode=CalibrationMode.ADVISORY)
    dept = await _department(session, tenant, legal_entity, "Engineering")
    await _population(
        session, tenant, cycle, legal_entity, dept,
        ratings=_shape(outstanding=5, exceeds=2, meets=2, below=1),
    )

    result = await calibration_service.cycle_distribution(session, hr_user, cycle.id)

    assert result["can_close"] is True, "advisory does not block"
    distribution = result["distributions"][0]
    assert distribution["is_within_tolerance"] is False, "but the gap is still shown"


async def test_none_mode_computes_no_distribution_at_all(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    cycle = await _cycle(session, tenant)
    await _policy(session, tenant, cycle, mode=CalibrationMode.NONE)
    dept = await _department(session, tenant, legal_entity, "Engineering")
    await _population(
        session, tenant, cycle, legal_entity, dept, ratings=_shape(outstanding=10)
    )

    result = await calibration_service.cycle_distribution(session, hr_user, cycle.id)
    assert result["distributions"] == []
    assert result["blockers"] == []


async def test_a_small_department_is_excluded_and_says_why(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate.

    A three-person team cannot express a 10% target, and forcing it means
    rating someone by arithmetic.
    """
    cycle = await _cycle(session, tenant)
    await _policy(session, tenant, cycle, min_group_size=8)
    small = await _department(session, tenant, legal_entity, "Design")
    await _population(
        session, tenant, cycle, legal_entity, small,
        ratings=_shape(outstanding=1, meets=2), prefix="DSG",
    )

    result = await calibration_service.cycle_distribution(session, hr_user, cycle.id)

    assert result["can_close"] is True, "not enforced against a group this size"
    assert result["excluded_scopes"]
    reason = result["excluded_scopes"][0]["reason"]
    assert "3 employee(s) in scope" in reason
    assert "rating someone by arithmetic" in reason


# ═══════════════════════════════════════════════════════════════════════════
# The justification — the gate
# ═══════════════════════════════════════════════════════════════════════════


async def _ready(session, tenant, hr_user, legal_entity):
    cycle = await _cycle(session, tenant)
    await _policy(session, tenant, cycle)
    dept = await _department(session, tenant, legal_entity, "Engineering")
    appraisals = await _population(
        session, tenant, cycle, legal_entity, dept,
        ratings=_shape(outstanding=5, exceeds=2, meets=2, below=1),
    )
    calib = await _session(session, tenant, cycle, dept, hr_user)
    return cycle, dept, appraisals, calib


async def test_a_change_without_a_justification_is_refused_by_the_service(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate: verified by direct service call, not UI validation.

    The API must not be able to bypass what the screen enforces.
    """
    _, _, appraisals, calib = await _ready(session, tenant, hr_user, legal_entity)

    with pytest.raises(HTTPException) as exc:
        await calibration_service.apply_change(
            session, hr_user, session_id=calib.id,
            appraisal_id=appraisals[0].id, new_rating="Meets", justification="   ",
        )
    assert exc.value.status_code == 422
    assert "needs a justification" in exc.value.detail

    stored = (
        await session.scalars(
            select(CalibrationChange).where(CalibrationChange.session_id == calib.id)
        )
    ).all()
    assert stored == [], "nothing was recorded"


async def test_a_change_records_the_previous_value_the_actor_and_the_reason(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate: every change traceable to actor, time and reason."""
    _, _, appraisals, calib = await _ready(session, tenant, hr_user, legal_entity)

    change = await calibration_service.apply_change(
        session, hr_user, session_id=calib.id, appraisal_id=appraisals[0].id,
        new_rating="Exceeds", new_score=Decimal(78),
        justification="Strong year, but the delivery goal slipped a quarter.",
    )
    await session.commit()

    assert change.previous_rating == "Outstanding"
    assert change.new_rating == "Exceeds"
    assert change.changed_by_user_id == hr_user.id
    assert change.changed_at is not None
    assert "delivery goal slipped" in change.justification

    await session.refresh(appraisals[0])
    assert appraisals[0].final_rating == "Outstanding", (
        "the appraisal is untouched until the session locks"
    )


async def test_a_change_starts_the_session(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    _, _, appraisals, calib = await _ready(session, tenant, hr_user, legal_entity)
    assert calib.status is SessionStatus.SCHEDULED

    await calibration_service.apply_change(
        session, hr_user, session_id=calib.id, appraisal_id=appraisals[0].id,
        new_rating="Exceeds", justification="Discussed in the session.",
    )
    await session.commit()

    assert calib.status is SessionStatus.IN_PROGRESS
    assert calib.started_at is not None


async def test_a_change_to_another_cycles_appraisal_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    _, _, appraisals, calib = await _ready(session, tenant, hr_user, legal_entity)
    other = await _cycle(session, tenant, name="A different cycle")
    dept = await _department(session, tenant, legal_entity, "Other")
    stray = await _population(
        session, tenant, other, legal_entity, dept, ratings=["Meets"], prefix="OTH"
    )

    with pytest.raises(HTTPException) as exc:
        await calibration_service.apply_change(
            session, hr_user, session_id=calib.id, appraisal_id=stray[0].id,
            new_rating="Exceeds", justification="Wrong cycle.",
        )
    assert "different cycle" in exc.value.detail


# ═══════════════════════════════════════════════════════════════════════════
# The workspace
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_workspace_returns_the_grid_and_a_live_distribution(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    _, _, _, calib = await _ready(session, tenant, hr_user, legal_entity)

    workspace = await calibration_service.session_workspace(session, hr_user, calib.id)

    assert len(workspace["grid"]) == 10
    assert workspace["distribution"]["population"] == 10
    assert workspace["distribution"]["is_within_tolerance"] is False
    assert workspace["mode"] == "enforced"
    assert "L3" in workspace["peer_scores_by_grade"]

    row = workspace["grid"][0]
    for key in ("employee_code", "name", "grade", "current_rating", "current_score"):
        assert key in row


async def test_the_workspace_distribution_stays_accurate_at_scale(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate, at 200 employees in scope.

    Assembled in one call so the meter cannot disagree with the rows above it.
    """
    cycle = await _cycle(session, tenant)
    await _policy(session, tenant, cycle)
    dept = await _department(session, tenant, legal_entity, "Engineering")
    await _population(
        session, tenant, cycle, legal_entity, dept,
        ratings=_shape(outstanding=20, exceeds=40, meets=110, below=30),
        prefix="BIG",
    )
    calib = await _session(session, tenant, cycle, dept, hr_user)

    workspace = await calibration_service.session_workspace(session, hr_user, calib.id)

    assert len(workspace["grid"]) == 200
    distribution = workspace["distribution"]
    assert distribution["population"] == 200

    by_rating = {c["rating"]: c for c in distribution["counts"]}
    assert by_rating["Outstanding"]["count"] == 20
    assert by_rating["Outstanding"]["actual_percent"] == "10.00"
    assert by_rating["Meets"]["count"] == 110
    assert by_rating["Meets"]["actual_percent"] == "55.00"
    assert distribution["is_within_tolerance"] is True

    # The grid and the meter come from the same query, so they agree.
    counted = sum(1 for row in workspace["grid"] if row["current_rating"] == "Outstanding")
    assert counted == by_rating["Outstanding"]["count"]


# ═══════════════════════════════════════════════════════════════════════════
# Locking and reopening
# ═══════════════════════════════════════════════════════════════════════════


async def test_locking_writes_the_agreed_ratings_back(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate."""
    _, _, appraisals, calib = await _ready(session, tenant, hr_user, legal_entity)

    await calibration_service.apply_change(
        session, hr_user, session_id=calib.id, appraisal_id=appraisals[0].id,
        new_rating="Meets", new_score=Decimal(65),
        justification="Moved down after peer comparison at grade.",
    )
    await session.commit()

    result = await calibration_service.lock_session(
        session, hr_user, session_id=calib.id
    )
    await session.commit()

    assert result["ratings_written"] == 1
    await session.refresh(appraisals[0])
    assert appraisals[0].final_rating == "Meets"
    assert Decimal(str(appraisals[0].final_score)) == Decimal(65)

    assert calib.status is SessionStatus.COMPLETED
    assert calib.locked_at is not None
    assert calib.final_distribution["population"] == 10


async def test_the_last_change_in_a_session_wins_and_both_stay_on_record(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """A rating moved twice ends where the room left it, and the history shows
    the room changed its mind."""
    _, _, appraisals, calib = await _ready(session, tenant, hr_user, legal_entity)

    for rating, why in [
        ("Meets", "Initial move down."),
        ("Exceeds", "Reconsidered after the reviewer spoke."),
    ]:
        await calibration_service.apply_change(
            session, hr_user, session_id=calib.id, appraisal_id=appraisals[0].id,
            new_rating=rating, justification=why,
        )
    await session.commit()

    await calibration_service.lock_session(session, hr_user, session_id=calib.id)
    await session.commit()

    await session.refresh(appraisals[0])
    assert appraisals[0].final_rating == "Exceeds"

    history = (
        await session.scalars(
            select(CalibrationChange)
            .where(CalibrationChange.appraisal_id == appraisals[0].id)
            .order_by(CalibrationChange.changed_at)
        )
    ).all()
    assert len(history) == 2
    assert [h.new_rating for h in history] == ["Meets", "Exceeds"]


async def test_a_locked_session_refuses_further_changes(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    _, _, appraisals, calib = await _ready(session, tenant, hr_user, legal_entity)
    await calibration_service.lock_session(session, hr_user, session_id=calib.id)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await calibration_service.apply_change(
            session, hr_user, session_id=calib.id, appraisal_id=appraisals[0].id,
            new_rating="Meets", justification="Too late.",
        )
    assert "locked" in exc.value.detail


async def test_reopening_a_calibrated_appraisal_requires_an_hr_override(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate.

    The rating being undone was agreed by a group of people who are not in the
    room when it is undone.
    """
    _, _, appraisals, calib = await _ready(session, tenant, hr_user, legal_entity)
    await calibration_service.apply_change(
        session, hr_user, session_id=calib.id, appraisal_id=appraisals[0].id,
        new_rating="Meets", justification="Moved down after peer comparison.",
    )
    await calibration_service.lock_session(session, hr_user, session_id=calib.id)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await calibration_service.reopen_calibrated(
            session, hr_user, appraisal_id=appraisals[0].id,
            reason="Manager disputes the outcome.",
        )
    assert exc.value.status_code == 403
    assert "cannot be reopened without an HR override" in exc.value.detail["message"]
    assert exc.value.detail["calibration_history"][0]["to"] == "Meets"

    reopened = await calibration_service.reopen_calibrated(
        session, hr_user, appraisal_id=appraisals[0].id,
        reason="Manager disputes the outcome; HR agreed to revisit.",
        hr_override=True,
    )
    await session.commit()
    assert reopened.reopened_count == 1


async def test_reopening_keeps_the_calibration_history(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """Reopening adds to the record; it does not erase the session."""
    _, _, appraisals, calib = await _ready(session, tenant, hr_user, legal_entity)
    await calibration_service.apply_change(
        session, hr_user, session_id=calib.id, appraisal_id=appraisals[0].id,
        new_rating="Meets", justification="Moved down after peer comparison.",
    )
    await calibration_service.lock_session(session, hr_user, session_id=calib.id)
    await calibration_service.reopen_calibrated(
        session, hr_user, appraisal_id=appraisals[0].id,
        reason="Revisiting.", hr_override=True,
    )
    await session.commit()

    history = (
        await session.scalars(
            select(CalibrationChange).where(
                CalibrationChange.appraisal_id == appraisals[0].id
            )
        )
    ).all()
    assert len(history) == 1
    assert history[0].justification


async def test_an_uncalibrated_appraisal_reopens_without_an_override(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The override exists because a group agreed the rating. Where no group
    did, it is an ordinary reopen."""
    _, _, appraisals, _ = await _ready(session, tenant, hr_user, legal_entity)

    reopened = await calibration_service.reopen_calibrated(
        session, hr_user, appraisal_id=appraisals[0].id, reason="Ordinary correction."
    )
    await session.commit()
    assert reopened.reopened_count == 1


async def test_calibration_is_tenant_isolated(
    session: AsyncSession, tenant: Tenant, hr_user: User, other_hr_user: User,
    legal_entity: LegalEntity,
) -> None:
    _, _, _, calib = await _ready(session, tenant, hr_user, legal_entity)

    with pytest.raises(HTTPException) as exc:
        await calibration_service.session_workspace(session, other_hr_user, calib.id)
    assert exc.value.status_code == 404
