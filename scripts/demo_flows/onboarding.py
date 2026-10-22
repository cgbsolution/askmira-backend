"""Onboarding: the checklist templates, Fatima's first weeks, Sana before day one.

Fatima joined on 21 September and most of her checklist is done. Sana accepted
her offer last week and joins on 19 October: she gets a login now, so she can
upload her documents and work through the pre-joining tasks herself.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from seed_demo import generate_password

from .api import Api, Demo, Person, SeedError
from .attendance import HOLIDAYS, _times
from .cast import aadhaar_for, account_for, bank_for, pan_for, uan_for
from .payroll import pdf
from .people import _address
from .time_off import HOLIDAYS_2026, daterange, ist

FLOW = "onboarding"

ENGINEERING_TASKS = (
    ("Set up the development laptop", "it", 0, "general"),
    ("Grant GitHub, AWS and Sentry access", "it", 0, "general"),
    ("Walk through the architecture with your buddy", "buddy", 2, "meeting"),
    ("Ship a first small pull request", "new_joiner", 10, "general"),
    ("Read and accept the information security policy", "new_joiner", 1,
     "acknowledge_policy"),
    ("30-day check-in with your manager", "manager", 30, "meeting"),
)


async def setup(demo: Demo) -> None:
    priya = demo["priya"]
    templates = {t["name"]: t for t in await priya.get("/onboarding/templates")}
    if not any(t.get("is_default") for t in templates.values()):
        standard = await priya.post("/onboarding/templates/standard")
        templates[standard["name"]] = standard
    if "Engineering onboarding" not in templates:
        templates["Engineering onboarding"] = await priya.post(
            "/onboarding/templates",
            {"name": "Engineering onboarding",
             "description": "The standard checklist plus engineering access and a first PR.",
             "department_id": demo.ids["department"]["ENG"],
             "tasks": [{"title": title, "owner_type": owner, "due_offset_days": days,
                        "task_type": kind,
                        "owner_user_id": str(demo.people["manoj"].user_id)
                        if owner == "it" else None}
                       for title, owner, days, kind in ENGINEERING_TASKS]},
        )
    demo.ids["onboarding_template"] = {name: t["id"] for name, t in templates.items()}
    demo.note(FLOW, f"{len(templates)} checklist templates (standard India, engineering)")


def _by_user(demo: Demo) -> dict[str, str]:
    return {str(p.user_id): k for k, p in demo.people.items() if p.user_id}


async def _work(demo: Demo, plan_id: str, *, leave_open: int, as_hr: Api) -> dict:
    """Complete tasks in due order, as whoever owns each, leaving the last few open."""
    plan = await as_hr.get(f"/onboarding/plans/{plan_id}")
    tasks = sorted(plan["tasks"], key=lambda t: (t["due_date"], t["sort_order"]))
    people = _by_user(demo)
    for task in tasks[: max(0, len(tasks) - leave_open)]:
        owner = people.get(task["assignee_user_id"] or "")
        actor = demo[owner] if owner else as_hr
        if task["task_type"] == "document_upload":
            await actor.post(f"/onboarding/tasks/{task['id']}/document",
                             files={"file": pdf(f"{task['title']}.pdf")})
            continue
        await actor.post(f"/onboarding/tasks/{task['id']}/complete",
                         {"note": "Done"})
    return await as_hr.get(f"/onboarding/plans/{plan_id}")


async def run(demo: Demo) -> None:
    priya, karthik = demo["priya"], demo["karthik"]
    templates = demo.ids["onboarding_template"]
    engineering = templates["Engineering onboarding"]

    plan = await priya.post(
        "/onboarding/plans",
        {"employee_id": demo.emp("fatima"), "template_id": engineering,
         "buddy_employee_id": demo.emp("arjun"), "joining_date": "2026-09-21"},
    )
    plan = await _work(demo, plan["id"], leave_open=1, as_hr=karthik)
    progress = plan["progress"]
    demo.note(FLOW, f"Fatima: engineering checklist with Arjun as buddy, "
                    f"{progress['done']} of {progress['total']} tasks done")

    # Sana: the hire from the hiring flow, joining 19 October.
    sana_id = demo.ids.get("sana_employee")
    if not sana_id:
        employees = (await priya.get("/employees", search="Sana", page_size=5))["items"]
        sana_id = employees[0]["id"]
    sana = Person(key="sana", code="DEMO023", first_name="Sana", last_name="Iqbal",
                  email="sana@askmira-demo.com", department="ENG",
                  designation="Software Engineer", grade="P3", manager="vikram",
                  date_of_joining="2026-10-19", employee_id=sana_id)
    users = {u["email"]: u for u in await demo["ananya"].get("/admin/users")}
    if sana.email in users:
        sana.user_id = users[sana.email]["id"]
    else:
        created = await demo["ananya"].post(
            "/admin/users",
            {"email": sana.email, "full_name": sana.name, "role": "employee",
             "employee_id": sana_id},
        )
        sana.user_id = created["user"]["id"]
        sana.password = generate_password()
        first = Api(demo.http, sana.user_id, demo.tenant_id, "employee", sana.name)
        await first.post("/auth/change-password",
                         {"current_password": created["temporary_password"],
                          "new_password": sana.password})
    sana.api = Api(demo.http, sana.user_id, demo.tenant_id, "employee", sana.name)
    demo.people["sana"] = sana

    standard = next(tid for name, tid in templates.items() if name != "Engineering onboarding")
    plan = await priya.post(
        "/onboarding/plans",
        {"employee_id": sana_id, "template_id": standard,
         "buddy_employee_id": demo.emp("meera"), "joining_date": "2026-10-19"},
    )
    # Before day one only the pre-joining tasks can be done.
    detail = await karthik.get(f"/onboarding/plans/{plan['id']}")
    pre = [t for t in detail["tasks"] if t["phase"] == "pre_joining"]
    people = _by_user(demo)
    done = 0
    for task in pre[:-1]:
        owner = people.get(task["assignee_user_id"] or "")
        actor = demo[owner] if owner else karthik
        if task["task_type"] == "document_upload":
            await actor.post(f"/onboarding/tasks/{task['id']}/document",
                             files={"file": pdf(f"{task['title']}.pdf")})
        else:
            await actor.post(f"/onboarding/tasks/{task['id']}/complete",
                             {"note": "Done before joining"})
        done += 1
    mine = await sana.api.get("/onboarding/me")
    if not mine:
        raise SeedError("Sana cannot see her own onboarding")

    # Her kit is set aside before she arrives.
    spare = await karthik.get("/assets", category_id=demo.ids["asset_category"]["LAP"],
                              status="in_stock")
    if spare["items"]:
        await karthik.post(f"/assets/{spare['items'][0]['id']}/assign",
                           {"employee_id": sana_id, "condition_out": "new",
                            "notes": "Kept ready for day one"})
    demo.note(FLOW, f"Sana: login created, standard checklist with Meera as buddy, "
                    f"{done} of {len(pre)} pre-joining tasks done, laptop set aside")
    await _joining_day(demo, sana)


async def _joining_day(demo: Demo, sana: Person) -> None:
    """What joining day brings: HR records the joining documents, the device her punches.

    Without them October's payroll would show her days as loss of pay, the bank
    file would have no account to credit and the PF return no UAN to file under.
    """
    karthik = demo["karthik"]
    employee_id = str(sana.employee_id)
    bank_name, ifsc = bank_for(sana)
    for change in (
        {"section": "statutory",
         "data": {"pan": pan_for(sana), "aadhaar": aadhaar_for(sana), "uan": uan_for(sana)}},
        {"section": "bank_account",
         "data": {"account_number": account_for(sana), "ifsc": ifsc,
                  "account_holder_name": sana.name, "bank_name": bank_name}},
        {"section": "addresses",
         "data": {"current": _address(sana), "permanent_same_as_current": True}},
    ):
        await karthik.put(f"/employees/{employee_id}/profile", {"change": change})

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    joined = date.fromisoformat(sana.date_of_joining or "2026-10-19")
    # Office holidays for everyone; optional ones are a choice, not a closure.
    closed = set(HOLIDAYS) | {date.fromisoformat(day) for day, _name, state, optional
                              in HOLIDAYS_2026 if not optional and state in (None, "KA")}
    rows = []
    for day in daterange(joined, today - timedelta(days=1)):
        if day in closed:
            continue
        times = _times(random.Random(f"{sana.code}{day}"), "sana", day, sana.location)
        if times is None:
            continue
        for direction, at in (("in", times[0]), ("out", times[1])):
            rows.append({"employee_code": sana.code, "punched_at": ist(day, at),
                         "direction": direction, "device_id": "BLR-ESSL-01"})
    if rows:
        await demo["priya"].post("/attendance/import", {"rows": rows})
        await demo["priya"].post("/attendance/recompute",
                                 {"start": str(joined), "end": str(today - timedelta(days=1))})
    demo.note(FLOW, f"Sana's joining day: PAN, UAN, salary account and address recorded; "
                    f"{len(rows)} punches since {joined:%d %b}")
