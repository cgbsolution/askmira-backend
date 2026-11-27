"""Project-wise holiday calendars.

A staffing company deploys its people to clients' projects, and the people on
a client's project keep the client's holidays. Each project has a calendar:
the company's holidays plus any added for it, or — once it keeps its own — the
project's holidays only, from a date, so a month already worked out keeps the
holidays it was paid for. HR starts an own calendar with a copy of the
company's and copies calendars between projects. Attendance, leave counting,
the calendars and Miraa all read it through the one resolver.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import AttendanceDay, AttendanceStatus
from app.models.audit import AuditLog
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import Holiday, OptionalHolidaySelection
from app.models.org import LegalEntity, Location
from app.models.tenant import Tenant
from app.models.timesheets import Project
from app.models.user import User, UserRole
from app.services.attendance import service as attendance_service
from app.services.grounding import service as grounding
from app.services.leave import service as leave_service
from app.services.leave.holidays import holidays_for_employees
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, client  # noqa: F401
from tests.integration.test_attendance_w9_flow import _general_shift

pytestmark = requires_db

API = "/api/v1"
#: "Today" for the calendar rules: past holidays are not edited or copied, and
#: an own calendar starts today or later.
TODAY = date(2026, 6, 15)
YEAR = TODAY.year


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(leave_service, "local_today", lambda: TODAY)


async def _project(
    session: AsyncSession, tenant: Tenant, code: str, *, entity: LegalEntity | None = None
) -> Project:
    row = Project(
        tenant_id=tenant.id,
        code=code,
        name=code.replace("-", " "),
        legal_entity_id=entity.id if entity else None,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _person(
    session: AsyncSession,
    tenant: Tenant,
    name: str,
    *,
    role: UserRole = UserRole.EMPLOYEE,
    project: Project | None = None,
    entity: LegalEntity | None = None,
    location: Location | None = None,
) -> User:
    slug = uuid.uuid4().hex[:8]
    employee = Employee(
        tenant_id=tenant.id,
        employee_code=f"E{slug}",
        first_name=name,
        work_email=f"{slug}@acme.test",
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2024, 1, 1),
        project_id=project.id if project else None,
        legal_entity_id=entity.id if entity else None,
        location_id=location.id if location else None,
    )
    session.add(employee)
    await session.flush()
    user = User(
        tenant_id=tenant.id,
        email=f"{name.lower()}.{slug}@acme.test",
        password_hash="not-a-real-hash",
        full_name=name,
        role=role,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _holiday(
    client: httpx.AsyncClient,  # noqa: F811
    hr: User,
    name: str,
    on: date,
    **extra,
) -> dict:
    response = await client.post(
        f"{API}/leave/holidays",
        json={"name": name, "holiday_date": on.isoformat(), **extra},
        headers=bearer(hr),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _days(session: AsyncSession, hr: User, people: list[User], start: date, end: date):
    found = await holidays_for_employees(
        session, hr, [person.employee_id for person in people], start, end
    )
    return [set(found[person.employee_id]) for person in people]


async def _names(client: httpx.AsyncClient, user: User, query: str = "") -> list[str]:  # noqa: F811
    response = await client.get(f"{API}/leave/holidays?year={YEAR}{query}", headers=bearer(user))
    assert response.status_code == 200, response.text
    return [row["name"] for row in response.json()]


async def test_a_project_holiday_is_for_the_people_on_that_project_only(
    session: AsyncSession,
    client: httpx.AsyncClient,  # noqa: F811
    tenant: Tenant,
    legal_entity: LegalEntity,
) -> None:
    dxc = await _project(session, tenant, "DXC-INET", entity=legal_entity)
    internal = await _project(session, tenant, "INTERNAL", entity=legal_entity)
    hr = await _person(session, tenant, "Hari", role=UserRole.ADMIN)
    on_dxc = await _person(session, tenant, "Deepa", project=dxc)
    on_internal = await _person(session, tenant, "Ishan", project=internal)
    benched = await _person(session, tenant, "Bina")

    republic = TODAY + timedelta(days=5)
    client_day = TODAY + timedelta(days=9)
    await _holiday(client, hr, "Company day", republic)
    made = await _holiday(client, hr, "DXC foundation day", client_day, project_id=str(dxc.id))
    assert made["project_id"] == str(dxc.id)
    assert made["project_name"] == "DXC INET"

    # The resolver every module reads: attendance, leave counting, Home, the feed.
    assert await _days(
        session, hr, [on_dxc, on_internal, benched], TODAY, TODAY + timedelta(days=30)
    ) == [{republic, client_day}, {republic}, {republic}]

    # Each person's own calendar.
    assert await _names(client, on_dxc) == ["Company day", "DXC foundation day"]
    assert await _names(client, on_internal) == ["Company day"]
    # "Everything" for an employee is still not another project's calendar.
    assert await _names(client, on_internal, "&scope=all") == ["Company day"]
    other = await client.get(
        f"{API}/leave/holidays?year={YEAR}&project_id={dxc.id}", headers=bearer(on_internal)
    )
    assert other.status_code == 403
    assert await _names(client, on_dxc, f"&project_id={dxc.id}") == [
        "Company day",
        "DXC foundation day",
    ]

    # HR: a project's calendar, the company's alone, or everything.
    assert await _names(client, hr, f"&project_id={dxc.id}") == [
        "Company day",
        "DXC foundation day",
    ]
    assert await _names(client, hr, f"&project_id={internal.id}") == ["Company day"]
    assert await _names(client, hr, "&scope=company") == ["Company day"]
    assert await _names(client, hr, "&scope=all") == ["Company day", "DXC foundation day"]

    calendars = (
        await client.get(f"{API}/leave/holiday-calendars?year={YEAR}", headers=bearer(hr))
    ).json()
    summary = {c["name"]: (c["holiday_count"], c["own_count"], c["headcount"]) for c in calendars}
    assert summary == {
        "Company calendar": (1, 0, None),
        "DXC INET": (2, 1, 1),
        "INTERNAL": (1, 0, 1),
    }
    assert (
        await client.get(f"{API}/leave/holiday-calendars?year={YEAR}", headers=bearer(on_dxc))
    ).status_code == 403


async def test_a_project_keeping_its_own_calendar_drops_the_companys_from_that_date(
    session: AsyncSession,
    client: httpx.AsyncClient,  # noqa: F811
    tenant: Tenant,
    legal_entity: LegalEntity,
) -> None:
    dxc = await _project(session, tenant, "DXC-INET", entity=legal_entity)
    hr = await _person(session, tenant, "Hari", role=UserRole.ADMIN)
    on_dxc = await _person(session, tenant, "Deepa", project=dxc)
    on_company = await _person(session, tenant, "Ishan")

    gone_by = TODAY - timedelta(days=5)
    first = TODAY + timedelta(days=5)
    second = TODAY + timedelta(days=10)
    session.add(
        Holiday(tenant_id=tenant.id, name="Already passed", holiday_date=gone_by, year=YEAR)
    )
    await session.commit()
    await _holiday(client, hr, "First", first)
    await _holiday(client, hr, "Second", second)
    span = (gone_by, TODAY + timedelta(days=40))
    url = f"{API}/leave/holiday-calendars/{dxc.id}?year={YEAR}"

    # Not from a day already worked out with the company's holidays.
    backdated = await client.put(
        url, json={"own_holidays_from": (TODAY - timedelta(days=1)).isoformat()}, headers=bearer(hr)
    )
    assert backdated.status_code == 422, backdated.text

    turned_on = await client.put(
        url, json={"own_holidays_from": TODAY.isoformat()}, headers=bearer(hr)
    )
    assert turned_on.status_code == 200, turned_on.text
    assert turned_on.json()["own_holidays_from"] == TODAY.isoformat()
    assert turned_on.json()["copied"] is None
    # Before the date the company's holidays still count; from it, only the project's.
    assert await _days(session, hr, [on_dxc, on_company], *span) == [
        {gone_by},
        {gone_by, first, second},
    ]

    # Start it with the company's holidays from that date.
    seeded = await client.put(
        url,
        json={"own_holidays_from": TODAY.isoformat(), "copy_company_holidays": True},
        headers=bearer(hr),
    )
    assert seeded.status_code == 200, seeded.text
    assert seeded.json()["copied"] == {
        "copied": 2,
        "already_there": 0,
        "past": 0,
        "other_company": 0,
    }
    assert seeded.json()["own_count"] == 2
    assert await _days(session, hr, [on_dxc], *span) == [{gone_by, first, second}]

    # The client does not give the second one: removed from its calendar only.
    project_rows = (
        await client.get(
            f"{API}/leave/holidays?year={YEAR}&project_id={dxc.id}", headers=bearer(hr)
        )
    ).json()
    dxc_second = next(r for r in project_rows if r["name"] == "Second" and r["project_id"])
    assert (
        await client.delete(f"{API}/leave/holidays/{dxc_second['id']}", headers=bearer(hr))
    ).status_code == 204
    # A company holiday added afterwards is not on the project's calendar either.
    third = TODAY + timedelta(days=20)
    await _holiday(client, hr, "Third", third)
    assert await _days(session, hr, [on_dxc, on_company], *span) == [
        {gone_by, first},
        {gone_by, first, second, third},
    ]

    projects = (await client.get(f"{API}/timesheets/projects", headers=bearer(hr))).json()
    assert projects[0]["own_holidays_from"] == TODAY.isoformat()

    # A start date that has passed is not moved: the days since used it.
    started = TODAY - timedelta(days=3)
    dxc.own_holidays_from = started
    await session.commit()
    moved = await client.put(
        url, json={"own_holidays_from": (TODAY + timedelta(days=2)).isoformat()}, headers=bearer(hr)
    )
    assert moved.status_code == 409, moved.text
    kept = await client.put(
        url, json={"own_holidays_from": started.isoformat()}, headers=bearer(hr)
    )
    assert kept.status_code == 200, kept.text

    # Back to the company calendar: its holidays apply again, the project's own too.
    off = await client.put(url, json={"own_holidays_from": None}, headers=bearer(hr))
    assert off.status_code == 200, off.text
    assert off.json()["own_holidays_from"] is None
    assert await _days(session, hr, [on_dxc], *span) == [{gone_by, first, second, third}]

    summaries = (
        await session.scalars(
            select(AuditLog.summary)
            .where(AuditLog.entity_type == "project", AuditLog.entity_id == dxc.id)
            .order_by(AuditLog.created_at)
        )
    ).all()
    assert summaries == [
        f"DXC INET keeps its own holiday calendar from {TODAY}",
        f"DXC INET keeps its own holiday calendar from {TODAY}",
        "DXC INET follows the company holiday calendar again",
    ]
    # Only HR sets it.
    assert (
        await client.put(url, json={"own_holidays_from": None}, headers=bearer(on_dxc))
    ).status_code == 403


async def test_copying_a_calendar_skips_what_is_there_past_days_and_other_companies(
    session: AsyncSession,
    client: httpx.AsyncClient,  # noqa: F811
    tenant: Tenant,
    legal_entity: LegalEntity,
) -> None:
    other_entity = LegalEntity(tenant_id=tenant.id, name="Acme Two", code="ACME2")
    session.add(other_entity)
    await session.commit()
    dxc = await _project(session, tenant, "DXC-INET", entity=legal_entity)
    jio = await _project(session, tenant, "JIO", entity=legal_entity)
    dxc.own_holidays_from = TODAY
    await session.commit()
    hr = await _person(session, tenant, "Hari", role=UserRole.ADMIN)
    employee = await _person(session, tenant, "Deepa", project=dxc)

    one, two, three, four = (TODAY + timedelta(days=n) for n in (3, 6, 9, 12))
    session.add(
        Holiday(
            tenant_id=tenant.id, name="Passed", holiday_date=TODAY - timedelta(days=2), year=YEAR
        )
    )
    await session.commit()
    await _holiday(client, hr, "National", one)
    await _holiday(client, hr, "Other company's", two, legal_entity_id=str(other_entity.id))
    await _holiday(client, hr, "Karnataka", three, state_code="KA")
    await _holiday(client, hr, "Client's own name", one, project_id=str(dxc.id))

    copy = {"year": YEAR, "to_project_id": str(dxc.id)}
    first = await client.post(f"{API}/leave/holidays/copy", json=copy, headers=bearer(hr))
    assert first.status_code == 200, first.text
    assert first.json() == {"copied": 1, "already_there": 1, "past": 1, "other_company": 1}
    copied = (
        await session.scalars(
            select(Holiday).where(Holiday.project_id == dxc.id, Holiday.name == "Karnataka")
        )
    ).one()
    assert copied.state_code == "KA"
    # Twice is harmless.
    again = await client.post(f"{API}/leave/holidays/copy", json=copy, headers=bearer(hr))
    assert again.json()["copied"] == 0

    # Project to project: what DXC's people get, less what JIO's already have.
    await _holiday(client, hr, "DXC only", four, project_id=str(dxc.id))
    across = await client.post(
        f"{API}/leave/holidays/copy",
        json={"year": YEAR, "to_project_id": str(jio.id), "from_project_id": str(dxc.id)},
        headers=bearer(hr),
    )
    assert across.status_code == 200, across.text
    # JIO follows the company calendar: one and three are already its people's.
    assert across.json() == {"copied": 1, "already_there": 2, "past": 1, "other_company": 0}
    assert await _names(client, hr, f"&project_id={jio.id}") == [
        "Passed",
        "National",
        "Other company's",
        "Karnataka",
        "DXC only",
    ]

    same = await client.post(
        f"{API}/leave/holidays/copy",
        json={"year": YEAR, "to_project_id": str(dxc.id), "from_project_id": str(dxc.id)},
        headers=bearer(hr),
    )
    assert same.status_code == 422
    assert (
        await client.post(f"{API}/leave/holidays/copy", json=copy, headers=bearer(employee))
    ).status_code == 403
    audit_rows = (
        await session.scalars(
            select(AuditLog.summary).where(
                AuditLog.entity_type == "holiday", AuditLog.summary.like("Copied holiday%")
            )
        )
    ).all()
    assert sorted(audit_rows) == sorted(
        [
            f"Copied holiday Karnataka on {three} to DXC INET",
            f"Copied holiday DXC only on {four} to JIO",
        ]
    )


async def test_adding_and_editing_a_project_holiday_is_checked(
    session: AsyncSession,
    client: httpx.AsyncClient,  # noqa: F811
    tenant: Tenant,
    other_tenant: Tenant,
    legal_entity: LegalEntity,
) -> None:
    other_entity = LegalEntity(tenant_id=tenant.id, name="Acme Two", code="ACME2")
    session.add(other_entity)
    await session.commit()
    dxc = await _project(session, tenant, "DXC-INET", entity=legal_entity)
    foreign = await _project(session, other_tenant, "GLOBEX")
    hr = await _person(session, tenant, "Hari", role=UserRole.ADMIN)
    on_dxc = await _person(session, tenant, "Deepa", project=dxc)
    on_company = await _person(session, tenant, "Ishan")
    day = TODAY + timedelta(days=7)
    body = {"name": "Client day", "holiday_date": day.isoformat()}

    async def post(**extra):
        return await client.post(
            f"{API}/leave/holidays", json={**body, **extra}, headers=bearer(hr)
        )

    # Another workspace's project cannot be named.
    assert (await post(project_id=str(foreign.id))).status_code == 404
    # Nor a company the project does not belong to.
    mismatch = await post(project_id=str(dxc.id), legal_entity_id=str(other_entity.id))
    assert mismatch.status_code == 422, mismatch.text
    assert (await post(project_id=str(dxc.id))).status_code == 201
    duplicate = await post(project_id=str(dxc.id))
    assert duplicate.status_code == 409
    assert "project's calendar" in duplicate.json()["detail"]
    # The same date on the company calendar is a different calendar.
    assert (await post()).status_code == 201

    # An optional company holiday both chose; moved to DXC's calendar, the
    # other person's choice goes so it stops counting against their quota.
    optional = await _holiday(client, hr, "Floating", day + timedelta(days=3), is_optional=True)
    for person in (on_dxc, on_company):
        session.add(
            OptionalHolidaySelection(
                tenant_id=tenant.id,
                employee_id=person.employee_id,
                holiday_id=uuid.UUID(optional["id"]),
                year=YEAR,
            )
        )
    await session.commit()
    moved = await client.patch(
        f"{API}/leave/holidays/{optional['id']}",
        json={"project_id": str(dxc.id)},
        headers=bearer(hr),
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["project_name"] == "DXC INET"
    kept = (
        await session.scalars(
            select(OptionalHolidaySelection.employee_id).where(
                OptionalHolidaySelection.holiday_id == uuid.UUID(optional["id"])
            )
        )
    ).all()
    assert kept == [on_dxc.employee_id]
    edit_audit = (
        await session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == uuid.UUID(optional["id"]))
            .order_by(AuditLog.created_at.desc())
        )
    ).first()
    assert edit_audit.changes["opt_ins_dropped"] == 1
    assert edit_audit.changes["before"]["project_id"] is None

    # Back to the company calendar.
    back = await client.patch(
        f"{API}/leave/holidays/{optional['id']}", json={"project_id": None}, headers=bearer(hr)
    )
    assert back.status_code == 200, back.text
    assert back.json()["project_id"] is None


async def test_a_project_with_its_own_holidays_can_still_be_deleted(
    session: AsyncSession,
    client: httpx.AsyncClient,  # noqa: F811
    tenant: Tenant,
) -> None:
    dxc = await _project(session, tenant, "DXC-INET")
    hr = await _person(session, tenant, "Hari", role=UserRole.ADMIN)
    await _holiday(client, hr, "Client one", TODAY + timedelta(days=3), project_id=str(dxc.id))
    await _holiday(client, hr, "Client two", TODAY + timedelta(days=4), project_id=str(dxc.id))
    await _holiday(client, hr, "Company", TODAY + timedelta(days=4))

    gone = await client.delete(f"{API}/timesheets/projects/{dxc.id}", headers=bearer(hr))
    assert gone.status_code == 204, gone.text
    left = (await session.scalars(select(Holiday.name).where(Holiday.tenant_id == tenant.id))).all()
    assert left == ["Company"]
    record = (
        await session.scalars(
            select(AuditLog).where(AuditLog.entity_type == "project", AuditLog.entity_id == dxc.id)
        )
    ).one()
    assert record.changes["holidays_removed"] == 2


async def test_attendance_follows_the_project_calendar(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    location: Location,
) -> None:
    """Worked out by attendance itself: a project's holiday is a holiday for its
    people only, and the company's stop counting from its own-calendar date."""
    monday = date(2026, 4, 6)
    tuesday = monday + timedelta(days=1)
    await _general_shift(session, hr_user)
    dxc = await _project(session, tenant, "DXC-INET", entity=legal_entity)
    dxc.own_holidays_from = tuesday
    on_dxc = await _person(
        session, tenant, "Deepa", project=dxc, entity=legal_entity, location=location
    )
    on_company = await _person(session, tenant, "Ishan", entity=legal_entity, location=location)
    session.add_all(
        [
            Holiday(
                tenant_id=tenant.id,
                name="Client day",
                holiday_date=monday,
                year=2026,
                project_id=dxc.id,
            ),
            Holiday(tenant_id=tenant.id, name="Company day", holiday_date=tuesday, year=2026),
        ]
    )
    await session.commit()
    await attendance_service.recompute_range(
        session,
        hr_user,
        employee_ids=[on_dxc.employee_id, on_company.employee_id],
        start=monday,
        end=tuesday,
    )
    days = {
        (d.employee_id, d.attendance_date): d.status
        for d in (await session.scalars(select(AttendanceDay))).all()
    }
    assert days[(on_dxc.employee_id, monday)] is AttendanceStatus.HOLIDAY
    assert days[(on_dxc.employee_id, tuesday)] is AttendanceStatus.ABSENT
    assert days[(on_company.employee_id, monday)] is AttendanceStatus.ABSENT
    assert days[(on_company.employee_id, tuesday)] is AttendanceStatus.HOLIDAY


async def test_miraa_reads_only_the_askers_own_project_holidays(
    session: AsyncSession,
    tenant: Tenant,
) -> None:
    """Grounding reads holiday rows directly: a client's holidays must not reach
    somebody on another project, nor the company's once a project keeps its own."""
    soon = date.today() + timedelta(days=5)
    dxc = await _project(session, tenant, "DXC-INET")
    jio = await _project(session, tenant, "JIO")
    jio.own_holidays_from = date.today()
    on_dxc = await _person(session, tenant, "Deepa", project=dxc)
    on_jio = await _person(session, tenant, "Jaya", project=jio)
    benched = await _person(session, tenant, "Bina")
    rows = {
        "company": Holiday(tenant_id=tenant.id, name="Company", holiday_date=soon, year=soon.year),
        "dxc": Holiday(
            tenant_id=tenant.id, name="DXC", holiday_date=soon, year=soon.year, project_id=dxc.id
        ),
        "jio": Holiday(
            tenant_id=tenant.id, name="JIO", holiday_date=soon, year=soon.year, project_id=jio.id
        ),
    }
    session.add_all(rows.values())
    await session.commit()

    async def reached(user: User) -> set[str]:
        result = await grounding.retrieve(session, user, sources=("holidays",))
        names = {str(row.id): key for key, row in rows.items()}
        return {names[r.record_id] for r in result.records}

    assert await reached(on_dxc) == {"company", "dxc"}
    assert await reached(on_jio) == {"jio"}
    assert await reached(benched) == {"company"}
