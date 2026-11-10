"""My calendar link: holidays and approved leave, subscribed to from any calendar app.

Proved here: the person's own holidays only (their state, not another's; an
optional one only when opted in), their approved leave with its type and never
a pending one, a manager's team as "on leave" with no type, the link shown once
and stopped by a new link, a turned-off link or a deactivated person.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import Holiday, LeaveRequest, LeaveRequestStatus, LeaveType
from app.models.org import LegalEntity, Location
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from tests.integration.conftest import requires_db

pytestmark = requires_db

SOON = datetime.now(UTC).date() + timedelta(days=20)


@pytest.fixture
async def client(session: AsyncSession) -> AsyncGenerator[httpx.AsyncClient, None]:
    async def override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_session] = override
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _auth(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
    )
    return {"Authorization": f"Bearer {token}"}


async def _login(session, tenant, employee, tag, role=UserRole.EMPLOYEE) -> User:
    user = User(
        tenant_id=tenant.id,
        email=f"{tag}@acme.test",
        password_hash="x",
        full_name=tag,
        role=role,
        is_active=True,
        employee_id=employee.id if employee else None,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _person(session, tenant, entity, location, code, first, manager=None) -> Employee:
    person = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=first,
        last_name="Rao",
        work_email=f"{code.lower()}@acme.test",
        legal_entity_id=entity.id,
        location_id=location.id,
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2025, 1, 1),
        manager_id=manager.id if manager else None,
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


def _holiday(tenant, name, day, *, state=None, optional=False) -> Holiday:
    return Holiday(
        tenant_id=tenant.id,
        name=name,
        holiday_date=day,
        year=day.year,
        state_code=state,
        is_optional=optional,
    )


def _leave(tenant, employee, leave_type, start, days, status) -> LeaveRequest:
    return LeaveRequest(
        tenant_id=tenant.id,
        employee_id=employee.id,
        leave_type_id=leave_type.id,
        start_date=start,
        end_date=start + timedelta(days=days - 1),
        total_days=Decimal(days),
        status=status,
    )


async def test_my_calendar_link(
    client, session: AsyncSession, tenant: Tenant, legal_entity: LegalEntity, location: Location
) -> None:
    manager = await _person(session, tenant, legal_entity, location, "M001", "Meera")
    report = await _person(session, tenant, legal_entity, location, "R001", "Ravi", manager)
    sick = LeaveType(tenant_id=tenant.id, name="Sick Leave", code="SL")
    casual = LeaveType(tenant_id=tenant.id, name="Casual Leave", code="CL")
    session.add_all([sick, casual])
    await session.flush()
    session.add_all(
        [
            _holiday(tenant, "Founders, Day; Off", SOON),
            _holiday(tenant, "Karnataka Rajyotsava", SOON + timedelta(days=1), state="KA"),
            _holiday(tenant, "Gudi Padwa", SOON + timedelta(days=2), state="MH"),
            _holiday(tenant, "Optional festival", SOON + timedelta(days=3), optional=True),
            _leave(
                tenant, manager, casual, SOON + timedelta(days=5), 2, LeaveRequestStatus.APPROVED
            ),
            _leave(
                tenant, manager, casual, SOON + timedelta(days=9), 1, LeaveRequestStatus.PENDING
            ),
            _leave(tenant, report, sick, SOON + timedelta(days=6), 1, LeaveRequestStatus.APPROVED),
        ]
    )
    await session.commit()
    me = await _login(session, tenant, manager, "meera")

    state = (await client.get("/api/v1/me/calendar-feed", headers=_auth(me))).json()
    assert state == {
        "active": False,
        "include_team": False,
        "created_at": None,
        "last_used_at": None,
        "has_team": True,
    }

    made = await client.post(
        "/api/v1/me/calendar-feed", headers=_auth(me), json={"include_team": True}
    )
    assert made.status_code == 201, made.text
    path = made.json()["path"]
    assert made.json()["include_team"] is True

    ics_response = await client.get(path)
    assert ics_response.status_code == 200
    assert ics_response.headers["content-type"].startswith("text/calendar")
    ics = ics_response.text
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
    assert "SUMMARY:Founders\\, Day\\; Off (holiday)" in ics
    assert "Karnataka Rajyotsava" in ics
    assert "Gudi Padwa" not in ics  # another state's holiday
    assert "Optional festival" not in ics  # not opted in
    assert "SUMMARY:On leave — Casual Leave" in ics
    assert ics.count("Casual Leave") == 1  # the pending one is not there
    start = SOON + timedelta(days=5)
    assert f"DTSTART;VALUE=DATE:{start:%Y%m%d}" in ics
    assert f"DTEND;VALUE=DATE:{start + timedelta(days=2):%Y%m%d}" in ics
    assert "Ravi Rao — on leave" in ics
    assert "Sick Leave" not in ics  # a report's leave type is not the manager's to see

    # A colleague's own link carries nothing of the team.
    reporter = await _login(session, tenant, report, "ravi")
    theirs = await client.post(
        "/api/v1/me/calendar-feed", headers=_auth(reporter), json={"include_team": True}
    )
    assert theirs.json()["include_team"] is False
    their_ics = (await client.get(theirs.json()["path"])).text
    assert "On leave — Sick Leave" in their_ics and "Casual Leave" not in their_ics

    # A new link stops the old one.
    newer = await client.post(
        "/api/v1/me/calendar-feed", headers=_auth(me), json={"include_team": False}
    )
    assert (await client.get(path)).status_code == 404
    newer_ics = (await client.get(newer.json()["path"])).text
    assert "Ravi Rao" not in newer_ics

    # Turned off, then a deactivated person: no calendar.
    assert (await client.delete("/api/v1/me/calendar-feed", headers=_auth(me))).status_code == 204
    assert (await client.get(newer.json()["path"])).status_code == 404
    assert (await client.delete("/api/v1/me/calendar-feed", headers=_auth(me))).status_code == 404

    reporter.is_active = False
    await session.commit()
    assert (await client.get(theirs.json()["path"])).status_code == 404

    assert (await client.get("/api/v1/public/calendar/not-a-token.ics")).status_code == 404
