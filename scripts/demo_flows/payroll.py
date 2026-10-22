"""Payroll: the structure, salaries, statutory rules, tax declarations, and the runs.

Setup is HR's: a CTC structure, everyone's salary with the April 2026 increment,
and the statutory rule sets — each entered by Priya and attested by Ananya (both
admins; rule sets are admin-only), because whoever enters a rate cannot approve
it. Employees elect a tax regime and declare investments; some upload proofs
that HR verifies. Loans and reimbursements are applied for, approved and paid
through payroll.

Then April to September are run in full: computed by Priya, approved by
Karthik, payslips published, the bank file prepared, approved, released and
reconciled, the run marked paid and the monthly returns generated and filed.
October's run is opened; reimbursement claims made this month wait for it, and
it waits for October's attendance to be locked.
"""

from __future__ import annotations

from .api import Demo, SeedError
from .cast import account_for

FLOW = "payroll"
FY = "2026-27"
#: Reimbursements, filings and statutory bonus name the year in full.
FY_LONG = "2026-2027"

SOURCE = (
    "Demo company. Rates as notified for FY 2026-27 and entered for demonstration; "
    "confirm against the official notification before using them for a real payroll."
)

COMPONENTS = (
    # code, name, calculation, base, value, formula, pf, esi, order
    ("BASIC", "Basic salary", "percentage_of", "CTC", "40", None, True, True, 1),
    ("HRA", "House rent allowance", "percentage_of", "BASIC", "50", None, False, True, 2),
    ("LTA", "Leave travel allowance", "formula", None, None, "min(CTC * 0.05, 30000)",
     False, True, 3),
    ("SPECIAL", "Special allowance", "balance", None, None, None, False, True, 9),
)

TDS_RULES = {
    "financial_year_start_month": 4,
    "cess_percent": 4,
    "regime_switches_per_year": 1,
    "landlord_pan_threshold_annual": 100000,
    "regimes": {
        "new": {
            "slabs": [
                {"up_to": 400000, "rate": 0}, {"up_to": 800000, "rate": 5},
                {"up_to": 1200000, "rate": 10}, {"up_to": 1600000, "rate": 15},
                {"up_to": 2000000, "rate": 20}, {"up_to": 2400000, "rate": 25},
                {"up_to": None, "rate": 30},
            ],
            "standard_deduction": 75000,
            "rebate": {"income_limit": 1200000, "max_rebate": 60000, "marginal_relief": True},
            "deductions_allowed": ["80CCD2"],
        },
        "old": {
            "slabs": [
                {"up_to": 250000, "rate": 0}, {"up_to": 500000, "rate": 5},
                {"up_to": 1000000, "rate": 20}, {"up_to": None, "rate": 30},
            ],
            "standard_deduction": 50000,
            "rebate": {"income_limit": 500000, "max_rebate": 12500},
            "deductions_allowed": ["HRA", "LTA", "PT", "80C", "80CCD1B", "80CCD2", "80D",
                                   "24B", "80E", "80G", "80TTA"],
        },
    },
    "section_limits": {
        "80C": {"ceiling": 150000},
        "80CCD1B": {"ceiling": 50000},
        "80D": {"ceiling_by_age": [{"max_age": 59, "ceiling": 25000},
                                   {"max_age": None, "ceiling": 50000}]},
        "24B": {"ceiling": 200000},
        "80TTA": {"ceiling": 10000},
    },
    "hra": {"metro_percent": 50, "non_metro_percent": 40, "rent_less_basic_percent": 10},
    "surcharge_bands": [
        {"above": 5000000, "up_to": 10000000, "rate": 10},
        {"above": 10000000, "up_to": 20000000, "rate": 15},
        {"above": 20000000, "up_to": None, "rate": 25},
    ],
}

RULE_SETS = (
    ("pf", "IN", {"wage_ceiling": 15000, "employee_rate": 12, "employer_rate": 12,
                  "eps_wage_ceiling": 15000, "edli_wage_ceiling": 15000, "eps_rate": 8.33,
                  "apply_wage_ceiling": True}),
    ("esi", "IN", {"wage_threshold": 21000, "employee_rate": 0.75, "employer_rate": 3.25,
                   "contribution_periods": [
                       {"start_month": 4, "end_month": 9, "label": "April to September"},
                       {"start_month": 10, "end_month": 3, "label": "October to March"}]}),
    ("tds", "IN", TDS_RULES),
    # Karnataka (Act 33 of 2025, from 1 April 2025): 300 in February, 2,500 a year.
    ("professional_tax", "KA", {"slabs": [{"up_to": 24999, "amount": 0},
                                          {"up_to": None, "amount": 200,
                                           "month_amounts": {"2": 300}}]}),
    # Maharashtra: 300 in February makes up the annual 2,500; women are exempt up to 25,000.
    ("professional_tax", "MH", {"slabs": [{"up_to": 7500, "amount": 0},
                                          {"up_to": 10000, "amount": 175},
                                          {"up_to": None, "amount": 200,
                                           "month_amounts": {"2": 300}}],
                                "slabs_by_gender": {"female": [
                                    {"up_to": 25000, "amount": 0},
                                    {"up_to": None, "amount": 200,
                                     "month_amounts": {"2": 300}}]}}),
    ("labour_welfare_fund", "KA", {"applicable": True, "deduction_months": [12],
                                   "employee_amount": 50, "employer_amount": 100}),
    ("labour_welfare_fund", "MH", {"applicable": True, "deduction_months": [6, 12],
                                   "employee_amount": 25, "employer_amount": 75}),
    ("bonus", "IN", {"eligibility_wage_ceiling": 21000, "calculation_wage_ceiling": 7000,
                     "minimum_percentage": 8.33, "maximum_percentage": 20,
                     "minimum_service_days": 30}),
)

#: The salary before the April 2026 increment, for people here before then.
INCREMENT = 1.10

REGIMES = {"priya": "old", "arjun": "old", "suresh": "old", "rahul": "old", "kavya": "old"}

DECLARATIONS = (
    # who, section, sub-category, amount, description
    ("priya", "80C", "PPF", 100000, "Public Provident Fund, SBI"),
    ("priya", "80C", "ELSS", 50000, "Mirae Asset ELSS Tax Saver"),
    ("priya", "80D", "Self and family", 25000, "Star Health family floater"),
    ("arjun", "80C", "EPF and life insurance", 150000, "LIC term plan and EPF"),
    ("arjun", "24B", "Home loan interest", 200000, "HDFC home loan, self-occupied flat"),
    ("arjun", "80CCD1B", "NPS", 50000, "NPS Tier I"),
    ("suresh", "80C", "PPF and tuition fees", 150000, "Children's school fees and PPF"),
    ("suresh", "80D", "Parents", 25000, "Parents' health insurance"),
    ("rahul", "80C", "ELSS", 150000, "Axis ELSS"),
    ("kavya", "80C", "PPF", 80000, "Public Provident Fund"),
    ("kavya", "80E", "Education loan interest", 42000, "MBA loan interest, SBI"),
)

RENT = {
    # who: (monthly rent, landlord, landlord PAN, address, metro)
    "priya": (32000, "K. Shankar", "AKZPS4821M", "HSR Layout, Bengaluru", False),
    "suresh": (28000, "Lalitha Venkatesh", "BCDPV7712K", "Jayanagar, Bengaluru", False),
    "rahul": (58000, "Sanjay Mehra", "AFQPM3390L", "Bandra East, Mumbai", True),
}

RECEIPT_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def pdf(name: str) -> tuple[str, bytes, str]:
    return (name, RECEIPT_PDF, "application/pdf")


async def setup(demo: Demo) -> None:
    await _structure(demo)
    await _salaries(demo)
    await _rule_sets(demo)
    await _statutory(demo)
    await _tax(demo)
    await _loans(demo)
    await _reimbursement_components(demo)


async def _structure(demo: Demo) -> None:
    priya = demo["priya"]
    have = {c["code"] for c in await priya.get("/salary-components")}
    for code, name, calc, base, value, formula, pf, esi, order in COMPONENTS:
        if code in have:
            continue
        await priya.post(
            "/salary-components",
            {"code": code, "name": name, "component_type": "earning", "calculation_type": calc,
             "base_component_code": base, "value": value, "formula": formula,
             "counts_toward_pf": pf, "counts_toward_esi": esi, "display_order": order},
        )
    structures = {s["name"]: s for s in await priya.get("/salary-structures")}
    name = "Standard CTC - India"
    if name not in structures:
        structures[name] = await priya.post(
            "/salary-structures",
            {"name": name,
             "description": "Basic 40% of CTC, HRA half of basic, LTA up to 30,000, "
                            "the rest as special allowance.",
             "effective_from": "2021-04-01", "applicable_grades": [], "applicable_locations": [],
             "components": [{"component_code": c[0], "display_order": c[8]} for c in COMPONENTS]},
        )
    demo.ids["structure"] = structures[name]["id"]
    preview = await priya.get(f"/salary-structures/{demo.ids['structure']}/preview", ctc=228000)
    demo.note(FLOW, "CTC structure with 4 components "
                    f"(preview at 2.28 L: {_monthly(preview)} a month)")


def _monthly(preview) -> str:
    for key in ("monthly_total", "monthly_gross", "monthly_total_in_ctc"):
        if isinstance(preview, dict) and key in preview:
            return str(preview[key])
    return "ok"


async def _salaries(demo: Demo) -> None:
    revised = 0
    for key, person in demo.people.items():
        if not person.code or not person.ctc:
            continue
        hr = demo.hr_for(key)
        history = await hr.get(f"/employees/{demo.emp(key)}/salary/history")
        if history:
            continue
        joined = person.date_of_joining
        if joined < "2026-04-01":
            before = round(person.ctc / INCREMENT / 1000) * 1000
            await hr.post(
                f"/employees/{demo.emp(key)}/salary",
                {"structure_id": demo.ids["structure"], "ctc_annual": str(before),
                 "effective_from": max(joined, "2021-04-01"),
                 "revision_reason": "Salary at joining, migrated from the previous payroll"},
            )
            await hr.post(
                f"/employees/{demo.emp(key)}/salary",
                {"structure_id": demo.ids["structure"], "ctc_annual": str(person.ctc),
                 "effective_from": "2026-04-01", "revision_reason": "Annual increment, April 2026"},
            )
            revised += 1
        else:
            await hr.post(
                f"/employees/{demo.emp(key)}/salary",
                {"structure_id": demo.ids["structure"], "ctc_annual": str(person.ctc),
                 "effective_from": joined, "revision_reason": "Offer letter CTC"},
            )
    demo.note(FLOW, f"salaries for 22 people; {revised} with the April 2026 increment")


async def _rule_sets(demo: Demo) -> None:
    priya = demo["priya"]
    existing = {(r["rule_type"], r["jurisdiction"]) for r in await priya.get("/payroll/rule-sets")}
    for rule_type, jurisdiction, rules in RULE_SETS:
        if (rule_type, jurisdiction) in existing:
            continue
        created = await priya.post(
            "/payroll/rule-sets",
            {"rule_type": rule_type, "jurisdiction": jurisdiction, "effective_from": "2026-04-01",
             "rules": rules, "source_reference": SOURCE},
        )
        # The person who entered it cannot attest it.
        await priya.refused("POST", f"/payroll/rule-sets/{created['id']}/approve",
                            {"attest": True}, status=403)
        await demo["ananya"].step_up("payroll.rule_change")
        await demo["ananya"].post(f"/payroll/rule-sets/{created['id']}/approve",
                                  {"attest": True})
    demo.note(FLOW, f"{len(RULE_SETS)} statutory rule sets entered by Priya, attested by Ananya")


async def _statutory(demo: Demo) -> None:
    priya = demo["priya"]
    for key, body in (
        ("vikram", {"pf_on_actual_wage": True}),
        ("arjun", {"pf_on_actual_wage": True, "vpf_percentage": "5"}),
    ):
        await demo.hr_for(key).post(
            "/statutory/pf-elections",
            {"employee_id": demo.emp(key), "effective_from": "2026-04-01", **body},
        )
    # Anjali moved from the Mumbai office to Bengaluru in June.
    for location, since, reason in (
        ("MUM", "2023-01-09", "Joined in the Mumbai office"),
        ("BLR", "2026-06-01", "Transferred to Bengaluru HQ"),
    ):
        await priya.post(
            "/statutory/work-locations",
            {"employee_id": demo.emp("anjali"), "location_id": demo.ids["location"][location],
             "effective_from": since, "reason": reason},
        )
    profiles = await priya.get("/disbursement/bank-profiles")
    if not profiles:
        profiles = [await priya.post(
            "/disbursement/bank-profiles",
            {"legal_entity_id": demo.ids["entity"], "bank_name": "HDFC Bank",
             "account_number": "50200074210031", "ifsc": "HDFC0000523",
             "corporate_id": "ASKMIRADEMO", "file_format": "hdfc"},
        )]
    demo.ids["bank_profile"] = profiles[0]["id"]
    demo.note(FLOW, "PF on actual wage for 2, VPF for 1, Anjali's state transfer, "
                    "HDFC salary account")


async def _tax(demo: Demo) -> None:
    priya, karthik = demo["priya"], demo["karthik"]
    await priya.put("/tax/windows", {"financial_year": FY, "window_type": "declaration",
                                     "opens_at": "2026-04-01", "closes_at": "2026-12-31",
                                     "reminder_days_before": [14, 3]})
    # Proofs collected early this year, from October to the end of January.
    await priya.put("/tax/windows", {"financial_year": FY, "window_type": "proof",
                                     "opens_at": "2026-10-01", "closes_at": "2027-01-31",
                                     "reminder_days_before": [14, 3]})
    for key, person in demo.people.items():
        if not person.code or key == "ravi":
            continue
        regime = REGIMES.get(key, "new")
        await demo[key].post("/tax/regime", {"financial_year": FY, "regime": regime})

    declared: dict[tuple[str, str], dict] = {}
    for who, section, sub, amount, description in DECLARATIONS:
        row = await demo[who].put(
            "/tax/declarations",
            {"financial_year": FY, "section": section, "sub_category": sub,
             "declared_amount": str(amount), "description": description},
        )
        await demo[who].post(f"/tax/declarations/{row['id']}/submit")
        declared[(who, f"{section}:{sub}")] = row
    for who, (rent, landlord, pan, address, metro) in RENT.items():
        await demo[who].put(
            "/tax/house-rent",
            {"financial_year": FY, "landlord_name": landlord, "landlord_pan": pan,
             "property_address": address, "is_metro": metro,
             "monthly_rent": [{"month": m, "year": 2026 if m >= 4 else 2027, "rent": str(rent)}
                              for m in (4, 5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3)]},
        )

    # Proofs: one verified, one partly accepted, one rejected, two waiting.
    proofs = (
        ("arjun", "24B:Home loan interest", 200000, "verified", 200000,
         "Interest certificate from HDFC for FY 2026-27 (provisional)"),
        ("priya", "80C:PPF", 100000, "partially_accepted", 60000,
         "Passbook shows 60,000 deposited so far; upload the rest after deposit"),
        ("suresh", "80D:Parents", 25000, "rejected", 0,
         "The policy schedule is for FY 2025-26; upload this year's renewal"),
        ("rahul", "80C:ELSS", 150000, None, 0, ""),
        ("kavya", "80E:Education loan interest", 42000, None, 0, ""),
    )
    for who, key, amount, decision, accepted, remark in proofs:
        declaration = declared[(who, key)]
        proof = await demo[who].post(
            "/tax/proofs",
            data={"declaration_id": declaration["id"], "proof_amount": str(amount)},
            files={"file": pdf(f"{key.split(':')[0]}-proof.pdf")},
        )
        if decision:
            await karthik.post(
                f"/tax/proofs/{proof['id']}/verify",
                {"decision": decision, "accepted_amount": str(accepted), "remarks": remark},
            )
    projection = await demo["arjun"].get("/tax/projection", financial_year=FY)
    demo.note(FLOW, f"tax: 5 on the old regime, {len(DECLARATIONS)} declarations, 3 rent "
                    f"records, 5 proofs; Arjun's projected tax "
                    f"{projection.get('annual_tax_liability', '?')}")


async def _loans(demo: Demo) -> None:
    # Loans are decided and paid out by admins: Priya approves, Ananya disburses.
    priya = demo["priya"]
    schemes = {s["name"]: s for s in await priya.get("/loans/schemes")}
    for name, body in (
        ("Salary advance", {"loan_type": "salary_advance", "interest_type": "interest_free",
                            "max_tenure_months": 3, "max_amount_as_multiple_of_salary": "1",
                            "eligibility_min_service_months": 3}),
        ("Personal loan", {"loan_type": "company_loan", "interest_type": "reducing",
                           "interest_rate": "7", "max_tenure_months": 24,
                           "max_amount": "300000", "eligibility_min_service_months": 12,
                           "minimum_net_pay": "15000"}),
    ):
        if name not in schemes:
            schemes[name] = await priya.post("/loans/schemes", {"name": name, **body})
    advance, personal = schemes["Salary advance"]["id"], schemes["Personal loan"]["id"]

    loan = await demo["rohan"].post(
        "/loans/mine",
        {"scheme_id": advance, "principal": "30000", "tenure_months": 3,
         "purpose": "Medical bills for my mother"},
    )
    await priya.post(f"/loans/{loan['id']}/approve", {"note": "Approved; recovered over 3 months"})
    await demo["ananya"].post(f"/loans/{loan['id']}/disburse", {"disbursed_on": "2026-07-24"})

    loan = await demo["aditya"].post(
        "/loans/mine",
        {"scheme_id": personal, "principal": "200000", "tenure_months": 24,
         "purpose": "Down payment for a car"},
    )
    await priya.post(f"/loans/{loan['id']}/approve", {"note": "Within policy"})
    await demo["ananya"].post(f"/loans/{loan['id']}/disburse", {"disbursed_on": "2026-08-10"})

    loan = await demo["lakshmi"].post(
        "/loans/mine",
        {"scheme_id": personal, "principal": "100000", "tenure_months": 12,
         "purpose": "Home renovation"},
    )
    await priya.post(f"/loans/{loan['id']}/reject",
                       {"reason": "Personal loans need 12 months of service; you have "
                                  "15 months, but the minimum net pay rule fails at this EMI."})
    await demo["imran"].post(
        "/loans/mine",
        {"scheme_id": advance, "principal": "15000", "tenure_months": 3,
         "purpose": "Laptop repair and college fees"},
    )
    demo.note(FLOW, "loans: 2 disbursed and recovering, 1 rejected, 1 awaiting approval")


async def _reimbursement_components(demo: Demo) -> None:
    priya = demo["priya"]
    have = {c["code"] for c in await priya.get("/reimbursements/components")}
    for code, name, entitlement, basis in (
        ("INTERNET", "Internet and broadband", "12000", "Exempt against bills"),
        ("PHONE", "Mobile phone bills", "9600", "Exempt against bills"),
        ("FUEL", "Fuel and vehicle upkeep", "24000", "Exempt against bills for official use"),
    ):
        if code not in have:
            await priya.post("/reimbursements/components",
                             {"code": code, "name": name, "annual_entitlement": entitlement,
                              "exemption_basis": basis})
    demo.note(FLOW, "reimbursement components: internet, phone, fuel")


async def claims(demo: Demo) -> None:
    """Bills claimed this month, verified by HR, paid in October's payroll."""
    karthik = demo["karthik"]
    rows = (
        ("meera", "INTERNET", "1180", "2026-07-05", "ACT/JUL/88213", "verified", "1180"),
        ("meera", "INTERNET", "1180", "2026-08-05", "ACT/AUG/90122", "verified", "1180"),
        ("meera", "INTERNET", "1180", "2026-09-05", "ACT/SEP/91874", "verified", "1180"),
        ("rahul", "FUEL", "6400", "2026-09-30", "HPCL/BKC/5521", "partial", "4800"),
        ("pooja", "PHONE", "799", "2026-09-28", "JIO/SEP/44120", None, None),
        ("sneha", "INTERNET", "999", "2026-09-12", "AIRTEL/SEP/1203", "rejected", "0"),
    )
    for who, code, amount, bill_date, reference, decision, accepted in rows:
        claim = await demo[who].post(
            "/reimbursements/claims",
            data={"component_code": code, "financial_year": FY_LONG, "amount": amount,
                  "bill_date": bill_date, "bill_reference": reference},
            files={"file": pdf(f"{reference.replace('/', '-')}.pdf")},
        )
        if decision:
            await karthik.patch(
                f"/reimbursements/claims/{claim['id']}",
                {"status": decision, "accepted_amount": accepted,
                 "verification_note": {
                     "verified": "Bill matches",
                     "partial": "Two receipts are for a personal trip; accepted 4,800",
                     "rejected": "The bill is in your landlord's name; ask them for an "
                                 "authorisation letter or a bill in yours",
                 }[decision]},
            )
    demo.note(FLOW, "claims: 4 verified (1 partly) for October's payroll, 1 rejected, 1 pending")


# ── The monthly runs ────────────────────────────────────────────────────────

MONTHS = (
    (2026, 4, "2026-04-30"), (2026, 5, "2026-05-29"), (2026, 6, "2026-06-30"),
    (2026, 7, "2026-07-31"), (2026, 8, "2026-08-31"), (2026, 9, "2026-09-30"),
)


async def runs(demo: Demo) -> None:
    priya, karthik = demo["priya"], demo["karthik"]
    for year, month, pay_date in MONTHS:
        run = await priya.post(
            "/payroll/runs",
            {"legal_entity_id": demo.ids["entity"], "period_year": year,
             "period_month": month, "payment_date": pay_date},
        )
        run = await priya.post(f"/payroll/runs/{run['id']}/compute")
        detail = await priya.get(f"/payroll/runs/{run['id']}")
        if detail["error_log"] or detail["status"] != "pending_review":
            raise SeedError(f"{year}-{month:02d} run is {detail['status']}: {detail['error_log']}")
        flags = detail["review_flags"]
        for variance in detail["variance_flags"]:
            if variance.get("acknowledged"):
                continue
            await priya.post(
                f"/payroll/runs/{run['id']}/variances/{variance['employee_id']}/acknowledge",
                {"note": variance_note(variance)},
            )
        await priya.refused("POST", f"/payroll/runs/{run['id']}/approve", status=403)
        await karthik.step_up("payroll.run_approve")
        await karthik.post(f"/payroll/runs/{run['id']}/approve")
        await priya.post(f"/payroll/runs/{run['id']}/publish")

        # The bank file: Karthik prepares it; Priya, an admin, approves it,
        # downloads it and releases it to the bank; Karthik records the response.
        batch = await karthik.post(
            "/disbursement/batches",
            {"payroll_run_id": run["id"], "bank_profile_id": demo.ids["bank_profile"],
             "value_date": pay_date},
        )
        await karthik.refused("POST", f"/disbursement/batches/{batch['id']}/approve",
                              {"file_hash": batch["file_hash"]}, status=403)
        await priya.post(f"/disbursement/batches/{batch['id']}/approve",
                         {"file_hash": batch["file_hash"]})
        await priya.get(f"/disbursement/batches/{batch['id']}/file")
        await priya.step_up("disbursement.release")
        await priya.post(f"/disbursement/batches/{batch['id']}/release")
        # The bank's response file names accounts, which the batch lines mask;
        # Finance matches them from the bank's own statement.
        lines = await karthik.get(f"/disbursement/batches/{batch['id']}/lines")
        accounts = {str(p.employee_id): account_for(p) for p in demo.people.values() if p.code}
        await karthik.post(
            f"/disbursement/batches/{batch['id']}/reconcile",
            {"responses": [{"account_number": accounts[line["employee_id"]], "status": "success",
                            "reference": f"UTR{year}{month:02d}{index:05d}"}
                           for index, line in enumerate(lines, start=1)]},
        )
        await priya.post(f"/payroll/runs/{run['id']}/mark-paid", {"payment_date": pay_date})
        final = await priya.get(f"/payroll/runs/{run['id']}")
        demo.ids.setdefault("runs", {})[f"{year}-{month:02d}"] = run["id"]
        demo.note(FLOW, f"{year}-{month:02d}: {final['employee_count']} payslips, gross "
                        f"{final['total_gross']}, net {final['total_net']}, {final['status']}; "
                        f"{len(detail['variance_flags'])} variance(s), {len(flags)} review flag(s)")
        for flag in flags:
            print(f"      review flag: {flag}")

    october = await priya.post(
        "/payroll/runs",
        {"legal_entity_id": demo.ids["entity"], "period_year": 2026, "period_month": 10,
         "payment_date": "2026-10-30"},
    )
    demo.ids["runs"]["2026-10"] = october["id"]
    demo.note(FLOW, "2026-10 run opened; waits for October's attendance lock")


def variance_note(variance: dict) -> str:
    return "Checked: the change follows leave without pay, joining or exit dates, or loans."


# ── Returns, challans and exit benefits ─────────────────────────────────────


async def filings(demo: Demo) -> None:
    """Monthly PF, ESIC and PT returns; TDS challans; the quarterly 24Q.

    Everything to August is filed and acknowledged. September's monthly returns
    are generated and await filing (due mid-October); the second quarter's 24Q
    is generated, due by 31 October.
    """
    priya, karthik = demo["priya"], demo["karthik"]
    entity = demo.ids["entity"]
    filed = generated = 0
    for year, month, _ in MONTHS:
        for kind in ("pf-ecr", "esic", "professional-tax"):
            result = await karthik.post(f"/filings/{kind}",
                                        {"legal_entity_id": entity, "year": year, "month": month})
            # Professional tax is one return per state.
            for index, item in enumerate(result if isinstance(result, list) else [result]):
                if month < 9:
                    await priya.post(
                        f"/filings/{item['record']['id']}/acknowledgement",
                        {"acknowledgement_number": f"{kind.upper()[:3]}{year}{month:02d}"
                                                   f"{sum(map(ord, kind)) + index:05d}",
                         "filed_at": f"{year}-{month + 1:02d}-1{2 if kind == 'pf-ecr' else 4}"
                                     "T11:30:00+05:30"},
                    )
                    filed += 1
                else:
                    generated += 1

    # TDS is deposited by the 7th of the next month, one challan a month, and
    # each payslip's deduction is mapped to the challan that paid it.
    for quarter, months in ((1, (4, 5, 6)), (2, (7, 8, 9))):
        unmapped = await karthik.get("/filings/unmapped-tds", financial_year=FY_LONG,
                                     quarter=quarter)
        for month in months:
            rows = [r for r in unmapped if r["period"] == f"{month:02d}/2026"]
            total = sum(float(r["tds_amount"]) for r in rows)
            # Recording money paid to the government is an admin's call.
            challan = await priya.post(
                "/filings/tds-challans",
                {"legal_entity_id": entity, "bsr_code": "0510308",
                 "deposit_date": f"2026-{month + 1:02d}-06", "challan_serial": f"{month:02d}417",
                 "financial_year": FY_LONG, "quarter": quarter, "tds_amount": f"{total:.2f}",
                 "total_amount": f"{total:.2f}"},
            )
            for row in rows:
                await karthik.post(
                    "/filings/tds-challan-mappings",
                    {"challan_id": challan["id"], "payslip_id": row["payslip_id"],
                     "tds_amount": row["tds_amount"],
                     "deduction_date": f"2026-{month:02d}-{_last_day(month)}"},
                )
        result = await karthik.post(
            "/filings/tds-24q",
            {"legal_entity_id": entity, "financial_year": FY_LONG, "quarter": quarter},
        )
        if quarter == 1:
            await priya.post(f"/filings/{result['record']['id']}/acknowledgement",
                             {"acknowledgement_number": "QAEDWN26Q1074210",
                              "filed_at": "2026-07-29T16:05:00+05:30"})
    demo.note(FLOW, f"returns: {filed} filed and acknowledged, {generated} generated for "
                    "September; 6 TDS challans mapped; 24Q Q1 filed, Q2 generated")


def _last_day(month: int) -> int:
    return 30 if month in (4, 6, 9, 11) else 31


async def benefits(demo: Demo) -> None:
    """Gratuity and leave encashment rules, the gratuity provision, an encashment."""
    priya, ananya = demo["priya"], demo["ananya"]
    await priya.put(
        "/gratuity/config",
        {"eligibility_years": 5, "eligibility_days_in_final_year": 240,
         "formula_basis": ["BASIC"], "days_per_year_divisor": 26, "days_per_year": "15",
         "max_exempt_amount": "2000000", "service_rounding": "nearest_year_six_month_rule",
         "include_notice_period_in_service": True,
         "eligibility_waived_reasons": ["death", "disablement"]},
    )
    await ananya.post("/gratuity/config/approve")
    await priya.post("/gratuity/accrue", {"as_of": "2026-09-30"})
    await priya.put(
        "/encashment/policies",
        {"leave_type_code": "EL", "encashable_on_exit": True, "encashable_in_service": True,
         "max_days_per_year": "10", "cap_days": "45", "min_balance_after_encashment": "10",
         "formula_basis": ["BASIC"], "days_per_month_divisor": 30},
    )
    request = await demo["suresh"].post(
        "/encashment/requests",
        {"leave_type_code": "EL", "days": "5", "financial_year": FY},
    )
    await ananya.patch(f"/encashment/requests/{request['id']}",
                       {"approve": True, "note": "Approved; paid with October salary"})
    demo.note(FLOW, "gratuity rules approved and provision accrued to 30 Sep; EL encashment "
                    "policy; Suresh encashes 5 days in service")
