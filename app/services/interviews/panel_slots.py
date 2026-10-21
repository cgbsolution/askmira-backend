"""Bulk slot generation for campus and walk-in drives.

A campus drive schedules dozens of interviews across a pool of interviewers in
one operation, and the failure mode is specific: two candidates given the same
interviewer at the same time. That is not a scheduling inconvenience — it is a
student who travelled for it standing in a corridor.

So the allocator is exhaustive about one thing: **no interviewer appears in two
overlapping assignments.** It is checked as it builds, and `verify_no_overlap`
re-checks the finished plan, because the invariant is worth more than the
cleverness of the algorithm that maintains it.

The strategy is deliberately simple — earliest slot, least-loaded eligible
interviewer. A cleverer optimiser would produce a plan nobody can predict or
explain, and the person running a drive needs to be able to look at the grid and
say "why is Priya on four in a row".

Pure. No database.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise

from app.services.interviews.availability import AvailabilityError, Slot, render_for

__all__ = [
    "Assignment",
    "BulkPlan",
    "InterviewerCapacity",
    "allocate",
    "generate_slots",
    "verify_no_overlap",
]


@dataclass(frozen=True, slots=True)
class InterviewerCapacity:
    """One interviewer in the pool.

    `max_interviews` exists because a drive will happily give somebody eleven
    interviews in a day, and the eleventh one is not an interview — it is a
    tired person signing whatever the tenth said.
    """

    interviewer_id: str
    label: str = ""
    max_interviews: int = 6
    #: Slots this person cannot take, as UTC start times.
    unavailable_starts: frozenset[datetime] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class Assignment:
    candidate_id: str
    start: datetime
    end: datetime
    interviewer_ids: tuple[str, ...]

    def as_dict(self, timezone_name: str = "Asia/Kolkata") -> dict:
        return {
            "candidate_id": self.candidate_id,
            "start_utc": self.start.isoformat(),
            "end_utc": self.end.isoformat(),
            "local_label": render_for(self.start, timezone_name).strftime("%a %d %b, %I:%M %p"),
            "interviewer_ids": list(self.interviewer_ids),
        }


@dataclass(frozen=True, slots=True)
class BulkPlan:
    assignments: tuple[Assignment, ...]
    #: Candidates the pool could not seat. Named, not counted — each one is a
    #: person who has to be told something.
    unplaced_candidate_ids: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        return not self.unplaced_candidate_ids

    def as_dict(self, timezone_name: str = "Asia/Kolkata") -> dict:
        return {
            "assignments": [a.as_dict(timezone_name) for a in self.assignments],
            "unplaced_candidate_ids": list(self.unplaced_candidate_ids),
            "complete": self.is_complete,
            "notes": list(self.notes),
        }


def generate_slots(
    *,
    window_start: datetime,
    window_end: datetime,
    duration_minutes: int,
    gap_minutes: int = 10,
    breaks: list[tuple[datetime, datetime]] | None = None,
) -> list[tuple[datetime, datetime]]:
    """A day's grid of interview slots.

    `gap_minutes` is not padding for its own sake. Interviews overrun; a grid
    with no gap means the 11:00 candidate is called at 11:07 and the 4:00 one at
    half past five.
    """
    if duration_minutes <= 0:
        raise AvailabilityError("An interview needs a duration greater than zero.")
    if window_end <= window_start:
        raise AvailabilityError("The drive window ends at or before it starts.")

    duration = timedelta(minutes=duration_minutes)
    stride = duration + timedelta(minutes=max(0, gap_minutes))
    pauses = breaks or []

    out: list[tuple[datetime, datetime]] = []
    cursor = window_start
    while cursor + duration <= window_end:
        end = cursor + duration
        if not any(b_start < end and cursor < b_end for b_start, b_end in pauses):
            out.append((cursor, end))
        cursor += stride
    return out


def allocate(
    *,
    slots: list[tuple[datetime, datetime]],
    candidate_ids: list[str],
    interviewers: list[InterviewerCapacity],
    panel_size: int = 1,
) -> BulkPlan:
    """Seat every candidate, or say who is left over and why.

    Earliest slot first, least-loaded eligible interviewer. Predictable rather
    than optimal, because the person running the drive has to be able to explain
    the grid to the people standing in it.
    """
    if panel_size < 1:
        raise AvailabilityError("A panel needs at least one interviewer.")
    if not interviewers:
        raise AvailabilityError("No interviewers in the pool; nothing can be scheduled.")

    load: dict[str, int] = defaultdict(int)
    # interviewer -> the slot starts they are already committed to.
    committed: dict[str, set[datetime]] = defaultdict(set)
    by_id = {i.interviewer_id: i for i in interviewers}

    assignments: list[Assignment] = []
    unplaced: list[str] = []
    notes: list[str] = []

    remaining = list(candidate_ids)
    for start, end in slots:
        if not remaining:
            break

        eligible = [
            i
            for i in interviewers
            if start not in i.unavailable_starts
            and start not in committed[i.interviewer_id]
            and load[i.interviewer_id] < i.max_interviews
        ]
        if len(eligible) < panel_size:
            continue

        eligible.sort(key=lambda i: (load[i.interviewer_id], i.interviewer_id))
        chosen = eligible[:panel_size]

        candidate = remaining.pop(0)
        for interviewer in chosen:
            load[interviewer.interviewer_id] += 1
            committed[interviewer.interviewer_id].add(start)

        assignments.append(
            Assignment(
                candidate_id=candidate,
                start=start,
                end=end,
                interviewer_ids=tuple(i.interviewer_id for i in chosen),
            )
        )

    unplaced = remaining
    if unplaced:
        exhausted = [by_id[i].label or i for i in load if load[i] >= by_id[i].max_interviews]
        notes.append(
            f"{len(unplaced)} candidate(s) could not be seated. "
            + (
                f"{len(exhausted)} interviewer(s) hit their daily limit: "
                f"{', '.join(sorted(exhausted))}. "
                if exhausted
                else "The slot grid ran out before the candidate list did. "
            )
            + "Add interviewers, extend the day, or shorten the interview — the "
            "alternative is people who travelled for this being sent home."
        )

    problems = verify_no_overlap(assignments)
    if problems:  # pragma: no cover - the allocator prevents this; the check stays
        raise AvailabilityError(
            "The allocator produced overlapping assignments, which should be "
            f"impossible: {problems}"
        )

    for interviewer_id, count in sorted(load.items()):
        if count >= by_id[interviewer_id].max_interviews:
            notes.append(
                f"{by_id[interviewer_id].label or interviewer_id} is at their limit of "
                f"{count} interviews for this drive."
            )

    return BulkPlan(
        assignments=tuple(assignments),
        unplaced_candidate_ids=tuple(unplaced),
        notes=tuple(notes),
    )


def verify_no_overlap(assignments: list[Assignment]) -> list[str]:
    """Re-check the finished plan. Empty means it is sound.

    The allocator already prevents this. The check stays because the invariant —
    one interviewer is in one place at a time — matters more than confidence in
    the code that maintains it, and a drive plan is acted on by dozens of people
    at once.
    """
    problems: list[str] = []
    by_interviewer: dict[str, list[Assignment]] = defaultdict(list)
    for assignment in assignments:
        for interviewer_id in assignment.interviewer_ids:
            by_interviewer[interviewer_id].append(assignment)

    for interviewer_id, rows in by_interviewer.items():
        ordered = sorted(rows, key=lambda a: a.start)
        for earlier, later in pairwise(ordered):
            if later.start < earlier.end:
                problems.append(
                    f"{interviewer_id} is double-booked: {earlier.candidate_id} at "
                    f"{earlier.start.isoformat()} overlaps {later.candidate_id} at "
                    f"{later.start.isoformat()}."
                )

    seen: dict[tuple[str, datetime], str] = {}
    for assignment in assignments:
        key = (assignment.candidate_id, assignment.start)
        if key in seen:
            problems.append(
                f"{assignment.candidate_id} is scheduled twice at {assignment.start.isoformat()}."
            )
        seen[key] = assignment.candidate_id
    return problems


def slots_from(slots: list[Slot]) -> list[tuple[datetime, datetime]]:
    """Adapt proposed slots into the allocator's shape."""
    return [(slot.start, slot.end) for slot in slots]
