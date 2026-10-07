"""Performance: values and competencies, two review cycles, goals, 1:1s, feedback,
a PIP, a mid-year increment cycle and probation reviews.

Last year's annual review is finished: every stage submitted, calibrated,
released and acknowledged, and the cycle closed. This half-year's review is in
progress: launched this month, most self reviews in, a few manager reviews
done. Goals cascade from a company goal to teams to individuals, with
check-ins and comments.
"""

from __future__ import annotations

from datetime import date

from .api import Demo, SeedError

FLOW = "performance"

VALUES = (
    ("ownership", "Ownership", "We finish what we start, and we say so when we can't."),
    ("customer_first", "Customer first", "Every decision starts with a customer's problem."),
    ("candour", "Candour", "We say the hard thing kindly, and early."),
    ("craft", "Craft", "We take pride in work done properly."),
)

COMPETENCIES = (
    ("COLLAB", "Collaboration", "behavioural", ["candour"]),
    ("OWN", "Ownership and delivery", "behavioural", ["ownership"]),
    ("CUST", "Customer focus", "functional", ["customer_first"]),
    ("CRAFT", "Craft and quality", "functional", ["craft"]),
    ("LEAD", "Leading people", "leadership", []),
)

BANDS = (("Needs improvement", 0, 50), ("Meets expectations", 50, 75),
         ("Exceeds expectations", 75, 90), ("Outstanding", 90, 100))

#: Last year's ratings, as (self, manager) on a five-point scale per competency.
ANNUAL = {
    "priya": ("4", "4"), "karthik": ("4", "3"), "vikram": ("4", "4"), "meera": ("5", "4"),
    "arjun": ("4", "5"), "sneha": ("4", "4"), "rohan": ("3", "3"), "aditya": ("4", "4"),
    "kavya": ("4", "4"), "nikhil": ("4", "3"), "rahul": ("4", "4"), "pooja": ("4", "4"),
    "imran": ("3", "2"), "neha": ("4", "3"), "suresh": ("4", "4"), "divya": ("3", "3"),
    "manoj": ("4", "3"), "lakshmi": ("3", "3"), "anjali": ("4", "4"), "ananya": ("4", "4"),
}


async def setup(demo: Demo) -> None:
    priya = demo["priya"]
    for order, (code, name, description) in enumerate(VALUES):
        await priya.put("/culture/values", {"code": code, "name": name,
                                            "description": description, "display_order": order})
    scales = await priya.get("/competency/scales")
    scale = next((s for s in scales if s["name"] == "Five-point scale"), None)
    if scale is None:
        labels = ("Well below", "Developing", "Meets", "Strong", "Role model")
        scale = await priya.post(
            "/competency/scales",
            {"name": "Five-point scale", "scale_type": "numeric", "min_value": "1",
             "max_value": "5",
             "levels": [{"value": str(v), "label": labels[v - 1],
                         "description": f"{labels[v - 1]} for the role"} for v in range(1, 6)]},
        )
    demo.ids["scale"] = scale["id"]
    have = {c["code"]: c for c in await priya.get("/competency/competencies")}
    for code, name, category, values in COMPETENCIES:
        if code not in have:
            have[code] = await priya.post(
                "/competency/competencies",
                {"code": code, "name": name, "category": category, "scale_id": scale["id"],
                 "description": f"How someone shows {name.lower()}.",
                 "linked_value_codes": values,
                 "indicators": [{"scale_value": str(v),
                                 "behaviour_description": f"{name}: level {v} behaviour"}
                                for v in range(1, 6)]},
            )
    demo.ids["competency"] = {code: c["id"] for code, c in have.items()}
    templates = {t["name"]: t for t in await priya.get("/competency/templates")}
    for name, codes in (("Individual contributor", ("COLLAB", "OWN", "CUST", "CRAFT")),
                        ("People manager", ("COLLAB", "OWN", "CUST", "LEAD"))):
        if name not in templates:
            templates[name] = await priya.post(
                "/competency/templates",
                {"name": name, "items": [{"competency_id": demo.ids["competency"][c],
                                          "weight": "25", "display_order": i}
                                         for i, c in enumerate(codes)]},
            )
    demo.ids["competency_template"] = {n: t["id"] for n, t in templates.items()}
    demo.note(FLOW, "4 company values, a five-point scale, 5 competencies, 2 templates")


async def _cycle(demo: Demo, *, name: str, kind: str, start: str, end: str,
                 due_self: str, due_manager: str, exclude: list[str],
                 launch: bool = True, goal_weight: int = 50) -> str:
    priya = demo["priya"]
    cycle = await priya.post(
        "/appraisal/cycles",
        {"name": name, "cycle_type": kind, "period_start": start, "period_end": end,
         "eligibility_rule": {"min_service_months": 3,
                              "exclude_employee_ids": [demo.emp(k) for k in exclude]},
         "stages": [
             {"sequence": 1, "role": "self", "label": "Self review", "due_date": due_self},
             {"sequence": 2, "role": "manager", "label": "Manager review",
              "due_date": due_manager},
             {"sequence": 3, "role": "hr", "label": "HR sign-off"},
         ]},
    )
    await priya.put(
        "/competency/appraisal-templates",
        {"cycle_id": cycle["id"], "rating_scale_id": demo.ids["scale"],
         "competency_template_id": demo.ids["competency_template"]["Individual contributor"],
         "goal_weight": str(goal_weight), "competency_weight": str(100 - goal_weight),
         "bands": [{"label": label, "lower": str(lo), "upper": str(hi)}
                   for label, lo, hi in BANDS]},
    )
    await priya.post("/cycle-config/rules",
                     {"cycle_id": cycle["id"], "priority": 10,
                      "template_id": demo.ids["competency_template"]["People manager"],
                      "conditions": {"grades": ["M2", "M3", "M4", "E1"]},
                      "label": "Managers (M and E grades)"})
    await priya.post("/cycle-config/rules",
                     {"cycle_id": cycle["id"], "priority": 100,
                      "template_id": demo.ids["competency_template"]["Individual contributor"],
                      "label": "Everyone else"})
    await priya.post("/calibration/policies",
                     {"cycle_id": cycle["id"], "mode": "advisory", "scope": "company",
                      "min_group_size": 5,
                      "targets": [
                          {"rating_value": "Outstanding", "target_percent": "10",
                           "tolerance_percent": "10"},
                          {"rating_value": "Exceeds expectations", "target_percent": "25",
                           "tolerance_percent": "15"},
                          {"rating_value": "Meets expectations", "target_percent": "55",
                           "tolerance_percent": "20"},
                          {"rating_value": "Needs improvement", "target_percent": "10",
                           "tolerance_percent": "10"}]})
    if launch:
        await priya.post(f"/cycle-config/cycles/{cycle['id']}/launch")
    return cycle["id"]


def _ratings(appraisal: dict, value: str) -> dict:
    rows = appraisal.get("competencies") or []
    return {"competencies": [{"competency_id": c["competency_id"] if "competency_id" in c
                              else c["id"], "rating": value} for c in rows]}


async def _form(api, appraisal_id: str) -> dict:
    return await api.get(f"/appraisal/appraisals/{appraisal_id}")


async def _submit(api, appraisal_id: str, rating: str, comment: str) -> None:
    form = await _form(api, appraisal_id)
    await api.post(f"/appraisal/appraisals/{appraisal_id}/submit",
                   {"ratings": _ratings(form, rating), "comments": comment})


async def _appraisals(demo: Demo, cycle_id: str) -> dict[str, dict]:
    rows = await demo["priya"].get("/appraisal/appraisals", cycle_id=cycle_id)
    rows = rows["items"] if isinstance(rows, dict) else rows
    by_employee = {str(p.employee_id): k for k, p in demo.people.items() if p.employee_id}
    return {by_employee[r["employee_id"]]: r for r in rows if r["employee_id"] in by_employee}


async def annual(demo: Demo) -> None:
    """FY 2025-26: launched, reviewed, calibrated, released, acknowledged, closed."""
    cycle_id = await _cycle(
        demo, name="Annual review FY 2025-26", kind="annual", start="2025-04-01",
        end="2026-03-31", due_self="2026-04-15", due_manager="2026-04-25",
        # Last year's goals lived in the previous system: rated on competencies only.
        exclude=["fatima", "ravi"], goal_weight=0)
    demo.ids["cycle_annual"] = cycle_id
    appraisals = await _appraisals(demo, cycle_id)
    for key, row in appraisals.items():
        mine, theirs = ANNUAL.get(key, ("3", "3"))
        await _submit(demo[key], row["id"], mine, "My year in review: delivered what I owned.")
        manager = demo.people[key].manager
        if manager:
            await _submit(demo[manager], row["id"], theirs, "Consistent and dependable.")
            await _submit(demo.hr_for(key), row["id"], theirs, "Signed off by HR.")
        else:
            # Nobody above the CEO: HR steps in for the line, and the other HR
            # admin signs off, so the two views stay independent.
            await _submit(demo["priya"], row["id"], theirs, "Reviewed with the board.")
            await _submit(demo["karthik"], row["id"], theirs, "Signed off by HR.")
    done = await _appraisals(demo, cycle_id)
    unfinished = [k for k, r in done.items() if r.get("status") not in ("completed",)]
    if unfinished:
        raise SeedError(f"annual reviews not completed for {unfinished}: "
                        f"{[done[k].get('status') for k in unfinished]}")

    # Calibration across the company, facilitated by Ananya.
    session = await demo["priya"].post(
        "/calibration/sessions",
        {"cycle_id": cycle_id, "name": "Company calibration FY 2025-26", "scope": "company",
         "scope_value": "all", "facilitator_user_id": str(demo.people["ananya"].user_id),
         "participant_user_ids": [str(demo.people[k].user_id)
                                  for k in ("vikram", "rahul", "kavya", "suresh", "priya")]},
    )
    await demo["priya"].post(f"/calibration/sessions/{session['id']}/start")
    for key, rating, why in (
        ("arjun", "Outstanding", "Led the payments rebuild and mentored two engineers."),
        ("imran", "Needs improvement", "Below the demo target for three quarters running."),
    ):
        await demo["ananya"].post(
            f"/calibration/sessions/{session['id']}/changes",
            {"appraisal_id": appraisals[key]["id"], "new_rating": rating, "justification": why},
        )
    await demo["priya"].post(f"/calibration/sessions/{session['id']}/lock")

    for key, row in appraisals.items():
        await demo.hr_for(key).post(f"/appraisal/appraisals/{row['id']}/release", {})
    acknowledged = 0
    for key, row in appraisals.items():
        if key in ("kavya", "lakshmi"):
            continue   # not yet acknowledged
        body = {"acknowledgement": "agreed"}
        if key == "neha":
            body = {"acknowledgement": "disagreed",
                    "objection": "The Q4 renewals I closed are not reflected in the rating."}
        await demo[key].post(f"/appraisal/appraisals/{row['id']}/acknowledge", body)
        acknowledged += 1
    await demo["priya"].post(f"/cycle-config/cycles/{cycle_id}/close",
                             {"acknowledge_incomplete": True, "release_completed": True})
    demo.note(FLOW, f"annual review FY 2025-26: {len(appraisals)} reviews through 3 stages, "
                    f"2 calibrated, released, {acknowledged} acknowledged (1 objection), closed")


GOALS = (
    # key, owner, title, level, parent key, weight, visibility, key results
    ("arr", "ananya", "Reach 12 crore ARR by March 2027", "company", None, "100", "company",
     [("ARR (crore)", "number", "8.4", "12", "9.6"),
      ("Net revenue retention (%)", "percent", "104", "115", "108")]),
    ("v5", "vikram", "Ship the AskMira v5 platform", "team", "arr", "60", "company",
     [("v5 generally available", "milestone", "0", "1", None),
      ("p95 API latency (ms)", "number", "900", "400", "560")]),
    ("pipeline", "rahul", "Double the enterprise pipeline", "team", "arr", "70", "company",
     [("Qualified pipeline (lakh)", "number", "300", "600", "455"),
      ("New enterprise logos", "number", "0", "12", "5")]),
    ("payments", "meera", "Migrate payments to the new ledger service", "individual", "v5",
     "60", "manager_line",
     [("Traffic on the new service (%)", "percent", "0", "100", "70"),
      ("Unit test coverage (%)", "percent", "62", "80", "74")]),
    ("design", "sneha", "Roll out the design system across the web app", "individual", "v5",
     "50", "manager_line", [("Screens migrated", "number", "0", "86", "61")]),
    ("mentor", "arjun", "Mentor Rohan and Fatima to independent delivery", "individual", "v5",
     "30", "manager_line", [("Mentoring plan agreed", "milestone", "0", "1", "1")]),
    ("newbiz", "pooja", "Close 80 lakh of new business", "individual", "pipeline", "70",
     "manager_line", [("New business booked (₹)", "currency", "0", "8000000", "5200000")]),
    ("demos", "imran", "Book 25 qualified demos this half", "individual", "pipeline", "80",
     "manager_line", [("Demos booked", "number", "0", "25", "9")]),
)


async def goals(demo: Demo) -> None:
    created: dict[str, dict] = {}
    for key, owner, title, level, parent, weight, visibility, krs in GOALS:
        goal = await demo[owner].post(
            "/goals",
            {"title": title, "level": level, "weight": weight, "visibility": visibility,
             "start_date": "2026-04-01",
             "due_date": "2027-03-31" if level == "company" else "2026-09-30",
             "parent_goal_id": created[parent]["id"] if parent else None,
             "appraisal_cycle_id": demo.ids.get("cycle_h1") if level == "individual" else None,
             "key_results": [{"title": t, "kind": k, "start_value": s, "target_value": tv,
                              "unit": None} for t, k, s, tv, _ in krs]},
        )
        created[key] = goal
        for kr, (_, _, _, _, current) in zip(goal["key_results"], krs, strict=True):
            if current is None:
                continue
            confidence = "at_risk" if key == "demos" else "on_track"
            await demo[owner].post(
                f"/goals/{goal['id']}/check-ins",
                {"key_result_id": kr["id"], "value": current, "confidence": confidence,
                 "note": "Update at the end of September."},
            )
            manager = demo.people[owner].manager
            if manager and key in ("payments", "demos", "newbiz"):
                check_in = (await demo[manager].get(f"/goals/{goal['id']}/check-ins"))[0]
                await demo[manager].post(
                    f"/goals/{goal['id']}/check-ins/{check_in['id']}/comments",
                    {"body": "Let's look at what is blocking this in our next 1:1."
                     if key == "demos" else "Good progress, keep going."},
                )
    # Everyone else in the review has the half's commitments as one goal.
    owners = {owner for _, owner, *_ in GOALS}
    extra = 0
    for key, person in demo.people.items():
        if not person.code or key in owners or key in ("ravi", "neha", "fatima"):
            continue
        goal = await demo[key].post(
            "/goals",
            {"title": f"Deliver my H1 commitments as {person.designation}",
             "level": "individual", "weight": "100", "visibility": "manager_line",
             "start_date": "2026-04-01", "due_date": "2026-09-30",
             "appraisal_cycle_id": demo.ids.get("cycle_h1"),
             "key_results": [{"title": "Commitments delivered (%)", "kind": "percent",
                              "start_value": "0", "target_value": "100"}]},
        )
        done = 60 + (int(person.code[-2:]) * 7) % 36
        await demo[key].post(f"/goals/{goal['id']}/check-ins",
                             {"key_result_id": goal["key_results"][0]["id"], "value": str(done),
                              "confidence": "on_track", "note": "End of September update."})
        extra += 1
    demo.ids["goal"] = {k: g["id"] for k, g in created.items()}
    demo.note(FLOW, f"{len(GOALS)} goals cascading company → team → individual, and "
                    f"{extra} individual H1 goals, all with check-ins")


async def midyear(demo: Demo) -> None:
    """H1 2026-27: launched this month and part-way through."""
    cycle_id = await _cycle(
        demo, name="Mid-year review H1 2026-27", kind="half_yearly", start="2026-04-01",
        end="2026-09-30", due_self="2026-10-14", due_manager="2026-10-23",
        exclude=["ravi", "neha"], launch=False)
    demo.ids["cycle_h1"] = cycle_id
    # The half's goals are set before the review opens, so the review scores them.
    await goals(demo)
    await demo["priya"].post(f"/cycle-config/cycles/{cycle_id}/launch")
    appraisals = await _appraisals(demo, cycle_id)
    selfs = ("meera", "sneha", "arjun", "rohan", "aditya", "pooja", "imran", "nikhil",
             "divya", "manoj", "anjali", "karthik")
    for key in selfs:
        await _submit(demo[key], appraisals[key]["id"], "4" if key != "imran" else "3",
                      "Half-year update: on track on my goals; details in the goal check-ins.")
    for manager, key in (("vikram", "meera"), ("vikram", "sneha"), ("rahul", "pooja")):
        await _submit(demo[manager], appraisals[key]["id"], "4", "A strong first half.")
    # One draft, saved but not submitted.
    form = await _form(demo["kavya"], appraisals["kavya"]["id"])
    await demo["kavya"].post(f"/appraisal/appraisals/{appraisals['kavya']['id']}/draft",
                             {"ratings": _ratings(form, "4"), "comments": "Draft - to finish"})
    progress = await demo["priya"].get(f"/appraisal/cycles/{cycle_id}/progress")
    demo.note(FLOW, f"mid-year review launched for {len(appraisals)}: 12 self reviews in, "
                    f"3 manager reviews done, 1 draft ({_summary(progress)})")


def _summary(progress) -> str:
    if isinstance(progress, dict):
        for key in ("completion_percent", "percent_complete", "completed"):
            if key in progress:
                return f"{key} {progress[key]}"
    return "progress tracked"


async def conversations(demo: Demo) -> None:
    """1:1s, 360 feedback and appreciation."""
    meeting = await demo["vikram"].post(
        "/one-on-ones",
        {"with_employee_id": demo.emp("meera"), "scheduled_on": "2026-10-01",
         "start_time": "16:00", "duration_minutes": 30,
         "agenda": ["Payments migration cut-over", "Mid-year review", "Conference budget"]},
    )
    await demo["meera"].post(f"/one-on-ones/{meeting['id']}/items",
                             {"kind": "talking_point", "body": "Can I speak at JSFoo in November?"})
    await demo["vikram"].post(f"/one-on-ones/{meeting['id']}/items",
                              {"kind": "action_item", "body": "Book the cut-over window with SRE",
                               "owner_employee_id": demo.emp("meera"), "due_on": "2026-10-09"})
    await demo["vikram"].put(f"/one-on-ones/{meeting['id']}/my-note",
                             {"body": "Meera is ready to lead the cut-over. Nominate for JSFoo."})
    await demo["vikram"].post(f"/one-on-ones/{meeting['id']}/complete",
                              {"summary": "Cut-over on 12 Oct; Vikram to approve JSFoo travel."})
    for manager, report, on in (("vikram", "meera", "2026-10-15"), ("rahul", "imran", "2026-10-08"),
                                ("rahul", "pooja", "2026-10-09"), ("arjun", "rohan", "2026-10-12")):
        await demo[manager].post(
            "/one-on-ones",
            {"with_employee_id": demo.emp(report), "scheduled_on": on, "start_time": "11:00",
             "duration_minutes": 30, "agenda": ["Goals check-in", "Anything blocking you?"]},
        )

    request = await demo["vikram"].post(
        "/feedback/requests",
        {"subject_employee_id": demo.emp("meera"),
         "reviewer_employee_ids": [demo.emp(k) for k in ("arjun", "sneha", "kavya")],
         "appraisal_cycle_id": demo.ids["cycle_h1"], "due_on": "2026-10-16",
         "message": "Feedback for Meera's mid-year review, please.",
         "employee_visibility": "anonymised"},
    )
    inbox_owner = {}
    for key in ("arjun", "sneha", "kavya"):
        for item in _list(await demo[key].get("/feedback/inbox")):
            if item.get("request_id") == request["id"] or item.get("subject_name", "").startswith(
                    "Meera"):
                inbox_owner[key] = item
    if len(inbox_owner) != 3:
        raise SeedError(f"feedback did not reach all three reviewers: {list(inbox_owner)}")
    competency = demo.ids["competency"]
    for key in ("arjun", "sneha"):
        reviewer_id = inbox_owner[key].get("reviewer_id") or inbox_owner[key]["id"]
        await demo[key].post(
            f"/feedback/responses/{reviewer_id}/submit",
            {"competency_ratings": [{"competency_id": competency["COLLAB"], "rating": "4",
                                     "comment": "Explains trade-offs clearly in design reviews."},
                                    {"competency_id": competency["CRAFT"], "rating": "5"}],
             "strengths": "Calm under pressure; writes excellent design notes.",
             "improvements": "Could delegate more of the on-call load.",
             "overall_rating": "4"},
        )
    reviewer_id = inbox_owner["kavya"].get("reviewer_id") or inbox_owner["kavya"]["id"]
    await demo["kavya"].post(f"/feedback/responses/{reviewer_id}/decline",
                             {"reason": "I have not worked with Meera this half."})

    for sender, receiver, kind, body in (
        ("kavya", "nikhil", "appreciation",
         "The onboarding redesign tested beautifully with customers. Thank you!"),
        ("arjun", "rohan", "suggestion",
         "Try writing the test plan before the code review; it will catch the edge cases."),
        ("meera", "aditya", "appreciation", "Thanks for staying late to fix the deploy pipeline."),
    ):
        await demo[sender].post("/feedback/private",
                                {"to_employee_id": demo.emp(receiver), "kind": kind,
                                 "body": body})
    demo.note(FLOW, "1:1s (1 completed with notes and actions, 4 upcoming); 360 for Meera "
                    "(2 responses, 1 declined); 3 private notes")


def _list(payload) -> list:
    if isinstance(payload, dict):
        for key in ("items", "requests", "pending"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return []
    return list(payload or [])


async def development(demo: Demo) -> None:
    """A PIP, a mid-year increment cycle with a promotion, probation reviews."""
    pip = await demo["rahul"].post(
        "/performance/pips",
        {"employee_id": demo.emp("imran"), "start_date": "2026-09-15",
         "end_date": "2026-11-14",
         "reason": "Demo bookings have been below target for three quarters.",
         "expectations": "Book at least 8 qualified demos a month and log every call.",
         "support_provided": "Weekly coaching with Rahul; call shadowing with Pooja.",
         "milestones": [
             {"description": "40 logged calls a day", "target_date": "2026-09-30",
              "measurable_criteria": "CRM call log average over the last 10 working days"},
             {"description": "8 qualified demos in October", "target_date": "2026-10-31",
              "measurable_criteria": "Demos marked qualified by the account executive"},
         ]},
    )
    await demo["rahul"].post(
        f"/performance/pips/{pip['pip_id']}/check-ins",
        {"scheduled_for": "2026-09-30", "held_on": "2026-09-30",
         "manager_comments": "Call volume is up to 42 a day. Demo quality is improving.",
         "employee_input_state": "given",
         "employee_comments": "The call scripts help. I need more warm leads."},
    )
    detail = await demo["rahul"].get(f"/performance/pips/{pip['pip_id']}")
    first = detail["milestones"][0]
    await demo["rahul"].post(f"/performance/pips/{pip['pip_id']}/milestones/{first['id']}/assess",
                             {"status": "met", "note": "42 calls a day on average."})
    await demo["imran"].post(f"/performance/pips/{pip['pip_id']}/statements",
                             {"statement": "I understand the expectations and I am working on "
                                           "them. I would like help with lead quality."})

    cycle = await demo["priya"].post(
        "/performance/cycles",
        {"name": "Mid-year corrections, January 2027", "effective_from": "2027-01-01",
         "budget_total": "1500000", "overage_mode": "escalate",
         "linked_appraisal_cycle_id": demo.ids["cycle_annual"],
         "bands": [
             {"rating_label": "Outstanding", "min_percentage": "10", "max_percentage": "18",
              "recommended_percentage": "14"},
             {"rating_label": "Exceeds expectations", "min_percentage": "6",
              "max_percentage": "12", "recommended_percentage": "9"},
             {"rating_label": "Meets expectations", "min_percentage": "3",
              "max_percentage": "8", "recommended_percentage": "5"},
             {"rating_label": "Needs improvement", "min_percentage": "0",
              "max_percentage": "2", "recommended_percentage": "0"}]},
    )
    recommendation = await demo["vikram"].post(
        "/performance/recommendations",
        {"cycle_id": cycle["cycle_id"], "employee_id": demo.emp("arjun"),
         "rating_label": "Outstanding", "percentage": "14", "promotion_to_grade": "M1",
         "justification": "Leads the payments rebuild; ready for an Engineering Lead role."},
    )
    decision = f"/performance/recommendations/{recommendation['recommendation_id']}/decision"
    await demo["ananya"].post(decision,
                              {"approve": True, "note": "Approved with the promotion."})
    await demo["priya"].post("/performance/promotions",
                             {"recommendation_id": recommendation["recommendation_id"],
                              "effective_from": "2027-01-01"})
    await demo["vikram"].post(
        "/performance/recommendations",
        {"cycle_id": cycle["cycle_id"], "employee_id": demo.emp("meera"),
         "rating_label": "Exceeds expectations", "percentage": "9",
         "justification": "Owned the payments migration end to end."},
    )
    await demo["rahul"].post(
        "/performance/recommendations",
        {"cycle_id": cycle["cycle_id"], "employee_id": demo.emp("pooja"),
         "rating_label": "Exceeds expectations", "percentage": "8",
         "justification": "Strong renewals; on track for her annual number."},
    )

    for key, end, due, outcome in (
        ("imran", "2026-05-03", "2026-04-20", "confirm"),
        ("fatima", "2027-03-20", "2027-03-06", None),
    ):
        review = await demo["priya"].post(
            "/cycle-config/probation-reviews",
            {"employee_id": demo.emp(key), "probation_end_date": end, "review_due_date": due},
        )
        if outcome:
            await demo["priya"].post(
                f"/cycle-config/probation-reviews/{review['id']}/outcome",
                {"outcome": outcome, "notes": "Confirmed on Rahul's recommendation."},
            )
    demo.note(FLOW, "PIP for Imran with a check-in, a met milestone and his statement; "
                    "January increments with Arjun's promotion approved and 2 pending; "
                    "probation: Imran confirmed, Fatima's review due in March")


def today() -> date:
    return date.today()
