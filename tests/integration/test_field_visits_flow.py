"""Field visits, and punches that wait for an accurate location.

Asked 2026-11-26: Punch in / Punch out must stay off until a correct location
is found (web and app), and sales and marketing people need Visit in / Visit
out for each client call — location always on and the client's name — so HR
sees how many visits were made and where. The selfie at visit in was dropped
the same day ("not required"); an older app that still sends one is taken.

What this holds, over HTTP:

- A punch whose location is less accurate than the workspace's
  `location_accuracy_metres` is refused when location is required; a better
  one is taken; an older app that sends no accuracy is still taken; with
  location optional the accuracy does not matter.
- A visit needs a client name and a location (within the accuracy if one is set),
  whatever the punch rule says. One open visit at a time. Only the person on
  the visit closes it, once.
- The person lists their own visits; HR everyone's; a manager their report's;
  a colleague with no reports nobody else's. A photo, where sent, likewise.
- The Field visits export has one row per visit with addresses, Google Maps
  links and the time spent there; a date after today reads "Upcoming".
"""

from __future__ import annotations

import base64
import io
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from openpyxl import load_workbook
from PIL import Image
from sqlalchemy import select, update

from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.attendance import AttendancePunch, FieldVisit
from app.models.audit import AuditLog
from app.models.employee import Employee
from app.models.user import UserRole
from app.services.attendance import maps
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, make_person

pytestmark = requires_db

API = "/api/v1/attendance"
CLIENT_SITE = (18.5204, 73.8567)
OFFICE = (18.5308, 73.8475)


def _selfie() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 120, 90)).save(buffer, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


@pytest.fixture
async def client(session, tmp_path, monkeypatch):
    async def override():
        yield session

    monkeypatch.setattr(settings, "FILE_STORAGE_ROOT", str(tmp_path))
    app.dependency_overrides[get_session] = override
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def geocoder(monkeypatch):
    async def fake(client, latitude, longitude):
        return f"{latitude}, {longitude}, FC Road, Pune"

    monkeypatch.setattr(maps, "geocoding_enabled", lambda: True)
    monkeypatch.setattr(maps, "reverse_geocode", fake)


async def _team(session, tenant):
    hr = await make_person(session, tenant, role=UserRole.ADMIN, name="Priya Hr")
    manager = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Mohan Lead")
    lead = await session.get(Employee, manager.employee_id)
    seller = await make_person(
        session, tenant, role=UserRole.EMPLOYEE, name="Ravi Sales", manager=lead
    )
    colleague = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Sara Khan")
    await session.commit()
    return hr, manager, seller, colleague


async def _settings(client, hr, **values):
    response = await client.put(f"{API}/settings", json=values, headers=bearer(hr))
    assert response.status_code == 200, response.text
    return response.json()


def _visit_in(name="Tata Motors, Pimpri", where=CLIENT_SITE, accuracy=25.0, **extra):
    return {
        "client_name": name,
        "latitude": where[0],
        "longitude": where[1],
        "accuracy_metres": accuracy,
        "source": "mobile",
        **extra,
    }


def _visit_out(where=CLIENT_SITE, accuracy=30.0):
    return {"latitude": where[0], "longitude": where[1], "accuracy_metres": accuracy}


# ── Punches wait for an accurate location ─────────────────────────────────────


async def test_punch_refuses_a_location_less_accurate_than_the_workspace_asks(
    client, session, tenant
):
    hr, _, seller, _ = await _team(session, tenant)
    settings_now = await _settings(
        client, hr, location_mode="required", location_accuracy_metres=100
    )
    assert settings_now["location_accuracy_metres"] == 100

    rough = await client.post(
        f"{API}/punch",
        json={
            "direction": "in",
            "latitude": OFFICE[0],
            "longitude": OFFICE[1],
            "accuracy_metres": 850,
        },
        headers=bearer(seller),
    )
    assert rough.status_code == 422
    assert "850 m" in rough.json()["detail"] and "100 m" in rough.json()["detail"]

    none = await client.post(f"{API}/punch", json={"direction": "in"}, headers=bearer(seller))
    assert none.status_code == 422

    good = await client.post(
        f"{API}/punch",
        json={
            "direction": "in",
            "latitude": OFFICE[0],
            "longitude": OFFICE[1],
            "accuracy_metres": 18.4,
        },
        headers=bearer(seller),
    )
    assert good.status_code == 201, good.text
    stored = await session.get(AttendancePunch, uuid.UUID(good.json()["id"]))
    assert stored.raw_payload["accuracy_metres"] == 18

    # An app build from before the accuracy rule sends none: taken at its word.
    older = await client.post(
        f"{API}/punch",
        json={"direction": "out", "latitude": OFFICE[0], "longitude": OFFICE[1]},
        headers=bearer(seller),
    )
    assert older.status_code == 201

    # A looser rule lets the rough fix through.
    await _settings(client, hr, location_accuracy_metres=1000)
    loose = await client.post(
        f"{API}/punch",
        json={
            "direction": "in",
            "latitude": OFFICE[0],
            "longitude": OFFICE[1],
            "accuracy_metres": 850,
        },
        headers=bearer(seller),
    )
    assert loose.status_code == 201


async def test_accuracy_is_off_by_default_and_can_be_switched_off_again(client, session, tenant):
    """Office desktops have no GPS: by default any location is taken, however rough."""
    hr, _, seller, _ = await _team(session, tenant)
    read = await client.get(f"{API}/settings", headers=bearer(hr))
    assert read.json()["location_accuracy_metres"] is None
    await _settings(client, hr, location_mode="required")
    rough = await client.post(
        f"{API}/punch",
        json={
            "direction": "in",
            "latitude": OFFICE[0],
            "longitude": OFFICE[1],
            "accuracy_metres": 2600,
        },
        headers=bearer(seller),
    )
    assert rough.status_code == 201, rough.text
    # Location itself is still required.
    nothing = await client.post(f"{API}/punch", json={"direction": "out"}, headers=bearer(seller))
    assert nothing.status_code == 422
    visit = await client.post(
        f"{API}/visits", json=_visit_in(accuracy=1800), headers=bearer(seller)
    )
    assert visit.status_code == 201, visit.text

    # On, then Off again (null).
    await _settings(client, hr, location_accuracy_metres=100)
    refused = await client.post(
        f"{API}/punch",
        json={
            "direction": "out",
            "latitude": OFFICE[0],
            "longitude": OFFICE[1],
            "accuracy_metres": 2600,
        },
        headers=bearer(seller),
    )
    assert refused.status_code == 422
    off = await _settings(client, hr, location_accuracy_metres=None)
    assert off["location_accuracy_metres"] is None
    taken = await client.post(
        f"{API}/punch",
        json={
            "direction": "out",
            "latitude": OFFICE[0],
            "longitude": OFFICE[1],
            "accuracy_metres": 2600,
        },
        headers=bearer(seller),
    )
    assert taken.status_code == 201


async def test_accuracy_does_not_matter_when_location_is_optional(client, session, tenant):
    hr, _, seller, _ = await _team(session, tenant)
    await _settings(client, hr, location_mode="optional")
    response = await client.post(
        f"{API}/punch",
        json={
            "direction": "in",
            "latitude": OFFICE[0],
            "longitude": OFFICE[1],
            "accuracy_metres": 4000,
        },
        headers=bearer(seller),
    )
    assert response.status_code == 201


async def test_accuracy_setting_has_bounds_and_survives_other_saves(client, session, tenant):
    hr, *_ = await _team(session, tenant)
    for bad in (0, 5, 5001, -1):
        response = await client.put(
            f"{API}/settings", json={"location_accuracy_metres": bad}, headers=bearer(hr)
        )
        assert response.status_code == 422, bad
    await _settings(client, hr, location_accuracy_metres=250)
    after = await _settings(client, hr, photo_mode="optional")
    assert after["location_accuracy_metres"] == 250
    read = await client.get(f"{API}/settings", headers=bearer(hr))
    assert read.json()["location_accuracy_metres"] == 250


# ── Visit in and visit out ────────────────────────────────────────────────────


async def test_visit_in_needs_a_name_and_an_accurate_location_but_no_selfie(
    client, session, tenant
):
    hr, _, seller, _ = await _team(session, tenant)
    # Location "off" for punches does not switch it off for visits.
    await _settings(client, hr, location_mode="off", location_accuracy_metres=100)
    auth = bearer(seller)

    blank = await client.post(f"{API}/visits", json=_visit_in(name="   "), headers=auth)
    assert blank.status_code == 422
    no_place = _visit_in()
    no_place.pop("latitude")
    assert (await client.post(f"{API}/visits", json=no_place, headers=auth)).status_code == 422
    no_accuracy = _visit_in()
    no_accuracy.pop("accuracy_metres")
    assert (await client.post(f"{API}/visits", json=no_accuracy, headers=auth)).status_code == 422
    rough = await client.post(f"{API}/visits", json=_visit_in(accuracy=420), headers=auth)
    assert rough.status_code == 422 and "420 m" in rough.json()["detail"]
    not_a_picture = await client.post(
        f"{API}/visits",
        json=_visit_in(photo="data:image/jpeg;base64," + base64.b64encode(b"%PDF-1.4").decode()),
        headers=auth,
    )
    assert not_a_picture.status_code == 422
    junk = await client.post(f"{API}/visits", json=_visit_in(extra_field=1), headers=auth)
    assert junk.status_code == 422
    assert (await session.scalars(select(FieldVisit))).all() == []

    ok = await client.post(f"{API}/visits", json=_visit_in(name="  Tata   Motors "), headers=auth)
    assert ok.status_code == 201, ok.text
    visit = ok.json()
    assert visit["client_name"] == "Tata Motors"
    # No selfie is asked for any more.
    assert visit["has_photo"] is False
    assert visit["ended_at"] is None and visit["duration_minutes"] is None
    assert visit["start_accuracy_metres"] == 25
    assert visit["start_source"] == "mobile"
    started = datetime.fromisoformat(visit["started_at"])
    assert abs((datetime.now(UTC) - started).total_seconds()) < 60

    audit = await session.scalar(
        select(AuditLog).where(AuditLog.entity_type == "field_visit", AuditLog.action == "create")
    )
    assert audit is not None and audit.changes["client_name"] == "Tata Motors"


async def test_one_open_visit_and_only_its_owner_closes_it_once(client, session, tenant):
    hr, _, seller, colleague = await _team(session, tenant)
    await _settings(client, hr, location_accuracy_metres=100)
    auth = bearer(seller)
    first = (await client.post(f"{API}/visits", json=_visit_in(), headers=auth)).json()

    second = await client.post(f"{API}/visits", json=_visit_in(name="Sharma Traders"), headers=auth)
    assert second.status_code == 409 and "Tata Motors" in second.json()["detail"]

    stranger = await client.post(
        f"{API}/visits/{first['id']}/end", json=_visit_out(), headers=bearer(colleague)
    )
    assert stranger.status_code == 404
    by_hr = await client.post(
        f"{API}/visits/{first['id']}/end", json=_visit_out(), headers=bearer(hr)
    )
    assert by_hr.status_code == 404
    rough = await client.post(
        f"{API}/visits/{first['id']}/end", json=_visit_out(accuracy=900), headers=auth
    )
    assert rough.status_code == 422

    # Forty minutes at the client.
    await session.execute(
        update(FieldVisit)
        .where(FieldVisit.id == uuid.UUID(first["id"]))
        .values(started_at=datetime.now(UTC) - timedelta(minutes=40))
    )
    await session.commit()
    closed = await client.post(
        f"{API}/visits/{first['id']}/end", json=_visit_out(where=OFFICE), headers=auth
    )
    assert closed.status_code == 200, closed.text
    body = closed.json()
    assert body["duration_minutes"] == 40
    assert body["end_latitude"] == pytest.approx(OFFICE[0])
    assert body["end_source"] == "web"

    again = await client.post(f"{API}/visits/{first['id']}/end", json=_visit_out(), headers=auth)
    assert again.status_code == 409

    # Now the next visit can start.
    nxt = await client.post(f"{API}/visits", json=_visit_in(name="Sharma Traders"), headers=auth)
    assert nxt.status_code == 201


async def test_a_login_without_an_employee_record_cannot_visit(client, session, tenant):
    admin = await make_person(
        session, tenant, role=UserRole.ADMIN, name="Only Admin", with_employee=False
    )
    await session.commit()
    response = await client.post(f"{API}/visits", json=_visit_in(), headers=bearer(admin))
    assert response.status_code == 403


async def test_who_sees_which_visits_and_selfies(client, session, tenant, geocoder):
    hr, manager, seller, colleague = await _team(session, tenant)
    # An older app build still sends a selfie: it is kept and shown as before.
    visit = (
        await client.post(f"{API}/visits", json=_visit_in(photo=_selfie()), headers=bearer(seller))
    ).json()
    assert visit["has_photo"] is True
    await client.post(f"{API}/visits/{visit['id']}/end", json=_visit_out(), headers=bearer(seller))
    await client.post(
        f"{API}/visits", json=_visit_in(name="Office supplies shop"), headers=bearer(colleague)
    )
    today = datetime.now(UTC).date()
    span = {
        "from": (today - timedelta(days=1)).isoformat(),
        "to": (today + timedelta(days=1)).isoformat(),
    }

    mine = await client.get(f"{API}/visits/mine", params=span, headers=bearer(seller))
    assert mine.status_code == 200
    assert [v["client_name"] for v in mine.json()["visits"]] == ["Tata Motors, Pimpri"]
    assert "FC Road" in mine.json()["visits"][0]["start_address"]

    everyone = (await client.get(f"{API}/visits", params=span, headers=bearer(hr))).json()
    assert everyone["total"] == 2 and everyone["people"] == 2
    assert {v["employee_name"] for v in everyone["visits"]} == {"Ravi Sales", "Sara Khan"}

    team = (await client.get(f"{API}/visits", params=span, headers=bearer(manager))).json()
    assert [v["employee_name"] for v in team["visits"]] == ["Ravi Sales"]

    nobody = await client.get(f"{API}/visits", params=span, headers=bearer(colleague))
    assert nobody.status_code == 403

    one = await client.get(
        f"{API}/visits",
        params={**span, "employee_id": str(seller.employee_id)},
        headers=bearer(hr),
    )
    assert one.json()["total"] == 1

    photo = f"{API}/visits/{visit['id']}/photo"
    assert (await client.get(photo, headers=bearer(seller))).status_code == 200
    assert (await client.get(photo, headers=bearer(hr))).status_code == 200
    assert (await client.get(photo, headers=bearer(manager))).status_code == 200
    assert (await client.get(photo, headers=bearer(colleague))).status_code == 403

    too_long = await client.get(
        f"{API}/visits/mine",
        params={"from": "2026-01-01", "to": "2026-12-31"},
        headers=bearer(seller),
    )
    assert too_long.status_code == 422


async def test_visits_stay_inside_their_workspace(client, session, tenant, other_tenant):
    _, _, seller, _ = await _team(session, tenant)
    visit = (
        await client.post(f"{API}/visits", json=_visit_in(photo=_selfie()), headers=bearer(seller))
    ).json()
    # Same modules on, so a refusal can only come from the workspace boundary.
    other_tenant.enabled_modules = dict(tenant.enabled_modules)
    outsider = await make_person(session, other_tenant, role=UserRole.ADMIN, name="Out Sider")
    await session.commit()
    today = datetime.now(UTC).date().isoformat()
    seen = await client.get(
        f"{API}/visits", params={"from": today, "to": today}, headers=bearer(outsider)
    )
    # Refused, or an empty list: never the other workspace's visit.
    assert seen.status_code == 200, seen.text
    assert seen.json()["total"] == 0
    photo = await client.get(f"{API}/visits/{visit['id']}/photo", headers=bearer(outsider))
    assert photo.status_code == 404
    end = await client.post(
        f"{API}/visits/{visit['id']}/end", json=_visit_out(), headers=bearer(outsider)
    )
    assert end.status_code in (403, 404)


# ── Export ────────────────────────────────────────────────────────────────────


async def test_field_visits_export_and_future_days(client, session, tenant, geocoder):
    hr, _, seller, _ = await _team(session, tenant)
    visit = (await client.post(f"{API}/visits", json=_visit_in(), headers=bearer(seller))).json()
    await session.execute(
        update(FieldVisit)
        .where(FieldVisit.id == uuid.UUID(visit["id"]))
        .values(started_at=datetime.now(UTC) - timedelta(minutes=75))
    )
    await session.commit()
    await client.post(f"{API}/visits/{visit['id']}/end", json=_visit_out(), headers=bearer(seller))
    today = datetime.now(UTC).date()

    response = await client.post(
        f"{API}/export",
        json={
            "kind": "visits",
            "start": (today - timedelta(days=1)).isoformat(),
            "end": today.isoformat(),
        },
        headers=bearer(hr),
    )
    assert response.status_code == 200, response.text
    assert "attendance-field-visits-" in response.headers["content-disposition"]
    sheet = load_workbook(io.BytesIO(response.content)).active
    rows = list(sheet.iter_rows())
    headers = [c.value for c in rows[0]]
    assert len(rows) == 2
    row = dict(zip(headers, rows[1], strict=True))
    assert row["Name"].value == "Ravi Sales"
    assert row["Client / place"].value == "Tata Motors, Pimpri"
    assert "FC Road" in row["In address"].value
    assert row["In map (Google Maps)"].hyperlink.target.startswith(
        "https://www.google.com/maps/search/?api=1&query=18.520400,73.856700"
    )
    assert row["Time there (h:mm)"].value == timedelta(minutes=75)
    assert "Selfie" not in headers
    assert row["From"].value == "App"

    future = await client.post(
        f"{API}/export",
        json={
            "kind": "in_out",
            "start": today.isoformat(),
            "end": (today + timedelta(days=2)).isoformat(),
        },
        headers=bearer(hr),
    )
    sheet = load_workbook(io.BytesIO(future.content)).active
    rows = list(sheet.iter_rows(values_only=True))
    status = rows[0].index("Status")
    ravi = [r for r in rows[1:] if r[1] == "Ravi Sales"]
    assert [r[status] for r in ravi][1:] == ["Upcoming", "Upcoming"]


# ── Fake GPS ──────────────────────────────────────────────────────────────────


async def test_a_location_the_phone_flags_as_fake_is_refused(client, session, tenant):
    hr, _, seller, _ = await _team(session, tenant)
    auth = bearer(seller)
    # Even with location optional for punches, a fake one is never taken.
    await _settings(client, hr, location_mode="optional")
    punch = await client.post(
        f"{API}/punch",
        json={
            "direction": "in",
            "latitude": OFFICE[0],
            "longitude": OFFICE[1],
            "accuracy_metres": 5,
            "is_mocked": True,
        },
        headers=auth,
    )
    assert punch.status_code == 422 and "fake location" in punch.json()["detail"]

    fake_in = await client.post(f"{API}/visits", json=_visit_in(is_mocked=True), headers=auth)
    assert fake_in.status_code == 422 and "fake location" in fake_in.json()["detail"]
    assert (await session.scalars(select(FieldVisit))).all() == []

    real = (
        await client.post(f"{API}/visits", json=_visit_in(is_mocked=False), headers=auth)
    ).json()
    fake_out = await client.post(
        f"{API}/visits/{real['id']}/end", json={**_visit_out(), "is_mocked": True}, headers=auth
    )
    assert fake_out.status_code == 422
    still_open = await session.get(FieldVisit, uuid.UUID(real["id"]))
    await session.refresh(still_open)
    assert still_open.ended_at is None
