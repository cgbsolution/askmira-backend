"""Work weeks per legal entity, entity holidays, unpaid holidays and salary days.

A group with a five-day company, a six-day one and an alternate-Saturday one:
each person's week comes from their own shift, else their legal entity's work
week, else the workspace default; payroll divides the month the way their
legal entity says (calendar, fixed 26, scheduled working days).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import AttendanceDay, AttendanceStatus, Shift
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import Holiday
from app.models.org import LegalEntity, Location
from app.models.payroll import Payslip
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.attendance import ShiftAssignmentCreate, ShiftCreate
from app.schemas.leave import HolidayCreate, HolidayUpdate
from app.schemas.org import LegalEntityUpdate
from app.services import org as org_service
from app.services.attendance import service as attendance_service
from app.services.attendance import setup as attendance_setup
from app.services.leave import holiday_calendar
from app.services.payroll import run as run_service
from tests.integration.conftest import requires_db
from tests.integration.test_payroll_run_flow import (  # noqa: F401  (fixture)
    _approved_rules,
    _lock,
    _open_run,
    org,
)

pytestmark = requires_db

# September 2026 starts on a Tuesday. Saturdays: 5, 12, 19, 26.
SEPT_START, SEPT_END = date(2026, 9, 1), date(2026, 9, 30)


async def _shift(
    session: AsyncSession,
    hr_user: User,
    code: str,
    weekly: list[int],
    alternate: dict[str, list[int]] | None = None,
) -> Shift:
    return await attendance_setup.create_shift(
        session,
        hr_user,
        ShiftCreate(
            code=code,
            name=code,
            start_time=time(9, 30),
            end_time=time(18, 30),
            break_minutes=60,
            half_day_threshold_minutes=240,
            full_day_threshold_minutes=420,
            weekly_off_days=weekly,
            alternate_off_weeks=alternate or {},
        ),
    )


async def _statuses(
    session: AsyncSession, employee_id: uuid.UUID
) -> dict[date, tuple[AttendanceStatus, Decimal]]:
    rows = (
        await session.scalars(select(AttendanceDay).where(AttendanceDay.employee_id == employee_id))
    ).all()
    return {d.attendance_date: (d.status, Decimal(str(d.payable_fraction))) for d in rows}


# ── Work week per legal entity ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_legal_entity_work_week_judges_people_without_a_shift(
    session: AsyncSession, hr_user: User, legal_entity: LegalEntity, employee: Employee
):
    general = await _shift(session, hr_user, "FIVE", [6, 7])  # the workspace default
    assert general.is_default
    alternate = await _shift(session, hr_user, "ALTSAT", [7], {"6": [2, 4]})

    await org_service.update_legal_entity(
        session,
        hr_user,
        legal_entity.id,
        LegalEntityUpdate(default_shift_id=alternate.id),
        request=None,
    )
    await attendance_service.recompute_range(
        session, hr_user, employee_ids=[employee.id], start=SEPT_START, end=SEPT_END
    )
    days = await _statuses(session, employee.id)
    assert days[date(2026, 9, 5)][0] is AttendanceStatus.ABSENT  # 1st Saturday: works
    assert days[date(2026, 9, 12)][0] is AttendanceStatus.WEEK_OFF  # 2nd: off
    assert days[date(2026, 9, 19)][0] is AttendanceStatus.ABSENT  # 3rd: works
    assert days[date(2026, 9, 26)][0] is AttendanceStatus.WEEK_OFF  # 4th: off
    assert days[date(2026, 9, 13)][0] is AttendanceStatus.WEEK_OFF  # Sunday

    # The person's own shift still beats the entity's week.
    await attendance_setup.assign_shift(
        session,
        hr_user,
        ShiftAssignmentCreate(
            employee_id=employee.id, shift_id=general.id, effective_from=SEPT_START
        ),
    )
    await attendance_service.recompute_range(
        session, hr_user, employee_ids=[employee.id], start=SEPT_START, end=SEPT_END
    )
    days = await _statuses(session, employee.id)
    assert days[date(2026, 9, 5)][0] is AttendanceStatus.WEEK_OFF


@pytest.mark.asyncio
async def test_a_legal_entity_refuses_another_workspaces_shift(
    session: AsyncSession,
    hr_user: User,
    other_hr_user: User,
    legal_entity: LegalEntity,
):
    theirs = await _shift(session, other_hr_user, "THEIRS", [7])
    with pytest.raises(HTTPException) as exc:
        await org_service.update_legal_entity(
            session,
            hr_user,
            legal_entity.id,
            LegalEntityUpdate(default_shift_id=theirs.id),
            request=None,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_fixed_salary_days_need_a_number(
    session: AsyncSession, hr_user: User, legal_entity: LegalEntity
):
    with pytest.raises(HTTPException) as exc:
        await org_service.update_legal_entity(
            session,
            hr_user,
            legal_entity.id,
            LegalEntityUpdate(salary_days_basis="fixed"),
            request=None,
        )
    assert exc.value.status_code == 422
    updated = await org_service.update_legal_entity(
        session,
        hr_user,
        legal_entity.id,
        LegalEntityUpdate(salary_days_basis="fixed", salary_fixed_days=26),
        request=None,
    )
    assert (updated.salary_days_basis, updated.salary_fixed_days) == ("fixed", 26)
    with pytest.raises(ValueError):
        LegalEntityUpdate(salary_days_basis="fixed", salary_fixed_days=45)
    with pytest.raises(ValueError):
        LegalEntityUpdate(salary_days_basis="weekly")  # type: ignore[arg-type]


# ── Holidays per legal entity, paid and unpaid ──────────────────────────────


@pytest.mark.asyncio
async def test_an_entity_holiday_applies_only_to_that_companys_people(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    location: Location,
    employee: Employee,
):
    await _shift(session, hr_user, "FIVE", [6, 7])
    sister = LegalEntity(tenant_id=tenant.id, name="Acme Manufacturing", code="ACMEMFG")
    session.add(sister)
    await session.commit()

    # September is past, so seeded directly: the calendar refuses past edits.
    session.add_all(
        [
            Holiday(
                tenant_id=tenant.id,
                name="Foundation day",
                holiday_date=date(2026, 9, 7),
                year=2026,
                legal_entity_id=legal_entity.id,
            ),
            Holiday(
                tenant_id=tenant.id,
                name="Sister foundation day",
                holiday_date=date(2026, 9, 8),
                year=2026,
                legal_entity_id=sister.id,
            ),
        ]
    )
    await session.commit()

    await attendance_service.recompute_range(
        session, hr_user, employee_ids=[employee.id], start=SEPT_START, end=SEPT_END
    )
    days = await _statuses(session, employee.id)
    assert days[date(2026, 9, 7)] == (AttendanceStatus.HOLIDAY, Decimal(1))
    assert days[date(2026, 9, 8)][0] is AttendanceStatus.ABSENT


@pytest.mark.asyncio
async def test_an_entity_holiday_refuses_another_entitys_office(
    session: AsyncSession, tenant: Tenant, hr_user: User, location: Location
):
    sister = LegalEntity(tenant_id=tenant.id, name="Acme Manufacturing", code="ACMEMFG")
    session.add(sister)
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await holiday_calendar.create_holiday(
            session,
            hr_user,
            HolidayCreate(
                name="Plant day",
                holiday_date=date(2027, 1, 5),
                location_id=location.id,
                legal_entity_id=sister.id,
            ),
        )
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        await holiday_calendar.create_holiday(
            session,
            hr_user,
            HolidayCreate(
                name="Ghost", holiday_date=date(2027, 1, 5), legal_entity_id=uuid.uuid4()
            ),
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_holiday_entity_and_pay_are_saved_and_read_back(
    session: AsyncSession, hr_user: User, legal_entity: LegalEntity
):
    created = await holiday_calendar.create_holiday(
        session,
        hr_user,
        HolidayCreate(
            name="Plant shutdown",
            holiday_date=date(2027, 1, 6),
            legal_entity_id=legal_entity.id,
            is_paid=False,
        ),
    )
    assert created.is_paid is False
    assert created.legal_entity_id == legal_entity.id
    assert created.legal_entity_name == legal_entity.name

    edited = await holiday_calendar.update_holiday(
        session, hr_user, created.id, HolidayUpdate(is_paid=True, legal_entity_id=None)
    )
    assert edited.is_paid is True
    assert edited.legal_entity_id is None
    listed = await holiday_calendar.list_holidays(session, hr_user, year=2027, scope="all")
    assert [(h.name, h.is_paid) for h in listed] == [("Plant shutdown", True)]


@pytest.mark.asyncio
async def test_an_unpaid_holiday_is_loss_of_pay(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
):
    await _shift(session, hr_user, "FIVE", [6, 7])
    session.add(
        Holiday(
            tenant_id=tenant.id,
            name="Plant shutdown",
            holiday_date=date(2026, 9, 9),
            year=2026,
            is_paid=False,
        )
    )
    await session.commit()
    await attendance_service.recompute_range(
        session, hr_user, employee_ids=[employee.id], start=date(2026, 9, 9), end=date(2026, 9, 9)
    )
    days = await _statuses(session, employee.id)
    assert days[date(2026, 9, 9)] == (AttendanceStatus.HOLIDAY, Decimal(0))
    lop = await attendance_service.get_lop_days(
        session, hr_user, employee.id, date(2026, 9, 9), date(2026, 9, 9), require_lock=False
    )
    assert lop == Decimal(1)


# ── Payroll: how the month is divided ───────────────────────────────────────


async def _absent(session: AsyncSession, employee: Employee, on: date) -> None:
    session.add(
        AttendanceDay(
            tenant_id=employee.tenant_id,
            employee_id=employee.id,
            attendance_date=on,
            status=AttendanceStatus.ABSENT,
            payable_fraction=0,
            computed_at=datetime.now(UTC),
        )
    )
    await session.commit()


async def _slip(session: AsyncSession, run_id: uuid.UUID, employee_id: uuid.UUID) -> Payslip:
    slip = await session.scalar(
        select(Payslip).where(Payslip.run_id == run_id, Payslip.employee_id == employee_id)
    )
    assert slip is not None
    return slip


@pytest.mark.asyncio
async def test_calendar_days_stay_the_default(session: AsyncSession, org) -> None:  # noqa: F811
    await _approved_rules(session, org, tds=False)
    session.add(_lock(org["tenant"], org["maker"], 2026, 4))
    await _absent(session, org["meera_emp"], date(2026, 4, 8))
    run = await _open_run(session, org, 4)
    await run_service.compute_run(session, org["maker"], run.id)
    slip = await _slip(session, run.id, org["meera_emp"].id)
    assert Decimal(str(slip.working_days)) == 30
    # 100,000 a month, one day of April's 30 unpaid.
    assert Decimal(str(slip.gross_earnings)) == Decimal("96666.67")


@pytest.mark.asyncio
async def test_a_fixed_26_day_month(session: AsyncSession, org) -> None:  # noqa: F811
    entity = org["entity"]
    entity.salary_days_basis = "fixed"
    entity.salary_fixed_days = 26
    await _approved_rules(session, org, tds=False)
    session.add(_lock(org["tenant"], org["maker"], 2026, 4))
    ravi = org["ravi_emp"]
    ravi.status = EmploymentStatus.EXITED
    ravi.date_of_exit = date(2026, 4, 15)
    await session.commit()
    await _absent(session, org["meera_emp"], date(2026, 4, 8))

    run = await _open_run(session, org, 4)
    computed = await run_service.compute_run(session, org["maker"], run.id)
    meera = await _slip(session, run.id, org["meera_emp"].id)
    assert Decimal(str(meera.working_days)) == 26
    assert Decimal(str(meera.paid_days)) == 25
    assert Decimal(str(meera.gross_earnings)) == Decimal("96153.85")  # 100,000 x 25/26

    # Left on 15 April: 15 of 30 calendar days is 13 of 26.
    leaver = await _slip(session, run.id, ravi.id)
    assert Decimal(str(leaver.not_employed_days)) == 13
    assert Decimal(str(leaver.gross_earnings)) == Decimal("50000.00")
    assert any(n.get("key") == "salary_days" for n in computed.input_notices)


@pytest.mark.asyncio
async def test_working_days_follow_each_persons_week(
    session: AsyncSession,
    org,  # noqa: F811
    hr_user: User,
) -> None:
    five = await _shift(session, hr_user, "FIVE", [6, 7])
    six = await _shift(session, hr_user, "SIX", [7])
    entity = org["entity"]
    entity.salary_days_basis = "working_days"
    entity.default_shift_id = five.id
    await _approved_rules(session, org, tds=False)
    session.add(_lock(org["tenant"], org["maker"], 2026, 4))
    await session.commit()
    # Ravi works a six-day week of his own.
    await attendance_setup.assign_shift(
        session,
        hr_user,
        ShiftAssignmentCreate(
            employee_id=org["ravi_emp"].id, shift_id=six.id, effective_from=date(2026, 4, 1)
        ),
    )
    await _absent(session, org["meera_emp"], date(2026, 4, 8))

    run = await _open_run(session, org, 4)
    await run_service.compute_run(session, org["maker"], run.id)

    # April 2026: 22 weekdays; 26 days that are not Sundays.
    meera = await _slip(session, run.id, org["meera_emp"].id)
    assert Decimal(str(meera.working_days)) == 22
    assert Decimal(str(meera.gross_earnings)) == Decimal("95454.55")  # 100,000 x 21/22
    ravi = await _slip(session, run.id, org["ravi_emp"].id)
    assert Decimal(str(ravi.working_days)) == 26
    assert Decimal(str(ravi.gross_earnings)) == Decimal("100000.00")
