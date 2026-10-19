"""Time off: the catalogue HR sets up, the year's leave so far, and what is pending now.

The story is set in October 2026. Leave taken in July to September is in the
attendance and payroll that follow (approved leave is a paid day; leave without
pay is loss of pay). Requests around today sit in managers' approval queues.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .api import Demo, SeedError

FLOW = "time off"

#: Added to seed_demo's catalogue (EL, CL, SL, ML).
LEAVE_TYPES = (
    # code, name, paid, half day, document, statutory, comp-off, description
    ("PTL", "Paternity Leave", True, False, False, False, False,
     "For the birth or adoption of a child."),
    ("CO", "Compensatory Off", True, True, False, False, True,
     "Earned by working a weekly off or holiday, with the manager's approval."),
    ("LWP", "Leave Without Pay", False, True, False, False, False,
     "Unpaid. Each day is loss of pay in that month's payroll."),
)

#: Holidays whose date moves every year, from the 2026 Karnataka and Maharashtra
#: lists. seed_demo adds the fixed-date ones.
HOLIDAYS_2026 = (
    ("2026-03-19", "Ugadi", "KA", False),
    ("2026-04-14", "Dr. B. R. Ambedkar Jayanti", None, False),
    ("2026-03-19", "Gudi Padwa", "MH", False),
    ("2026-04-03", "Good Friday", None, False),
    ("2026-05-01", "Maharashtra Day", "MH", False),
    ("2026-09-14", "Ganesh Chaturthi", None, False),
    ("2026-10-20", "Vijayadashami", None, False),
    ("2026-03-04", "Holi", None, True),
    ("2026-08-28", "Raksha Bandhan", None, True),
    ("2026-11-24", "Guru Nanak Jayanti", None, True),
)

#: Earned leave carried into 2026, from the previous system's closing balances.
OPENING_EL = {
    "ananya": 20, "priya": 10, "vikram": 12, "arjun": 8, "sneha": 4, "aditya": 5,
    "kavya": 6, "rahul": 9, "neha": 7, "suresh": 15, "manoj": 4, "anjali": 3,
    "ravi": 6, "karthik": 2, "meera": 3,
}

#: Leave already taken this year, decided by the person's manager.
#: (who, type, start, end, half-day session or None, reason, decision)
HISTORY = (
    ("vikram", "EL", "2026-04-06", "2026-04-08", None, "Family visit to Ahmedabad", "approve"),
    ("meera", "CL", "2026-04-17", "2026-04-17", None, "Personal work", "approve"),
    ("rahul", "SL", "2026-04-28", "2026-04-28", None, "Fever", "approve"),
    ("sneha", "EL", "2026-05-18", "2026-05-22", None, "Summer trip to Manali", "approve"),
    ("ananya", "EL", "2026-05-25", "2026-05-29", None, "Family vacation", "approve"),
    ("imran", "SL", "2026-06-02", "2026-06-02", None, "Food poisoning", "approve"),
    ("kavya", "CL", "2026-06-12", "2026-06-12", None, "Bank work", "approve"),
    ("suresh", "SL", "2026-06-15", "2026-06-16", None, "Back pain", "approve"),
    ("neha", "EL", "2026-06-22", "2026-06-26", None, "Brother's wedding in Indore", "approve"),
    ("lakshmi", "CL", "2026-06-29", "2026-06-29", None, "Personal", "reject"),
    ("aditya", "EL", "2026-07-06", "2026-07-07", None, "Family function in Jaipur", "approve"),
    ("priya", "CL", "2026-07-10", "2026-07-10", None, "Personal work", "approve"),
    ("neha", "CL", "2026-07-17", "2026-07-17", None, "Bank and passport errands", "approve"),
    ("arjun", "EL", "2026-07-20", "2026-07-24", None, "Vacation in Coorg", "approve"),
    ("lakshmi", "SL", "2026-07-22", "2026-07-22", None, "Fever", "approve"),
    ("suresh", "EL", "2026-07-27", "2026-07-31", None, "Annual family trip", "approve"),
    ("vikram", "EL", "2026-08-03", "2026-08-05", None, "Child's school admission", "approve"),
    ("nikhil", "CL", "2026-08-07", "2026-08-07", None, "Moving house", "approve"),
    ("meera", "SL", "2026-08-11", "2026-08-11", None, "Viral fever", "approve"),
    ("pooja", "EL", "2026-08-17", "2026-08-21", None, "Sister's wedding", "approve"),
    ("anjali", "EL", "2026-08-24", "2026-08-26", None, "Trip to Rishikesh", "approve"),
    ("ravi", "EL", "2026-08-26", "2026-08-28", None, "Before joining the new job", "approve"),
    ("rohan", "CL", "2026-08-28", "2026-08-28", None, "Personal", "reject"),
    ("karthik", "SL", "2026-09-02", "2026-09-02", None, "Dental procedure", "approve"),
    ("sneha", "CL", "2026-09-04", "2026-09-04", "first_half", "Doctor's appointment", "approve"),
    ("imran", "LWP", "2026-09-08", "2026-09-09", None, "Exam at college", "approve"),
    ("divya", "SL", "2026-09-17", "2026-09-17", None, "Migraine", "approve"),
    ("rahul", "EL", "2026-09-21", "2026-09-23", None, "Diwali shopping trip to Delhi", "approve"),
    ("manoj", "CL", "2026-09-25", "2026-09-25", None, "Vehicle registration", "approve"),
    ("kavya", "EL", "2026-09-28", "2026-10-01", None, "Long weekend in Goa", "approve"),
    ("rohan", "SL", "2026-10-06", "2026-10-06", None, "Stomach infection", "approve"),
    ("divya", "SL", "2026-10-07", "2026-10-07", None, "Unwell, resting today", "approve"),
)

#: Requests made around today. Pending ones wait in the manager's queue.
CURRENT = (
    ("pooja", "CL", "2026-10-09", "2026-10-09", None, "Housewarming at home", "pending"),
    ("nikhil", "EL", "2026-10-26", "2026-10-28", None, "Family trip to Pune", "pending"),
    ("meera", "CL", "2026-10-23", "2026-10-23", None, "Parent-teacher meeting", "pending"),
    ("sneha", "EL", "2026-10-26", "2026-10-30", None, "Trip to Kerala", "approve"),
    ("arjun", "EL", "2026-11-09", "2026-11-13", None, "Diwali at home in Ahmedabad", "pending"),
    ("karthik", "EL", "2026-12-21", "2026-12-24", None, "Year-end break", "pending"),
    ("aditya", "EL", "2026-11-02", "2026-11-03", None, "Wedding in the family", "cancel"),
)


async def setup(demo: Demo) -> None:
    """HR's catalogue, policies, calendar, shifts, rosters and geofences."""
    priya = demo["priya"]
    types = {t["code"]: t for t in await priya.get("/leave/types", include_inactive=True)}
    for code, name, paid, half, document, statutory, comp_off, description in LEAVE_TYPES:
        if code not in types:
            types[code] = await priya.post(
                "/leave/types",
                {"code": code, "name": name, "is_paid": paid, "allows_half_day": half,
                 "requires_document": document, "is_statutory": statutory,
                 "is_comp_off": comp_off, "description": description},
            )
    demo.ids["leave_type"] = {code: t["id"] for code, t in types.items()}

    policies = {p["leave_type_id"]: p for p in await priya.get("/leave/policies")}
    ml = policies.get(types["ML"]["id"])
    if ml and ml.get("applies_to_gender") != "female":
        await priya.patch(f"/leave/policies/{ml['id']}", {"applies_to_gender": "female"})
    for code, name, body in (
        ("PTL", "Paternity Leave - fathers",
         {"annual_quota_days": "5", "accrual_frequency": "on_joining",
          "applies_to_gender": "male", "max_consecutive_days": 5}),
        ("CO", "Compensatory Off - all employees",
         {"annual_quota_days": "0", "accrual_frequency": "annual"}),
        ("LWP", "Leave Without Pay - all employees",
         {"annual_quota_days": "0", "accrual_frequency": "annual",
          "allows_negative_balance": True}),
    ):
        if types[code]["id"] not in policies:
            await priya.post(
                "/leave/policies",
                {"leave_type_id": types[code]["id"], "name": name,
                 "effective_from": "2024-01-01", **body},
            )
    await priya.put("/leave/settings", {"optional_holiday_quota": 2, "comp_off_expiry_days": 60})

    listed = await priya.get("/leave/holidays", year=2026)
    existing = {(h["holiday_date"], h["name"]) for h in listed}
    added = 0
    for day, name, state, optional in HOLIDAYS_2026:
        if (day, name) not in existing:
            await priya.post(
                "/leave/holidays",
                {"name": name, "holiday_date": day, "state_code": state, "is_optional": optional},
            )
            added += 1
    demo.note(FLOW, f"3 more leave types and policies, {added} festival and optional holidays")

    # ── Shifts and rosters ────────────────────────────────────────────────
    shifts = {s["code"]: s for s in await priya.get("/attendance/shifts", include_inactive=True)}
    for code, name, start, end, offs in (
        ("EARLY", "Early shift", "08:00", "17:00", [6, 7]),
        ("SUP-M", "Support - morning", "07:00", "16:00", []),
        ("SUP-E", "Support - evening", "13:00", "22:00", []),
    ):
        if code not in shifts:
            shifts[code] = await priya.post(
                "/attendance/shifts",
                {"code": code, "name": name, "start_time": start, "end_time": end,
                 "break_minutes": 60, "grace_in_minutes": 10, "grace_out_minutes": 10,
                 "half_day_threshold_minutes": 240, "full_day_threshold_minutes": 420,
                 "weekly_off_days": offs},
            )
    demo.ids["shift"] = {code: s["id"] for code, s in shifts.items()}

    rosters = {r["name"]: r for r in await priya.get("/attendance/rosters")}
    if "Customer support rotation" not in rosters:
        # Two weeks: mornings Monday to Friday, then evenings Tuesday to Saturday.
        morning, evening = shifts["SUP-M"]["id"], shifts["SUP-E"]["id"]
        days = [morning] * 5 + [None, None] + [None] + [evening] * 5 + [None]
        rosters["Customer support rotation"] = await priya.post(
            "/attendance/rosters",
            {"name": "Customer support rotation",
             "description": "Fortnightly: a week of mornings, then a week of evenings.",
             "cycle_days": 14,
             "days": [{"day_index": i, "shift_id": s} for i, s in enumerate(days)]},
        )
    demo.ids["roster"] = rosters["Customer support rotation"]["id"]

    assignments = await priya.get("/attendance/assignments", employee_id=demo.emp("manoj"))
    early = shifts["EARLY"]["id"]
    if not any(a.get("shift_id") == early for a in _flat(assignments)):
        await priya.post(
            "/attendance/shift-assignments",
            {"employee_id": demo.emp("manoj"), "shift_id": shifts["EARLY"]["id"],
             "effective_from": "2026-04-01"},
        )
    assignments = await priya.get("/attendance/assignments", employee_id=demo.emp("lakshmi"))
    if not _flat(assignments):
        # 2026-03-23 is a Monday: the cycle's day 0.
        await priya.post(
            "/attendance/roster-assignments",
            {"employee_id": demo.emp("lakshmi"), "roster_id": demo.ids["roster"],
             "cycle_start_date": "2026-03-23", "effective_from": "2026-04-01"},
        )

    await priya.put("/attendance/settings", {"location_mode": "optional"})
    places = {w["name"] for w in await priya.get("/attendance/work-locations")}
    for name, code, lat, lon in (
        ("Bengaluru HQ - Prestige Tech Park", "BLR", "12.9417", "77.6966"),
        ("Mumbai office - BKC", "MUM", "19.0662", "72.8679"),
    ):
        if name not in places:
            await priya.post(
                "/attendance/work-locations",
                {"name": name, "location_id": demo.ids["location"][code],
                 "latitude": lat, "longitude": lon, "radius_metres": 300},
            )
    demo.note(FLOW, "early shift for Manoj, a support roster for Lakshmi, 2 office geofences")


def _flat(assignments) -> list:
    if isinstance(assignments, dict):
        return [a for value in assignments.values() if isinstance(value, list) for a in value]
    return list(assignments or [])


async def _decide(demo: Demo, request: dict, decision: str, applicant: str) -> dict:
    """The manager decides; an HR step, if the chain has one, follows."""
    person = demo.people[applicant]
    approver = person.manager or "priya"
    if decision == "cancel":
        return await demo[applicant].post(f"/leave/requests/{request['id']}/cancel")
    note = {"approve": "Approved. Enjoy the time off.",
            "reject": "We are short-staffed that day; please pick another date."}[decision]
    result = await demo[approver].post(
        f"/leave/requests/{request['id']}/{decision}", {"note": note}
    )
    if decision == "approve" and result["status"] == "pending":
        result = await demo["priya"].post(
            f"/leave/requests/{request['id']}/approve", {"note": "HR approval"}
        )
    expected = {"approve": "approved", "reject": "rejected"}[decision]
    if result["status"] != expected:
        raise SeedError(f"leave for {person.name} ended {result['status']}, not {expected}")
    return result


async def _apply(demo: Demo, rows) -> dict[str, int]:
    counts: dict[str, int] = {}
    for who, code, start, end, session, reason, decision in rows:
        body = {
            "leave_type_id": demo.ids["leave_type"][code],
            "start_date": start,
            "end_date": end,
            "is_half_day": session is not None,
            "half_day_session": session,
            "reason": reason,
        }
        preview = await demo[who].post("/leave/requests/preview", body)
        if preview.get("problems"):
            raise SeedError(f"{who} {code} {start}: {preview['problems']}")
        request = await demo[who].post("/leave/requests", body)
        if decision != "pending":
            await _decide(demo, request, decision, who)
        counts[decision] = counts.get(decision, 0) + 1
    return counts


async def history(demo: Demo) -> None:
    """Opening balances, accruals, and the leave taken from July to today."""
    priya = demo["priya"]
    for key, days in OPENING_EL.items():
        await demo.hr_for(key).post(
            "/leave/balances/adjustments",
            {"employee_id": demo.emp(key), "leave_type_id": demo.ids["leave_type"]["EL"],
             "year": 2026, "kind": "opening", "days": str(days),
             "reason": "Closing 2025 balance carried forward from the previous HRMS"},
        )
    await priya.post("/leave/accruals/run", params={"year": 2026})
    counts = await _apply(demo, HISTORY)
    demo.note(FLOW, f"opening balances for {len(OPENING_EL)}; past leave {counts}")


async def current(demo: Demo) -> None:
    """What is in flight today: pending requests, optional holidays, comp-offs."""
    counts = await _apply(demo, CURRENT)

    holidays = await demo["meera"].get("/leave/optional-holidays", year=2026)
    guru_nanak = next(h for h in holidays["holidays"] if h["name"] == "Guru Nanak Jayanti")
    for who in ("meera", "vikram", "anjali"):
        await demo[who].post(f"/leave/optional-holidays/{guru_nanak['id']}")

    # Weekend work, claimed as compensatory off.
    claim = await demo["aditya"].post(
        "/leave/comp-off",
        {"worked_date": "2026-09-19", "days": "1",
         "reason": "Production database migration over the weekend"},
    )
    await demo["vikram"].post(f"/leave/comp-off/{claim['id']}/approve",
                              {"note": "Thanks for handling the migration."})
    await demo["arjun"].post(
        "/leave/comp-off",
        {"worked_date": "2026-10-04", "days": "0.5",
         "reason": "Hotfix for the payments outage on Sunday morning"},
    )
    demo.note(FLOW, f"current leave {counts}; 3 optional holidays chosen; 2 comp-off claims")


def ist(day: str | date, at: str) -> str:
    """An ISO timestamp in India time, which is how a device reports it."""
    d = day if isinstance(day, date) else date.fromisoformat(day)
    hh, mm = (int(x) for x in at.split(":"))
    return datetime.combine(d, time(hh, mm)).isoformat() + "+05:30"


def daterange(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)
