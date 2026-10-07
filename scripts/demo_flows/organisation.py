"""Organisation: cost centres, positions and budgets, lifecycle events, arrears.

Every employee sits in a budgeted position, with vacancies for the open
requisitions. Divya's promotion is approved in October but takes effect from 1
August, so August and September were paid at her old salary: the revision
becomes an arrears batch, approved and waiting for October's payroll.
"""

from __future__ import annotations

from .api import Demo, SeedError

FLOW = "organisation"

COST_CENTRES = (("CC-ENG", "Engineering and product", "vikram"),
                ("CC-SAL", "Sales", "rahul"),
                ("CC-GA", "General and administration", "suresh"))
DEPARTMENT_CC = {"ENG": "CC-ENG", "PRD": "CC-ENG", "SAL": "CC-SAL", "FIN": "CC-GA",
                 "OPS": "CC-GA", "HR": "CC-GA", "LDR": "CC-GA"}

#: Vacant positions, for the open requisitions and the role on hold.
VACANCIES = (
    ("ENG-BE-VAC1", "Backend Engineer, Payments", "ENG", "P3", "vikram", "1400000", "1900000"),
    ("ENG-EM-PLAT", "Engineering Manager, Platform", "ENG", "M2", "vikram", "3500000",
     "4500000"),
    ("OPS-CS-VAC1", "Customer Support Executive", "OPS", "P1", "manoj", "240000", "320000"),
)


async def run(demo: Demo) -> None:
    priya = demo["priya"]
    centres = {c["code"]: c for c in await priya.get("/positions/cost-centres")}
    for code, name, owner in COST_CENTRES:
        if code not in centres:
            centres[code] = await priya.post("/positions/cost-centres",
                                             {"code": code, "name": name,
                                              "owner_employee_id": demo.emp(owner)})

    # A position per person, top down so each reports to its manager's position.
    positions: dict[str, str] = {}
    order = [k for k, p in demo.people.items() if p.code and k not in ("ravi", "sana")]
    order.sort(key=lambda k: _depth(demo, k))
    for key in order:
        person = demo.people[key]
        ctc = person.ctc or 0
        created = await priya.post(
            "/positions",
            {"code": f"{person.department}-{person.code[-3:]}",
             "title": person.designation,
             "department_id": demo.ids["department"][person.department],
             "location_id": demo.ids["location"][person.location],
             "cost_centre_id": centres[DEPARTMENT_CC[person.department]]["id"],
             "grade": person.grade,
             "reports_to_position_id": positions.get(person.manager) if person.manager else None,
             "budgeted_ctc_min": str(int(ctc * 0.9)), "budgeted_ctc_max": str(int(ctc * 1.2)),
             "effective_from": "2026-04-01"},
        )
        positions[key] = created["id"]
        await priya.post(f"/positions/{created['id']}/assign",
                         {"employee_id": demo.emp(key),
                          "from_date": max(person.date_of_joining, "2026-04-01")})
    for code, title, dept, grade, manager, lo, hi in VACANCIES:
        await priya.post(
            "/positions",
            {"code": code, "title": title, "department_id": demo.ids["department"][dept],
             "location_id": demo.ids["location"]["BLR"],
             "cost_centre_id": centres[DEPARTMENT_CC[dept]]["id"], "grade": grade,
             "reports_to_position_id": positions[manager], "budgeted_ctc_min": lo,
             "budgeted_ctc_max": hi, "effective_from": "2026-10-01"},
        )
    for dept, approved, cost in (("ENG", 11, "26000000"), ("SAL", 5, "7500000"),
                                 ("PRD", 2, "4500000"), ("FIN", 2, "3200000"),
                                 ("OPS", 3, "1100000"), ("HR", 3, "5200000"),
                                 ("LDR", 1, "6000000")):
        await priya.put("/positions/budgets",
                        {"financial_year": "FY 2026-27",
                         "department_id": demo.ids["department"][dept],
                         "approved_positions": approved, "approved_cost": cost,
                         "note": "Board-approved plan, April 2026"})
    await priya.post(
        "/positions/scenarios",
        {"name": "FY 2027-28 platform team (draft)",
         "description": "Split platform from product engineering under the new EM.",
         "as_of": "2027-04-01",
         "changes": [{"kind": "create",
                      "values": {"code": "ENG-SRE-1", "title": "Site Reliability Engineer",
                                 "department_id": demo.ids["department"]["ENG"],
                                 "grade": "P3", "budgeted_ctc_min": "1600000",
                                 "budgeted_ctc_max": "2200000"},
                      "reason": "The on-call load needs a dedicated SRE"}]},
    )
    demo.ids["position"] = positions
    demo.note(FLOW, f"3 cost centres, {len(positions)} filled positions in a reporting tree, "
                    f"{len(VACANCIES)} vacancies, budgets for 7 departments, a draft scenario")

    # ── Lifecycle events ────────────────────────────────────────────────────
    hr = demo["karthik"]
    # HR raises the promotion Suresh agreed with Divya; approvals follow the chain.
    promotion = await hr.post(
        "/lifecycle/events",
        {"employee_id": demo.emp("divya"), "event_type": "promotion",
         "effective_from": "2026-08-01",
         "new_values": {"grade": "P3", "designation": "Senior Accounts Executive"},
         "new_ctc_annual": "720000",
         "reason": "Took over GST and TDS filings in July; promotion agreed in the "
                   "mid-year conversation, backdated to 1 August."},
    )
    promotion = await _decide(demo, promotion)
    await priya.post(f"/lifecycle/events/{promotion['id']}/effect")

    event = await hr.post(
        "/lifecycle/events",
        {"employee_id": demo.emp("manoj"),
         "event_type": "redesignation", "effective_from": "2026-10-01",
         "new_values": {"designation": "Facilities & IT Manager"},
         "reason": "Now runs facilities as well as IT."},
    )
    event = await _decide(demo, event)
    await priya.post(f"/lifecycle/events/{event['id']}/effect")

    transfer = await hr.post(
        "/lifecycle/events",
        {"employee_id": demo.emp("pooja"), "event_type": "location_change",
         "effective_from": "2026-11-02",
         "new_values": {"location_id": demo.ids["location"]["BLR"]},
         "reason": "Moving to Bengaluru to run the south enterprise accounts."},
    )
    await _decide(demo, transfer)
    await priya.put("/lifecycle/probation/policy",
                    {"silence_policy": "require_decision", "grace_days": 3,
                     "confirmation_raises_salary": False,
                     "hr_escalation_user_id": str(demo.people["priya"].user_id)})
    demo.note(FLOW, "lifecycle: Divya's backdated promotion effected, Manoj redesignated, "
                    "Pooja's move to Bengaluru approved for 2 November; probation policy")

    # ── Arrears for Divya's August and September ───────────────────────────
    history = await priya.get(f"/employees/{demo.emp('divya')}/salary/history")
    history = sorted(history, key=lambda s: s["effective_from"])
    new, previous = history[-1], history[-2]
    if new["effective_from"] != "2026-08-01":
        raise SeedError(f"Divya's new salary starts {new['effective_from']}, not 1 August")
    preview = await priya.post(
        "/arrears/preview",
        {"legal_entity_id": demo.ids["entity"], "effective_from": "2026-08-01",
         "revisions": [{"employee_id": demo.emp("divya"), "new_salary_id": new["id"],
                        "previous_salary_id": previous["id"]}]},
    )
    batch = await priya.post(
        "/arrears/revisions",
        {"legal_entity_id": demo.ids["entity"], "effective_from": "2026-08-01",
         "revision_type": "promotion", "reason": "Divya's promotion from 1 August",
         "revisions": [{"employee_id": demo.emp("divya"), "new_salary_id": new["id"],
                        "previous_salary_id": previous["id"]}]},
    )
    batch_id = batch.get("batch_id") or batch.get("id")
    await demo["ananya"].post(f"/arrears/batches/{batch_id}/approve",
                              {"acknowledge_filings": True})
    payable = await priya.get("/arrears/payable")
    demo.note(FLOW, f"arrears: Aug-Sep difference for Divya previewed "
                    f"({_total(preview)}), approved, payable in October "
                    f"({len(payable) if isinstance(payable, list) else payable})")


def _depth(demo: Demo, key: str) -> int:
    depth, manager = 0, demo.people[key].manager
    while manager:
        depth, manager = depth + 1, demo.people[manager].manager
    return depth


async def _decide(demo: Demo, event: dict) -> dict:
    """Approve each pending step as the person the step is waiting for."""
    for _ in range(4):
        if event.get("status") not in ("pending", "pending_approval"):
            return event
        for approver in ("ananya", "priya", "karthik"):
            response = await demo.http.post(
                f"/api/v1/lifecycle/events/{event['id']}/decide",
                json={"approve": True, "note": "Approved"},
                headers=demo[approver]._headers(),
            )
            if response.status_code == 200:
                event = {**event, **response.json()}
                break
        else:
            raise SeedError(f"nobody could approve lifecycle event {event['id']}")
    return event


def _total(preview) -> str:
    if isinstance(preview, dict):
        for key in ("total_net", "net_payable", "total", "total_arrears"):
            if key in preview:
                return f"{key} {preview[key]}"
    return "previewed"
