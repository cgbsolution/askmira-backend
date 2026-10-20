"""Attendance from 1 April to today: biometric punches, corrections, monthly locks.

The office devices' exports are imported the way HR imports them. Each person
punches to their own shift or roster, around their holidays and approved leave,
with the ordinary mess of a real office: late arrivals, a forgotten punch-out, a
device that missed someone, a day nobody explained. Employees then ask for
corrections and their managers decide them, and HR locks each finished month so
payroll can run on it.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .api import Demo, SeedError
from .time_off import HISTORY, current_rows, daterange, ist

FLOW = "attendance"
IST = ZoneInfo("Asia/Kolkata")
#: The financial year's first day: the whole year's payroll is in AskMira.
START = date(2026, 4, 1)
LOCKED_MONTHS = tuple((2026, month) for month in range(4, 10))

#: Holidays in the period: None for everyone, or the state whose office is closed.
HOLIDAYS: dict[date, str | None] = {
    date(2026, 4, 3): None, date(2026, 4, 14): None, date(2026, 5, 1): None,
    date(2026, 8, 15): None, date(2026, 9, 14): None, date(2026, 10, 2): None,
}

#: Days that are not an ordinary day at the office, by person.
#: "none" = no punch at all; "in" / "out" = only that punch reached the device.
SPECIAL: dict[tuple[str, date], str | tuple[str, str]] = {
    ("nikhil", date(2026, 4, 21)): "in",          # left early for a doctor, forgot to punch
    ("sneha", date(2026, 5, 12)): "none",         # client workshop at their office
    ("divya", date(2026, 6, 9)): "out",           # came in through the back gate
    ("imran", date(2026, 7, 14)): "none",        # absent, never explained
    ("rohan", date(2026, 7, 15)): "out",          # forgot to punch in
    ("pooja", date(2026, 8, 12)): "in",           # left for a client meeting
    ("lakshmi", date(2026, 9, 3)): "none",        # claims the device missed her
    ("rahul", date(2026, 9, 10)): "none",         # on duty at a client in Thane
    ("meera", date(2026, 9, 15)): "in",           # forgot to punch out
    ("aditya", date(2026, 9, 21)): "none",        # working from home
    ("aditya", date(2026, 9, 22)): "none",
    ("aditya", date(2026, 9, 19)): ("10:02", "17:41"),   # Saturday migration
    ("arjun", date(2026, 10, 4)): ("08:31", "12:47"),    # Sunday hotfix
    ("arjun", date(2026, 10, 1)): "out",
    ("sneha", date(2026, 10, 5)): "in",
    ("nikhil", date(2026, 10, 5)): "in",
    ("pooja", date(2026, 10, 6)): "none",         # worked from home, asks later
}

#: Corrections employees ask for, and what their manager decides.
REGULARISATIONS = (
    ("nikhil", "2026-04-21", "missed_punch", {"requested_last_out": ("2026-04-21", "18:35")},
     "Forgot to punch out before leaving", "approve"),
    ("sneha", "2026-05-12", "on_duty", {},
     "Discovery workshop at Infosys, Electronic City", "approve"),
    ("divya", "2026-06-09", "missed_punch", {"requested_first_in": ("2026-06-09", "09:28")},
     "Came in through the back gate; no reader there", "approve"),
    ("rohan", "2026-07-15", "missed_punch", {"requested_first_in": ("2026-07-15", "09:34")},
     "Card reader at the gate was not working in the morning", "approve"),
    ("pooja", "2026-08-12", "missed_punch", {"requested_last_out": ("2026-08-12", "18:40")},
     "Went straight to the client office in Andheri after lunch", "approve"),
    ("meera", "2026-09-15", "missed_punch", {"requested_last_out": ("2026-09-15", "18:52")},
     "Forgot to punch out after the release call", "approve"),
    ("lakshmi", "2026-09-03", "status_change", {},
     "I was at my desk; the device did not register me", "reject"),
    ("rahul", "2026-09-10", "on_duty", {},
     "Full day at Tata Capital, Thane, for the renewal negotiation", "approve"),
)
CURRENT_REGULARISATIONS = (
    ("arjun", "2026-10-01", "missed_punch", {"requested_first_in": ("2026-10-01", "09:22")},
     "Punched in on the side door reader, which seems to be offline"),
    ("sneha", "2026-10-05", "missed_punch", {"requested_last_out": ("2026-10-05", "18:47")},
     "Forgot to punch out"),
)


def _leave_days() -> dict[tuple[str, date], str]:
    """Approved leave from the time-off flow: full days, or the half taken."""
    out: dict[tuple[str, date], str] = {}
    for who, _code, start, end, session, _reason, decision in HISTORY + current_rows():
        if decision != "approve":
            continue
        for day in daterange(date.fromisoformat(start), date.fromisoformat(end)):
            out[(who, day)] = session or "full"
    return out


def _roster_shift(day: date) -> str | None:
    """The support rotation: day 0 is Monday 23 March 2026."""
    index = (day - date(2026, 3, 23)).days % 14
    if index <= 4:
        return "morning"
    if 8 <= index <= 12:
        return "evening"
    return None


def _times(rng: random.Random, key: str, day: date, location: str) -> tuple[str, str] | None:
    """The in and out time for an ordinary day, or None for a day off."""
    if key == "lakshmi":
        slot = _roster_shift(day)
        if slot is None:
            return None
        if slot == "morning":
            start = datetime(2000, 1, 1, 6, 50) + timedelta(minutes=rng.randint(0, 18))
            end = datetime(2000, 1, 1, 16, 0) + timedelta(minutes=rng.randint(0, 30))
        else:
            start = datetime(2000, 1, 1, 12, 50) + timedelta(minutes=rng.randint(0, 18))
            end = datetime(2000, 1, 1, 22, 0) + timedelta(minutes=rng.randint(0, 20))
        return f"{start:%H:%M}", f"{end:%H:%M}"
    if day.isoweekday() in (6, 7):
        return None
    if key == "manoj":
        start = datetime(2000, 1, 1, 7, 45) + timedelta(minutes=rng.randint(0, 27))
        end = datetime(2000, 1, 1, 17, 0) + timedelta(minutes=rng.randint(0, 40))
        return f"{start:%H:%M}", f"{end:%H:%M}"
    late = rng.random() < 0.09
    if late:   # past the 15-minute grace
        start = datetime(2000, 1, 1, 9, 50) + timedelta(minutes=rng.randint(0, 40))
    else:
        start = datetime(2000, 1, 1, 9, 8) + timedelta(minutes=rng.randint(0, 36))
    if not late and rng.random() < 0.05:
        # Left early, but still past the seven hours a full day needs.
        end = datetime(2000, 1, 1, 17, 52) + timedelta(minutes=rng.randint(0, 18))
    else:
        end = start + timedelta(hours=9, minutes=rng.randint(4, 55))
    return f"{start:%H:%M}", f"{end:%H:%M}"


def _rows(demo: Demo, today: date) -> list[dict]:
    leave = _leave_days()
    rows: list[dict] = []
    yesterday = today - timedelta(days=1)
    for key, person in demo.people.items():
        if not person.code:
            continue
        joined = date.fromisoformat(person.date_of_joining)
        last = date(2026, 8, 31) if key == "ravi" else yesterday
        device = "MUM-ESSL-01" if person.location == "MUM" else "BLR-ESSL-01"
        for day in daterange(max(START, joined), last):
            special = SPECIAL.get((key, day))
            if isinstance(special, tuple):
                times: tuple[str, str] | None = special
            else:
                if day in HOLIDAYS or leave.get((key, day)) == "full":
                    continue
                rng = random.Random(f"{person.code}{day}")
                times = _times(rng, key, day, person.location)
                if times is None:
                    continue
                if leave.get((key, day)) == "first_half":
                    times = ("13:41", times[1])
                elif leave.get((key, day)) == "second_half":
                    times = (times[0], "13:20")
            if special == "none":
                continue
            if special != "out":
                rows.append({"employee_code": person.code, "punched_at": ist(day, times[0]),
                             "direction": "in", "device_id": device})
            if special != "in":
                rows.append({"employee_code": person.code, "punched_at": ist(day, times[1]),
                             "direction": "out", "device_id": device})

    # Today, if the office has opened: everyone in so far, bar Meera and Vikram,
    # who are left to punch in themselves through the portal.
    now = datetime.now(IST)
    if now.hour >= 10 and today.isoweekday() < 6:
        for key, person in demo.people.items():
            if not person.code or key in ("meera", "vikram", "ravi") or leave.get((key, today)):
                continue
            rng = random.Random(f"{person.code}{today}")
            times = _times(rng, key, today, person.location)
            if times and times[0] < f"{now:%H:%M}":
                rows.append({"employee_code": person.code, "punched_at": ist(today, times[0]),
                             "direction": "in",
                             "device_id": "MUM-ESSL-01" if person.location == "MUM"
                             else "BLR-ESSL-01"})
    return rows


def _at(pair: tuple[str, str]) -> str:
    return ist(pair[0], pair[1])


async def _raise(demo: Demo, who: str, day: str, kind: str, times: dict, reason: str) -> dict:
    body = {"attendance_date": day, "kind": kind, "reason": reason,
            "requested_status": "present"}
    body.update({k: _at(v) for k, v in times.items()})
    return await demo[who].post("/attendance/regularisation", body)


async def run(demo: Demo) -> None:
    priya = demo["priya"]
    today = datetime.now(IST).date()
    rows = _rows(demo, today)
    imported = duplicates = 0
    for chunk in range(0, len(rows), 4000):
        result = await priya.post("/attendance/import", {"rows": rows[chunk:chunk + 4000]})
        if result["errors"] or result["skipped_unknown_employee"]:
            raise SeedError(f"punch import problems: {result['errors'][:3]}")
        imported += result["imported"]
        duplicates += result["duplicates"]
    # A month at a time: the endpoint takes at most 93 days.
    month_start = START
    while month_start <= today:
        next_month = date(month_start.year + month_start.month // 12, month_start.month % 12 + 1, 1)
        await priya.post("/attendance/recompute", {
            "start": str(month_start), "end": str(min(today, next_month - timedelta(days=1)))})
        month_start = next_month
    demo.note(FLOW, f"{imported} biometric punches imported ({duplicates} duplicates) and computed")

    # Corrections for July to September, decided before the months are locked.
    decided = {"approve": 0, "reject": 0}
    for who, day, kind, times, reason, decision in REGULARISATIONS:
        request = await _raise(demo, who, day, kind, times, reason)
        approver = demo.people[who].manager
        await demo[approver].patch(
            f"/attendance/regularisation/{request['id']}",
            {"approve": decision == "approve",
             "note": "Confirmed with the team." if decision == "approve"
             else "The floor camera and your laptop show no activity that day; marked absent."},
        )
        decided[decision] += 1
    wfh = await demo["aditya"].post(
        "/attendance/regularisations/range",
        {"kind": "work_from_home", "start_date": "2026-09-21", "end_date": "2026-09-22",
         "reason": "Plumbing work at home; on calls all day"},
    )
    for request in wfh if isinstance(wfh, list) else wfh.get("items", []):
        await demo["vikram"].patch(f"/attendance/regularisation/{request['id']}",
                                   {"approve": True, "note": "Fine."})
        decided["approve"] += 1
    demo.note(FLOW, f"regularisations decided: {decided}")

    # Whatever is still unexplained at month end is what HR sees before locking.
    for year, month in LOCKED_MONTHS:
        start = date(year, month, 1)
        end = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
        exceptions = await priya.get("/attendance/exceptions",
                                     **{"from": str(start), "to": str(end)})
        await priya.post("/attendance/lock", {"period_start": str(date(year, month, 1)),
                                              "period_end": str(end)})
        demo.note(FLOW, f"{year}-{month:02d} locked after reviewing "
                        f"{len(exceptions['exceptions'])} exception(s)")

    # October so far: corrections waiting in managers' queues.
    for who, day, kind, times, reason in CURRENT_REGULARISATIONS:
        await _raise(demo, who, day, kind, times, reason)
    await demo["pooja"].post(
        "/attendance/regularisations/range",
        {"kind": "work_from_home", "start_date": "2026-10-06", "end_date": "2026-10-06",
         "reason": "Internet installation at the new flat; worked from home"},
    )
    await demo["fatima"].post(
        "/attendance/regularisations/range",
        {"kind": "work_from_home", "start_date": "2026-10-12", "end_date": "2026-10-13",
         "reason": "Laptop setup at home for the on-call rotation training"},
    )
    demo.note(FLOW, "October: 4 corrections pending with managers, "
                    "Nikhil's missed punch unexplained")
