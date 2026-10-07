"""Hiring: approval chains, requisitions, the careers site, candidates, interview
loops with feedback, offers through approval to acceptance, and conversion of
the hire into a pre-joining employee. Plus the referral programme.

Anjali is the recruiter. Her login starts as a plain employee, which cannot
reach hiring at all, so Ananya grants her sub-admin first — the admin role
change every company makes for its first recruiter.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .api import Demo, SeedError

FLOW = "hiring"


def _at(day: str, hhmm: str) -> str:
    return f"{day}T{hhmm}:00+05:30"

PIPELINE = ("applied", "screening", "human_interview")


async def _move(api, candidate_id: str, to: str, reason: str) -> None:
    """Walk a candidate along the pipeline to `to`, a stage at a time.

    Rejected and withdrawn are reachable from anywhere; the rest go in order.
    The offer stage is reached by releasing an offer, never by a move.
    """
    current = (await api.get(f"/hiring/candidates/{candidate_id}"))["candidate"]["stage"]
    if to in ("rejected", "withdrawn"):
        await api.post(f"/hiring/candidates/{candidate_id}/stage",
                       {"to_stage": to, "reason": reason})
        return
    for stage in PIPELINE[PIPELINE.index(current) + 1: PIPELINE.index(to) + 1]:
        await api.post(f"/hiring/candidates/{candidate_id}/stage",
                       {"to_stage": stage, "reason": reason if stage == to else "Screened"})


async def setup(demo: Demo) -> None:
    ananya, priya = demo["ananya"], demo["priya"]
    anjali = demo.people["anjali"]
    if anjali.role != "sub_admin":
        await ananya.patch(f"/admin/users/{anjali.user_id}/role",
                           {"role": "sub_admin",
                            "reason": "Recruiter: needs requisitions, candidates and offers"})
        anjali.role = "sub_admin"
        anjali.api.role = "sub_admin"
        anjali.api._token = None

    rules = await priya.get("/hiring/approval-rules")
    if not rules:
        for scope in ("requisition", "offer"):
            first = "manager" if scope == "requisition" else "hiring_manager"
            steps = [(1, first, "0", None, False, "Line approval"),
                     (2, "admin", "2000000", None, False, "Leadership, 20 lakh and above")]
            if scope == "offer":
                steps.append((3, "finance_head", "0", None, True, "Finance, over the band"))
            for sequence, role, lo, hi, over_band, label in steps:
                await priya.post("/hiring/approval-rules",
                                 {"scope": scope, "sequence": sequence, "approver_role": role,
                                  "ctc_min": lo, "ctc_max": hi,
                                  "only_when_over_band": over_band, "label": label})
    await ananya.put(
        "/careers/config",
        {"slug": "askmira-demo", "is_published": True,
         "hero_heading": "Build the HR platform Indian companies love",
         "hero_subheading": "Small teams, real ownership, and a culture we measure.",
         "about_html": "<p>AskMira Demo builds culture-first HR software in Bengaluru and "
                       "Mumbai.</p>",
         "privacy_notice_html": "<p>We use your application only to assess you for roles "
                                "at AskMira Demo and keep it for two years.</p>",
         "seo_title": "Careers at AskMira Demo"},
    )
    programmes = await priya.get("/recruiting/referral-programmes/all")
    if not programmes:
        await priya.post(
            "/recruiting/referral-programmes",
            {"name": "Employee referrals 2026",
             "description": "Refer friends; the bonus is paid half on joining and half "
                            "after 90 days.",
             "bonus_by_grade": {"P1": "10000", "P2": "25000", "P3": "40000", "P4": "60000",
                                "M2": "75000"},
             "milestones": [
                 {"key": "joined", "label": "On joining", "share_percent": "50",
                  "days_after_joining": 0},
                 {"key": "retained", "label": "After 90 days", "share_percent": "50",
                  "days_after_joining": 90}]},
        )
    demo.note(FLOW, "Anjali made sub-admin; approval chains for requisitions and offers; "
                    "careers site published; referral programme")


REQUISITIONS = (
    # key, raised by, title, dept, location, hiring manager, grade, openings, min, max, skills
    ("backend", "vikram", "Backend Engineer, Payments", "ENG", "BLR", "vikram", "P3", 2,
     "1400000", "1900000", ["Python", "PostgreSQL", "Distributed systems"]),
    ("ae", "rahul", "Enterprise Account Executive", "SAL", "MUM", "rahul", "P3", 1,
     "1000000", "1600000", ["B2B SaaS sales", "Negotiation"]),
    ("support", "manoj", "Customer Support Executive", "OPS", "BLR", "manoj", "P1", 1,
     "240000", "320000", ["Kannada", "English", "Ticketing tools"]),
    ("em", "vikram", "Engineering Manager, Platform", "ENG", "BLR", "vikram", "M2", 1,
     "3500000", "4500000", ["People leadership", "Platform engineering"]),
)


async def run(demo: Demo) -> None:
    anjali, priya = demo["anjali"], demo["priya"]
    reqs: dict[str, dict] = {}
    for key, raiser, title, dept, loc, hm, grade, openings, lo, hi, skills in REQUISITIONS:
        req = await demo[raiser].post(
            "/hiring/requisitions",
            {"title": title, "department_id": demo.ids["department"][dept],
             "location_id": demo.ids["location"][loc], "hiring_manager_id": demo.emp(hm),
             "recruiter_id": demo.emp("anjali"), "openings": openings, "grade": grade,
             "experience_min_years": 2 if grade.startswith("P") else 8,
             "experience_max_years": 6 if grade.startswith("P") else 14,
             "budget_ctc_min": lo, "budget_ctc_max": hi, "skills": skills,
             "justification": "Growth hire in the FY 2026-27 plan",
             "description": f"<p>We are hiring a {title} to join the team.</p>",
             "target_joining_date": "2026-11-16", "target_close_date": "2026-10-31"},
        )
        await demo[raiser].post(f"/hiring/requisitions/{req['id']}/submit")
        reqs[key] = req
    # Approvals follow the chain; nobody approves their own.
    for key, approvers in (("backend", ("ananya",)), ("ae", ("ananya",)),
                           ("em", ("ananya", "priya"))):
        for approver in approvers:
            await demo[approver].post(f"/hiring/requisitions/{reqs[key]['id']}/decide",
                                      {"approve": True, "note": "Approved in the hiring plan"})
    detail = await anjali.get(f"/hiring/requisitions/{reqs['backend']['id']}")
    if detail["status"] != "open":
        raise SeedError(f"the backend requisition is {detail['status']} after approval")
    await priya.post(f"/hiring/requisitions/{reqs['em']['id']}/status",
                     {"status": "on_hold", "reason": "Hold until the Q4 budget review"})
    demo.ids["requisition"] = {k: r["id"] for k, r in reqs.items()}
    demo.note(FLOW, "4 requisitions: 2 open, 1 awaiting approval, 1 on hold")

    # ── The interview loop for the backend role ────────────────────────────
    stages = []
    for sequence, name, kind, minutes in ((1, "Technical screen", "technical", 60),
                                          (2, "System design", "panel", 90),
                                          (3, "Hiring manager", "managerial", 45),
                                          (4, "HR discussion", "hr", 30)):
        stages.append(await anjali.post(
            "/interviews/stages",
            {"requisition_id": reqs["backend"]["id"], "sequence": sequence, "name": name,
             "stage_type": kind, "duration_minutes": minutes, "mode": "video",
             "required_competencies": ["Problem solving", "Communication"]},
        ))

    candidates: dict[str, str] = {}
    for key, name, email, company, years, current, expected, notice in (
        ("sana", "Sana Iqbal", "sana.iqbal@example.com", "Razorpay", "4.5", "1500000",
         "1800000", 30),
        ("karan", "Karan Malhotra", "karan.m@example.com", "Swiggy", "5", "1700000",
         "2100000", 60),
        ("ritu", "Ritu Sharma", "ritu.sharma@example.com", "Infosys", "3", "900000",
         "1400000", 90),
    ):
        created = await anjali.post(
            "/hiring/candidates",
            {"requisition_id": reqs["backend"]["id"], "full_name": name, "email": email,
             "phone": "+91 98860 4" + str(len(name)).zfill(4), "current_company": company,
             "total_experience_years": years, "current_ctc": current,
             "expected_ctc": expected, "notice_period_days": notice,
             "source_note": "LinkedIn outreach"},
        )
        candidates[key] = created["candidate_id"]

    panel = {"Technical screen": ["arjun"], "System design": ["arjun", "aditya"],
             "Hiring manager": ["vikram"], "HR discussion": ["anjali"]}
    # Sana went through every round in late September.
    await _move(anjali, candidates['sana'], "human_interview", "Shortlisted from LinkedIn")
    for index, stage in enumerate(stages):
        interview = await anjali.post(
            "/interviews",
            {"candidate_id": candidates["sana"], "stage_id": stage["id"],
             "start": _at(f"2026-09-{22 + index * 2:02d}", "11:00"),
             "panelist_user_ids": [str(demo.people[k].user_id) for k in panel[stage["name"]]]},
        )
        for panelist in panel[stage["name"]]:
            await demo[panelist].post(
                f"/interviews/{interview['id']}/feedback",
                {"recommendation": "strong_yes" if panelist == "arjun" else "yes",
                 "ratings": {"Problem solving": "4", "Communication": "5"},
                 "strengths": "Clear thinking about idempotency and ledgers.",
                 "concerns": "Limited Kubernetes experience."},
            )
        await anjali.patch(f"/interviews/{interview['id']}/outcome",
                           {"status": "completed", "note": "Feedback complete"})

    # Ritu: rejected after the technical screen.
    await _move(anjali, candidates['ritu'], "human_interview", "Shortlisted")
    interview = await anjali.post(
        "/interviews",
        {"candidate_id": candidates["ritu"], "stage_id": stages[0]["id"],
         "start": _at("2026-09-29", "15:00"),
         "panelist_user_ids": [str(demo.people["arjun"].user_id)]},
    )
    await demo["arjun"].post(f"/interviews/{interview['id']}/feedback",
                             {"recommendation": "no",
                              "ratings": {"Problem solving": "2", "Communication": "3"},
                              "strengths": "Good fundamentals.",
                              "concerns": "Could not reason about concurrency."})
    await anjali.patch(f"/interviews/{interview['id']}/outcome", {"status": "completed"})
    await _move(anjali, candidates['ritu'], "rejected", "Did not clear the technical screen")

    # Karan: system design booked for later this week.
    await _move(anjali, candidates['karan'], "human_interview", "Cleared the phone screen")
    upcoming = (date.today() + timedelta(days=2)).isoformat()
    await anjali.post(
        "/interviews",
        {"candidate_id": candidates["karan"], "stage_id": stages[1]["id"],
         "start": _at(upcoming, "14:00"),
         "panelist_user_ids": [str(demo.people[k].user_id) for k in ("arjun", "aditya")]},
    )
    demo.note(FLOW, "backend loop: Sana through 4 rounds to offer, Ritu rejected, Karan's "
                    "system design booked")

    # ── The offer, its approvals, acceptance and conversion ────────────────
    offer = await anjali.post(
        "/offers",
        {"candidate_id": candidates["sana"], "code": "OFF-2026-014",
         "salary_structure_id": demo.ids["structure"], "ctc_annual": "1800000",
         "joining_date": "2026-10-19", "designation": "Software Engineer", "grade": "P3",
         "joining_location_id": demo.ids["location"]["BLR"],
         "department_id": demo.ids["department"]["ENG"],
         "legal_entity_id": demo.ids["entity"], "probation_months": 6,
         "notice_period_days": 60, "valid_until": "2026-10-10",
         "components": [{"kind": "joining_bonus", "label": "Joining bonus",
                         "amount": "100000", "payable_terms": "With the first salary"}]},
    )
    await anjali.post(f"/offers/{offer['id']}/submit")
    await demo["vikram"].post(f"/offers/{offer['id']}/decide",
                              {"approve": True, "note": "Strong hire for payments"})
    current = (await anjali.get(f"/offers/{offer['id']}"))["offer"]
    if current["status"] not in ("approved",):
        raise SeedError(f"Sana's offer is {current['status']} after the hiring manager approved")
    await anjali.post(f"/offers/{offer['id']}/release",
                      {"template_id": demo.ids["letter_template"]["OFFER_STD"],
                       "valid_until": "2026-10-10"})
    await anjali.post(f"/offers/{offer['id']}/accept",
                      {"signature_reference": "Signed copy received by email, 2 Oct 2026"})
    converted = await priya.post(f"/offers/{offer['id']}/convert",
                                 {"employee_code": "DEMO023",
                                  "work_email": "sana@askmira-demo.com"})
    demo.ids["sana_employee"] = converted.get("employee_id")
    demo.note(FLOW, "Sana's offer: drafted, approved by Vikram, released, accepted, "
                    "converted to a pre-joining employee joining 19 Oct")

    # The account executive role: an offer out, waiting for the candidate.
    created = await anjali.post(
        "/hiring/candidates",
        {"requisition_id": reqs["ae"]["id"], "full_name": "Meghna Pillai",
         "email": "meghna.p@example.com", "current_company": "Zoho",
         "total_experience_years": "4", "source_note": "Agency: TalentNest"},
    )
    await _move(anjali, created['candidate_id'], "withdrawn", "Accepted a counter-offer")

    created = await anjali.post(
        "/hiring/candidates",
        {"requisition_id": reqs["ae"]["id"], "full_name": "Siddharth Rao",
         "email": "sid.rao@example.com", "current_company": "Freshworks",
         "total_experience_years": "6", "current_ctc": "1250000", "expected_ctc": "1500000",
         "notice_period_days": 60, "source_note": "Naukri"},
    )
    await _move(anjali, created['candidate_id'], "human_interview",
                "Cleared all rounds with Rahul")
    offer = await anjali.post(
        "/offers",
        {"candidate_id": created["candidate_id"], "code": "OFF-2026-015",
         "salary_structure_id": demo.ids["structure"], "ctc_annual": "1500000",
         "joining_date": "2026-12-01", "designation": "Enterprise Account Executive",
         "grade": "P3", "joining_location_id": demo.ids["location"]["MUM"],
         "department_id": demo.ids["department"]["SAL"], "legal_entity_id": demo.ids["entity"],
         "valid_until": "2026-10-15"},
    )
    await anjali.post(f"/offers/{offer['id']}/submit")
    await demo["rahul"].post(f"/offers/{offer['id']}/decide", {"approve": True})
    await anjali.post(f"/offers/{offer['id']}/release",
                      {"template_id": demo.ids["letter_template"]["OFFER_STD"],
                       "valid_until": "2026-10-15"})
    # Applications through the public careers page, and a referral.
    for name, email, years in (("Deepak Rawat", "deepak.rawat@example.com", "3"),
                               ("Nandini Rao", "nandini.rao@example.com", "5")):
        response = await demo.http.post(
            f"/api/v1/public/careers/askmira-demo/jobs/{detail['code']}/apply",
            data={"full_name": name, "email": email, "consent_application_processing": "true",
                  "total_experience_years": years, "notice_period_days": "30",
                  "expected_ctc": "1600000"},
            files={"resume": ("resume.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf")},
        )
        if response.status_code not in (200, 201):
            raise SeedError(f"careers apply failed: {response.status_code} {response.text}")
    programmes = await priya.get("/recruiting/referral-programmes/all")
    # Arjun interviews for this role, so he cannot refer into it; Kavya can.
    await demo["kavya"].post(
        "/recruiting/refer",
        {"programme_id": programmes[0]["id"], "requisition_id": reqs["backend"]["id"],
         "candidate_name": "Ayesha Fernandes", "candidate_email": "ayesha.f@example.com",
         "candidate_phone": "+91 98450 77123", "channel": "whatsapp",
         "note": "We worked together at my last company; an excellent backend engineer."},
    )
    for period, component, amount in (("2026-Q2", "job_board_spend", "85000"),
                                      ("2026-Q2", "agency_fees", "180000"),
                                      ("2026-Q2", "recruiter_time", "120000")):
        await priya.put("/recruiting/costs", {"period_label": period, "component": component,
                                              "amount": amount, "source": "Finance ledger"})
    demo.note(FLOW, "AE offer released and awaiting Siddharth; Meghna withdrew; 2 careers-page "
                    "applications; Kavya referred Ayesha; Q2 hiring costs")


def _today() -> datetime:
    return datetime.now()
