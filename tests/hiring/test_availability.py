"""Everything UTC. Timezones at the edges. A silent calendar is not a free one."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services.interviews.availability import (
    AvailabilityError,
    BusyInterval,
    PanelistBusy,
    WorkingHours,
    free_slots,
    merge_intervals,
    propose_slots,
    render_for,
    to_utc,
)

IST = ZoneInfo("Asia/Kolkata")
NY = ZoneInfo("America/New_York")


def _utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def _busy(day: int, from_hour: int, to_hour: int) -> BusyInterval:
    return BusyInterval(start=_utc(day, from_hour), end=_utc(day, to_hour))


# ═══════════════════════════════════════════════════════════════════════════
# Timezone discipline
# ═══════════════════════════════════════════════════════════════════════════


def test_a_naive_datetime_is_refused_rather_than_guessed_at() -> None:
    """Assuming UTC is wrong for a calendar read; assuming local is wrong for
    stored data. Both produce an interview at the wrong hour that renders
    plausibly enough that nobody questions it."""
    with pytest.raises(AvailabilityError, match="no timezone"):
        BusyInterval(start=datetime(2026, 9, 7, 10), end=_utc(7, 11))


def test_a_wall_clock_time_resolves_against_its_own_zone() -> None:
    """3pm in Delhi and 3pm in New York are nine and a half hours apart."""
    delhi = to_utc(datetime(2026, 9, 7, 15, 0), "Asia/Kolkata")
    new_york = to_utc(datetime(2026, 9, 7, 15, 0), "America/New_York")
    assert delhi == datetime(2026, 9, 7, 9, 30, tzinfo=UTC)
    assert new_york == datetime(2026, 9, 7, 19, 0, tzinfo=UTC)
    assert (new_york - delhi) == timedelta(hours=9, minutes=30)


def test_an_aware_datetime_passes_through_to_utc_unchanged() -> None:
    instant = datetime(2026, 9, 7, 15, 0, tzinfo=NY)
    assert to_utc(instant, "Asia/Kolkata") == instant.astimezone(UTC)


def test_an_unknown_timezone_is_refused_rather_than_defaulted() -> None:
    """A fallback here silently books the wrong hour."""
    with pytest.raises(AvailabilityError, match="not a timezone"):
        to_utc(datetime(2026, 9, 7, 15, 0), "Mars/Olympus_Mons")


def test_the_same_instant_renders_differently_for_two_people() -> None:
    """The interview is one moment. What each side sees is not the same number,
    and both are correct."""
    instant = _utc(7, 13, 30)
    assert render_for(instant, "Asia/Kolkata").strftime("%H:%M") == "19:00"
    assert render_for(instant, "America/New_York").strftime("%H:%M") == "09:30"


def test_working_hours_survive_a_dst_transition() -> None:
    """The US falls back on 1 November 2026. "10am to 6pm" is still 10am to 6pm
    on both sides of it — adding 24 hours repeatedly instead would drift the
    later windows by an hour.
    """
    hours = WorkingHours(
        timezone_name="America/New_York", start=time(10, 0), end=time(18, 0),
        weekdays=(0, 1, 2, 3, 4, 5, 6),
    )
    windows = hours.windows(from_day=date(2026, 10, 30), days=5)
    for start, end in windows:
        assert render_for(start, "America/New_York").hour == 10
        assert render_for(end, "America/New_York").hour == 18

    # And the UTC offsets genuinely differ across the boundary, which is the
    # thing a naive implementation gets wrong.
    offsets = {render_for(start, "America/New_York").utcoffset() for start, _ in windows}
    assert len(offsets) == 2


def test_working_hours_that_end_before_they_start_are_refused() -> None:
    hours = WorkingHours(start=time(18, 0), end=time(10, 0))
    with pytest.raises(AvailabilityError, match="zero length"):
        hours.windows(from_day=date(2026, 9, 7), days=1)


def test_weekends_are_excluded_by_configuration() -> None:
    hours = WorkingHours(timezone_name="Asia/Kolkata")
    windows = hours.windows(from_day=date(2026, 9, 7), days=7)
    assert len(windows) == 5


# ═══════════════════════════════════════════════════════════════════════════
# Intervals
# ═══════════════════════════════════════════════════════════════════════════


def test_touching_busy_blocks_merge_so_no_phantom_gap_appears() -> None:
    """10-11 and 11-12 leave no gap at 11, and reporting one would offer a slot
    that does not exist."""
    merged = merge_intervals([_busy(7, 10, 11), _busy(7, 11, 12)])
    assert len(merged) == 1
    assert merged[0].start == _utc(7, 10)
    assert merged[0].end == _utc(7, 12)


def test_overlapping_blocks_merge_and_separate_ones_do_not() -> None:
    merged = merge_intervals([_busy(7, 10, 12), _busy(7, 11, 13), _busy(7, 15, 16)])
    assert len(merged) == 2
    assert merged[0].end == _utc(7, 13)


def test_a_zero_length_busy_block_is_refused() -> None:
    with pytest.raises(AvailabilityError, match="at or before"):
        BusyInterval(start=_utc(7, 10), end=_utc(7, 10))


def test_back_to_back_is_not_an_overlap() -> None:
    """Closed intervals make every back-to-back pair a conflict, and a scheduler
    that refuses back-to-back interviews is useless on a panel day."""
    block = _busy(7, 10, 11)
    assert not block.overlaps(_utc(7, 11), _utc(7, 12))
    assert block.overlaps(_utc(7, 10, 30), _utc(7, 11, 30))


# ═══════════════════════════════════════════════════════════════════════════
# Free slots
# ═══════════════════════════════════════════════════════════════════════════


def test_a_busy_block_removes_the_slots_it_covers() -> None:
    """The checklist gate: free/busy read excludes genuinely busy slots."""
    slots = free_slots(
        window_start=_utc(7, 9),
        window_end=_utc(7, 13),
        busy=[_busy(7, 10, 11)],
        duration_minutes=60,
        step_minutes=60,
        buffer_minutes=0,
    )
    starts = [start for start, _ in slots]
    assert _utc(7, 9) in starts
    assert _utc(7, 10) not in starts
    assert _utc(7, 11) in starts


def test_the_buffer_keeps_an_interview_off_the_edge_of_a_meeting() -> None:
    """Back-to-back with no gap means the first overrunning by two minutes makes
    the second late, every time."""
    without = free_slots(
        window_start=_utc(7, 9), window_end=_utc(7, 13), busy=[_busy(7, 10, 11)],
        duration_minutes=60, step_minutes=60, buffer_minutes=0,
    )
    with_buffer = free_slots(
        window_start=_utc(7, 9), window_end=_utc(7, 13), busy=[_busy(7, 10, 11)],
        duration_minutes=60, step_minutes=60, buffer_minutes=15,
    )
    assert len(with_buffer) < len(without)
    assert _utc(7, 11) not in [s for s, _ in with_buffer]


def test_no_slot_runs_past_the_end_of_the_window() -> None:
    slots = free_slots(
        window_start=_utc(7, 9), window_end=_utc(7, 10, 30), busy=[],
        duration_minutes=60, step_minutes=30,
    )
    assert all(end <= _utc(7, 10, 30) for _, end in slots)


def test_a_zero_duration_interview_is_refused() -> None:
    with pytest.raises(AvailabilityError, match="greater than zero"):
        free_slots(
            window_start=_utc(7, 9), window_end=_utc(7, 17), busy=[], duration_minutes=0
        )


# ═══════════════════════════════════════════════════════════════════════════
# Panel proposals
# ═══════════════════════════════════════════════════════════════════════════


def _panel(*specs) -> list[PanelistBusy]:
    return [
        PanelistBusy(
            panelist_id=pid,
            intervals=tuple(intervals),
            read_failed=failed,
            failure_reason=reason,
            label=pid,
        )
        for pid, intervals, failed, reason in specs
    ]


def test_a_slot_must_be_free_for_the_whole_panel() -> None:
    """A slot that suits three of four interviewers is not a slot — it is a
    meeting somebody will miss."""
    panel = _panel(
        ("alice", [_busy(7, 10, 11)], False, ""),
        ("bob", [_busy(7, 11, 12)], False, ""),
    )
    slots, _ = propose_slots(
        panelists=panel,
        working_hours=WorkingHours(timezone_name="UTC", start=time(9), end=time(13)),
        from_day=date(2026, 9, 7),
        days=1,
        duration_minutes=60,
        step_minutes=60,
        buffer_minutes=0,
    )
    starts = [slot.start for slot in slots]
    assert _utc(7, 10) not in starts
    assert _utc(7, 11) not in starts
    assert _utc(7, 9) in starts
    assert _utc(7, 12) in starts


def test_a_calendar_that_could_not_be_read_is_not_an_empty_calendar() -> None:
    """Treating a failed read as free is how an interview gets booked over
    somebody's existing meeting, discovered on the day."""
    panel = _panel(
        ("alice", [_busy(7, 10, 11)], False, ""),
        ("bob", [], True, "consent revoked"),
    )
    slots, caveats = propose_slots(
        panelists=panel,
        working_hours=WorkingHours(timezone_name="UTC", start=time(9), end=time(13)),
        from_day=date(2026, 9, 7),
        days=1,
        duration_minutes=60,
        step_minutes=60,
        buffer_minutes=0,
    )
    assert any("could not be read" in c for c in caveats)
    assert any("consent revoked" in c for c in caveats)
    assert slots
    for slot in slots:
        assert "bob" in slot.unverified_panelist_ids
        assert not slot.is_fully_verified
        assert "bob" not in slot.available_panelist_ids


def test_a_readable_calendar_still_vetoes_a_slot_when_another_is_unreadable() -> None:
    """The unreadable one does not confirm anything, but the readable one still
    says no."""
    panel = _panel(
        ("alice", [_busy(7, 10, 11)], False, ""),
        ("bob", [], True, "no calendar connected"),
    )
    slots, _ = propose_slots(
        panelists=panel,
        working_hours=WorkingHours(timezone_name="UTC", start=time(9), end=time(13)),
        from_day=date(2026, 9, 7), days=1, duration_minutes=60,
        step_minutes=60, buffer_minutes=0,
    )
    assert _utc(7, 10) not in [s.start for s in slots]


def test_a_panel_with_no_common_time_says_so_rather_than_returning_nothing() -> None:
    panel = _panel(
        ("alice", [_busy(7, 9, 13)], False, ""),
        ("bob", [], False, ""),
    )
    slots, caveats = propose_slots(
        panelists=panel,
        working_hours=WorkingHours(timezone_name="UTC", start=time(9), end=time(13)),
        from_day=date(2026, 9, 7), days=1, duration_minutes=60,
        step_minutes=60, buffer_minutes=0,
    )
    assert slots == []
    assert any("no slot to offer" in c for c in caveats)


def test_an_empty_panel_is_refused() -> None:
    with pytest.raises(AvailabilityError, match="No panelists"):
        propose_slots(
            panelists=[], working_hours=WorkingHours(), from_day=date(2026, 9, 7),
            days=1, duration_minutes=60,
        )


def test_a_slot_renders_in_the_candidates_own_zone() -> None:
    panel = _panel(("alice", [], False, ""))
    slots, _ = propose_slots(
        panelists=panel,
        working_hours=WorkingHours(timezone_name="Asia/Kolkata", start=time(10), end=time(12)),
        from_day=date(2026, 9, 7), days=1, duration_minutes=60,
        step_minutes=60, buffer_minutes=0,
    )
    payload = slots[0].as_dict("America/New_York")
    assert payload["timezone"] == "America/New_York"
    assert payload["start_utc"].endswith("+00:00")
    assert payload["start_local"] != payload["start_utc"]
