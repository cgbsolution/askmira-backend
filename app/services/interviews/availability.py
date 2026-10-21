"""Free/busy intersection and slot proposal.

═══════════════════════════════════════════════════════════════════════════════
EVERYTHING HERE IS UTC. TIMEZONES EXIST ONLY AT THE EDGES.
═══════════════════════════════════════════════════════════════════════════════

Scheduling bugs are almost never arithmetic. They are one participant's clock
leaking into the comparison — an interviewer's calendar in `Asia/Kolkata`
compared against a candidate's preference in `America/New_York`, with one of
them naive. The result is an interview at 3am that everybody accepted because
the invitation looked right in their own inbox.

So: every function in this module takes and returns aware UTC datetimes, and
refuses a naive one rather than assuming. Conversion to a person's own zone
happens once, in `render_for`, at the point of display.

**A proposed slot must be free for every panelist, not most of them.** The
intersection is the point. An "optimistic" proposal that assumes one silent
calendar is free produces exactly the failure this module exists to prevent —
a booked interview that one interviewer cannot attend, discovered on the day.

Pure. No database, no calendar API — the caller supplies busy intervals, which
is what lets the DST cases be tested against a table instead of against a
provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "AvailabilityError",
    "BusyInterval",
    "PanelistBusy",
    "Slot",
    "WorkingHours",
    "free_slots",
    "merge_intervals",
    "propose_slots",
    "render_for",
    "to_utc",
]


class AvailabilityError(ValueError):
    """An input this module refuses to guess about."""


def _require_utc(value: datetime, label: str) -> datetime:
    """Refuse a naive datetime rather than assuming what it meant.

    Assuming UTC is wrong for a calendar read; assuming local is wrong for
    stored data. Both produce an interview at the wrong hour that nobody
    questions because it renders plausibly.
    """
    if value.tzinfo is None:
        raise AvailabilityError(
            f"{label} has no timezone. A naive datetime here would be guessed at, and "
            "the guess is how an interview ends up at 3am for one side."
        )
    return value.astimezone(UTC)


def to_utc(moment: datetime, timezone_name: str) -> datetime:
    """Interpret a wall-clock time in somebody's zone as an instant.

    The one place a naive datetime is legitimate: a candidate picking "Tuesday
    at 3pm" means 3pm where they are, and that has to be resolved against their
    zone before it is comparable with anything.
    """
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise AvailabilityError(
            f"'{timezone_name}' is not a timezone this system knows. Nothing was "
            "scheduled — a fallback here would silently book the wrong hour."
        ) from exc
    if moment.tzinfo is not None:
        return moment.astimezone(UTC)
    return moment.replace(tzinfo=zone).astimezone(UTC)


def render_for(moment: datetime, timezone_name: str) -> datetime:
    """The one place a UTC instant becomes somebody's wall clock."""
    return _require_utc(moment, "moment").astimezone(ZoneInfo(timezone_name))


@dataclass(frozen=True, slots=True)
class BusyInterval:
    """A block a calendar reports as busy. Half-open: `[start, end)`.

    Half-open is what makes a 10:00-11:00 meeting and an 11:00-12:00 interview
    not overlap. Closed intervals make every back-to-back pair a conflict, and a
    scheduler that refuses back-to-back interviews is useless on a panel day.
    """

    start: datetime
    end: datetime
    source: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _require_utc(self.start, "busy start"))
        object.__setattr__(self, "end", _require_utc(self.end, "busy end"))
        if self.end <= self.start:
            raise AvailabilityError(
                f"A busy interval ends at or before it starts ({self.start} to "
                f"{self.end}). A zero-length block would silently match nothing."
            )

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and start < self.end


@dataclass(frozen=True, slots=True)
class PanelistBusy:
    """One panelist's calendar, and whether we actually managed to read it.

    `read_failed` is the important field. A calendar we could not read is not an
    empty calendar, and treating it as one is how an interview gets booked over
    somebody's existing meeting.
    """

    panelist_id: str
    intervals: tuple[BusyInterval, ...] = ()
    read_failed: bool = False
    failure_reason: str = ""
    label: str = ""


@dataclass(frozen=True, slots=True)
class WorkingHours:
    """When a person will take an interview, in their own zone.

    Held as wall-clock times plus a zone rather than as UTC, because "9 to 6"
    means 9 to 6 wherever they are, including after a DST change.
    """

    timezone_name: str = "Asia/Kolkata"
    start: time = time(10, 0)
    end: time = time(18, 0)
    #: Monday is 0. Weekends off by default; Indian offices vary and this is
    #: configuration, not a rule.
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)

    def windows(self, *, from_day: date, days: int) -> list[tuple[datetime, datetime]]:
        """Working windows as UTC instants, one per eligible day.

        Built day by day in the local zone and converted individually. Adding 24
        hours repeatedly instead would drift by an hour across a DST boundary
        and put the last day's window in the wrong place.
        """
        if self.end <= self.start:
            raise AvailabilityError(
                f"Working hours end at or before they start ({self.start} to {self.end}). "
                "Nobody can be scheduled inside a window of zero length."
            )
        zone = ZoneInfo(self.timezone_name)
        out: list[tuple[datetime, datetime]] = []
        for offset in range(days):
            day = from_day + timedelta(days=offset)
            if day.weekday() not in self.weekdays:
                continue
            out.append(
                (
                    datetime.combine(day, self.start, tzinfo=zone).astimezone(UTC),
                    datetime.combine(day, self.end, tzinfo=zone).astimezone(UTC),
                )
            )
        return out


@dataclass(frozen=True, slots=True)
class Slot:
    start: datetime
    end: datetime
    #: Panelists confirmed free for this slot.
    available_panelist_ids: tuple[str, ...] = ()
    #: Panelists whose calendar could not be read. Named rather than counted, so
    #: a recruiter offering the slot knows who they are gambling on.
    unverified_panelist_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def is_fully_verified(self) -> bool:
        return not self.unverified_panelist_ids

    def as_dict(self, timezone_name: str = "Asia/Kolkata") -> dict:
        local_start = render_for(self.start, timezone_name)
        return {
            "start_utc": self.start.isoformat(),
            "end_utc": self.end.isoformat(),
            "start_local": local_start.isoformat(),
            "local_label": local_start.strftime("%a %d %b, %I:%M %p"),
            "timezone": timezone_name,
            "duration_minutes": self.duration_minutes,
            "available_panelist_ids": list(self.available_panelist_ids),
            "unverified_panelist_ids": list(self.unverified_panelist_ids),
            "fully_verified": self.is_fully_verified,
        }


def merge_intervals(intervals: list[BusyInterval]) -> list[BusyInterval]:
    """Collapse overlapping and touching busy blocks.

    Touching blocks are merged too: 10-11 and 11-12 leave no gap at 11, and
    reporting one would offer a slot that does not exist.
    """
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda i: (i.start, i.end))
    merged = [ordered[0]]
    for interval in ordered[1:]:
        last = merged[-1]
        if interval.start <= last.end:
            if interval.end > last.end:
                merged[-1] = BusyInterval(start=last.start, end=interval.end)
        else:
            merged.append(interval)
    return merged


def free_slots(
    *,
    window_start: datetime,
    window_end: datetime,
    busy: list[BusyInterval],
    duration_minutes: int,
    step_minutes: int = 30,
    buffer_minutes: int = 0,
) -> list[tuple[datetime, datetime]]:
    """Candidate start times inside one window that clear every busy block.

    `buffer_minutes` pads each busy block on both sides. Back-to-back interviews
    are normal on a panel day; back-to-back with no gap at all means the first
    overrunning by two minutes makes the second late, every time.
    """
    if duration_minutes <= 0:
        raise AvailabilityError("An interview needs a duration greater than zero.")
    if step_minutes <= 0:
        raise AvailabilityError("The slot step must be greater than zero.")

    start = _require_utc(window_start, "window start")
    end = _require_utc(window_end, "window end")
    duration = timedelta(minutes=duration_minutes)
    step = timedelta(minutes=step_minutes)
    padding = timedelta(minutes=buffer_minutes)

    padded = [BusyInterval(start=b.start - padding, end=b.end + padding) for b in busy]
    blocks = merge_intervals(padded)

    out: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor + duration <= end:
        slot_end = cursor + duration
        if not any(block.overlaps(cursor, slot_end) for block in blocks):
            out.append((cursor, slot_end))
        cursor += step
    return out


def propose_slots(
    *,
    panelists: list[PanelistBusy],
    working_hours: WorkingHours,
    from_day: date,
    days: int,
    duration_minutes: int,
    step_minutes: int = 30,
    buffer_minutes: int = 10,
    limit: int = 20,
    require_all: bool = True,
) -> tuple[list[Slot], list[str]]:
    """Slots that work for the whole panel, and what to say about the gaps.

    Returns `(slots, caveats)`. The caveats are not decoration: a panel where one
    calendar could not be read produces slots that *look* confirmed, and the
    recruiter has to know which ones are a guess before they send them to a
    candidate.

    `require_all` defaults to True. A slot that suits three of four interviewers
    is not a slot — it is a meeting somebody will miss.
    """
    if not panelists:
        raise AvailabilityError(
            "No panelists. Proposing times for an interview with nobody in it would "
            "produce a calendar full of slots that mean nothing."
        )

    caveats: list[str] = []
    unreadable = [p for p in panelists if p.read_failed]
    for panelist in unreadable:
        caveats.append(
            f"{panelist.label or panelist.panelist_id}'s calendar could not be read "
            f"({panelist.failure_reason or 'no reason given'}). Their slots below are "
            "unverified — a calendar we cannot read is not an empty calendar."
        )

    windows = working_hours.windows(from_day=from_day, days=days)
    if not windows:
        raise AvailabilityError(
            f"No working days between {from_day} and {from_day + timedelta(days=days)}. "
            "Widen the range or the working-day configuration."
        )

    slots: list[Slot] = []
    for window_start, window_end in windows:
        per_panelist: dict[str, set[datetime]] = {}
        for panelist in panelists:
            starts = {
                start
                for start, _ in free_slots(
                    window_start=window_start,
                    window_end=window_end,
                    busy=list(panelist.intervals),
                    duration_minutes=duration_minutes,
                    step_minutes=step_minutes,
                    buffer_minutes=buffer_minutes,
                )
            }
            per_panelist[panelist.panelist_id] = starts

        # A calendar we could not read contributes no evidence either way. It
        # does not veto the slot and it does not confirm it — it is named.
        verified_ids = [p.panelist_id for p in panelists if not p.read_failed]
        unverified_ids = tuple(p.panelist_id for p in unreadable)

        if require_all and verified_ids:
            common = set.intersection(*(per_panelist[pid] for pid in verified_ids))
        elif verified_ids:
            common = set().union(*(per_panelist[pid] for pid in verified_ids))
        else:
            common = set()

        for start in sorted(common):
            slots.append(
                Slot(
                    start=start,
                    end=start + timedelta(minutes=duration_minutes),
                    available_panelist_ids=tuple(
                        pid for pid in verified_ids if start in per_panelist[pid]
                    ),
                    unverified_panelist_ids=unverified_ids,
                )
            )

    slots.sort(key=lambda s: s.start)
    if not slots:
        caveats.append(
            f"No time in the next {days} day(s) is free for the whole panel. Either "
            "widen the window, shorten the interview, or drop a panelist — there is no "
            "slot to offer as things stand."
        )
    return slots[:limit], caveats
