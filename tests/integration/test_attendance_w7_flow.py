"""W7 attendance against a real database.

Shift and roster assignment → punches in local time → recompute → a
regularisation the manager decides → the payroll lock. And who may do each:
nobody decides their own, a locked period refuses the change, another tenant
gets a 404.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_session
from app.main import app
from app.models.attendance import (
    AttendanceDay,
    AttendanceStatus,
    PunchDirection,
    PunchSource,
    RegularisationStatus,
    ShiftAssignment,
)
from app.models.audit import AuditLog
from app.models.employee import Employee, EmploymentStatus
from app.models.org import LegalEntity, Location
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.attendance import (
    RosterAssignmentCreate,
    RosterCreate,
    RosterDayIn,
    ShiftAssignmentCreate,
    ShiftCreate,
)
from app.services.attendance import csv_import, review, setup
from app.services.attendance import service as attendance_service
from tests.integration.conftest import requires_db

pytestmark = requires_db

IST = ZoneInfo("Asia/Kolkata")
CSV_ONE_ROW = b"employee_code,punched_at,direction\nEMP1,2026-04-06 09:00,in\n"
MONDAY = date(2026, 4, 6)
TUESDAY = date(2026, 4, 7)


def ist(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=IST).astimezone(UTC)


async def _person(
    session: AsyncSession,
    tenant: Tenant,
    entity: LegalEntity,
    location: Location,
    *,
    code: str,
    role: UserRole,
    manager: Employee | None = None,
) -> tuple[Employee, User]:
    employee = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code.title(),
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
        full_name=code.title(),
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
async def team(session, tenant, legal_entity, location):
    manager, manager_user = await _person(
        session, tenant, legal_entity, location, code="MGR1", role=UserRole.MANAGER
    )
    report, report_user = await _person(
        session, tenant, legal_entity, location, code="EMP1", role=UserRole.EMPLOYEE,
        manager=manager,
    )
    outsider, outsider_user = await _person(
        session, tenant, legal_entity, location, code="EMP2", role=UserRole.EMPLOYEE
    )
    return {
        "manager": manager,
        "manager_user": manager_user,
        "report": report,
        "report_user": report_user,
        "outsider": outsider,
        "outsider_user": outsider_user,
    }


async def _general_shift(session, hr_user):
    return await setup.create_shift(
        session,
        hr_user,
        ShiftCreate(
            code="gen",
            name="General",
            start_time=time(9, 0),
            end_time=time(18, 0),
            break_minutes=60,
            grace_in_minutes=10,
            grace_out_minutes=10,
            half_day_threshold_minutes=240,
            full_day_threshold_minutes=480,
            weekly_off_days=[6, 7],
        ),
    )


async def test_the_whole_flow_assignment_punch_recompute_regularise_lock(
    session, hr_user, team
):
    report, report_user = team["report"], team["report_user"]
    manager_user = team["manager_user"]
    shift = await _general_shift(session, hr_user)
    assert shift.code == "GEN"

    # A roster first, then a direct shift, which wins over the roster.
    roster = await setup.create_roster(
        session,
        hr_user,
        RosterCreate(
            name="Two-day",
            cycle_days=2,
            days=[RosterDayIn(day_index=0, shift_id=shift.id), RosterDayIn(day_index=1)],
        ),
    )
    assert len(roster.days) == 2
    await setup.assign_roster(
        session,
        hr_user,
        RosterAssignmentCreate(
            employee_id=report.id,
            roster_id=roster.id,
            cycle_start_date=date(2026, 4, 1),
            effective_from=date(2026, 4, 1),
        ),
    )
    await setup.assign_shift(
        session,
        hr_user,
        ShiftAssignmentCreate(
            employee_id=report.id, shift_id=shift.id, effective_from=date(2026, 4, 1)
        ),
    )
    listed = await setup.list_assignments(session, hr_user, employee_id=report.id)
    assert {a.kind for a in listed} == {"shift", "roster"}

    # Punches in Indian time: 09:05 is inside a 10-minute grace, not late.
    for at, direction in (
        (ist(MONDAY, 9, 5), PunchDirection.IN),
        (ist(MONDAY, 18, 10), PunchDirection.OUT),
        (ist(TUESDAY, 9, 0), PunchDirection.IN),
    ):
        await attendance_service.record_punch(
            session, report_user, employee_id=report.id, punched_at=at,
            direction=direction, source=PunchSource.WEB,
        )

    await attendance_service.recompute_range(
        session, hr_user, employee_ids=[report.id], start=MONDAY, end=TUESDAY
    )
    period = await review.my_period(
        session, report_user, employee_id=report.id, start=MONDAY, end=TUESDAY
    )
    by_date = {d.attendance_date: d for d in period.days}
    assert by_date[MONDAY].status is AttendanceStatus.PRESENT
    assert by_date[MONDAY].late_by_minutes == 0
    assert by_date[MONDAY].early_by_minutes == 0
    assert by_date[TUESDAY].status is AttendanceStatus.NEEDS_REGULARISATION
    assert period.summary.needs_regularisation == 1

    # The employee raises a correction for the missed punch-out.
    reg = await attendance_service.raise_regularisation(
        session, report_user, employee_id=report.id, attendance_date=TUESDAY,
        requested_status=AttendanceStatus.PRESENT, reason="Forgot to punch out",
    )
    queue = await review.pending_regularisations(session, manager_user)
    assert [item.id for item in queue] == [reg.id]
    assert await review.pending_regularisations(session, team["outsider_user"]) == []

    # Somebody outside the reporting line cannot decide it.
    with pytest.raises(HTTPException) as exc:
        await attendance_service.decide_regularisation(
            session, team["outsider_user"], reg.id, approve=True
        )
    assert exc.value.status_code == 403

    # The payroll lock stops the approval until an audited unlock.
    lock = await attendance_service.lock_period(
        session, hr_user, date(2026, 4, 1), date(2026, 4, 30)
    )
    with pytest.raises(HTTPException) as exc:
        await attendance_service.decide_regularisation(
            session, manager_user, reg.id, approve=True
        )
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        await attendance_service.recompute_range(
            session, hr_user, employee_ids=[report.id], start=MONDAY, end=TUESDAY
        )
    assert exc.value.status_code == 409

    await attendance_service.unlock_period(session, hr_user, lock.id, "Late regularisation")
    decided = await attendance_service.decide_regularisation(
        session, manager_user, reg.id, approve=True, note="Seen on CCTV"
    )
    assert decided.status is RegularisationStatus.APPROVED
    day = await session.scalar(
        select(AttendanceDay).where(
            AttendanceDay.employee_id == report.id, AttendanceDay.attendance_date == TUESDAY
        )
    )
    await session.refresh(day)
    assert day.status is AttendanceStatus.PRESENT
    assert day.is_regularised

    await attendance_service.lock_period(session, hr_user, date(2026, 4, 1), date(2026, 4, 30))
    lop = await attendance_service.get_lop_days(
        session, hr_user, report.id, MONDAY, TUESDAY
    )
    assert lop == 0

    summaries = (
        await session.scalars(
            select(AuditLog).where(AuditLog.entity_type == "attendance_period_lock")
        )
    ).all()
    assert any("Late regularisation" in (row.summary or "") for row in summaries)


async def test_nobody_decides_their_own_regularisation(session, hr_user, team):
    manager, manager_user = team["manager"], team["manager_user"]
    reg = await attendance_service.raise_regularisation(
        session, manager_user, employee_id=manager.id, attendance_date=MONDAY,
        requested_status=AttendanceStatus.PRESENT, reason="Client visit",
    )
    assert await review.pending_regularisations(session, manager_user) == []
    with pytest.raises(HTTPException) as exc:
        await attendance_service.decide_regularisation(
            session, manager_user, reg.id, approve=True
        )
    assert exc.value.status_code == 403
    # HR can, because it is not HR's own.
    queue = await review.pending_regularisations(session, hr_user)
    assert reg.id in {item.id for item in queue}


async def test_a_rejection_needs_a_reason_and_a_duplicate_is_refused(session, team):
    report, report_user = team["report"], team["report_user"]
    reg = await attendance_service.raise_regularisation(
        session, report_user, employee_id=report.id, attendance_date=MONDAY,
        requested_status=AttendanceStatus.PRESENT, reason="Missed punch",
    )
    with pytest.raises(HTTPException) as exc:
        await attendance_service.raise_regularisation(
            session, report_user, employee_id=report.id, attendance_date=MONDAY,
            requested_status=AttendanceStatus.HALF_DAY, reason="Again",
        )
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        await attendance_service.decide_regularisation(
            session, team["manager_user"], reg.id, approve=False, note="  "
        )
    assert exc.value.status_code == 422
    withdrawn = await attendance_service.cancel_regularisation(session, report_user, reg.id)
    assert withdrawn.status is RegularisationStatus.CANCELLED


async def test_another_tenant_gets_a_404(session, other_hr_user, team):
    reg = await attendance_service.raise_regularisation(
        session, team["report_user"], employee_id=team["report"].id,
        attendance_date=MONDAY, requested_status=AttendanceStatus.PRESENT, reason="x-tenant",
    )
    with pytest.raises(HTTPException) as exc:
        await attendance_service.decide_regularisation(
            session, other_hr_user, reg.id, approve=True
        )
    assert exc.value.status_code == 404


async def test_a_new_assignment_closes_the_open_one(session, hr_user, team):
    report = team["report"]
    shift = await _general_shift(session, hr_user)
    first = await setup.assign_shift(
        session, hr_user,
        ShiftAssignmentCreate(employee_id=report.id, shift_id=shift.id,
                              effective_from=date(2026, 4, 1)),
    )
    await setup.assign_shift(
        session, hr_user,
        ShiftAssignmentCreate(employee_id=report.id, shift_id=shift.id,
                              effective_from=date(2026, 4, 10)),
    )
    row = await session.get(ShiftAssignment, first.id)
    await session.refresh(row)
    assert row.effective_to == date(2026, 4, 9)
    with pytest.raises(HTTPException) as exc:
        await setup.assign_shift(
            session, hr_user,
            ShiftAssignmentCreate(employee_id=report.id, shift_id=shift.id,
                                  effective_from=date(2026, 4, 5)),
        )
    assert exc.value.status_code == 409


async def test_csv_import_reports_rows_and_deduplicates(session, hr_user, team):
    text = (
        "employee_code,punched_at,direction\n"
        "EMP1,2026-04-06 09:00,in\n"
        "EMP1,2026-04-06 18:00,out\n"
        "NOPE,2026-04-06 09:00,in\n"
        "EMP1,not a time,in\n"
    )
    parsed, errors = csv_import.parse_punch_csv(
        text, timezone_name="Asia/Kolkata", now=datetime(2026, 4, 7, tzinfo=UTC)
    )
    assert [e.row for e in errors] == [4]

    def rows():
        return [
            attendance_service.ImportRow(
                row=p.row, employee_code=p.employee_code, punched_at=p.punched_at,
                direction=p.direction, device_id=None, raw={"row": p.row},
            )
            for p in parsed
        ]

    first = await attendance_service.import_punch_rows(
        session, hr_user, rows(), errors=[e.as_dict() for e in errors]
    )
    assert first["imported"] == 2
    assert first["skipped_unknown_employee"] == ["NOPE"]
    assert {e["row"] for e in first["errors"]} == {3, 4}
    again = await attendance_service.import_punch_rows(session, hr_user, rows())
    assert again["imported"] == 0 and again["duplicates"] == 2


async def test_nightly_recompute_skips_a_locked_day(session, hr_user, team):
    await attendance_service.lock_period(session, hr_user, date(2026, 4, 1), date(2026, 4, 30))
    # Inside the locked month nothing is written, and the job still finishes.
    # (Unlocked March before it is backfilled; that is the backfill's job.)
    result = await attendance_service.nightly_recompute(
        session, hr_user, as_of=MONDAY + timedelta(days=1)
    )
    assert result["days_computed"] == 0
    # 30 April to 2 May: the locked 30th is skipped, 1-2 May are computed.
    result = await attendance_service.nightly_recompute(session, hr_user, as_of=date(2026, 5, 3))
    assert result["days_computed"] >= 2


# ── Through HTTP: the gates ─────────────────────────────────────────────────


@pytest.fixture
async def as_user(session: AsyncSession) -> AsyncGenerator:
    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_session] = override_session
    transport = httpx.ASGITransport(app=app)

    def client_for(user: User) -> httpx.AsyncClient:
        app.dependency_overrides[get_current_user] = lambda: user
        return httpx.AsyncClient(transport=transport, base_url="http://testserver")

    try:
        yield client_for
    finally:
        app.dependency_overrides.clear()


async def test_http_an_employee_cannot_backdate_a_punch_or_decide(session, team, as_user):
    report_user = team["report_user"]
    async with as_user(report_user) as client:
        ok = await client.post("/api/v1/attendance/punch", json={"direction": "in"})
        assert ok.status_code == 201, ok.text
        assert ok.json()["source"] == "web"
        backdated = await client.post(
            "/api/v1/attendance/punch",
            json={"direction": "in", "punched_at": "2026-04-01T09:00:00+05:30"},
        )
        assert backdated.status_code == 403
        biometric = await client.post(
            "/api/v1/attendance/punch", json={"direction": "in", "source": "biometric"}
        )
        assert biometric.status_code == 403
        geo = await client.post(
            "/api/v1/attendance/punch",
            json={"direction": "out", "latitude": 12.97, "longitude": 77.59},
        )
        assert geo.status_code == 201 and geo.json()["source"] == "mobile_geo"
        other = await client.get(
            "/api/v1/attendance/punches",
            params={"from": "2026-04-01", "to": "2026-04-02",
                    "employee_id": str(team["outsider"].id)},
        )
        assert other.status_code == 403
        queue = await client.get("/api/v1/attendance/regularisations/pending")
        assert queue.status_code == 403
        shifts = await client.get("/api/v1/attendance/shifts")
        assert shifts.status_code == 403


async def test_http_csv_upload_is_validated_by_its_bytes(session, hr_user, team, as_user):
    async with as_user(hr_user) as client:
        binary = await client.post(
            "/api/v1/attendance/import/csv",
            files={"file": ("punches.csv", b"\x00\x01\x02PK", "text/csv")},
        )
        assert binary.status_code == 422
        good = await client.post(
            "/api/v1/attendance/import/csv",
            files={"file": ("x.txt", CSV_ONE_ROW, "application/octet-stream")},
        )
        assert good.status_code == 200, good.text
        assert good.json()["imported"] == 1
