"""The old HRMS report layouts: hours text, periods, month splits and day codes."""

from __future__ import annotations

from datetime import date, time
from types import SimpleNamespace

from app.models.attendance import AttendanceStatus
from app.services.attendance.reports import (
    file_period,
    hh_mm,
    hh_mm_ss,
    month_segments,
    period_text,
    shift_span_minutes,
    ytd_code,
)


def test_hours_are_written_like_the_old_hrms():
    assert hh_mm(0) == "00:00"
    assert hh_mm(1990) == "33:10"
    assert hh_mm(-124) == "-02:04"
    assert hh_mm(7205) == "120:05"
    assert hh_mm_ss(28331) == "07:52:11"
    assert hh_mm_ss(432522) == "120:08:42"
    assert hh_mm_ss(-5) == "00:00:00"


def test_periods_read_as_a_month_or_as_dates():
    assert period_text(date(2026, 11, 1), date(2026, 11, 30)) == "the month of November-2026"
    assert period_text(date(2026, 2, 1), date(2026, 2, 28)) == "the month of February-2026"
    assert (
        period_text(date(2026, 11, 1), date(2026, 11, 15))
        == "the period 01-Nov-2026 to 15-Nov-2026"
    )
    assert file_period(date(2026, 11, 1), date(2026, 11, 30)) == "NOV-26"
    assert file_period(date(2026, 4, 1), date(2026, 11, 30)) == "APR-26 to NOV-26"
    assert file_period(date(2026, 11, 3), date(2026, 11, 9)) == "03-Nov-2026 to 09-Nov-2026"


def test_a_range_splits_into_calendar_months():
    assert month_segments(date(2026, 10, 15), date(2026, 12, 3)) == [
        (date(2026, 10, 15), date(2026, 10, 31)),
        (date(2026, 11, 1), date(2026, 11, 30)),
        (date(2026, 12, 1), date(2026, 12, 3)),
    ]
    assert month_segments(date(2026, 12, 20), date(2027, 1, 5)) == [
        (date(2026, 12, 20), date(2026, 12, 31)),
        (date(2027, 1, 1), date(2027, 1, 5)),
    ]


def _day(status: AttendanceStatus, worked: int = 0):
    return SimpleNamespace(status=status, worked_minutes=worked)


def test_day_codes_match_the_old_hrms():
    assert ytd_code(None, None) == ""  # not computed: blank, never absent
    assert ytd_code(_day(AttendanceStatus.PRESENT, 480), None) == "P"
    assert ytd_code(_day(AttendanceStatus.HALF_DAY, 250), None) == "HD"
    assert ytd_code(_day(AttendanceStatus.ABSENT), None) == "ABS"
    assert ytd_code(_day(AttendanceStatus.NEEDS_REGULARISATION), None) == "MSP"
    assert ytd_code(_day(AttendanceStatus.WEEK_OFF), None) == "WO"
    assert ytd_code(_day(AttendanceStatus.WEEK_OFF, 300), None) == "P/WO"
    assert ytd_code(_day(AttendanceStatus.HOLIDAY), None) == "HO"
    assert ytd_code(_day(AttendanceStatus.ON_LEAVE), "CL") == "CL"
    assert ytd_code(_day(AttendanceStatus.ON_LEAVE), None) == "LV"
    assert ytd_code(_day(AttendanceStatus.LOP), "LWP") == "LWP"
    assert ytd_code(_day(AttendanceStatus.LOP), None) == "LOP"


def test_standard_hours_span_the_shift_including_night_shifts():
    assert shift_span_minutes(None) == 0
    day = SimpleNamespace(start_time=time(9, 30), end_time=time(18, 0))
    night = SimpleNamespace(start_time=time(22, 0), end_time=time(6, 0))
    assert shift_span_minutes(day) == 510
    assert shift_span_minutes(night) == 480


def test_a_year_to_date_file_is_named_by_months():
    assert file_period(date(2026, 4, 1), date(2026, 11, 18)) == "APR-26 to NOV-26"
    assert file_period(date(2026, 11, 1), date(2026, 11, 15)) == "01-Nov-2026 to 15-Nov-2026"


def test_the_company_logo_is_drawn_and_a_bad_one_is_skipped():
    import io as _io

    from openpyxl import Workbook
    from PIL import Image

    from app.services.attendance.reports import _put_logo

    png = _io.BytesIO()
    Image.new("RGB", (600, 300), "red").save(png, format="PNG")
    sheet = Workbook().active
    _put_logo(sheet, png.getvalue(), "A1", box=(130, 70))
    assert len(sheet._images) == 1
    assert (sheet._images[0].width, sheet._images[0].height) == (130, 65)

    other = Workbook().active
    _put_logo(other, b"<svg xmlns='http://www.w3.org/2000/svg'/>", "A1", box=(130, 70))
    _put_logo(other, None, "A1", box=(130, 70))
    assert other._images == []
