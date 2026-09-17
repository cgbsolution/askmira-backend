"""Punch CSV reading and the attendance input rules (W7)."""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest
from pydantic import ValidationError

from app.models.attendance import AttendanceStatus, PunchDirection
from app.schemas.attendance import (
    RegularisationCreate,
    RegularisationDecision,
    RosterCreate,
    ShiftCreate,
)
from app.services.attendance import csv_import
from app.services.attendance.computation import DayInputs, PunchRecord, compute_day

NOW = datetime(2026, 4, 7, tzinfo=UTC)


def test_a_naive_timestamp_is_read_in_the_stated_timezone() -> None:
    rows, errors = csv_import.parse_punch_csv(
        "employee_code,punched_at,direction\nE1,2026-04-06 09:00,IN\n",
        timezone_name="Asia/Kolkata",
        now=NOW,
    )
    assert not errors
    assert rows[0].punched_at == datetime(2026, 4, 6, 3, 30, tzinfo=UTC)
    assert rows[0].timezone_assumed is True
    assert rows[0].direction is PunchDirection.IN


def test_an_offset_timestamp_is_not_reinterpreted() -> None:
    rows, _ = csv_import.parse_punch_csv(
        "employee_code,punched_at,direction\nE1,2026-04-06T09:00:00Z,out\n",
        timezone_name="Asia/Kolkata",
        now=NOW,
    )
    assert rows[0].punched_at == datetime(2026, 4, 6, 9, 0, tzinfo=UTC)
    assert rows[0].timezone_assumed is False


def test_bad_rows_are_reported_with_their_numbers_and_the_rest_kept() -> None:
    text = (
        "employee_code,punched_at,direction,device_id\n"
        "E1,2026-04-06 09:00,in,D1\n"
        ",2026-04-06 09:00,in,\n"
        "E2,yesterday,in,\n"
        "E3,2026-04-06 09:00,sideways,\n"
        "E4,2027-01-01 09:00,in,\n"
    )
    rows, errors = csv_import.parse_punch_csv(text, timezone_name="UTC", now=NOW)
    assert [r.employee_code for r in rows] == ["E1"]
    assert rows[0].device_id == "D1"
    assert [e.row for e in errors] == [2, 3, 4, 5]


@pytest.mark.parametrize(
    "raw",
    [b"", b"\x00\x01binary", "employee_code,naïve".encode("latin-1") + b"\xff\xfe"],
)
def test_a_file_that_is_not_utf8_text_is_refused(raw: bytes) -> None:
    with pytest.raises(csv_import.CsvImportError):
        csv_import.decode_csv(raw)


def test_an_oversized_file_is_refused_before_reading() -> None:
    with pytest.raises(csv_import.CsvTooLargeError):
        csv_import.decode_csv(b"a" * 11, max_bytes=10)


def test_a_missing_header_column_is_refused() -> None:
    with pytest.raises(csv_import.CsvImportError):
        csv_import.parse_punch_csv("code,time\nE1,x\n", timezone_name="UTC", now=NOW)


def test_an_unknown_timezone_is_refused() -> None:
    with pytest.raises(csv_import.CsvImportError):
        csv_import.parse_punch_csv(
            "employee_code,punched_at,direction\n", timezone_name="Mars/Olympus", now=NOW
        )


def test_a_regularisation_cannot_ask_for_leave_or_be_blank() -> None:
    with pytest.raises(ValidationError):
        RegularisationCreate(
            attendance_date="2026-04-06", requested_status=AttendanceStatus.ON_LEAVE, reason="Sick"
        )
    with pytest.raises(ValidationError):
        RegularisationCreate(
            attendance_date="2026-04-06", requested_status=AttendanceStatus.PRESENT, reason="   "
        )


def test_a_rejection_needs_a_reason() -> None:
    with pytest.raises(ValidationError):
        RegularisationDecision(approve=False)
    assert RegularisationDecision(approve=True).note is None


def test_a_shift_that_crosses_midnight_must_say_so() -> None:
    base = dict(
        code="N1", name="Night", start_time=time(22), end_time=time(6),
        half_day_threshold_minutes=240, full_day_threshold_minutes=420,
    )
    with pytest.raises(ValidationError):
        ShiftCreate(**base)
    assert ShiftCreate(**base, is_night_shift=True).is_night_shift


def test_thresholds_and_weekdays_are_checked() -> None:
    with pytest.raises(ValidationError):
        ShiftCreate(code="G", name="G", start_time=time(9), end_time=time(18),
                    half_day_threshold_minutes=500, full_day_threshold_minutes=480)
    with pytest.raises(ValidationError):
        ShiftCreate(code="G", name="G", start_time=time(9), end_time=time(18),
                    half_day_threshold_minutes=240, full_day_threshold_minutes=480,
                    weekly_off_days=[0])


def test_a_roster_must_cover_every_day_of_its_cycle() -> None:
    with pytest.raises(ValidationError):
        RosterCreate(name="R", cycle_days=3, days=[{"day_index": 0}, {"day_index": 1}])
    with pytest.raises(ValidationError):
        RosterCreate(name="R", cycle_days=2, days=[{"day_index": 0}, {"day_index": 0}])


class _Shift:
    start_time = time(9, 0)
    end_time = time(18, 0)
    break_minutes = 60
    grace_in_minutes = 10
    grace_out_minutes = 10
    half_day_threshold_minutes = 240
    full_day_threshold_minutes = 480
    is_night_shift = False
    weekly_off_days = ()


def test_shift_times_are_read_in_the_employees_timezone() -> None:
    """09:05 in Bengaluru is 03:35 UTC; read as UTC it would be absent or early."""
    from datetime import date
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    day = date(2026, 4, 6)
    computed = compute_day(
        DayInputs(
            employee_id="e1",
            attendance_date=day,
            shift=_Shift(),
            punches=[
                PunchRecord(datetime(2026, 4, 6, 3, 35, tzinfo=UTC), PunchDirection.IN),
                PunchRecord(datetime(2026, 4, 6, 12, 40, tzinfo=UTC), PunchDirection.OUT),
            ],
            timezone=ist,
        )
    )
    assert computed.status is AttendanceStatus.PRESENT
    assert computed.late_by_minutes == 0
    assert computed.early_by_minutes == 0
