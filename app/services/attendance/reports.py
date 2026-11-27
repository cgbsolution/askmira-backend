"""Attendance reports in the old HRMS's own layouts, for cross-checking.

HR moving from factoHR keeps its three attendance downloads open side by side
with ours, so these copy them cell for cell — same title block, same columns
in the same order, same codes, same number formats:

- **In / Out Activities Report** — one row per punch: terminal, place, date,
  time, the time since that person's previous punch, punch details, a "View
  Map" link and when the punch reached the server.
- **Monthly Summary Attendance Report** — one row per person: shift times, days
  in the period, P / HD / WO / HO / AB, late comings, early goings, overtime,
  days worked on a week-off, and actual against standard hours.
- **YTD Attendance Report** — one row per person per month: a code for each day
  of the month (P, HD, ABS, MSP, WO, P/WO, HO, the leave type's code), then
  present, week-off, paid-holiday and paid-day totals, one column per leave
  type, absent days, late and early counts.

Where ours differs, it is on purpose and shown, never hidden:

- A day not computed yet (today, the days ahead, a month before attendance was
  kept) is not counted as absent. The Monthly Summary's "NA" column and the
  YTD's "Days Not Computed" column hold those days, so every row still adds
  up to the days in the period.
- Leave is its own column ("LV") in the Monthly Summary instead of vanishing.
- Punch details carry what AskMiraa records (source, coordinates, device id,
  photo); GPS accuracy and phone model are not recorded, so are not shown.
"""

from __future__ import annotations

import io
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import Decimal
from typing import Any, Literal

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.attendance import (
    AttendanceDay,
    AttendancePunch,
    AttendanceStatus,
    PunchDirection,
    PunchSource,
    Shift,
)
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.exit_workflow import ExitRecord, ExitStatus
from app.models.leave import LeaveRequest, LeaveRequestStatus, LeaveType
from app.models.org import Department, LegalEntity
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit, branding
from app.services.attendance import addresses
from app.services.attendance import service as attendance_service
from app.services.attendance.export import AttendanceFile, _people, _place_text, _tenure
from app.services.attendance.review import OrgFilters, check_range

__all__ = ["REPORTS", "ReportKind", "export_report"]

ReportKind = Literal["in_out_activities", "monthly_summary", "ytd"]

REPORTS: dict[str, str] = {
    "in_out_activities": "In / Out Activities Report",
    "monthly_summary": "Monthly Summary Attendance Report",
    "ytd": "YTD Attendance Report",
}

#: The YTD report spans months, so it may run for a year; the others, like
#: every other attendance export, for at most 93 days.
MAX_YTD_DAYS = 366

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_LOOKUPS = 1500
_LOOKUP_SECONDS = 25.0


# ── Small pure helpers ──────────────────────────────────────────────────────


def hh_mm(minutes: int) -> str:
    """Minutes as the old HRMS writes hours: "33:10", "-02:04", "120:05"."""
    sign = "-" if minutes < 0 else ""
    minutes = abs(int(minutes))
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def hh_mm_ss(seconds: int) -> str:
    """Seconds as "07:52:11"; hours run past 24 ("120:08:42")."""
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def shift_span_minutes(shift: Shift | None) -> int:
    """Shift start to end, as the old HRMS counts standard hours (breaks included)."""
    if shift is None:
        return 0
    start = shift.start_time.hour * 60 + shift.start_time.minute
    end = shift.end_time.hour * 60 + shift.end_time.minute
    return (end - start) % (24 * 60)


def _number(value: Decimal | int | float) -> int | float:
    """Whole numbers as integers, halves as decimals — never a float sum's noise."""
    as_decimal = Decimal(str(value))
    return int(as_decimal) if as_decimal == as_decimal.to_integral() else float(as_decimal)


def period_text(start: date, end: date) -> str:
    """ "November-2026" for a whole month, else "01-Nov-2026 to 15-Nov-2026"."""
    whole_month = (
        start.day == 1
        and (end + timedelta(days=1)).day == 1
        and (
            start.year,
            start.month,
        )
        == (end.year, end.month)
    )
    if whole_month:
        return f"the month of {start:%B-%Y}"
    return f"the period {start:%d-%b-%Y} to {end:%d-%b-%Y}"


def file_period(start: date, end: date) -> str:
    """ "NOV-26" for a month, "APR-26 to NOV-26" from the 1st across months
    (a year to date ends today), else the dates."""
    first = start.day == 1
    last = (end + timedelta(days=1)).day == 1
    if first and (last or (start.year, start.month) != (end.year, end.month)):
        a, b = f"{start:%b-%y}".upper(), f"{end:%b-%y}".upper()
        return a if a == b else f"{a} to {b}"
    return f"{start:%d-%b-%Y} to {end:%d-%b-%Y}"


def month_segments(start: date, end: date) -> list[tuple[date, date]]:
    """The calendar months a range touches, each clipped to the range."""
    out: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        next_month = (cursor.replace(day=1) + timedelta(days=32)).replace(day=1)
        out.append((cursor, min(end, next_month - timedelta(days=1))))
        cursor = next_month
    return out


def ytd_code(day: AttendanceDay | None, leave_code: str | None) -> str:
    """The old HRMS's code for one day; blank when the day is not computed."""
    if day is None:
        return ""
    worked = day.worked_minutes > 0
    match day.status:
        case AttendanceStatus.PRESENT:
            return "P"
        case AttendanceStatus.HALF_DAY:
            return "HD"
        case AttendanceStatus.WEEK_OFF:
            return "P/WO" if worked else "WO"
        case AttendanceStatus.HOLIDAY:
            return "P/HO" if worked else "HO"
        case AttendanceStatus.ON_LEAVE:
            return leave_code or "LV"
        case AttendanceStatus.LOP:
            return leave_code or "LOP"
        case AttendanceStatus.NEEDS_REGULARISATION:
            return "MSP"
        case _:
            return "ABS"


def _terminal(punch: AttendancePunch) -> tuple[str, str]:
    """Terminal name and type, worded as the old HRMS words them."""
    way = "In" if punch.direction is PunchDirection.IN else "Out"
    check = "Check In" if punch.direction is PunchDirection.IN else "Check Out"
    match punch.source:
        case PunchSource.MOBILE_GEO:
            return f"Mobile {check} : ", f"Online{way}"
        case PunchSource.WEB:
            return f"Web {check} : ", f"Online{way}"
        case PunchSource.BIOMETRIC:
            return (f"Biometric : {punch.device_id}" if punch.device_id else "Biometric"), way
        case PunchSource.KIOSK:
            return (f"Kiosk : {punch.device_id}" if punch.device_id else "Kiosk"), way
        case PunchSource.MANUAL:
            return "Entered by HR", way
        case _:
            return "Imported", way


def _punch_info(punch: AttendancePunch) -> str:
    lines = [f"Source:{_terminal(punch)[0].rstrip(' :')}"]
    if punch.latitude is not None and punch.longitude is not None:
        lines += [f"Latitude:{float(punch.latitude)}", f"Longitude:{float(punch.longitude)}"]
    if punch.device_id:
        lines.append(f"Device Id:{punch.device_id}")
    lines.append(f"Photo:{'Yes' if punch.photo_key else 'No'}")
    return "\n".join(lines)


def _remarks(punch: AttendancePunch) -> str | None:
    if punch.latitude is not None and punch.outside_geofence:
        return "Outside work location"
    return None


# ── Data for one report ─────────────────────────────────────────────────────


@dataclass
class _Data:
    start: date
    end: date
    company: str
    logo: bytes | None
    people: list[Employee]
    zones: dict[uuid.UUID, tzinfo]
    departments: dict[uuid.UUID, str]
    shifts: dict[uuid.UUID, Shift]
    planned_shift: dict[uuid.UUID, Shift | None]
    days: dict[tuple[uuid.UUID, date], AttendanceDay]
    punches: list[AttendancePunch]
    places: dict[addresses.Place, str | None]
    leave: dict[tuple[uuid.UUID, date], str]
    leave_codes: list[str]
    last_working_day: dict[uuid.UUID, date]

    def zone(self, employee_id: uuid.UUID) -> tzinfo:
        return self.zones.get(employee_id, UTC)


# ── Excel styling shared by the three ───────────────────────────────────────


class _Style:
    def __init__(self, family: str, size: float) -> None:
        from openpyxl.styles import Alignment, Border, Font, Side

        thin = Side(style="thin", color="000000")
        self.border = Border(left=thin, right=thin, top=thin, bottom=thin)
        self.font = Font(name=family, size=size, color="000000")
        self.bold = Font(name=family, size=size, bold=True, color="000000")
        self.title = Font(name=family, size=10 if family == "Arial" else 11, color="000000")
        self.title_bold = Font(name=family, size=10 if family == "Arial" else 11, bold=True)
        self.link = Font(name=family, size=size, color="3366CC", underline="single")
        self.wrap = Alignment(wrap_text=True, vertical="top")
        self.center = Alignment(horizontal="center", vertical="center", wrap_text=True)
        self.right = Alignment(horizontal="right", vertical="top", wrap_text=True)
        self.left = Alignment(horizontal="left", wrap_text=True)


def _put_logo(sheet: Any, logo: bytes | None, anchor: str, *, box: tuple[int, int]) -> None:
    """The company logo in the title block; skipped quietly if it cannot be drawn."""
    if not logo:
        return
    try:
        from openpyxl.drawing.image import Image as SheetImage
        from PIL import Image

        picture = Image.open(io.BytesIO(logo))
        picture.thumbnail(box)
        png = io.BytesIO()
        picture.save(png, format="PNG")
        png.seek(0)
        image = SheetImage(png)
        image.width, image.height = picture.size
        sheet.add_image(image, anchor)
    except Exception:  # an SVG or broken logo just leaves the corner empty
        return


def _book_bytes(book: Any) -> bytes:
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


# ── In / Out Activities Report ──────────────────────────────────────────────


def in_out_activities(data: _Data) -> tuple[bytes, int]:
    from openpyxl import Workbook

    style = _Style("Arial", 9)
    book = Workbook()
    sheet = book.active
    sheet.title = "rptInOutActivities"
    widths = {
        "A": 4.5,
        "B": 11.5,
        "C": 18,
        "D": 12,
        "E": 9.8,
        "F": 30,
        "G": 12.8,
        "H": 10.1,
        "I": 3.7,
        "J": 10.2,
        "K": 12,
        "L": 22,
        "M": 9,
        "N": 13.5,
    }
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width

    for row in (1, 2, 3, 4):
        sheet.merge_cells(f"C{row}:N{row}")
    sheet["C1"], sheet["C1"].font = data.company, style.title_bold
    sheet["C2"], sheet["C2"].font = "In / Out Activities Report", style.title
    sheet["C3"] = f"For {period_text(data.start, data.end)}"
    sheet["C3"].font = style.title
    sheet["C4"] = " "
    sheet.row_dimensions[1].height = 18

    headers = {
        "A": "S#",
        "B": "Emp Code",
        "C": "Emp Name",
        "D": "Terminal Name",
        "E": "Terminal Type",
        "F": "Location",
        "G": "Punch Date",
        "H": "Punch \nTime",
        "J": "Duration From Previous Punch",
        "K": "Remarks",
        "L": "Punch Info",
        "M": "Location Link",
        "N": "Generation Time / Upload Time",
    }
    sheet.merge_cells("H5:I5")
    for column, label in headers.items():
        cell = sheet[f"{column}5"]
        cell.value, cell.font, cell.alignment, cell.border = (
            label,
            style.bold,
            style.center,
            style.border,
        )
    sheet["I5"].border = style.border
    sheet.row_dimensions[5].height = 46
    sheet.merge_cells("A6:N6")
    sheet.row_dimensions[6].height = 8.75
    sheet.freeze_panes = "A7"

    by_id = {e.id: e for e in data.people}
    order = {e.id: n for n, e in enumerate(data.people)}
    punches = sorted(
        (p for p in data.punches if p.employee_id in by_id),
        key=lambda p: (order[p.employee_id], p.punched_at),
    )
    row = 7
    previous: AttendancePunch | None = None
    for number, punch in enumerate(punches, start=1):
        employee = by_id[punch.employee_id]
        zone = data.zone(punch.employee_id)
        local = punch.punched_at.astimezone(zone).replace(tzinfo=None)
        uploaded = (punch.created_at or punch.punched_at).astimezone(zone).replace(tzinfo=None)
        same_person = previous is not None and previous.employee_id == punch.employee_id
        since = (
            hh_mm_ss(int((punch.punched_at - previous.punched_at).total_seconds()))
            if same_person and previous is not None
            else ""
        )
        name, kind = _terminal(punch)
        place = addresses.place_of(punch.latitude, punch.longitude)
        address = data.places.get(place) if place else None
        where = [text for text in (address, _place_text(punch)) if text]
        location = "\n".join(f"{n}. {text}" for n, text in enumerate(where, start=1))
        info = _punch_info(punch)
        values: list[tuple[str, Any, str | None]] = [
            ("A", number, None),
            ("B", employee.employee_code, None),
            ("C", employee.full_name, None),
            ("D", name, None),
            ("E", kind, None),
            ("F", location or "", None),
            ("G", local, "[$-14009]ddd dd-mmm-yy"),
            ("H", local, "[$-14009]hh:mm:ss AM/PM"),
            ("I", None, None),
            ("J", since, None),
            ("K", _remarks(punch), None),
            ("L", info, None),
            ("M", None, None),
            ("N", uploaded, "[$-14009]dd-mm-yyyy hh:mm:ss"),
        ]
        for column, value, fmt in values:
            cell = sheet[f"{column}{row}"]
            cell.value = value
            cell.font, cell.border = style.font, style.border
            cell.alignment = style.center if column == "H" else style.wrap
            if fmt:
                cell.number_format = fmt
            elif isinstance(value, str):
                cell.data_type = "s"  # a value, never a formula
        if punch.latitude is not None and punch.longitude is not None:
            link = sheet[f"M{row}"]
            link.value = "View Map"
            link.hyperlink = addresses.maps_link(punch.latitude, punch.longitude)
            link.font = style.link
        lines = max(info.count("\n") + 1, location.count("\n") + 1 if location else 1)
        sheet.row_dimensions[row].height = max(15, 12.5 * lines + 4)
        previous = punch
        row += 1
    return _book_bytes(book), len(punches)


# ── Monthly Summary Attendance Report ───────────────────────────────────────


def _main_shift(data: _Data, employee: Employee, own: list[AttendanceDay]) -> Shift | None:
    used = Counter(d.shift_id for d in own if d.shift_id)
    if used:
        return data.shifts.get(used.most_common(1)[0][0])
    return data.planned_shift.get(employee.id)


def monthly_summary(data: _Data) -> tuple[bytes, int]:
    from openpyxl import Workbook

    style = _Style("Arial", 8)
    book = Workbook()
    sheet = book.active
    sheet.title = "rptMonthlySummaryAttendance"
    widths = {
        "A": 3.5,
        "B": 10,
        "C": 8,
        "D": 10,
        "E": 11,
        "F": 6.2,
        "G": 6.2,
        "H": 6.2,
        "I": 4.5,
        "J": 4.5,
        "K": 4.5,
        "L": 4.5,
        "M": 4.5,
        "N": 4.5,
        "O": 4.5,
        "P": 6,
        "Q": 4,
        "R": 2.5,
        "S": 8.2,
        "T": 8.2,
        "U": 8,
        "V": 8.2,
        "W": 8.2,
        "X": 4.5,
        "Y": 4.5,
    }
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width

    sheet.merge_cells("A1:C4")
    _put_logo(sheet, data.logo, "A1", box=(130, 70))
    sheet["D1"], sheet["D1"].font = data.company, style.title_bold
    sheet.merge_cells("D2:Q2")
    sheet["D2"], sheet["D2"].font = "Monthly Summary Attendance Report", style.title
    sheet["D3"] = f"For {period_text(data.start, data.end)}"
    sheet["D3"].font = style.title
    sheet["D4"] = " "
    sheet["D5"], sheet["D5"].font = "Punch Type: All", style.title
    for row in range(1, 6):
        sheet.row_dimensions[row].height = 14.4

    headers = {
        "A": "s#",
        "B": "Emp Code",
        "C": "Emp Name",
        "E": "Department",
        "F": "Shift Time In",
        "G": "Shift Time Out",
        "H": "Days In Month",
        "I": "P",
        "J": "HD",
        "K": "WO",
        "L": "HO",
        "M": "AB",
        "N": "LC",
        "O": "EG",
        "P": "OT",
        "Q": "WO (Worked)",
        "S": "Actual Worked Hours",
        "T": "Standard Working Hours",
        "U": "Deficit Hours",
        "V": "Actual Working Hours / Day",
        "W": "Standard Working Hours / Day",
        "X": "LV",
        "Y": "NA",
    }
    sheet.merge_cells("C8:D8")
    sheet.merge_cells("Q8:R8")
    for column, label in headers.items():
        cell = sheet[f"{column}8"]
        cell.value, cell.font, cell.alignment, cell.border = (
            label,
            style.bold,
            style.center,
            style.border,
        )
    for column in ("D", "R"):
        sheet[f"{column}8"].border = style.border
    sheet.row_dimensions[8].height = 51
    sheet.merge_cells("A9:Y9")
    sheet.row_dimensions[9].height = 10.8
    sheet.freeze_panes = "A10"

    dates = [data.start + timedelta(days=n) for n in range((data.end - data.start).days + 1)]
    one_decimal = "[$-014009]#,##0.0;-#,##0.0"
    row = 10
    for number, employee in enumerate(data.people, start=1):
        span = _tenure(employee, dates)
        own = [data.days[(employee.id, on)] for on in span if (employee.id, on) in data.days]

        def count(*statuses: AttendanceStatus, own: list[AttendanceDay] = own) -> int:
            return sum(1 for d in own if d.status in statuses)

        worked_off = sum(
            1 for d in own if d.status is AttendanceStatus.WEEK_OFF and d.worked_minutes > 0
        )
        present = count(AttendanceStatus.PRESENT)
        half = count(AttendanceStatus.HALF_DAY)
        actual = sum(d.worked_minutes for d in own)
        shift = _main_shift(data, employee, own)
        standard = sum(
            shift_span_minutes(data.shifts.get(d.shift_id) if d.shift_id else shift)
            for d in own
            if d.status in (AttendanceStatus.PRESENT, AttendanceStatus.HALF_DAY)
        )
        days_worked = present + half
        values: list[tuple[str, Any, str | None, Any]] = [
            ("A", number, None, style.wrap),
            ("B", employee.employee_code, None, style.wrap),
            ("C", employee.full_name, None, style.wrap),
            (
                "E",
                data.departments.get(employee.department_id) if employee.department_id else None,
                None,
                style.wrap,
            ),
            ("F", shift.start_time if shift else None, "[$-014009]hh:mm", style.center),
            ("G", shift.end_time if shift else None, "[$-014009]hh:mm", style.center),
            ("H", len(dates), None, style.right),
            ("I", present, one_decimal, style.right),
            ("J", half, one_decimal, style.right),
            ("K", count(AttendanceStatus.WEEK_OFF) - worked_off, one_decimal, style.right),
            ("L", count(AttendanceStatus.HOLIDAY), one_decimal, style.right),
            (
                "M",
                count(AttendanceStatus.ABSENT, AttendanceStatus.NEEDS_REGULARISATION),
                one_decimal,
                style.right,
            ),
            ("N", sum(1 for d in own if d.late_by_minutes > 0), one_decimal, style.right),
            ("O", sum(1 for d in own if d.early_by_minutes > 0), one_decimal, style.right),
            ("P", hh_mm(sum(d.overtime_minutes for d in own)), None, style.right),
            ("Q", worked_off, None, style.right),
            ("S", hh_mm(actual), None, style.right),
            ("T", hh_mm(standard), None, style.right),
            ("U", hh_mm(actual - standard), None, style.right),
            ("V", hh_mm(actual // days_worked if days_worked else 0), None, style.right),
            ("W", hh_mm(shift_span_minutes(shift)), None, style.right),
            ("X", count(AttendanceStatus.ON_LEAVE, AttendanceStatus.LOP), one_decimal, style.right),
            ("Y", len(dates) - len(own), one_decimal, style.right),
        ]
        sheet.merge_cells(f"C{row}:D{row}")
        sheet.merge_cells(f"Q{row}:R{row}")
        for column, value, fmt, align in values:
            cell = sheet[f"{column}{row}"]
            cell.value = value
            cell.font, cell.border, cell.alignment = style.font, style.border, align
            if fmt:
                cell.number_format = fmt
            elif isinstance(value, str):
                cell.data_type = "s"
        for column in ("D", "R"):
            sheet[f"{column}{row}"].border = style.border
        sheet.row_dimensions[row].height = 18
        row += 1

    legend = [
        "'P' for Present Days, 'HD' for Half-day, 'OT' for Overtime hours, 'WO' for Week-off days,",
        "'HO' for Holidays, 'AB' for Absent Days, 'LC' for Late Coming, 'EG' for Early going",
        "'WO (Worked)' for Week-off days worked, 'LV' for Leave days, 'NA' for days not computed "
        "yet, before joining or after leaving",
    ]
    for text in legend:
        sheet[f"A{row}"], sheet[f"A{row}"].font = text, style.font
        row += 1
    return _book_bytes(book), len(data.people)


# ── YTD Attendance Report ───────────────────────────────────────────────────


def ytd(data: _Data) -> tuple[bytes, int]:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    book = Workbook()
    sheet = book.active
    sheet.title = "Sheet1"
    bold = Font(name="Calibri", size=11, bold=True)
    plain = Font(name="Calibri", size=11)
    center = Alignment(horizontal="center")

    sheet.merge_cells("A1:B5")
    _put_logo(sheet, data.logo, "A1", box=(150, 95))
    sheet["C1"], sheet["C1"].font = data.company, bold
    sheet["C2"] = (
        f"YTD Attendance Report: Date From: {data.start:%d-%b-%Y} - Date Till: {data.end:%d-%b-%Y}"
    )
    sheet["C2"].font = bold
    sheet["C3"], sheet["C3"].font = "Punch Type: All", bold

    head = [
        "S#",
        "Emp Code",
        "Employee Name",
        "Date of Joining",
        "Last Working Day",
        "Date of Leaving",
        "Designation",
        "Start Date",
        "End Date",
        *range(1, 32),
        "Present Day",
        "Weekly Off",
        "Paid Holiday",
        "Total Paid Days",
        *data.leave_codes,
        "WO",
        "Absent Day",
        "Total Days",
        "Total Late",
        "Total Early",
        "Days Not Computed",
    ]
    for index, label in enumerate(head, start=1):
        cell = sheet.cell(row=7, column=index, value=label)
        cell.font, cell.alignment = bold, Alignment(horizontal="center", wrap_text=True)
    widths = [9.2, 14.3, 30, 14.7, 17.1, 15.3, 30, 12.8, 12.8]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    for index in range(10, 41):
        sheet.column_dimensions[get_column_letter(index)].width = 5.5
    for index in range(41, len(head) + 1):
        sheet.column_dimensions[get_column_letter(index)].width = 11.5
    sheet.freeze_panes = "D8"

    date_format = "dd-mmm-yyyy"
    row = 8
    number = 0
    for employee in data.people:
        for seg_start, seg_end in month_segments(data.start, data.end):
            dates = [seg_start + timedelta(days=n) for n in range((seg_end - seg_start).days + 1)]
            span = set(_tenure(employee, dates))
            if not span:
                continue
            number += 1
            own = {on: data.days.get((employee.id, on)) for on in dates if on in span}
            computed = [d for d in own.values() if d is not None]
            per_leave: dict[str, Decimal] = dict.fromkeys(data.leave_codes, Decimal(0))
            for on, day in own.items():
                code = data.leave.get((employee.id, on))
                if day is None or code is None or code not in per_leave:
                    continue
                if day.status in (AttendanceStatus.ON_LEAVE, AttendanceStatus.LOP):
                    per_leave[code] += 1
                elif day.status is AttendanceStatus.HALF_DAY:
                    per_leave[code] += Decimal("0.5")

            def count(*statuses: AttendanceStatus, days: list[AttendanceDay] = computed) -> int:
                return sum(1 for d in days if d.status in statuses)

            worked_off = sum(
                1
                for d in computed
                if d.status is AttendanceStatus.WEEK_OFF and d.worked_minutes > 0
            )
            paid = sum((Decimal(str(d.payable_fraction)) for d in computed), start=Decimal(0))
            unpaid = sum(
                (
                    Decimal(1) - Decimal(str(d.payable_fraction))
                    for d in computed
                    if d.status not in (AttendanceStatus.WEEK_OFF, AttendanceStatus.HOLIDAY)
                ),
                start=Decimal(0),
            )
            values: list[Any] = [
                number,
                employee.employee_code,
                employee.full_name,
                employee.date_of_joining,
                data.last_working_day.get(employee.id) or "",
                employee.date_of_exit or "",
                employee.designation or "",
                seg_start,
                seg_end,
            ]
            for day_number in range(1, 32):
                try:
                    on = seg_start.replace(day=day_number)
                except ValueError:
                    values.append(None)  # no such day this month
                    continue
                if on < seg_start or on > seg_end or on not in span:
                    values.append("")
                else:
                    values.append(ytd_code(own.get(on), data.leave.get((employee.id, on))))
            values += [
                _number(
                    count(AttendanceStatus.PRESENT)
                    + Decimal("0.5") * count(AttendanceStatus.HALF_DAY)
                ),
                count(AttendanceStatus.WEEK_OFF) - worked_off,
                sum(
                    1
                    for d in computed
                    if d.status is AttendanceStatus.HOLIDAY and d.payable_fraction > 0
                ),
                _number(paid),
                *(_number(per_leave[code]) for code in data.leave_codes),
                worked_off,
                _number(unpaid),
                len(dates),
                sum(1 for d in computed if d.late_by_minutes > 0),
                sum(1 for d in computed if d.early_by_minutes > 0),
                # Before joining, after leaving, or not computed yet.
                len(dates) - len(computed),
            ]
            for index, value in enumerate(values, start=1):
                cell = sheet.cell(row=row, column=index, value=value)
                cell.font, cell.alignment = plain, center
                if isinstance(value, date):
                    cell.number_format = date_format
                elif isinstance(value, str):
                    cell.data_type = "s"
            row += 1
    return _book_bytes(book), number


# ── Reading ─────────────────────────────────────────────────────────────────


async def _company(
    session: AsyncSession, user: User, filters: OrgFilters, people: list[Employee]
) -> tuple[str, bytes | None]:
    """The company the report is for: the one filtered to, the one everybody
    in it belongs to, else the workspace."""
    tenant = await session.get(Tenant, user.tenant_id)
    entity_ids = {e.legal_entity_id for e in people}
    entity_id = filters.legal_entity_id or (
        next(iter(entity_ids)) if len(entity_ids) == 1 else None
    )
    entity = (
        await session.scalar(
            tenant_scoped(select(LegalEntity).where(LegalEntity.id == entity_id), LegalEntity, user)
        )
        if entity_id
        else None
    )
    name = entity.name if entity else (tenant.name if tenant else "")
    return name, await branding.payslip_logo(entity, tenant)


async def _leave_by_day(
    session: AsyncSession, user: User, ids: list[uuid.UUID], start: date, end: date
) -> tuple[dict[tuple[uuid.UUID, date], str], list[str]]:
    """Approved leave's type code by person and date, and every leave code in use."""
    types = (
        await session.scalars(
            tenant_scoped(select(LeaveType).order_by(LeaveType.code), LeaveType, user)
        )
    ).all()
    by_day: dict[tuple[uuid.UUID, date], str] = {}
    if ids:
        rows = (
            await session.execute(
                tenant_scoped(
                    select(
                        LeaveRequest.employee_id,
                        LeaveRequest.start_date,
                        LeaveRequest.end_date,
                        LeaveType.code,
                    )
                    .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
                    .where(
                        LeaveRequest.employee_id.in_(ids),
                        LeaveRequest.status == LeaveRequestStatus.APPROVED,
                        LeaveRequest.start_date <= end,
                        LeaveRequest.end_date >= start,
                    )
                    .order_by(LeaveRequest.start_date),
                    LeaveRequest,
                    user,
                )
            )
        ).all()
        for employee_id, first, last, code in rows:
            cursor = max(first, start)
            while cursor <= min(last, end):
                by_day.setdefault((employee_id, cursor), code)
                cursor += timedelta(days=1)
    used = set(by_day.values())
    # Holiday and Week off requests are not leave: their days are counted in
    # the Holiday and Week off columns, from the day's status.
    codes = [t.code for t in types if t.marks_day_as is None and (t.is_active or t.code in used)]
    return by_day, list(dict.fromkeys(codes))


async def _last_working_days(
    session: AsyncSession, user: User, ids: list[uuid.UUID]
) -> dict[uuid.UUID, date]:
    if not ids:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(
                select(
                    ExitRecord.employee_id,
                    ExitRecord.approved_last_working_day,
                    ExitRecord.requested_last_working_day,
                )
                .where(ExitRecord.employee_id.in_(ids), ExitRecord.status != ExitStatus.CANCELLED)
                .order_by(ExitRecord.resignation_date),
                ExitRecord,
                user,
            )
        )
    ).all()
    out: dict[uuid.UUID, date] = {}
    for employee_id, approved, requested in rows:
        if approved or requested:
            out[employee_id] = approved or requested
    return out


def check_report_range(kind: ReportKind, start: date, end: date) -> None:
    if kind != "ytd":
        check_range(start, end)
        return
    if end < start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The end date cannot be before the start date.",
        )
    if (end - start).days >= MAX_YTD_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Ask for at most a year at a time.",
        )


async def export_report(
    session: AsyncSession,
    user: User,
    *,
    start: date,
    end: date,
    kind: ReportKind,
    filters: OrgFilters,
    employee_ids: list[uuid.UUID] | None = None,
    request: Request | None = None,
) -> AttendanceFile:
    """One report in the old HRMS's layout, as Excel. HR: everyone; a manager: their line."""
    if not permissions.is_hr(user) and not user.has_reports:
        raise permissions.forbidden("Only HR and managers can download attendance reports.")
    check_report_range(kind, start, end)
    people = await _people(
        session, user, start=start, end=end, filters=filters, employee_ids=employee_ids
    )
    people.sort(key=lambda e: (e.employee_code or "", e.full_name))
    ids = [e.id for e in people]
    zones = await attendance_service.employee_timezones(session, user, ids) if ids else {}

    punches: list[AttendancePunch] = []
    places: dict[addresses.Place, str | None] = {}
    if kind == "in_out_activities" and ids:
        loaded = (
            await session.scalars(
                tenant_scoped(
                    select(AttendancePunch)
                    .where(
                        AttendancePunch.employee_id.in_(ids),
                        AttendancePunch.punched_at
                        >= datetime.combine(start - timedelta(days=1), time.min, UTC),
                        AttendancePunch.punched_at
                        < datetime.combine(end + timedelta(days=2), time.min, UTC),
                    )
                    .order_by(AttendancePunch.punched_at),
                    AttendancePunch,
                    user,
                )
            )
        ).all()
        punches = [
            p
            for p in loaded
            if start <= p.punched_at.astimezone(zones.get(p.employee_id, UTC)).date() <= end
        ]
        wanted = [(p.latitude, p.longitude) for p in punches if p.latitude is not None]
        if wanted:
            places = await addresses.addresses_for(
                session, user, wanted, budget_seconds=_LOOKUP_SECONDS, max_lookups=_LOOKUPS
            )

    days: dict[tuple[uuid.UUID, date], AttendanceDay] = {}
    if kind != "in_out_activities" and ids:
        for day in (
            await session.scalars(
                tenant_scoped(
                    select(AttendanceDay).where(
                        AttendanceDay.employee_id.in_(ids),
                        AttendanceDay.attendance_date >= start,
                        AttendanceDay.attendance_date <= end,
                    ),
                    AttendanceDay,
                    user,
                )
            )
        ).all():
            days[(day.employee_id, day.attendance_date)] = day

    shifts = {
        s.id: s for s in (await session.scalars(tenant_scoped(select(Shift), Shift, user))).all()
    }
    planned: dict[uuid.UUID, Shift | None] = {}
    if kind == "monthly_summary" and ids:
        resolver = await attendance_service.ShiftResolver.load(session, user, ids, start, end)
        planned = {e.id: resolver.resolve(e.id, start).shift for e in people}

    leave: dict[tuple[uuid.UUID, date], str] = {}
    leave_codes: list[str] = []
    last_working: dict[uuid.UUID, date] = {}
    if kind == "ytd":
        leave, leave_codes = await _leave_by_day(session, user, ids, start, end)
        last_working = await _last_working_days(session, user, ids)

    company, logo = await _company(session, user, filters, people)
    departments = dict(
        (
            await session.execute(
                tenant_scoped(select(Department.id, Department.name), Department, user)
            )
        ).all()  # type: ignore[arg-type]
    )
    data = _Data(
        start=start,
        end=end,
        company=company,
        logo=logo,
        people=people,
        zones=zones,
        departments=departments,
        shifts=shifts,
        planned_shift=planned,
        days=days,
        punches=punches,
        places=places,
        leave=leave,
        leave_codes=leave_codes,
        last_working_day=last_working,
    )
    builder = {
        "in_out_activities": in_out_activities,
        "monthly_summary": monthly_summary,
        "ytd": ytd,
    }
    content, rows = builder[kind](data)

    label = REPORTS[kind]
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.EXPORT,
        entity_type="attendance",
        summary=(f"Downloaded {label} ({start:%d %b %Y} to {end:%d %b %Y}, {len(people)} people)"),
        changes={
            "kind": kind,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "people": len(people),
            "rows": rows,
            "format": "xlsx",
            "ticked_people": len(employee_ids) if employee_ids is not None else None,
            **filters.audit(),
        },
        request=request,
    )
    names = {
        "in_out_activities": "In Out Activities Report",
        "monthly_summary": "Monthly Attendance Summary Report",
        "ytd": "YTD Attendance Report",
    }
    return AttendanceFile(
        content=content,
        filename=f"{names[kind]} {file_period(start, end)}.xlsx",
        media_type=_XLSX,
        people=len(people),
        rows=rows,
    )
