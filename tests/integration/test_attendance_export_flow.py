"""Attendance review: filters, punch addresses, and exports for any period.

HR asked for company / project / department / location filters on Attendance
review, the street address of each punch with a Google Maps link, and an
export for a day, a week, a month or any dates, in four layouts.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, time, timedelta

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app.models.attendance import PunchAddress, PunchDirection, PunchSource
from app.models.audit import AuditAction, AuditLog
from app.models.employee import Employee
from app.models.org import LegalEntity, Location
from app.models.timesheets import Project
from app.models.user import UserRole
from app.schemas.attendance import ShiftCreate
from app.services.attendance import maps
from app.services.attendance import service as attendance_service
from app.services.attendance import setup as attendance_setup
from tests.integration import platform_support
from tests.integration.conftest import as_caller, requires_db
from tests.integration.platform_support import bearer, make_person

pytestmark = requires_db

client = platform_support.client

API = "/api/v1/attendance"
IST = timedelta(hours=5, minutes=30)
OFFICE = (12.9716, 77.5946)
HOME = (12.9352, 77.6245)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=UTC) - IST


def _last_month() -> tuple[date, date]:
    today = datetime.now(UTC).date()
    last = today.replace(day=1) - timedelta(days=1)
    return last.replace(day=1), last


def _weekdays(first: date, last: date) -> list[date]:
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    return [d for d in days if d.weekday() < 5]


@pytest.fixture
def geocoder(monkeypatch):
    """Addresses without Google: each place answers with a made-up street."""
    calls: list[tuple[str, str]] = []

    async def fake(client, latitude, longitude):
        calls.append((str(latitude), str(longitude)))
        return f"{latitude}, {longitude}, MG Road, Bengaluru"

    monkeypatch.setattr(maps, "geocoding_enabled", lambda: True)
    monkeypatch.setattr(maps, "reverse_geocode", fake)
    return calls


async def _world(session, tenant):
    """HR, a manager with one report, and somebody in another company and project."""
    hr = await make_person(session, tenant, role=UserRole.ADMIN, name="Priya Hr")
    manager = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Mohan Lead")
    lead = await session.get(Employee, manager.employee_id)
    report = await make_person(
        session, tenant, role=UserRole.EMPLOYEE, name="Ravi Kumar", manager=lead
    )
    other = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Sara Khan")
    acme = LegalEntity(tenant_id=tenant.id, name="Acme Pvt Ltd", code="ACME")
    dxc = LegalEntity(tenant_id=tenant.id, name="DXC (client)", code="DXC")
    session.add_all([acme, dxc])
    await session.flush()
    project = Project(tenant_id=tenant.id, code="INET", name="DXC INET", legal_entity_id=dxc.id)
    pune = Location(
        tenant_id=tenant.id, legal_entity_id=acme.id, name="Pune", code="PUN", state_code="MH"
    )
    session.add_all([project, pune])
    await session.flush()
    for login, entity in ((hr, acme), (manager, acme), (report, acme), (other, dxc)):
        person = await session.get(Employee, login.employee_id)
        person.legal_entity_id = entity.id
    sara = await session.get(Employee, other.employee_id)
    sara.project_id = project.id
    sara.location_id = pune.id
    await session.commit()
    await attendance_setup.create_shift(
        session,
        hr,
        ShiftCreate(
            code="GEN",
            name="General",
            start_time=time(9, 30),
            end_time=time(18, 30),
            break_minutes=60,
            grace_in_minutes=10,
            half_day_threshold_minutes=240,
            full_day_threshold_minutes=420,
            weekly_off_days=[6, 7],
        ),
    )
    return hr, manager, report, other, acme, dxc, project, pune


async def _punch(session, hr, login, day, hour, minute, direction, where):
    await attendance_service.record_punch(
        session,
        hr,
        employee_id=login.employee_id,
        punched_at=_at(day, hour, minute),
        direction=direction,
        source=PunchSource.MOBILE_GEO,
        latitude=where[0] if where else None,
        longitude=where[1] if where else None,
        commit=False,
    )


def _sheet(content: bytes):
    sheet = load_workbook(io.BytesIO(content)).active
    rows = list(sheet.iter_rows())
    headers = [c.value for c in rows[0]]
    return headers, [{h: c for h, c in zip(headers, r, strict=True)} for r in rows[1:]]


async def test_punch_in_and_out_with_addresses_and_google_maps_links(
    client, session, tenant, geocoder
):
    hr, manager, report, other, *_ = await _world(session, tenant)
    first, last = _last_month()
    day = _weekdays(first, last)[0]
    await _punch(session, hr, report, day, 9, 58, PunchDirection.IN, OFFICE)
    await _punch(session, hr, report, day, 18, 45, PunchDirection.OUT, HOME)
    await session.commit()
    await attendance_service.recompute_range(session, hr, employee_ids=None, start=first, end=last)
    headers_ = bearer(hr)

    body = {"kind": "in_out", "start": day.isoformat(), "end": day.isoformat()}
    response = await client.post(f"{API}/export", json=body, headers=headers_)
    assert response.status_code == 200, response.text
    assert (
        f"attendance-punch-in-out-{day.isoformat()}.xlsx" in response.headers["content-disposition"]
    )
    headers, rows = _sheet(response.content)
    for label in ("In time", "In address", "In map (Google Maps)", "Out address", "Status"):
        assert label in headers
    ravi = next(r for r in rows if r["Name"].value == "Ravi Kumar")
    assert ravi["In time"].value == time(9, 58)
    assert ravi["In time"].number_format == "HH:MM"
    assert "MG Road" in ravi["In address"].value
    assert ravi["In address"].value != ravi["Out address"].value
    link = ravi["In map (Google Maps)"].hyperlink.target
    assert link.startswith("https://www.google.com/maps/search/?api=1&query=12.971600,77.594600")
    assert ravi["Status"].value == "Present"
    assert ravi["Late by (h:mm)"].value == timedelta(minutes=28)
    # Everybody in the company is a row for the day, punched or not.
    assert {r["Name"].value for r in rows} >= {"Priya Hr", "Mohan Lead", "Sara Khan"}
    assert len(geocoder) == 2

    # Each place is looked up once, then kept.
    again = await client.post(f"{API}/export", json=body, headers=headers_)
    assert again.status_code == 200
    assert len(geocoder) == 2
    kept = (await session.scalars(select(PunchAddress))).all()
    assert len(kept) == 2 and all(row.tenant_id == tenant.id for row in kept)

    exported = await session.scalar(
        select(AuditLog)
        .where(AuditLog.action == AuditAction.EXPORT, AuditLog.entity_type == "attendance")
        .order_by(AuditLog.created_at.desc())
    )
    assert exported.changes["kind"] == "in_out"
    assert exported.changes["addresses_included"] is True


async def test_every_punch_day_wise_and_summary_layouts(client, session, tenant, geocoder):
    hr, manager, report, other, *_ = await _world(session, tenant)
    first, last = _last_month()
    days = _weekdays(first, last)[:2]
    for day in days:
        await _punch(session, hr, report, day, 9, 30, PunchDirection.IN, OFFICE)
        await _punch(session, hr, report, day, 18, 30, PunchDirection.OUT, None)
    await session.commit()
    await attendance_service.recompute_range(session, hr, employee_ids=None, start=first, end=last)
    span = {"start": first.isoformat(), "end": last.isoformat()}

    punches = await client.post(
        f"{API}/export", json={**span, "kind": "punches"}, headers=bearer(hr)
    )
    headers, rows = _sheet(punches.content)
    ravi = [r for r in rows if r["Name"].value == "Ravi Kumar"]
    assert len(ravi) == 4  # one row per punch
    assert ravi[0]["In / Out"].value == "In"
    assert ravi[0]["Source"].value == "Mobile app"
    assert ravi[0]["Latitude"].value == pytest.approx(OFFICE[0])
    assert ravi[1]["Address"].value is None  # the punch out shared no location
    assert ravi[1]["Map (Google Maps)"].value is None

    daily = await client.post(
        f"{API}/export", json={**span, "kind": "daily", "file_type": "csv"}, headers=bearer(hr)
    )
    assert daily.status_code == 200
    lines = list(csv.DictReader(io.StringIO(daily.content.decode("utf-8-sig"))))
    ravi_days = [r for r in lines if r["Name"] == "Ravi Kumar"]
    assert len(ravi_days) == (last - first).days + 1
    worked = next(r for r in ravi_days if r["Date"] == days[0].strftime("%d-%m-%Y"))
    assert worked["Status"] == "Present"
    assert worked["First in"] == "09:30"
    assert worked["Worked (h:mm)"] == "8:00"

    summary = await client.post(
        f"{API}/export", json={**span, "kind": "summary"}, headers=bearer(hr)
    )
    headers, rows = _sheet(summary.content)
    ravi = next(r for r in rows if r["Name"].value == "Ravi Kumar")
    assert ravi["Present"].value == 2
    assert ravi["Days in period"].value == (last - first).days + 1
    assert ravi["Worked (h:mm)"].value == timedelta(hours=16)


async def test_filters_scope_and_ticked_people(client, session, tenant, other_tenant, geocoder):
    hr, manager, report, other, acme, dxc, project, pune = await _world(session, tenant)
    first, last = _last_month()
    span = {"start": first.isoformat(), "end": first.isoformat(), "kind": "summary"}

    async def names(body, who=hr):
        response = await client.post(f"{API}/export", json=body, headers=bearer(who))
        assert response.status_code == 200, response.text
        return {r["Name"].value for r in _sheet(response.content)[1]}

    assert await names({**span, "legal_entity_id": str(dxc.id)}) == {"Sara Khan"}
    assert await names({**span, "project_id": str(project.id)}) == {"Sara Khan"}
    assert await names({**span, "location_id": str(pune.id)}) == {"Sara Khan"}
    assert "Sara Khan" not in await names({**span, "legal_entity_id": str(acme.id)})
    assert await names({**span, "employee_ids": [str(report.employee_id)]}) == {"Ravi Kumar"}

    # A manager exports their reporting line and themselves, nobody else.
    await as_caller(session, manager)
    assert await names(span, manager) == {"Mohan Lead", "Ravi Kumar"}
    assert await names({**span, "employee_ids": [str(other.employee_id)]}, manager) == set()
    refused = await client.post(f"{API}/export", json=span, headers=bearer(report))
    assert refused.status_code == 403

    # Another workspace's HR sees none of these people, ticked or not.
    other_tenant.enabled_modules = {**(other_tenant.enabled_modules or {}), "leave": True}
    await session.commit()
    stranger = await make_person(session, other_tenant, role=UserRole.ADMIN, name="Olga Other")
    body = {**span, "employee_ids": [str(report.employee_id)]}
    assert await names(body, stranger) == set()

    # At most 93 days at a time, and never backwards.
    too_long = {**span, "end": (first + timedelta(days=93)).isoformat()}
    assert (
        await client.post(f"{API}/export", json=too_long, headers=bearer(hr))
    ).status_code == 422
    backwards = {**span, "end": (first - timedelta(days=1)).isoformat()}
    assert (
        await client.post(f"{API}/export", json=backwards, headers=bearer(hr))
    ).status_code == 422
    unknown = {**span, "kind": "payroll"}
    assert (await client.post(f"{API}/export", json=unknown, headers=bearer(hr))).status_code == 422

    # The same filters narrow the Today screen.
    today = await client.get(f"{API}/team/today?project_id={project.id}", headers=bearer(hr))
    assert [m["employee_name"] for m in today.json()["members"]] == ["Sara Khan"]
    everyone = await client.get(f"{API}/team/today", headers=bearer(hr))
    assert {m["employee_name"] for m in everyone.json()["members"]} == {
        "Priya Hr",
        "Mohan Lead",
        "Ravi Kumar",
        "Sara Khan",
    }


async def test_today_and_the_punch_list_show_the_address(client, session, tenant, geocoder):
    hr, manager, report, *_ = await _world(session, tenant)
    now = datetime.now(UTC)
    await attendance_service.record_punch(
        session,
        hr,
        employee_id=report.employee_id,
        punched_at=now - timedelta(minutes=5),
        direction=PunchDirection.IN,
        source=PunchSource.MOBILE_GEO,
        latitude=OFFICE[0],
        longitude=OFFICE[1],
    )
    today = await client.get(f"{API}/team/today", headers=bearer(hr))
    ravi = next(m for m in today.json()["members"] if m["employee_name"] == "Ravi Kumar")
    assert "MG Road" in ravi["last_punch_address"]

    local = await attendance_service.local_today(session, hr)
    listed = await client.get(
        f"{API}/punches?employee_id={report.employee_id}&from={local}&to={local}",
        headers=bearer(hr),
    )
    assert listed.status_code == 200, listed.text
    assert "MG Road" in listed.json()[0]["address"]
    assert len(geocoder) == 1  # looked up for Today, kept for the list


async def test_an_address_lookup_that_fails_leaves_the_address_blank(
    client, session, tenant, monkeypatch
):
    hr, manager, report, *_ = await _world(session, tenant)
    first, last = _last_month()
    day = _weekdays(first, last)[0]
    await _punch(session, hr, report, day, 9, 30, PunchDirection.IN, OFFICE)
    await session.commit()

    async def refused(client, latitude, longitude):
        raise maps.AddressLookupError("REQUEST_DENIED")

    monkeypatch.setattr(maps, "geocoding_enabled", lambda: True)
    monkeypatch.setattr(maps, "reverse_geocode", refused)
    body = {"kind": "punches", "start": day.isoformat(), "end": day.isoformat()}
    response = await client.post(f"{API}/export", json=body, headers=bearer(hr))
    assert response.status_code == 200
    _, rows = _sheet(response.content)
    assert rows[0]["Address"].value is None
    assert rows[0]["Map (Google Maps)"].hyperlink.target.startswith("https://www.google.com/maps")
    # Nothing was kept, so the next read asks again.
    assert (await session.scalars(select(PunchAddress))).all() == []

    # With the map switched to OpenStreetMap nothing is looked up at all.
    monkeypatch.setattr(maps, "geocoding_enabled", lambda: False)
    response = await client.post(f"{API}/export", json=body, headers=bearer(hr))
    assert response.status_code == 200


async def test_the_correction_allowance_says_how_many_are_left(client, session, tenant):
    hr, manager, report, other, *_ = await _world(session, tenant)
    first, last = _last_month()
    day = _weekdays(first, last)[0]
    url = f"{API}/corrections/allowance?on={day.isoformat()}"

    unlimited = await client.get(url, headers=bearer(report))
    assert unlimited.json() == {
        "month": first.isoformat(),
        "limit": None,
        "used": 0,
        "remaining": None,
    }

    settings = dict(tenant.settings or {})
    settings["attendance"] = {"monthly_correction_limit": 1}
    tenant.settings = settings
    await session.commit()
    ask = {
        "attendance_date": day.isoformat(),
        "kind": "missed_punch",
        "requested_status": "present",
        "requested_first_in": _at(day, 9, 30).isoformat(),
        "reason": "Forgot to punch in",
    }
    created = await client.post(f"{API}/regularisation", json=ask, headers=bearer(report))
    assert created.status_code == 201, created.text
    left = (await client.get(url, headers=bearer(report))).json()
    assert (left["limit"], left["used"], left["remaining"]) == (1, 1, 0)

    second = {**ask, "attendance_date": _weekdays(first, last)[1].isoformat()}
    second["requested_first_in"] = _at(_weekdays(first, last)[1], 9, 30).isoformat()
    refused = await client.post(f"{API}/regularisation", json=second, headers=bearer(report))
    assert refused.status_code == 422
    assert "all 1 correction requests" in refused.json()["detail"]

    # Their manager can see it; a colleague cannot.
    await as_caller(session, manager)
    seen = await client.get(f"{url}&employee_id={report.employee_id}", headers=bearer(manager))
    assert seen.json()["used"] == 1
    hidden = await client.get(f"{url}&employee_id={report.employee_id}", headers=bearer(other))
    assert hidden.status_code == 403
