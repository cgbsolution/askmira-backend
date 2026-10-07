"""Documents: the company library, everyone's vault, and letters HR issues."""

from __future__ import annotations

from .api import Demo
from .payroll import pdf

FLOW = "documents"

COMPANY = (
    # type, title, number, issued, valid until, visible to employees, remark
    ("Policy", "Employee handbook 2026", "HB-2026-01", "2026-04-01", None, True,
     "Code of conduct, working hours, leave and benefits"),
    ("Policy", "Leave and holiday policy FY 2026-27", "POL-LV-2026", "2026-04-01", None, True,
     None),
    ("Policy", "Prevention of sexual harassment (POSH) policy", "POL-POSH-03", "2025-04-01",
     None, True, "Internal Committee members are listed on the last page"),
    ("Policy", "Travel and expense policy", "POL-TE-2026", "2026-04-01", None, True, None),
    ("Registration", "Certificate of incorporation", "U72900KA2021PTC074210", "2021-05-18",
     None, False, None),
    ("Registration", "Karnataka Shops and Establishments registration", "KA/BLR/SE/074210",
     "2021-06-10", "2027-03-31", False, "Renew before 31 March 2027"),
    ("Registration", "GST registration certificate (Karnataka)", "29AAQCA7421K1Z6",
     "2021-07-02", None, False, None),
    ("Insurance", "Group mediclaim policy 2026-27", "STAR/GMC/26/074210", "2026-04-01",
     "2027-03-31", True, "Covers employee, spouse, two children and parents up to 5 lakh"),
)


async def setup(demo: Demo) -> None:
    priya = demo["priya"]
    templates = await priya.get("/letter-templates")
    if not templates:
        await priya.post("/letter-templates/seed")
        templates = await priya.get("/letter-templates")
    demo.ids["letter_template"] = {t["code"]: t["id"] for t in templates}

    existing = {d["title"] for d in await priya.get("/documents/company")}
    for kind, title, number, issued, valid, visible, remark in COMPANY:
        if title in existing:
            continue
        await priya.post(
            "/documents/company",
            data={"document_type": kind, "title": title, "document_number": number,
                  "issued_on": issued, "valid_until": valid,
                  "is_employee_visible": str(visible).lower(), "remark": remark},
            files={"file": pdf(f"{title}.pdf")},
        )
    demo.note(FLOW, f"{len(templates)} standard letter templates; {len(COMPANY)} company documents")


async def current(demo: Demo) -> None:
    """Employees upload their papers; HR verifies them; letters are issued."""
    karthik = demo["karthik"]
    uploaded = verified = rejected = 0
    for key, person in demo.people.items():
        if not person.code or key == "ravi":
            continue
        me = demo[key]
        rows = [
            ("id_proof", "PAN card", None, f"PAN {person.code}"),
            ("id_proof", "Aadhaar card", None, None),
            ("education", "Graduation degree certificate", None, None),
        ]
        if key in ("rahul", "ananya", "kavya"):
            # Passports that need renewing soon show up in HR's expiry list.
            valid = {"rahul": "2026-11-02", "ananya": "2031-08-14", "kavya": "2026-12-20"}[key]
            rows.append(("id_proof", "Passport", valid, "Travel for client visits"))
        for category, title, valid, remark in rows:
            document = await me.post(
                "/documents/mine",
                data={"category": category, "title": title, "valid_until": valid,
                      "remark": remark},
                files={"file": pdf(f"{title}.pdf")},
            )
            uploaded += 1
            if key in ("fatima", "imran", "lakshmi") and category == "education":
                continue   # waiting for HR
            if key == "nikhil" and title == "Aadhaar card":
                await demo.hr_for(key).post(
                    f"/documents/{document['id']}/verify",
                    {"decision": "rejected",
                     "note": "The scan is blurred and the number is unreadable; please re-upload."},
                )
                rejected += 1
                continue
            verifier = demo["priya"] if key == "karthik" else karthik
            await verifier.post(f"/documents/{document['id']}/verify",
                                {"decision": "verified", "note": "Checked against the original"})
            verified += 1

    # HR files signed contracts into the vault for the latest joiners.
    for key in ("fatima", "imran", "lakshmi"):
        await karthik.post(
            "/documents",
            data={"employee_id": demo.emp(key), "category": "contract",
                  "title": "Signed appointment letter", "issued_on":
                  demo.people[key].date_of_joining},
            files={"file": pdf("appointment-signed.pdf")},
        )
    demo.note(FLOW, f"vault: {uploaded} uploads, {verified} verified, {rejected} rejected, "
                    "3 awaiting HR; 3 contracts filed by HR")

    # Letters: an appointment letter for the newest joiner, salary certificates
    # people asked for, and a confirmation after probation.
    templates = demo.ids["letter_template"]
    issued = []
    for key, code, values in (
        ("fatima", "APPOINTMENT_STD", {"probation_end_date": "20 March 2027",
                                       "notice_period": "60 days'"}),
        ("aditya", "SALARY_CERT_STD", {"purpose": "a car loan application with HDFC Bank"}),
        ("pooja", "SALARY_CERT_STD", {"purpose": "a visa application to the UK"}),
        ("imran", "CONFIRMATION_STD", {"confirmation_date": "3 May 2026"}),
    ):
        letter = await demo["priya"].post(
            "/letters/issue",
            {"employee_id": demo.emp(key), "template_id": templates[code],
             "manual_values": values},
        )
        if letter.get("status") == "pending_approval":
            await demo["ananya"].post(f"/letters/{letter['id']}/approve")
        issued.append((key, letter["id"]))
    for key, letter_id in issued:
        if key in ("fatima", "imran"):
            await demo["priya"].post(f"/letters/{letter_id}/request-acknowledgement")
    await demo["fatima"].post(f"/letters/{issued[0][1]}/acknowledge",
                              {"typed_name": "Fatima Sheikh", "accept": True})
    demo.note(FLOW, "letters: appointment (acknowledged), 2 salary certificates, a "
                    "confirmation awaiting acknowledgement")
