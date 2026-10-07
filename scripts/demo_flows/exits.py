"""Exits: the clearance checklist, Ravi who left in August, Neha serving notice.

Ravi's exit is recorded by HR from his July resignation, cleared item by item,
and his access revoked after his last day — before attendance and payroll for
September are computed, so he is not counted absent or paid for it. His full
and final settlement follows payroll (`settlement`).
"""

from __future__ import annotations

from datetime import date

from .api import Demo, SeedError

FLOW = "exits"

TEMPLATES = (
    # department, description, owner role, named owner, order
    ("IT", "Laptop, charger and accessories returned; accounts disabled", "named_user", "manoj", 1),
    ("Engineering", "Knowledge transfer and handover notes complete", "reporting_manager", None, 2),
    ("Finance", "No dues: advances settled, reimbursements closed", "named_user", "suresh", 3),
    ("HR", "ID card returned and exit interview held", "hr", None, 4),
)


async def setup(demo: Demo) -> None:
    priya = demo["priya"]
    existing = {t["description"] for t in await priya.get("/exits/clearance-templates")}
    for department, description, role, owner, order in TEMPLATES:
        if description in existing:
            continue
        await priya.post(
            "/exits/clearance-templates",
            {"department": department, "description": description, "is_mandatory": True,
             "display_order": order, "owner_role": role,
             "owner_user_id": str(demo.people[owner].user_id) if owner else None},
        )
    await priya.put("/exits/policy", {"letters_require_settlement": True})
    demo.note(FLOW, f"{len(TEMPLATES)}-step clearance checklist; letters wait for the F&F")


async def ravi(demo: Demo) -> None:
    """Resigned 31 July, last day 31 August, notice shortfall waived."""
    priya = demo["priya"]
    record = await priya.post(
        "/exits",
        {"employee_id": demo.emp("ravi"), "exit_type": "resignation",
         "resignation_date": "2026-07-31", "requested_last_working_day": "2026-08-31",
         "approved_last_working_day": "2026-08-31", "notice_period_days": 60,
         "reason": "Moving to a product company in Hyderabad, closer to family"},
    )
    demo.ids["exit_ravi"] = record["id"]
    if record["notice_shortfall_days"]:
        await priya.post(
            f"/exits/{record['id']}/waive-notice",
            {"reason": "Handover finished early; Vikram agreed to release him on 31 August."},
        )

    # His kit comes back on his last day. HR records the return; Manoj, the
    # categories' custodian, signs off the clearance item it stands for.
    mine = await demo["ravi"].get("/assets/me")
    for item in mine["current"]:
        await demo["karthik"].post(
            f"/assets/assignments/{item['id']}/return",
            {"condition_in": "good", "notes": "Returned on the last working day",
             "next_status": "in_stock"},
        )
    for item in await priya.get(f"/exits/{record['id']}/clearance"):
        if item["status"] != "pending":
            continue
        owner = {
            "reporting_manager": "vikram", "hr": "karthik",
        }.get(item["owner_role"]) or _key_for(demo, item.get("owner_user_id"))
        await demo[owner].patch(
            f"/exits/clearance/{item['id']}",
            {"outcome": "cleared", "notes": "Done on 31 Aug"},
        )
    await priya.post(f"/exits/{record['id']}/revoke-access")
    after = await priya.get(f"/exits/{record['id']}")
    if after["clearance_pending_mandatory"]:
        raise SeedError(f"Ravi still has {after['clearance_pending_mandatory']} clearance items")
    demo.note(FLOW, "Ravi: resignation recorded, notice waived, kit returned, cleared, "
                    "access revoked")


def _key_for(demo: Demo, user_id: str | None) -> str:
    for key, person in demo.people.items():
        if person.user_id and str(person.user_id) == str(user_id):
            return key
    raise SeedError(f"no demo person has user id {user_id}")


async def neha(demo: Demo) -> None:
    """Resigned this month; Rahul accepts; clearance is under way."""
    record = await demo["neha"].post(
        "/exits/resign",
        {"reason": "Relocating to Pune after marriage",
         "requested_last_working_day": "2026-12-04",
         "comments": "Happy to help hire and train my replacement."},
    )
    await demo["rahul"].post(
        f"/exits/{record['id']}/accept",
        {"approved_last_working_day": "2026-12-04",
         "note": "Sorry to see you go, Neha. Let's plan the account handover."},
    )
    demo.ids["exit_neha"] = record["id"]
    demo.note(FLOW, "Neha: resigned, accepted by Rahul, last day 4 December, clearance open")


async def settlement(demo: Demo) -> None:
    """Ravi's full and final: encashment from his balance, then approve and pay."""
    exit_id = demo.ids["exit_ravi"]
    karthik, priya = demo["karthik"], demo["priya"]
    encashed = await karthik.post(f"/encashment/exits/{exit_id}")
    sources = await karthik.get(f"/exits/{exit_id}/settlement/sources")
    if sources["encashable_leave"]:
        raise SeedError(f"leave still shows as encashable: {sources['encashable_leave']}")
    body: dict = {"final_month_salary": "0", "final_month_basis": ""}
    for suggestion in sources["suggestions"]:
        body[suggestion["field"]] = str(suggestion["amount"])
        body[suggestion["basis_field"]] = suggestion["basis"]
    body["final_tds"] = "0"
    body["tax_basis"] = (
        "Projected income for 2026-27 is within the section 87A rebate under the new "
        "regime; nothing further to deduct."
    )
    settled = await karthik.post(f"/exits/{exit_id}/settlement", body)
    await karthik.refused("POST", f"/exits/settlements/{settled['id']}/approve", status=403)
    await priya.post(f"/exits/settlements/{settled['id']}/approve")
    # Paid the day it is approved, within 45 days of his last working day.
    await priya.post(f"/exits/settlements/{settled['id']}/mark-paid",
                     {"paid_on": str(date.today()), "payment_reference": "NEFT-HDFC-FNF-0074210"})
    await priya.post(f"/exits/settlements/{settled['id']}/statement")
    for kind in ("relieving", "experience"):
        await priya.post(f"/exits/{exit_id}/letters", {"kind": kind})
    days = ", ".join(f"{row['days_approved']} days of {row['leave_type_code']}" for row in encashed)
    demo.note(FLOW, f"Ravi's F&F: encashed {days}; net {settled['net_amount']}; approved, paid, "
                    "statement and relieving and experience letters issued")
