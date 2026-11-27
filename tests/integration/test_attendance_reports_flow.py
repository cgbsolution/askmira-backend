"""Attendance reports in the old HRMS's layouts, for cross-checking against it.

HR keeps factoHR's In / Out Activities, Monthly Summary and YTD attendance
downloads open beside ours, so each must have the same title block, columns,
codes and formats — and every number must come from real punches and days.
"""

from __future__ import annotations

import io
from datetime import UTC, date, datetime, timedelta

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app.models.attendance import PunchDirection
from app.models.audit import AuditAction, AuditLog
from app.services.attendance import service as attendance_service
from tests.integration import platform_support
from tests.integration.conftest import as_caller, requires_db
from tests.integration.platform_support import bearer
from tests.integration.test_attendance_export_flow import (  # noqa: F401 - geocoder is a fixture
    HOME,
    OFFICE,
    _last_month,
    _punch,
    _weekdays,
    _world,
    geocoder,
)

pytestmark = [requires_db, pytest.mark.usefixtures("geocoder")]

client = platform_support.client

API = "/api/v1/attendance"


def _book(content: bytes):
    return load_workbook(io.BytesIO(content)).active


async def _download(client, who, body):
    response = await client.post(f"{API}/export", json=body, headers=bearer(who))
    assert response.status_code == 200, response.text
    return response


async def _ravi_month(session, hr, report):
    """Last month for Ravi: a full day, a late day, a half day and a worked Saturday."""
    first, last = _last_month()
    weekdays = _weekdays(first, last)
    full, late, half = weekdays[0], weekdays[1], weekdays[2]
    saturday = next(
        first + timedelta(days=n) for n in range(7) if (first + timedelta(days=n)).weekday() == 5
    )
    await _punch(session, hr, report, full, 9, 30, PunchDirection.IN, OFFICE)
    await _punch(session, hr, report, full, 18, 30, PunchDirection.OUT, HOME)
    await _punch(session, hr, report, late, 9, 58, PunchDirection.IN, OFFICE)
    await _punch(session, hr, report, late, 18, 45, PunchDirection.OUT, OFFICE)
    await _punch(session, hr, report, half, 9, 30, PunchDirection.IN, OFFICE)
    await _punch(session, hr, report, half, 14, 0, PunchDirection.OUT, OFFICE)
    await _punch(session, hr, report, saturday, 10, 0, PunchDirection.IN, OFFICE)
    await _punch(session, hr, report, saturday, 14, 0, PunchDirection.OUT, OFFICE)
    await session.commit()
    await attendance_service.recompute_range(session, hr, employee_ids=None, start=first, end=last)
    return first, last, full, late, half, saturday


async def test_in_out_activities_report_matches_the_old_layout(client, session, tenant):
    hr, manager, report, other, *_ = await _world(session, tenant)
    first, last, full, late, *_ = await _ravi_month(session, hr, report)

    body = {"kind": "in_out_activities", "start": first.isoformat(), "end": last.isoformat()}
    response = await _download(client, hr, body)
    month = f"{first:%b-%y}".upper()
    assert (
        f'filename="In Out Activities Report {month}.xlsx"'
        in response.headers["content-disposition"]
    )
    sheet = _book(response.content)
    assert sheet.title == "rptInOutActivities"
    assert sheet["C2"].value == "In / Out Activities Report"
    assert sheet["C3"].value == f"For the month of {first:%B-%Y}"
    headers = [sheet.cell(row=5, column=c).value for c in range(1, 15)]
    assert headers == [
        "S#",
        "Emp Code",
        "Emp Name",
        "Terminal Name",
        "Terminal Type",
        "Location",
        "Punch Date",
        "Punch \nTime",
        None,  # the time header spans two columns, as in the old report
        "Duration From Previous Punch",
        "Remarks",
        "Punch Info",
        "Location Link",
        "Generation Time / Upload Time",
    ]
    rows = [r for r in sheet.iter_rows(min_row=7) if r[0].value is not None]
    ravi = [r for r in rows if r[2].value == "Ravi Kumar"]
    assert len(ravi) == 8  # one row per punch
    first_in, first_out, second_in = ravi[0], ravi[1], ravi[2]
    assert [c.value for c in first_in[2:5]] == ["Ravi Kumar", "Mobile Check In : ", "OnlineIn"]
    assert first_out[3].value == "Mobile Check Out : " and first_out[4].value == "OnlineOut"
    assert first_in[6].value == datetime.combine(full, datetime.min.time()).replace(
        hour=9, minute=30
    )
    assert first_in[6].number_format == "[$-14009]ddd dd-mmm-yy"
    assert first_in[7].number_format == "[$-14009]hh:mm:ss AM/PM"
    assert first_in[9].value in (None, "")  # nothing before the first punch
    assert first_out[9].value == "09:00:00"
    gap = datetime.combine(late, datetime.min.time()).replace(hour=9, minute=58) - datetime.combine(
        full, datetime.min.time()
    ).replace(hour=18, minute=30)
    seconds = int(gap.total_seconds())
    assert second_in[9].value == f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:00"
    assert "MG Road" in first_in[5].value and first_in[5].value.startswith("1. ")
    assert "Latitude:12.9716" in first_in[11].value
    assert first_in[12].value == "View Map"
    assert first_in[12].hyperlink.target.startswith("https://www.google.com/maps")
    assert isinstance(first_in[13].value, datetime)
    # Ordered by employee code, numbered from one.
    codes = [r[1].value for r in rows]
    assert codes == sorted(codes)
    assert [r[0].value for r in rows] == list(range(1, len(rows) + 1))

    logged = await session.scalar(
        select(AuditLog)
        .where(AuditLog.action == AuditAction.EXPORT, AuditLog.entity_type == "attendance")
        .order_by(AuditLog.created_at.desc())
    )
    assert logged.changes["kind"] == "in_out_activities"
    assert logged.changes["rows"] == len(rows)


async def test_monthly_summary_counts_and_hours(client, session, tenant):
    hr, manager, report, other, *_ = await _world(session, tenant)
    first, last, *_ = await _ravi_month(session, hr, report)

    body = {"kind": "monthly_summary", "start": first.isoformat(), "end": last.isoformat()}
    response = await _download(client, hr, body)
    sheet = _book(response.content)
    assert sheet["D2"].value == "Monthly Summary Attendance Report"
    assert sheet["D3"].value == f"For the month of {first:%B-%Y}"
    assert sheet["D5"].value == "Punch Type: All"
    header = {
        sheet.cell(row=8, column=c).value: c
        for c in range(1, 26)
        if sheet.cell(row=8, column=c).value
    }
    for label in (
        "s#",
        "Emp Code",
        "Emp Name",
        "Department",
        "Shift Time In",
        "Shift Time Out",
        "Days In Month",
        "P",
        "HD",
        "WO",
        "HO",
        "AB",
        "LC",
        "EG",
        "OT",
        "WO (Worked)",
        "Actual Worked Hours",
        "Standard Working Hours",
        "Deficit Hours",
        "Actual Working Hours / Day",
        "Standard Working Hours / Day",
        "LV",
        "NA",
    ):
        assert label in header, label
    row = next(
        r for r in range(10, sheet.max_row + 1) if sheet.cell(row=r, column=3).value == "Ravi Kumar"
    )

    def value(label):
        return sheet.cell(row=row, column=header[label]).value

    days = (last - first).days + 1
    assert value("Days In Month") == days
    assert (value("P"), value("HD"), value("LC")) == (2, 1, 1)
    assert value("WO (Worked)") == 1
    assert value("Shift Time In").strftime("%H:%M") == "09:30"
    assert value("Standard Working Hours") == "27:00"  # 9-hour shift x 3 days worked
    assert value("Standard Working Hours / Day") == "09:00"
    # Every day of the month is in exactly one column.
    counted = sum(value(c) for c in ("P", "HD", "WO", "HO", "AB", "WO (Worked)", "LV", "NA"))
    assert counted == days
    assert value("NA") == 0  # last month is fully computed: nothing is guessed
    actual = value("Actual Worked Hours")
    hours, minutes = (int(x) for x in actual.split(":"))
    deficit = hours * 60 + minutes - 27 * 60
    assert value("Deficit Hours") == ("-" if deficit < 0 else "") + (
        f"{abs(deficit) // 60:02d}:{abs(deficit) % 60:02d}"
    )
    legend = [sheet.cell(row=r, column=1).value for r in range(row, sheet.max_row + 1)]
    assert any(str(text).startswith("'P' for Present Days") for text in legend)


async def test_ytd_report_has_a_row_per_month_and_day_codes(client, session, tenant):
    hr, manager, report, other, *_ = await _world(session, tenant)
    first, last, full, late, half, saturday = await _ravi_month(session, hr, report)
    before = (first - timedelta(days=1)).replace(day=1)
    await attendance_service.recompute_range(
        session, hr, employee_ids=None, start=before, end=first - timedelta(days=1)
    )

    body = {"kind": "ytd", "start": before.isoformat(), "end": last.isoformat()}
    sheet = _book((await _download(client, hr, body)).content)
    assert sheet["C2"].value == (
        f"YTD Attendance Report: Date From: {before:%d-%b-%Y} - Date Till: {last:%d-%b-%Y}"
    )
    header = [sheet.cell(row=7, column=c).value for c in range(1, sheet.max_column + 1)]
    assert header[:9] == [
        "S#",
        "Emp Code",
        "Employee Name",
        "Date of Joining",
        "Last Working Day",
        "Date of Leaving",
        "Designation",
        "Start Date",
        "End Date",
    ]
    assert header[9:40] == list(range(1, 32))
    assert header[40:44] == ["Present Day", "Weekly Off", "Paid Holiday", "Total Paid Days"]
    assert header[-6:] == [
        "WO",
        "Absent Day",
        "Total Days",
        "Total Late",
        "Total Early",
        "Days Not Computed",
    ]
    col = {label: n + 1 for n, label in enumerate(header)}
    ravi = [
        r for r in range(8, sheet.max_row + 1) if sheet.cell(row=r, column=3).value == "Ravi Kumar"
    ]
    assert len(ravi) == 2  # one row per month
    row = ravi[1]
    assert sheet.cell(row=row, column=8).value.date() == first
    assert sheet.cell(row=row, column=9).value.date() == last

    def code(on: date) -> str:
        return sheet.cell(row=row, column=9 + on.day).value

    assert code(full) == "P" and code(late) == "P"
    assert code(half) == "HD"
    assert code(saturday) == "P/WO"
    assert code(saturday + timedelta(days=1)) == "WO"
    absent = next(d for d in _weekdays(first, last) if d not in (full, late, half))
    assert code(absent) == "ABS"
    if last.day < 31:
        assert sheet.cell(row=row, column=9 + 31).value in (None, "")

    def value(label):
        return sheet.cell(row=row, column=col[label]).value

    assert value("Present Day") == 2.5
    assert value("WO") == 1
    assert value("Total Late") == 1
    assert value("Total Days") == (last - first).days + 1
    # Paid, unpaid and not-yet-computed days add up to the month.
    assert value("Total Paid Days") + value("Absent Day") + value("Days Not Computed") == value(
        "Total Days"
    )


async def test_report_ranges_and_who_may_download(client, session, tenant):
    hr, manager, report, *_ = await _world(session, tenant)
    today = datetime.now(UTC).date()

    async def status(body, who=hr):
        response = await client.post(f"{API}/export", json=body, headers=bearer(who))
        return response.status_code

    long = {"start": (today - timedelta(days=200)).isoformat(), "end": today.isoformat()}
    assert await status({**long, "kind": "in_out_activities"}) == 422
    assert await status({**long, "kind": "monthly_summary"}) == 422
    assert await status({**long, "kind": "ytd"}) == 200  # the YTD report may run for a year
    year_and_more = {"start": (today - timedelta(days=400)).isoformat(), "end": today.isoformat()}
    assert await status({**year_and_more, "kind": "ytd"}) == 422
    backwards = {"start": today.isoformat(), "end": (today - timedelta(days=1)).isoformat()}
    assert await status({**backwards, "kind": "ytd"}) == 422

    month = {"start": today.replace(day=1).isoformat(), "end": today.isoformat()}
    assert await status({**month, "kind": "monthly_summary"}, report) == 403
    await as_caller(session, manager)
    response = await client.post(
        f"{API}/export", json={**month, "kind": "monthly_summary"}, headers=bearer(manager)
    )
    assert response.status_code == 200
    sheet = _book(response.content)
    names = {sheet.cell(row=r, column=3).value for r in range(10, sheet.max_row + 1)}
    names.discard(None)
    assert names == {"Mohan Lead", "Ravi Kumar"}
