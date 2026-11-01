"""Time off, the second pass, against a real database.

Approval and cancellation moving attendance in the same commit; comp-off from
claim to credit to leave; HR's balance adjustments and the history that has to
add up to the card; the year-end carry-forward; optional holidays; two halves
of one day; and a policy correction that actually takes effect.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import AttendanceDay, AttendanceStatus
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import (
    Holiday,
    LeaveBalance,
    LeaveBalanceAdjustment,
    LeavePolicy,
    LeaveType,
)
from app.models.org import LegalEntity, Location
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.leave import (
    BalanceAdjustmentCreate,
    CompOffCreate,
    LeavePolicyCreate,
    LeaveRequestCreate,
    LeaveSettingsUpdate,
    LeaveTypeCreate,
)
from app.services.leave import comp_off, holiday_calendar, ledger
from app.services.leave import service as leave_service
from app.services.leave.holidays import holidays_for_employees
from tests.integration.conftest import requires_db

pytestmark = requires_db

TODAY = leave_service.local_today()


def _weekday_before(day: date) -> date:
    day -= timedelta(days=1)
    while day.isoweekday() in (6, 7):
        day -= timedelta(days=1)
    return day


def _saturday_before(day: date) -> date:
    day -= timedelta(days=1)
    while day.isoweekday() != 6:
        day -= timedelta(days=1)
    return day


def _next_monday(after: date) -> date:
    return after + timedelta(days=(7 - after.weekday()) % 7 or 7)


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
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2020, 1, 1),
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
    hr_emp, hr = await _person(
        session,
        tenant,
        legal_entity,
        location,
        code="HR1",
        name="Priya Ramesh",
        role=UserRole.ADMIN,
    )
    boss_emp, boss = await _person(
        session,
        tenant,
        legal_entity,
        location,
        code="MGR1",
        name="Vikram Shah",
        role=UserRole.EMPLOYEE,
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
    )
    earned = LeaveType(tenant_id=tenant.id, name="Earned Leave", code="EL")
    session.add(earned)
    await session.flush()
    session.add(
        LeavePolicy(
            tenant_id=tenant.id,
            leave_type_id=earned.id,
            name="Earned leave - everyone",
            annual_quota_days=Decimal(24),
            accrual_frequency="annual",
            max_carry_forward_days=Decimal(5),
            effective_from=date(2020, 1, 1),
        )
    )
    await session.commit()
    await session.refresh(earned)
    return {
        "hr": hr,
        "hr_emp": hr_emp,
        "boss": boss,
        "meera": meera,
        "meera_emp": meera_emp,
        "earned": earned,
        "tenant": tenant,
        "location": location,
    }


async def _available(session: AsyncSession, org, code: str, year: int = TODAY.year) -> Decimal:
    rows = await leave_service.balances(session, org["meera"], employee_id=None, year=year)
    return next(r.available for r in rows if r.leave_type_code == code)


async def test_approving_past_leave_recomputes_attendance_and_cancelling_reverts_it(
    session: AsyncSession, org
) -> None:
    day = _weekday_before(TODAY)
    created = await leave_service.apply(
        session,
        org["meera"],
        LeaveRequestCreate(
            leave_type_id=org["earned"].id, start_date=day, end_date=day, reason="Unwell"
        ),
    )
    await leave_service.decide(session, org["boss"], created.id, approve=True, note=None)

    attendance = await session.scalar(
        select(AttendanceDay).where(
            AttendanceDay.employee_id == org["meera_emp"].id, AttendanceDay.attendance_date == day
        )
    )
    assert attendance is not None
    assert attendance.status is AttendanceStatus.ON_LEAVE

    # Started leave: only HR may cancel, and attendance follows in the same commit.
    await leave_service.cancel(session, org["hr"], created.id)
    await session.refresh(attendance)
    assert attendance.status is not AttendanceStatus.ON_LEAVE


async def test_comp_off_is_credited_on_approval_and_can_then_be_taken(
    session: AsyncSession, org, monkeypatch: pytest.MonkeyPatch
) -> None:
    worked = _saturday_before(TODAY)

    # Not set up yet: refused with what to do about it.
    with pytest.raises(HTTPException) as refused:
        await comp_off.create(
            session, org["meera"], CompOffCreate(worked_date=worked, reason="Release weekend")
        )
    assert refused.value.status_code == 409

    comp_type = await leave_service.create_type(
        session,
        org["hr"],
        LeaveTypeCreate(name="Comp Off", code="CO", is_comp_off=True),
    )

    with pytest.raises(HTTPException) as weekday:
        await comp_off.create(
            session,
            org["meera"],
            CompOffCreate(worked_date=_weekday_before(TODAY), reason="Stayed late"),
        )
    assert weekday.value.status_code == 422

    claim = await comp_off.create(
        session, org["meera"], CompOffCreate(worked_date=worked, reason="Release weekend")
    )
    assert claim.approver_name == "Vikram Shah"
    assert claim.worked_day_label == "Weekly off"
    assert [c.id for c in await comp_off.approvals(session, org["boss"])] == [claim.id]

    with pytest.raises(HTTPException) as own:
        await comp_off.decide(session, org["meera"], claim.id, approve=True, note=None)
    assert own.value.status_code == 403

    decided = await comp_off.decide(session, org["boss"], claim.id, approve=True, note=None)
    assert decided.expires_on == TODAY + timedelta(days=60)
    credit = await session.scalar(
        select(LeaveBalanceAdjustment).where(LeaveBalanceAdjustment.source_id == claim.id)
    )
    assert credit is not None and credit.kind == "comp_off"
    assert Decimal(str(credit.days)) == Decimal(1)
    assert await _available(session, org, "CO") == Decimal(1)

    # No policy is needed to spend it: the ledger is the entitlement.
    monday = _next_monday(TODAY + timedelta(days=7))
    preview = await leave_service.preview(
        session,
        org["meera"],
        LeaveRequestCreate(leave_type_id=comp_type.id, start_date=monday, end_date=monday),
    )
    assert preview.allowed, preview.problems
    assert preview.available_after == Decimal(0)

    # Lapses once the expiry has passed and it has not been used. From November
    # that day is in the next leave year: the daily job then closes this year
    # first (carrying the comp-off forward, the default) and lapses it there.
    lapse_day = TODAY + timedelta(days=61)
    if lapse_day.year > TODAY.year:
        await _close_year_as_on(session, org, monkeypatch, lapse_day)
    result = await ledger.expire_comp_offs(session, org["hr"], today=lapse_day)
    assert result["credits_expired"] == 1
    assert await _available(session, org, "CO", year=lapse_day.year) == Decimal(0)


async def _close_year_as_on(
    session: AsyncSession, org, monkeypatch: pytest.MonkeyPatch, day: date
) -> None:
    """Close TODAY's leave year as the daily job does on `day` in the next one."""
    monkeypatch.setattr(leave_service, "local_today", lambda: day)
    await ledger.close_year(session, org["hr"], year=TODAY.year)


async def _approved_comp_off(session: AsyncSession, org) -> LeaveType:
    """A comp-off type, and one day of comp-off for Meera, approved today."""
    comp_type = await leave_service.create_type(
        session, org["hr"], LeaveTypeCreate(name="Comp Off", code="CO", is_comp_off=True)
    )
    claim = await comp_off.create(
        session,
        org["meera"],
        CompOffCreate(worked_date=_saturday_before(TODAY), reason="Release weekend"),
    )
    await comp_off.decide(session, org["boss"], claim.id, approve=True, note=None)
    return comp_type


async def _ledger(session: AsyncSession, org, kind: str, year: int) -> list[LeaveBalanceAdjustment]:
    return list(
        (
            await session.scalars(
                select(LeaveBalanceAdjustment).where(
                    LeaveBalanceAdjustment.employee_id == org["meera_emp"].id,
                    LeaveBalanceAdjustment.kind == kind,
                    LeaveBalanceAdjustment.year == year,
                )
            )
        ).all()
    )


async def test_by_default_unused_comp_off_carries_into_the_new_year(
    session: AsyncSession, org, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = await leave_service.get_settings(session, org["hr"])
    assert settings.comp_off_year_end == "carry_forward"
    await _approved_comp_off(session, org)
    new_year = date(TODAY.year + 1, 1, 1)
    await _close_year_as_on(session, org, monkeypatch, new_year)

    [carried] = await _ledger(session, org, "carry_forward_in", new_year.year)
    assert Decimal(str(carried.days)) == Decimal(1)
    assert await _available(session, org, "CO") == Decimal(0)
    assert await _available(session, org, "CO", year=new_year.year) == Decimal(1)
    assert await _ledger(session, org, "lapse", TODAY.year) == []


async def test_an_admin_can_make_unused_comp_off_lapse_at_year_end(
    session: AsyncSession, org, monkeypatch: pytest.MonkeyPatch
) -> None:
    await leave_service.update_settings(
        session, org["hr"], LeaveSettingsUpdate(comp_off_year_end="lapse")
    )
    assert (await leave_service.get_settings(session, org["hr"])).comp_off_year_end == "lapse"
    await _approved_comp_off(session, org)

    # The employee sees the real last day: never past 31 December.
    [credit] = await _ledger(session, org, "comp_off", TODAY.year)
    assert credit.expires_on == min(TODAY + timedelta(days=60), date(TODAY.year, 12, 31))

    new_year = date(TODAY.year + 1, 1, 1)
    await _close_year_as_on(session, org, monkeypatch, new_year)
    [lapsed] = await _ledger(session, org, "lapse", TODAY.year)
    assert Decimal(str(lapsed.days)) == Decimal(-1)
    assert lapsed.reason == "Unused comp-off lapses at year end (workspace setting)"
    assert await _available(session, org, "CO") == Decimal(0)
    assert await _ledger(session, org, "carry_forward_in", new_year.year) == []

    # The expiry job does not lapse the same day a second time.
    await ledger.expire_comp_offs(session, org["hr"], today=new_year + timedelta(days=90))
    assert await _ledger(session, org, "comp_off_expiry", new_year.year) == []


async def test_only_an_admin_decides_whether_comp_off_lapses(
    session: AsyncSession, org, tenant: Tenant, legal_entity: LegalEntity, location: Location
) -> None:
    _, hr_exec = await _person(
        session,
        tenant,
        legal_entity,
        location,
        code="HR2",
        name="Asha Menon",
        role=UserRole.SUB_ADMIN,
    )
    with pytest.raises(HTTPException) as refused:
        await leave_service.update_settings(
            session, hr_exec, LeaveSettingsUpdate(comp_off_year_end="lapse")
        )
    assert refused.value.status_code == 403
    assert "Only an admin" in refused.value.detail
    # The rest of the leave settings stay HR's, and sending today's rule back is no change.
    updated = await leave_service.update_settings(
        session,
        hr_exec,
        LeaveSettingsUpdate(comp_off_expiry_days=45, comp_off_year_end="carry_forward"),
    )
    assert updated.comp_off_expiry_days == 45 and updated.comp_off_year_end == "carry_forward"


async def test_hr_adjustments_move_the_balance_and_the_history_adds_up(
    session: AsyncSession, org
) -> None:
    before = await _available(session, org, "EL")
    base = {"employee_id": org["meera_emp"].id, "leave_type_id": org["earned"].id}

    await ledger.adjust(
        session,
        org["hr"],
        BalanceAdjustmentCreate(
            **base, year=TODAY.year, kind="opening", days=Decimal(3), reason="Migrated balance"
        ),
    )
    await ledger.adjust(
        session,
        org["hr"],
        BalanceAdjustmentCreate(
            **base, year=TODAY.year, kind="credit", days=Decimal(2), reason="Award"
        ),
    )
    await ledger.adjust(
        session,
        org["hr"],
        BalanceAdjustmentCreate(
            **base, year=TODAY.year, kind="debit", days=Decimal("0.5"), reason="Correction"
        ),
    )
    after = await _available(session, org, "EL")
    assert after == before + Decimal("4.5")

    # Encashment reads the same live figure and debits it in the caller's
    # transaction; more than the balance holds is refused.
    assert (
        await ledger.live_available(
            session,
            org["hr"],
            employee_id=org["meera_emp"].id,
            leave_type_code="EL",
            year=TODAY.year,
        )
        == after
    )
    await ledger.post_encashment(
        session,
        org["hr"],
        employee_id=org["meera_emp"].id,
        leave_type_code="EL",
        year=TODAY.year,
        days=Decimal(2),
        source_id=org["earned"].id,
    )
    await session.commit()
    row = await session.scalar(
        select(LeaveBalance).where(
            LeaveBalance.employee_id == org["meera_emp"].id, LeaveBalance.year == TODAY.year
        )
    )
    assert row is not None and Decimal(str(row.encashed)) == Decimal(2)
    after = await _available(session, org, "EL")
    with pytest.raises(HTTPException) as too_many:
        await ledger.post_encashment(
            session,
            org["hr"],
            employee_id=org["meera_emp"].id,
            leave_type_code="EL",
            year=TODAY.year,
            days=after + 1,
            source_id=org["earned"].id,
        )
    assert too_many.value.status_code == 409

    history = await ledger.history(
        session,
        org["meera"],
        employee_id=None,
        leave_type_id=org["earned"].id,
        year=TODAY.year,
    )
    assert history.available == after
    assert history.entries[-1].running_balance == after
    kinds = {e.kind for e in history.entries}
    assert {"accrual", "opening", "credit", "debit", "encashment"} <= kinds

    with pytest.raises(HTTPException) as employee:
        await ledger.adjust(
            session,
            org["meera"],
            BalanceAdjustmentCreate(
                **base, year=TODAY.year, kind="credit", days=Decimal(5), reason="Please"
            ),
        )
    assert employee.value.status_code == 403
    with pytest.raises(HTTPException) as own:
        await ledger.adjust(
            session,
            org["hr"],
            BalanceAdjustmentCreate(
                employee_id=org["hr_emp"].id,
                leave_type_id=org["earned"].id,
                year=TODAY.year,
                kind="credit",
                days=Decimal(5),
                reason="Mine",
            ),
        )
    assert own.value.status_code == 403


async def test_year_end_carries_up_to_the_limit_lapses_the_rest_and_runs_once(
    session: AsyncSession, org
) -> None:
    last_year = TODAY.year - 1
    # A year nobody kept balances for closes nothing: carrying forward leave
    # earned before the workspace existed would be leave nobody earned here.
    nothing = await ledger.close_year(session, org["hr"], year=last_year - 1)
    assert nothing.balances_closed == 0

    await leave_service.run_accruals(session, org["hr"], year=last_year)
    first = await ledger.close_year(session, org["hr"], year=last_year)
    # Three people, 24 days each, a five-day limit.
    assert first.balances_closed == 3
    assert first.days_carried == Decimal(15)
    assert first.days_lapsed == Decimal(57)

    old = await session.scalar(
        select(LeaveBalance).where(
            LeaveBalance.employee_id == org["meera_emp"].id, LeaveBalance.year == last_year
        )
    )
    new = await session.scalar(
        select(LeaveBalance).where(
            LeaveBalance.employee_id == org["meera_emp"].id, LeaveBalance.year == TODAY.year
        )
    )
    assert old is not None and new is not None
    assert Decimal(str(new.carried_forward)) == Decimal(5)
    assert await _available(session, org, "EL", last_year) == Decimal(0)
    assert await _available(session, org, "EL") == Decimal(29)

    again = await ledger.close_year(session, org["hr"], year=last_year)
    assert again.balances_closed == 0 and again.already_closed == 3
    assert await _available(session, org, "EL") == Decimal(29)


async def test_optional_holidays_count_for_those_who_chose_them_within_the_quota(
    session: AsyncSession, org
) -> None:
    upcoming = [TODAY + timedelta(days=offset) for offset in (20, 21, 22)]
    for index, day in enumerate(upcoming):
        session.add(
            Holiday(
                tenant_id=org["tenant"].id,
                name=f"Festival {index}",
                holiday_date=day,
                year=day.year,
                is_optional=True,
            )
        )
    session.add(
        Holiday(
            tenant_id=org["tenant"].id,
            name="Maharashtra Day",
            holiday_date=upcoming[0],
            year=upcoming[0].year,
            state_code="MH",
        )
    )
    await session.commit()
    offered = await holiday_calendar.optional_holidays(session, org["meera"], year=upcoming[0].year)
    options = [h for h in offered.holidays if h.holiday_date.year == upcoming[0].year]
    assert offered.quota == 2
    # A Maharashtra holiday is not on a Bengaluru employee's calendar at all.
    mine = await holiday_calendar.list_holidays(session, org["meera"], year=upcoming[0].year)
    assert "Maharashtra Day" not in {h.name for h in mine}

    await holiday_calendar.select_optional(session, org["meera"], options[0].id)
    await holiday_calendar.select_optional(session, org["meera"], options[1].id)
    with pytest.raises(HTTPException) as over:
        await holiday_calendar.select_optional(session, org["meera"], options[2].id)
    assert over.value.status_code == 409

    resolved = await holidays_for_employees(
        session, org["meera"], [org["meera_emp"].id, org["hr_emp"].id], upcoming[0], upcoming[-1]
    )
    assert set(resolved[org["meera_emp"].id]) == {options[0].holiday_date, options[1].holiday_date}
    assert resolved[org["hr_emp"].id] == {}


async def test_two_halves_of_one_day_and_a_same_day_policy_correction(
    session: AsyncSession, org
) -> None:
    monday = _next_monday(TODAY + timedelta(days=14))
    base = {"leave_type_id": org["earned"].id, "start_date": monday, "end_date": monday}
    await leave_service.apply(
        session,
        org["meera"],
        LeaveRequestCreate(**base, is_half_day=True, half_day_session="first_half"),
    )
    second = await leave_service.apply(
        session,
        org["meera"],
        LeaveRequestCreate(**base, is_half_day=True, half_day_session="second_half"),
    )
    assert second.half_day_session == "second_half"
    with pytest.raises(HTTPException) as clash:
        await leave_service.apply(
            session,
            org["meera"],
            LeaveRequestCreate(**base, is_half_day=True, half_day_session="first_half"),
        )
    assert clash.value.status_code == 422

    # A correction effective the same day replaces the original rather than
    # losing to it: the quota change shows up in the balance.
    await leave_service.create_policy(
        session,
        org["hr"],
        LeavePolicyCreate(
            leave_type_id=org["earned"].id,
            name="Earned leave - corrected",
            annual_quota_days=Decimal(30),
            accrual_frequency="annual",
            effective_from=date(2020, 1, 1),
        ),
    )
    policies = await leave_service.list_policies(session, org["hr"])
    assert [p.name for p in policies] == ["Earned leave - corrected"]
    rows = await leave_service.balances(session, org["meera"], employee_id=None, year=TODAY.year)
    assert rows[0].accrued == Decimal(30)
    assert rows[0].pending == Decimal(1)
    assert rows[0].leave_type_id == org["earned"].id
