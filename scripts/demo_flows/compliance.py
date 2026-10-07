"""Compliance and administration: data protection, retention, security, AI
governance, devices, reports and notifications."""

from __future__ import annotations

from datetime import datetime, timedelta

from .api import Demo

FLOW = "compliance"


async def run(demo: Demo) -> None:
    ananya, priya = demo["ananya"], demo["priya"]

    # ── DPDP: who to complain to, consents, requests, a drill ──────────────
    await ananya.put("/dpdp/configuration",
                     {"grievance_contact": "privacy@askmira-demo.com",
                      "data_protection_officer_name": "Priya Ramesh",
                      "sla_days": {"access": 30, "correction": 30, "erasure": 30,
                                   "grievance": 7, "nomination": 30},
                      "default_notice_language": "en", "ai_processing_enabled": False})
    granted = declined = 0
    for index, (key, person) in enumerate(demo.people.items()):
        if not person.code or key in ("ravi",):
            continue
        for purpose in ("directory_visibility", "culture_analytics", "wellness"):
            yes = not (purpose == "wellness" and index % 3 == 0)
            await demo[key].post("/dpdp/me/consent",
                                 {"purpose_key": purpose, "granted": yes,
                                  "captured_via": "self_service"})
            granted += yes
            declined += not yes
        if index % 4 == 1:
            await demo[key].post("/dpdp/me/consent",
                                 {"purpose_key": "marketing_communication", "granted": False})
            declined += 1
    await demo["nikhil"].post("/dpdp/me/consent",
                              {"purpose_key": "ai_assistance", "granted": True})
    await demo["nikhil"].post("/dpdp/me/consent/ai_assistance/withdraw")

    access = await demo["meera"].post("/dpdp/me/requests",
                                      {"request_type": "access",
                                       "details": "A copy of the personal data you hold on me."})
    await priya.post(f"/dpdp/requests/{access['id']}/resolve",
                     {"status": "completed", "note": "Export generated and shared in the portal."})
    correction = await demo["rohan"].post(
        "/dpdp/me/requests",
        {"request_type": "correction",
         "details": "My father's name is spelt Tapan, not Tapon, on the PF record."})
    await priya.post(f"/dpdp/requests/{correction['id']}/resolve",
                     {"status": "in_progress", "note": "Raised with the EPFO KYC desk."})
    await demo["lakshmi"].post("/dpdp/me/requests",
                               {"request_type": "grievance",
                                "details": "Why is my shift roster visible to the whole team?"})
    aware = datetime.now().astimezone() - timedelta(hours=6)
    await ananya.post(
        "/dpdp/breaches",
        {"reference": "DRILL-2026-10", "became_aware_at": aware.isoformat(),
         "detected_at": aware.isoformat(),
         "description": "Tabletop drill: a laptop with a payroll export is reported stolen.",
         "affected_data_categories": ["financial", "identity"],
         "affected_principal_count": 21, "data_left_our_control": True, "is_rehearsal": True},
    )
    await ananya.post("/dpdp/transfers",
                      {"sub_processor_key": "resend", "purpose_key": "employment_administration",
                       "category_keys": ["identity", "communication"], "principal_count": 23,
                       "description": "Transactional email: sign-in and approval notices."})
    demo.note(FLOW, f"DPDP: grievance officer set; {granted} consents granted, {declined} "
                    "declined, 1 withdrawn; access request completed, correction in progress, "
                    "grievance open; a breach drill; a recorded transfer")

    # ── Retention, security, AI governance ─────────────────────────────────
    await ananya.post("/retention/policies/seed")
    await ananya.post("/retention/legal-holds",
                      {"reason": "Labour inspection at the Mumbai office: keep FY 2025-26 "
                                 "attendance and wage records.",
                       "departments": ["Sales"], "from_date": "2025-04-01",
                       "to_date": "2026-03-31"})
    await ananya.put("/opsec/sessions/policy",
                     {"idle_timeout_minutes": 30, "absolute_lifetime_hours": 12,
                      "max_concurrent_sessions": 3,
                      "enforce_ip_allowlist_for_privileged": False})
    await ananya.post("/agent-eval/suites/seed")
    demo.note(FLOW, "retention policies seeded with a legal hold; session policy; AI "
                    "evaluation suites seeded")

    # ── Devices, reports, notifications ────────────────────────────────────
    places = {w["name"]: w["id"] for w in await priya.get("/attendance/work-locations")}
    devices = {d["device_id"] for d in await priya.get("/devices")}
    for device_id, name, place in (
        ("BLR-ESSL-01", "Bengaluru HQ, main entrance", "Bengaluru HQ - Prestige Tech Park"),
        ("MUM-ESSL-01", "Mumbai office, reception", "Mumbai office - BKC"),
    ):
        if device_id not in devices:
            await priya.post("/devices", {"device_id": device_id, "name": name,
                                          "vendor": "essl",
                                          "work_location_id": places.get(place)})

    reports = []
    for key in ("headcount_by_department", "payroll_cost_by_department",
                "leave_taken_this_financial_year", "attrition_by_department"):
        reports.append(await priya.post("/reports/starters/clone", {"starter_key": key}))
    await priya.post("/reports/schedules",
                     {"report_id": reports[1]["id"], "cadence": "monthly", "day_of_month": 1,
                      "hour_of_day": 9,
                      "recipient_user_ids": [str(demo.people["ananya"].user_id),
                                             str(demo.people["suresh"].user_id)],
                      "export_format": "xlsx"})
    await priya.get(f"/reports/{reports[0]['id']}/run")

    await ananya.post("/notifications/templates/seed")
    await ananya.put("/notifications/admin/policy",
                     {"quiet_hours_enabled": True, "quiet_start": "21:00", "quiet_end": "08:00",
                      "timezone": "Asia/Kolkata", "rate_limit_per_hour": 20, "max_attempts": 3})
    demo.note(FLOW, "2 biometric devices registered; 4 reports from the library, a monthly "
                    "payroll-cost schedule; notification templates and quiet hours")
