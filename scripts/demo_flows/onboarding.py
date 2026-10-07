"""Onboarding: the checklist templates, Fatima's first weeks, Sana before day one.

Fatima joined on 21 September and most of her checklist is done. Sana accepted
her offer last week and joins on 19 October: she gets a login now, so she can
upload her documents and work through the pre-joining tasks herself.
"""

from __future__ import annotations

from seed_demo import generate_password

from .api import Api, Demo, Person, SeedError
from .payroll import pdf

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
