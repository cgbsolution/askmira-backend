"""Bulk drives: nobody in two places at once, and nobody sent home unexplained."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from app.services.interviews.availability import AvailabilityError
from app.services.interviews.panel_slots import (
    InterviewerCapacity,
    allocate,
    generate_slots,
    verify_no_overlap,
)

DAY_START = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)  # 09:30 IST
DAY_END = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)  # 17:30 IST


def _pool(count: int, *, max_each: int = 6) -> list[InterviewerCapacity]:
    return [
        InterviewerCapacity(
            interviewer_id=f"int-{i}", label=f"Interviewer {i}", max_interviews=max_each
        )
        for i in range(count)
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Slot generation
# ═══════════════════════════════════════════════════════════════════════════


def test_slots_do_not_overlap_and_carry_a_gap() -> None:
    """A grid with no gap means the 11:00 candidate is called at 11:07 and the
    4:00 one at half past five."""
    slots = generate_slots(
        window_start=DAY_START, window_end=DAY_END, duration_minutes=45, gap_minutes=15
    )
    assert slots
    for (_, first_end), (second_start, _) in pairwise(slots):
        assert second_start >= first_end
        assert second_start - first_end == timedelta(minutes=15)


def test_no_slot_runs_past_the_end_of_the_day() -> None:
    slots = generate_slots(
        window_start=DAY_START, window_end=DAY_END, duration_minutes=45, gap_minutes=15
    )
    assert all(end <= DAY_END for _, end in slots)


def test_a_lunch_break_removes_the_slots_it_covers() -> None:
    lunch = (DAY_START + timedelta(hours=3), DAY_START + timedelta(hours=4))
    with_break = generate_slots(
        window_start=DAY_START, window_end=DAY_END, duration_minutes=45,
        gap_minutes=15, breaks=[lunch],
    )
    assert all(not (start < lunch[1] and lunch[0] < end) for start, end in with_break)


def test_a_backwards_window_is_refused() -> None:
    with pytest.raises(AvailabilityError, match="at or before"):
        generate_slots(
            window_start=DAY_END, window_end=DAY_START, duration_minutes=45
        )


# ═══════════════════════════════════════════════════════════════════════════
# Allocation — the invariant
# ═══════════════════════════════════════════════════════════════════════════


def test_no_interviewer_is_ever_in_two_places_at_once() -> None:
    """The checklist gate. Two candidates given the same interviewer at the same
    time is a student who travelled for it standing in a corridor."""
    slots = generate_slots(
        window_start=DAY_START, window_end=DAY_END, duration_minutes=30, gap_minutes=10
    )
    plan = allocate(
        slots=slots,
        candidate_ids=[f"cand-{i}" for i in range(12)],
        interviewers=_pool(4, max_each=5),
        panel_size=1,
    )
    assert verify_no_overlap(list(plan.assignments)) == []


def test_a_panel_of_three_still_never_double_books_anyone() -> None:
    slots = generate_slots(
        window_start=DAY_START, window_end=DAY_END, duration_minutes=45, gap_minutes=15
    )
    plan = allocate(
        slots=slots,
        candidate_ids=[f"cand-{i}" for i in range(6)],
        interviewers=_pool(6, max_each=4),
        panel_size=3,
    )
    assert verify_no_overlap(list(plan.assignments)) == []
    assert all(len(a.interviewer_ids) == 3 for a in plan.assignments)
    assert all(len(set(a.interviewer_ids)) == 3 for a in plan.assignments)


def test_the_load_spreads_rather_than_piling_on_one_person() -> None:
    slots = generate_slots(
        window_start=DAY_START, window_end=DAY_END, duration_minutes=30, gap_minutes=10
    )
    plan = allocate(
        slots=slots,
        candidate_ids=[f"cand-{i}" for i in range(8)],
        interviewers=_pool(4),
        panel_size=1,
    )
    counts: dict[str, int] = {}
    for assignment in plan.assignments:
        for interviewer_id in assignment.interviewer_ids:
            counts[interviewer_id] = counts.get(interviewer_id, 0) + 1
    assert max(counts.values()) - min(counts.values()) <= 1


def test_a_daily_limit_is_respected() -> None:
    """The eleventh interview of the day is not an interview — it is a tired
    person signing whatever the tenth said."""
    slots = generate_slots(
        window_start=DAY_START, window_end=DAY_END, duration_minutes=20, gap_minutes=5
    )
    plan = allocate(
        slots=slots,
        candidate_ids=[f"cand-{i}" for i in range(20)],
        interviewers=_pool(2, max_each=3),
        panel_size=1,
    )
    counts: dict[str, int] = {}
    for assignment in plan.assignments:
        for interviewer_id in assignment.interviewer_ids:
            counts[interviewer_id] = counts.get(interviewer_id, 0) + 1
    assert all(count <= 3 for count in counts.values())
    assert len(plan.assignments) == 6


def test_candidates_who_cannot_be_seated_are_named_and_explained() -> None:
    """Each one is a person who has to be told something."""
    slots = generate_slots(
        window_start=DAY_START, window_end=DAY_START + timedelta(hours=1),
        duration_minutes=30, gap_minutes=10,
    )
    plan = allocate(
        slots=slots,
        candidate_ids=[f"cand-{i}" for i in range(10)],
        interviewers=_pool(1, max_each=2),
        panel_size=1,
    )
    assert not plan.is_complete
    # A one-hour window at a 40-minute stride fits exactly one slot, so nine of
    # the ten are left over.
    assert len(plan.assignments) == 1
    assert len(plan.unplaced_candidate_ids) == 9
    assert plan.notes
    assert "could not be seated" in plan.notes[0]
    assert "sent home" in plan.notes[0]


def test_an_unavailable_interviewer_is_not_given_that_slot() -> None:
    slots = generate_slots(
        window_start=DAY_START, window_end=DAY_END, duration_minutes=30, gap_minutes=10
    )
    blocked = slots[0][0]
    interviewers = [
        InterviewerCapacity(
            interviewer_id="int-0", label="Zero", unavailable_starts=frozenset({blocked})
        ),
        InterviewerCapacity(interviewer_id="int-1", label="One"),
    ]
    plan = allocate(
        slots=slots, candidate_ids=["cand-0"], interviewers=interviewers, panel_size=1
    )
    first = plan.assignments[0]
    assert first.start == blocked
    assert "int-0" not in first.interviewer_ids


def test_a_slot_with_too_few_free_interviewers_is_skipped_not_half_filled() -> None:
    slots = generate_slots(
        window_start=DAY_START, window_end=DAY_END, duration_minutes=30, gap_minutes=10
    )
    plan = allocate(
        slots=slots, candidate_ids=["cand-0"], interviewers=_pool(2), panel_size=3
    )
    assert plan.assignments == ()
    assert plan.unplaced_candidate_ids == ("cand-0",)


def test_an_empty_pool_is_refused() -> None:
    with pytest.raises(AvailabilityError, match="No interviewers"):
        allocate(slots=[], candidate_ids=["c"], interviewers=[], panel_size=1)


def test_a_panel_of_zero_is_refused() -> None:
    with pytest.raises(AvailabilityError, match="at least one interviewer"):
        allocate(slots=[], candidate_ids=["c"], interviewers=_pool(1), panel_size=0)


def test_the_verifier_catches_an_overlap_it_is_handed() -> None:
    """The allocator prevents this. The check stays because the invariant
    matters more than confidence in the code that maintains it."""
    from app.services.interviews.panel_slots import Assignment

    overlapping = [
        Assignment(
            candidate_id="a", start=DAY_START, end=DAY_START + timedelta(minutes=45),
            interviewer_ids=("int-0",),
        ),
        Assignment(
            candidate_id="b", start=DAY_START + timedelta(minutes=30),
            end=DAY_START + timedelta(minutes=75), interviewer_ids=("int-0",),
        ),
    ]
    problems = verify_no_overlap(overlapping)
    assert len(problems) == 1
    assert "double-booked" in problems[0]


def test_the_verifier_catches_a_candidate_scheduled_twice_at_one_time() -> None:
    from app.services.interviews.panel_slots import Assignment

    duplicated = [
        Assignment(
            candidate_id="a", start=DAY_START, end=DAY_START + timedelta(minutes=30),
            interviewer_ids=("int-0",),
        ),
        Assignment(
            candidate_id="a", start=DAY_START, end=DAY_START + timedelta(minutes=30),
            interviewer_ids=("int-1",),
        ),
    ]
    assert any("scheduled twice" in p for p in verify_no_overlap(duplicated))


def test_a_plan_renders_local_labels_for_the_people_standing_in_it() -> None:
    slots = generate_slots(
        window_start=DAY_START, window_end=DAY_END, duration_minutes=30, gap_minutes=10
    )
    plan = allocate(
        slots=slots, candidate_ids=["cand-0"], interviewers=_pool(1), panel_size=1
    )
    payload = plan.as_dict("Asia/Kolkata")
    assert "09:30 AM" in payload["assignments"][0]["local_label"]
