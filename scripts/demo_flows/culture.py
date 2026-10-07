"""Culture and rewards: the feed, clubs, awards, a pulse survey with an action
plan, announcements, and the points that recognition earns.

The engagement survey is answered by every active employee, so its results
clear the anonymity threshold; answers carry no respondent, and the seed never
learns who said what either.
"""

from __future__ import annotations

from datetime import date, timedelta

from .api import Demo, SeedError

FLOW = "culture"

SHOUTOUTS = (
    ("vikram", "meera", "ownership",
     "Meera ran the payments cut-over rehearsal end to end and wrote the runbook everyone "
     "used at 2 am. Thank you!"),
    ("rahul", "pooja", "customer_first",
     "Pooja turned a churn risk at Tata Capital into a two-year renewal."),
    ("kavya", "nikhil", "craft", "The new onboarding flow tested at 4.6/5 with customers."),
    ("arjun", "rohan", "candour",
     "Rohan flagged the release risk early and in writing. That saved us a weekend."),
    ("manoj", "lakshmi", "customer_first",
     "Lakshmi closed 140 tickets in September with a 4.8 CSAT."),
    ("priya", "karthik", "ownership", "Karthik closed six payroll months without one escalation."),
    ("meera", "aditya", "ownership", "Aditya fixed the deploy pipeline at midnight before launch."),
    ("suresh", "divya", "craft", "Divya's GST reconciliation matched to the rupee."),
    ("ananya", "vikram", "candour",
     "Vikram told the board exactly where v5 stood, including the bad news."),
    ("sneha", "fatima", "ownership", "Fatima shipped her first PR in week one!"),
)

REACTIONS = ("👏", "🎉", "❤️", "🙌")


async def feed(demo: Demo) -> None:
    posts = []
    for sender, receiver, value, message in SHOUTOUTS:
        posts.append(await demo[sender].post(
            "/culture/shoutouts",
            {"to_employee_id": demo.emp(receiver), "message": message, "value_tag": value,
             "visibility": "company"},
        ))
    reactors = [k for k, p in demo.people.items() if p.code and k not in ("ravi",)]
    for index, post in enumerate(posts):
        for offset in range(3 + index % 4):
            who = reactors[(index * 5 + offset * 3) % len(reactors)]
            if who in (SHOUTOUTS[index][0], SHOUTOUTS[index][1]):
                continue
            await demo[who].put(f"/culture/shoutouts/{post['id']}/reactions",
                                {"emoji": REACTIONS[(index + offset) % 4], "active": True})
    await demo["priya"].put(f"/culture/shoutouts/{posts[0]['id']}/pin", {"is_pinned": True})

    clubs = {c["name"]: c for c in await demo["priya"].get("/culture/clubs")}
    for name, founder, description, icon, members in (
        ("Cricket club", "rahul", "Box cricket every second Saturday at Turf Park.", "trophy",
         ("arjun", "rohan", "aditya", "imran", "nikhil", "manoj")),
        ("Book club", "kavya", "One book a month; this month: The Psychology of Money.", "book",
         ("meera", "sneha", "priya", "anjali", "divya")),
        ("Running group", "vikram", "Cubbon Park 6:30 am on Sundays. All paces welcome.",
         "activity", ("suresh", "meera", "fatima", "karthik")),
    ):
        if name not in clubs:
            clubs[name] = await demo[founder].post(
                "/culture/clubs", {"name": name, "description": description, "icon": icon})
        for member in members:
            await demo[member].post(f"/culture/clubs/{clubs[name]['id']}/join")

    for key, birthday, anniversary in (("meera", True, True), ("vikram", True, True),
                                       ("sneha", True, True), ("arjun", True, False),
                                       ("pooja", False, True)):
        await demo[key].put("/culture/event-privacy",
                            {"share_birthday": birthday, "share_work_anniversary": anniversary,
                             "birthday_scope": "company"})
    demo.note(FLOW, f"{len(SHOUTOUTS)} shoutouts tagged with values, reactions, 1 pinned; "
                    "3 clubs with 15 members; birthday and anniversary sharing for 5")


async def awards(demo: Demo) -> None:
    priya = demo["priya"]
    programme = await priya.post(
        "/culture/award-programmes",
        {"name": "Quarterly Stars", "cadence": "quarterly",
         "description": "Recognising the people who lived our values this quarter.",
         "categories": [{"key": "values", "name": "Values champion"},
                        {"key": "rookie", "name": "Rookie of the quarter"}],
         "criteria": [{"key": "impact", "name": "Impact", "weight": 2},
                      {"key": "values", "name": "Lived our values", "weight": 1}]},
    )
    jurors = ["ananya", "priya", "suresh"]
    cycle = await priya.post(
        "/culture/award-cycles",
        {"programme_id": programme["id"], "label": "Q2 FY 2026-27 (July to September)",
         # Nominations close today; the jury scores and results follow.
         "nominations_open_on": str(date.today() - timedelta(days=14)),
         "nominations_close_on": str(date.today()),
         "juror_employee_ids": [demo.emp(k) for k in jurors],
         "result_announcement_date": str(date.today() + timedelta(days=2))},
    )
    await priya.put(f"/culture/award-cycles/{cycle['id']}/status",
                    {"status": "nominations_open"})
    nominations = []
    for nominator, category, nominee, citation, values in (
        ("vikram", "values", "meera", "Owned the payments migration from design to cut-over.",
         ["ownership"]),
        ("rahul", "values", "pooja", "Saved the Tata Capital account with patience and data.",
         ["customer_first"]),
        ("manoj", "values", "lakshmi", "140 tickets, 4.8 CSAT, and trained two new agents.",
         ["customer_first"]),
        ("arjun", "rookie", "fatima", "First PR in week one; already on the on-call rota.",
         ["ownership"]),
    ):
        nominations.append(await demo[nominator].post(
            "/culture/nominations",
            {"cycle_id": cycle["id"], "category_key": category,
             "nominee_employee_id": demo.emp(nominee), "citation": citation,
             "value_codes": values},
        ))
    await priya.put(f"/culture/award-cycles/{cycle['id']}/status", {"status": "jury_scoring"})
    for juror in jurors:
        for index, nomination in enumerate(nominations):
            await demo[juror].post(
                f"/culture/nominations/{nomination['id']}/score",
                {"scores": {"impact": 5 - index % 2, "values": 4 + (index == 0)},
                 "comments": "Strong case.", "submit": True},
            )
    await priya.put(f"/culture/award-cycles/{cycle['id']}/status", {"status": "decided"})
    for nomination in (nominations[0], nominations[3]):
        await priya.post("/culture/award-results",
                         {"nomination_id": nomination["id"], "awarded_on": str(date.today())})
    demo.note(FLOW, "Quarterly Stars: 4 nominations scored by 3 jurors; Meera and Fatima win")


async def survey(demo: Demo) -> None:
    priya = demo["priya"]
    today = date.today()
    pulse = await priya.post(
        "/culture/surveys",
        {"name": "Engagement pulse, September 2026", "kind": "engagement",
         "opens_on": str(today - timedelta(days=12)), "closes_on": str(today + timedelta(days=3)),
         "instrument_key": "engagement_9", "min_responses_to_report": 5},
    )
    items = ("clarity", "resources", "strengths", "recognition", "care", "growth", "voice",
             "purpose", "quality")
    responders = [k for k, p in demo.people.items() if p.code and k not in ("ravi", "sana")]
    for index, key in enumerate(responders):
        answers = {item: 3 + ((index + position) % 3) for position, item in enumerate(items)}
        if key in ("imran", "lakshmi"):
            answers["growth"] = 2
            answers["recognition"] = 2
        await demo[key].post(f"/culture/surveys/{pulse['id']}/responses",
                             {"answers": answers,
                              "free_text": "More clarity on promotions, please."
                              if index % 4 == 0 else None})
    enps = await priya.post(
        "/culture/surveys",
        {"name": "eNPS, Q3", "kind": "enps", "opens_on": str(today - timedelta(days=2)),
         "closes_on": str(today + timedelta(days=12)), "instrument_key": "enps",
         "min_responses_to_report": 5},
    )
    for index, key in enumerate(responders[:12]):
        await demo[key].post(f"/culture/surveys/{enps['id']}/responses",
                             {"answers": {"enps": (9, 10, 8, 7, 9, 6)[index % 6]}})
    await priya.post(f"/culture/surveys/{pulse['id']}/close")
    results = await priya.get(f"/culture/surveys/{pulse['id']}/results")
    if results.get("suppressed") or results.get("is_suppressed"):
        raise SeedError(f"pulse results were suppressed: {results}")

    plan = await priya.post(
        "/culture/action-plans",
        {"survey_id": pulse["id"], "scope": "company", "scope_key": "company",
         "theme": "Growth and recognition",
         "owner_employee_id": demo.emp("priya"), "due_date": "2026-12-31",
         "items": [
             {"description": "Publish the career ladder for every function",
              "owner_employee_id": demo.emp("priya"), "due_date": "2026-11-15"},
             {"description": "Monthly recognition round-up in the all-hands",
              "owner_employee_id": demo.emp("anjali"), "due_date": "2026-10-31"},
             {"description": "Learning budget of 20,000 per person per year",
              "owner_employee_id": demo.emp("ananya"), "due_date": "2026-12-01"},
         ]},
    )
    await priya.post(f"/culture/action-plans/{plan['id']}/notes",
                     {"note": "Career ladders drafted for engineering and sales."})
    item = plan["items"][1]
    await demo["anjali"].patch(f"/culture/action-plan-items/{item['id']}",
                               {"done_on": str(today)})
    demo.note(FLOW, f"engagement pulse: {len(responders)} responses, closed, results above "
                    "the anonymity floor, an action plan with progress; eNPS open with 12")


async def announcements(demo: Demo) -> None:
    priya = demo["priya"]
    for title, body, ack, audience in (
        ("Diwali holidays and the office closure",
         "The office is closed on 9 and 10 November for Diwali. Have a safe festival!",
         False, {}),
        ("Updated travel and expense policy",
         "The FY 2026-27 travel and expense policy is in the document library. Please read "
         "and acknowledge it by 20 October.", True, {}),
        ("Engineering all-hands on 14 October",
         "v5 launch plan and the on-call rota. 4 pm in the Bengaluru cafeteria and on Meet.",
         False, {"department_ids": [demo.ids["department"]["ENG"]]}),
    ):
        created = await priya.post(
            "/culture/announcements",
            {"title": title, "body": body, "audience": audience,
             "requires_acknowledgement": ack, "channels": ["in_app"]},
        )
        if ack:
            for key in ("meera", "vikram", "sneha", "rahul", "pooja", "suresh", "arjun"):
                await demo[key].post(f"/culture/announcements/{created['id']}/acknowledge",
                                     {"channel": "in_app"})
    await priya.post("/culture/announcements/deliver-due")
    demo.note(FLOW, "3 announcements (one for engineering), one needing acknowledgement "
                    "with 7 of 21 acknowledged")


async def rewards(demo: Demo) -> None:
    priya = demo["priya"]
    await priya.post("/rewards/valuations",
                     {"points_per_rupee": "1", "effective_from": "2026-04-01",
                      "note": "One point is one rupee"})
    await priya.put("/rewards/expiry-policy",
                    {"enabled": True, "expire_after_days": 365, "notify_days_before": 30,
                     "disclosure": "Points expire a year after they are awarded; you are "
                                   "reminded 30 days before."})
    await priya.post("/rewards/tax-rules",
                     {"reward_type": "recognition", "treatment": "gift_rule",
                      "exemption_threshold_per_year": "5000", "effective_from": "2026-04-01",
                      "basis": "Gifts to employees up to 5,000 a year are not a perquisite.",
                      "reviewed": True, "reviewed_by": "Suresh Pillai (Finance)",
                      "reviewed_on": "2026-04-02"})
    await priya.post("/rewards/budgets",
                     {"scope": "company", "period_start": "2026-04-01",
                      "period_end": "2027-03-31", "allocated_points": 500000,
                      "enforcement": "soft_warning"})
    for manager, points in (("vikram", 40000), ("rahul", 25000), ("kavya", 15000)):
        await priya.post("/rewards/budgets",
                         {"scope": "manager", "manager_employee_id": demo.emp(manager),
                          "period_start": "2026-04-01", "period_end": "2027-03-31",
                          "allocated_points": points, "enforcement": "hard_stop"})
    awarded = 0
    for key, points in (("meera", 3000), ("pooja", 2500), ("nikhil", 1500), ("rohan", 1000),
                        ("lakshmi", 2000), ("karthik", 1500), ("aditya", 1500),
                        ("divya", 1000), ("fatima", 2000), ("vikram", 2000)):
        await demo.hr_for(key).post("/rewards/awards",
                                    {"employee_id": demo.emp(key), "points": points,
                                     "source_type": "recognition"})
        awarded += points
    redemption = await demo["meera"].post(
        "/rewards/redemptions",
        {"points": 2000, "item_sku": "AMZ-GV-2000", "item_description": "Amazon gift voucher"})
    await demo["pooja"].post(
        "/rewards/redemptions",
        {"points": 1500, "item_sku": "SWG-1500", "item_description": "Swiggy voucher"})
    statement = await demo["meera"].get("/rewards/me/statement")
    demo.note(FLOW, f"rewards: valuation, expiry policy, reviewed tax rule, company and 3 "
                    f"manager budgets, {awarded} points to 10 people, 2 redemptions "
                    f"(Meera's balance {statement.get('balance', '?')}; "
                    f"redemption {redemption.get('status', 'requested')})")
