"""Expenses: the policy, trips with advances, claims through manager and finance, GST.

Finance review is HR's queue (Priya and Karthik). Approved claims are paid in
the next payroll run, which is October's.
"""

from __future__ import annotations

from .api import Demo, SeedError
from .payroll import pdf

FLOW = "expenses"

COMPANY_GSTIN = {"KA": "29AAQCA7421K1Z6", "MH": "27AAQCA7421K1ZA"}

RULES = (
    # category, tier, limit type, limit, violation, receipt, threshold, pre-approval, per diem
    ("airfare", "a", "per_trip", "20000", "require_justification", True, "0", True, False),
    ("airfare", "b", "per_trip", "15000", "require_justification", True, "0", True, False),
    ("hotel", "a", "per_day", "6000", "flag", True, "0", False, False),
    ("hotel", "b", "per_day", "4000", "flag", True, "0", False, False),
    ("meals", "a", "per_day", "1500", "require_justification", True, "500", False, False),
    ("meals", "b", "per_day", "1000", "require_justification", True, "500", False, False),
    ("local_travel", "a", "per_claim", "2000", "flag", True, "300", False, False),
    ("local_travel", "b", "per_claim", "1500", "flag", True, "300", False, False),
    ("client_entertainment", "a", "per_claim", "5000", "require_justification", True, "0",
     False, False),
    ("office_supplies", "a", "per_claim", "3000", "block", True, "0", False, False),
    ("mileage", "a", "per_month", "6000", "flag", False, "0", False, False),
    ("per_diem", "b", "per_day", "1200", "flag", False, "0", False, True),
    ("training", "international", "per_claim", "50000", "flag", True, "0", False, False),
)


async def setup(demo: Demo) -> None:
    priya = demo["priya"]
    await priya.put("/expenses/settings", {"conversion_convention": "transaction_date",
                                           "rate_source": "RBI reference rate",
                                           "advance_block_after_days": 30})
    for code in ("BLR", "MUM"):
        await priya.put("/expenses/city-tiers",
                        {"location_id": demo.ids["location"][code], "city_tier": "a",
                         "note": "Metro office"})
    policies = await priya.get("/expenses/policies")
    if not policies:
        await priya.post(
            "/expenses/policies",
            {"name": "Travel and expense policy FY 2026-27", "effective_from": "2026-04-01",
             "description": "Limits by city tier. Airfare needs a travel request first.",
             "rules": [
                 {"category_key": c, "city_tier": t, "limit_type": lt, "limit_amount": amt,
                  "violation_action": va, "requires_receipt": rr, "receipt_threshold": th,
                  "requires_pre_approval": pre, "is_per_diem": pd}
                 for c, t, lt, amt, va, rr, th, pre, pd in RULES
             ]},
        )
    for vehicle, rate in (("car", "9"), ("two_wheeler", "4")):
        await priya.post("/expenses/mileage-rates",
                         {"vehicle_type": vehicle, "rate_per_km": rate,
                          "effective_from": "2026-04-01", "tolerance_percent": "10"})
    for grade, amount in (("P1", "900"), ("P2", "1000"), ("P3", "1200"), ("M3", "1800")):
        await priya.post("/expenses/per-diem-rates",
                         {"city_tier": "b", "grade": grade, "amount": amount,
                          "effective_from": "2026-04-01"})
    await priya.post("/expenses/exchange-rates",
                     {"from_currency": "USD", "rate": "83.92", "quote_date": "2026-09-01",
                      "source": "RBI reference rate"})

    # GST: the company's registrations, and what the law blocks from credit.
    for gstin in COMPANY_GSTIN.values():
        await priya.put("/gst/entity-registrations",
                        {"legal_entity_id": demo.ids["entity"], "gstin": gstin})
    for category, reason in (
        ("meals", "Food and beverages are blocked credits"),
        ("client_entertainment", "Outdoor catering and entertainment are blocked"),
    ):
        await priya.put("/gst/blocked-categories",
                        {"category_key": category, "reason": reason, "reviewed": True,
                         "reviewed_by": "Suresh Pillai (Finance)", "reviewed_on": "2026-04-02"})
    demo.note(FLOW, f"policy with {len(RULES)} rules, mileage, per diem and FX rates; "
                    "2 GSTINs; 2 blocked-credit categories")


async def _receipt(demo: Demo, who: str, name: str) -> str:
    stored = await demo[who].post("/expenses/receipts", files={"file": pdf(name)})
    return stored["id"]


async def _claim(demo: Demo, who: str, body: dict, *, receipt: str | None = None,
                 justification: str | None = None, submit: bool = True) -> dict:
    if receipt:
        body = {**body, "has_receipt": True,
                "receipt_document_id": await _receipt(demo, who, receipt)}
    captured = await demo[who].post("/expenses/claims", body)
    claim = {**captured, "id": captured["claim_id"]}
    if submit:
        justified = captured["verdict"]["kind"] == "needs_justification" and justification
        if not captured["may_submit"] and not justified:
            raise SeedError(f"{who}'s {body['category_key']} claim was blocked: "
                            f"{captured['verdict']}")
        submitted = await demo[who].post(f"/expenses/claims/{claim['id']}/submit",
                                         {"justification": justification})
        claim = {**submitted, "id": submitted["claim_id"]}
    return claim


async def _decide(demo: Demo, claim: dict, *, manager: str | None, finance: str | None,
                  manager_ok: bool = True, finance_ok: bool = True, note: str = "") -> dict:
    if manager:
        claim = await demo[manager].post(f"/expenses/claims/{claim['id']}/manager-decision",
                                         {"approve": manager_ok, "note": note or "Approved"})
    if finance and claim["status"] == "submitted":
        claim = await demo[finance].post(f"/expenses/claims/{claim['id']}/decision",
                                         {"approve": finance_ok, "note": "Checked receipts"})
    return claim


async def run(demo: Demo) -> None:
    priya, karthik = demo["priya"], demo["karthik"]

    # ── A completed trip: Pooja at the Bengaluru sales training ────────────
    trip = await demo["pooja"].post(
        "/expenses/travel-requests",
        {"purpose": "Quarterly sales training at Bengaluru HQ", "from_date": "2026-09-15",
         "to_date": "2026-09-17", "estimated_cost": "26000", "advance_requested": "10000",
         "destinations": [{"city": "Bengaluru", "city_tier": "a"}]},
    )
    trip = await demo["rahul"].post(f"/expenses/travel-requests/{trip['id']}/manager-decision",
                                    {"approve": True, "note": "Yes, mandatory training"})
    if trip["status"] == "submitted":
        trip = await priya.post(f"/expenses/travel-requests/{trip['id']}/decision",
                                {"approve": True, "note": "Advance approved"})
    advance = await priya.post("/expenses/advances",
                               {"travel_request_id": trip["id"], "amount": "10000",
                                "issued_on": "2026-09-11"})
    report = await demo["pooja"].post("/expenses/reports",
                                      {"title": "Bengaluru sales training, September",
                                       "travel_request_id": trip["id"]})
    lines = (
        ({"category_key": "airfare", "city_tier": "a", "amount": "8450",
          "incurred_on": "2026-09-15", "description": "IndiGo BOM-BLR-BOM"}, "indigo.pdf"),
        ({"category_key": "hotel", "city_tier": "a", "amount": "9600",
          "incurred_on": "2026-09-17", "description": "Lemon Tree, Whitefield, 2 nights"},
         "hotel.pdf"),
        ({"category_key": "meals", "city_tier": "a", "amount": "1380",
          "incurred_on": "2026-09-16", "description": "Dinner with the training batch"},
         "dinner.pdf"),
        ({"category_key": "local_travel", "city_tier": "a", "amount": "1240",
          "incurred_on": "2026-09-17", "description": "Airport cabs"}, "uber.pdf"),
    )
    claims = []
    for body, receipt in lines:
        claims.append(await _claim(demo, "pooja",
                                   {**body, "travel_request_id": trip["id"],
                                    "report_id": report["id"]},
                                   receipt=receipt, submit=False))
    await demo["pooja"].post(f"/expenses/reports/{report['id']}/submit", {"justifications": {}})
    for claim in claims:
        await _decide(demo, {**claim, "status": "pending_manager"}, manager="rahul",
                      finance="karthik")
    settled = await priya.post(f"/expenses/advances/{advance['id']}/settle",
                               {"settled_on": "2026-09-25"})
    demo.note(FLOW, f"Pooja's trip: request, advance, 4 claims in a report, approved, advance "
                    f"settled ({settled.get('outcome', settled.get('status', 'settled'))})")

    # ── A trip ahead: Rahul to Delhi, approved with an advance ─────────────
    trip = await demo["rahul"].post(
        "/expenses/travel-requests",
        {"purpose": "Renewal negotiation with Tata Capital, Delhi", "from_date": "2026-10-26",
         "to_date": "2026-10-28", "estimated_cost": "42000", "advance_requested": "15000",
         "destinations": [{"city": "New Delhi", "city_tier": "a"}]},
    )
    trip = await demo["ananya"].post(f"/expenses/travel-requests/{trip['id']}/manager-decision",
                                     {"approve": True, "note": "Go for it"})
    if trip["status"] == "submitted":
        trip = await priya.post(f"/expenses/travel-requests/{trip['id']}/decision",
                                {"approve": True, "note": "Book through the travel desk"})
    await priya.post("/expenses/advances", {"travel_request_id": trip["id"], "amount": "15000",
                                            "issued_on": "2026-10-07"})
    await demo["vikram"].post(
        "/expenses/travel-requests",
        {"purpose": "Campus hiring drive at IIT Delhi", "from_date": "2026-11-05",
         "to_date": "2026-11-06", "estimated_cost": "30000", "advance_requested": "0",
         "destinations": [{"city": "New Delhi", "city_tier": "a"}]},
    )
    demo.note(FLOW, "Rahul's Delhi trip approved with a 15,000 advance; Vikram's awaits Ananya")

    # ── Everyday claims in every state ─────────────────────────────────────
    c = await _claim(demo, "meera", {"category_key": "meals", "city_tier": "a",
                                     "amount": "3200", "incurred_on": "2026-09-30",
                                     "description": "Team lunch after the 4.2 release"},
                     receipt="lunch.pdf",
                     justification="Release celebration for six people; Vikram agreed")
    await _decide(demo, c, manager="vikram", finance="karthik")
    c = await _claim(demo, "aditya", {"category_key": "local_travel", "city_tier": "a",
                                      "amount": "640", "incurred_on": "2026-09-29",
                                      "description": "Cab to the Whitefield data centre"},
                     receipt="ola.pdf")
    await _decide(demo, c, manager="vikram", finance=None)
    c = await _claim(demo, "aditya", {"category_key": "mileage", "city_tier": "a",
                                      "kind": "mileage", "amount": "152",
                                      "incurred_on": "2026-10-05", "distance_km": "38",
                                      "route_km": "38", "vehicle_type": "two_wheeler",
                                      "description": "To the vendor office and back"})
    c = await _claim(demo, "nikhil", {"category_key": "office_supplies", "city_tier": "a",
                                      "amount": "2450", "incurred_on": "2026-10-03",
                                      "description": "Replacement stylus for the drawing tablet"},
                     receipt="stylus.pdf")
    c = await _claim(demo, "imran", {"category_key": "local_travel", "city_tier": "a",
                                     "amount": "3200", "incurred_on": "2026-09-24",
                                     "description": "Cabs for client meetings, Andheri to Thane"},
                     receipt="cabs.pdf")
    await _decide(demo, c, manager="rahul", finance=None, manager_ok=False,
                  note="Use the office pool cab for Thane; resubmit what is not covered")
    c = await _claim(demo, "rahul", {"category_key": "client_entertainment", "city_tier": "a",
                                     "amount": "4800", "incurred_on": "2026-09-18",
                                     "description": "Dinner with the Tata Capital team"},
                     receipt="dinner-tc.pdf", justification="Renewal kickoff with the client")
    await _decide(demo, c, manager="ananya", finance="priya")
    c = await _claim(demo, "sneha", {"category_key": "training", "city_tier": "international",
                                     "amount": "199", "currency": "USD",
                                     "incurred_on": "2026-09-02",
                                     "description": "React Summit online ticket"},
                     receipt="react-summit.pdf")
    await _decide(demo, c, manager="vikram", finance="karthik")
    await _claim(demo, "divya", {"category_key": "local_travel", "city_tier": "a",
                                 "amount": "380", "incurred_on": "2026-10-06",
                                 "description": "Auto to the bank for the GST filing"},
                 submit=False)
    queue = await priya.get("/expenses/review-queue")
    demo.note(FLOW, "claims: paid-ready, pending manager, pending finance, rejected, a USD "
                    f"claim, a draft; finance queue holds {len(_items(queue))}")

    # ── GST input credit on the trip's invoices ─────────────────────────────
    hotel = await karthik.post(
        "/gst/credits",
        {"vendor_gstin": "29AAECR4849R1ZD", "invoice_number": "LTW/26/09/4471",
         "invoice_date": "2026-09-17", "category_key": "hotel", "taxable_value": "8571.43",
         "cgst": "514.29", "sgst": "514.29", "supplier_state_code": "29",
         "place_of_supply_state_code": "29", "hsn_sac": "996311",
         "legal_entity_id": demo.ids["entity"], "employee_id": demo.emp("pooja"),
         "expense_claim_id": claims[1]["id"]},
    )
    await karthik.post(
        "/gst/credits",
        {"vendor_gstin": "07AAACI1681G1ZR", "invoice_number": "6E-26-BLR-88213",
         "invoice_date": "2026-09-15", "category_key": "airfare", "taxable_value": "8047.62",
         "igst": "402.38", "supplier_state_code": "07", "place_of_supply_state_code": "29",
         "hsn_sac": "996425", "legal_entity_id": demo.ids["entity"],
         "employee_id": demo.emp("pooja"), "expense_claim_id": claims[0]["id"]},
    )
    # Credits the engine could not decide wait for a person.
    for item in (await karthik.get("/gst/review-queue"))["lines"]:
        await demo["priya"].post(
            f"/gst/credits/{item['credit_id']}/review",
            {"decision": "eligible", "rationale": "Business travel; invoice in the company's name",
             "decided_on": "2026-10-05"},
        )
    await karthik.post(
        "/gst/gstr2b-imports",
        {"period_label": "2026-09", "imported_on": "2026-10-14",
         "legal_entity_id": demo.ids["entity"], "source_note": "GSTR-2B download, September",
         "lines": [
             {"vendor_gstin": "29AAECR4849R1ZD", "vendor_name": "Lemon Tree Hotels",
              "invoice_number": "LTW/26/09/4471", "invoice_date": "2026-09-17",
              "taxable_value": "8571.43", "cgst": "514.29", "sgst": "514.29"},
             {"vendor_gstin": "07AAACI1681G1ZR", "vendor_name": "InterGlobe Aviation",
              "invoice_number": "6E-26-BLR-88213", "invoice_date": "2026-09-15",
              "taxable_value": "8047.62", "igst": "402.38"},
         ]},
    )
    await karthik.post("/gst/reconciliations",
                       {"period_label": "2026-09", "period_start": "2026-09-01",
                        "period_end": "2026-09-30", "legal_entity_id": demo.ids["entity"]})
    if hotel["assessment"].get("eligibility") == "ineligible":
        raise SeedError(f"the hotel credit was judged ineligible: {hotel['assessment']}")
    demo.note(FLOW, "GST: 2 invoices captured, reviewed eligible, matched to GSTR-2B for September")


def _items(payload) -> list:
    if isinstance(payload, dict):
        for key in ("items", "claims", "credits", "rows"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return []
    return list(payload or [])
