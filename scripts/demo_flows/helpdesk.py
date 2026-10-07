"""Helpdesk: the standard categories with owners, and tickets in every state."""

from __future__ import annotations

from .api import Demo

FLOW = "helpdesk"

OWNERS = {"IT & access": "manoj", "Payroll & salary query": "karthik",
          "Reimbursement": "karthik", "ID card": "manoj"}


async def setup(demo: Demo) -> None:
    priya = demo["priya"]
    categories = {c["name"]: c for c in await priya.get("/helpdesk/categories",
                                                        include_inactive=True)}
    if not categories:
        await priya.post("/helpdesk/categories/standard")
        categories = {c["name"]: c for c in await priya.get("/helpdesk/categories")}
    for name, owner in OWNERS.items():
        await priya.patch(f"/helpdesk/categories/{categories[name]['id']}",
                          {"owner_user_id": str(demo.people[owner].user_id)})
    demo.ids["helpdesk_category"] = {name: c["id"] for name, c in categories.items()}
    demo.note(FLOW, f"{len(categories)} standard categories; IT, ID cards, payroll and "
                    "reimbursements have named owners")


async def _ticket(demo: Demo, who: str, category: str, subject: str, description: str,
                  priority: str = "medium") -> dict:
    return await demo[who].post(
        "/helpdesk/tickets",
        data={"category_id": demo.ids["helpdesk_category"][category], "subject": subject,
              "description": description, "priority": priority},
    )


async def _comment(api, ticket_id: str, body: str, internal: bool = False) -> None:
    await api.post(f"/helpdesk/tickets/{ticket_id}/comments",
                   data={"body": body, "is_internal": str(internal).lower()})


async def run(demo: Demo) -> None:
    karthik, manoj, priya = demo["karthik"], demo["manoj"], demo["priya"]

    t = await _ticket(demo, "meera", "Payroll & salary query", "Form 16 for FY 2025-26",
                      "I cannot find my Form 16 for last year on the portal. I need it to "
                      "file my return.")
    await karthik.patch(f"/helpdesk/tickets/{t['id']}", {"status": "in_progress"})
    await _comment(karthik, t["id"], "Last year's Form 16 was issued by our previous payroll "
                   "provider. I have attached it to your document vault under Tax.")
    await karthik.patch(f"/helpdesk/tickets/{t['id']}",
                        {"status": "resolved", "note": "Shared from the previous provider"})
    await demo["meera"].post(f"/helpdesk/tickets/{t['id']}/rate",
                             {"rating": 5, "comment": "Quick, thank you!"})

    t = await _ticket(demo, "imran", "Payroll & salary query",
                      "September salary credited short",
                      "My September salary is about 1,300 less than August.", "high")
    await karthik.patch(f"/helpdesk/tickets/{t['id']}", {"status": "in_progress"})
    await _comment(karthik, t["id"], "8 and 9 September were leave without pay, approved by "
                   "Rahul. Two days of pay were deducted; see the LOP line on your payslip.")
    await karthik.patch(f"/helpdesk/tickets/{t['id']}", {"status": "resolved"})
    await demo["imran"].post(f"/helpdesk/tickets/{t['id']}/reopen",
                             {"reason": "Can those two days be adjusted against sick leave?"})
    await _comment(karthik, t["id"], "Leave already taken as LWP can be converted only by "
                   "your manager cancelling it and you applying sick leave. I have asked Rahul.")
    await _comment(karthik, t["id"], "Rahul prefers to keep it as LWP; exam leave is unpaid "
                   "under the policy.", internal=True)

    t = await _ticket(demo, "rohan", "IT & access", "Laptop battery drains in two hours",
                      "The MacBook battery goes from 100% to 10% in about two hours.")
    await manoj.patch(f"/helpdesk/tickets/{t['id']}", {"status": "in_progress"})
    await _comment(manoj, t["id"], "Please send the battery cycle count from System "
                   "Information > Power, and drop the laptop at the IT desk on Friday.")
    await manoj.patch(f"/helpdesk/tickets/{t['id']}", {"status": "waiting_on_employee"})

    t = await _ticket(demo, "pooja", "Letters & documents", "Address proof letter",
                      "I need an address proof letter for my passport renewal.")
    await karthik.post(f"/helpdesk/tickets/{t['id']}/assign",
                       {"assignee_user_id": str(demo.people["karthik"].user_id)})
    await karthik.patch(f"/helpdesk/tickets/{t['id']}", {"status": "in_progress"})
    await _comment(karthik, t["id"], "The letter is issued; download it from My documents.")
    await karthik.patch(f"/helpdesk/tickets/{t['id']}", {"status": "resolved"})
    await karthik.patch(f"/helpdesk/tickets/{t['id']}", {"status": "closed"})

    await _ticket(demo, "lakshmi", "Policy question", "Cab for the evening support shift",
                  "My evening shift ends at 10 pm. Is there a company cab drop?")
    t = await _ticket(demo, "sneha", "Reimbursement", "Internet claim rejected",
                      "My September internet claim was rejected because the bill is in my "
                      "landlord's name. What should I upload?")
    await karthik.patch(f"/helpdesk/tickets/{t['id']}", {"status": "in_progress"})

    t = await _ticket(demo, "nikhil", "Grievance (confidential)",
                      "Comments in design reviews",
                      "I want to raise, in confidence, a pattern of dismissive comments in "
                      "design reviews.", "high")
    await priya.post(f"/helpdesk/tickets/{t['id']}/assign",
                     {"assignee_user_id": str(demo.people["priya"].user_id)})
    await priya.patch(f"/helpdesk/tickets/{t['id']}", {"status": "in_progress"})
    await _comment(priya, t["id"], "Thank you for raising this. Can we talk on Thursday at "
                   "4 pm? Only you and I can see this ticket.")

    t = await _ticket(demo, "divya", "ID card", "Lost my ID card",
                      "I lost my ID card on the way home yesterday.", "low")
    await demo["divya"].post(f"/helpdesk/tickets/{t['id']}/cancel",
                             {"reason": "Found it in the car."})
    summary = await priya.get("/helpdesk/queue/summary")
    demo.note(FLOW, f"8 tickets: resolved and rated, reopened, waiting on employee, closed, "
                    f"new, in progress, a confidential grievance, cancelled ({_short(summary)})")


def _short(summary) -> str:
    if isinstance(summary, dict):
        keys = [k for k in ("open", "in_progress", "overdue", "unassigned") if k in summary]
        return ", ".join(f"{k} {summary[k]}" for k in keys) or "queue summarised"
    return "queue summarised"
