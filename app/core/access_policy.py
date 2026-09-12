"""Every signed-in endpoint with no role gate, and the rule that makes it safe.

An endpoint without `require_roles` is reachable by every signed-in person in
the tenant. That is right for a great many of them — a holiday calendar, your
own notifications, your own tax declaration. It stopped being right, quietly,
for about thirty endpoints that took an `employee_id` or a record id from the
request and trusted it: a colleague's leave reason, their offer letter, their
reward points.

So every ungated endpoint is listed here with its rule, and invariants check
both halves: nothing ungated is missing from this list, and each rule is really
present in the code it claims to describe. A new endpoint cannot ship ungated by
omission; it has to be written down here with a reason somebody can argue with.

Rules, as the prefix of each entry:

- ``own``         — acts only on the caller's own identity, taken from the token.
- ``reference``   — tenant-wide or static material about nobody in particular.
                    It takes no person id, and the invariant checks that it does not.
- ``permissions`` — decides self / reporting line / HR through `app.core.permissions`.
- ``scoped:<marker>`` — a different enforced check, named by a marker that must
                    appear in the handler or the service it calls.
"""

from __future__ import annotations

__all__ = ["RULES", "UNGATED_ENDPOINTS"]

RULES = ("own", "reference", "permissions", "scoped")

UNGATED_ENDPOINTS: dict[str, str] = {
    # ── Identity and account ────────────────────────────────────────────────
    "GET /auth/me": "own: the caller's own user record.",
    "POST /auth/change-password": "own: changes the caller's own password.",
    "POST /auth/logout": "reference: stateless; the client discards its own token.",
    "GET /dashboard/summary": "reference: tenant-wide counts; no individual's record.",
    # ── People and organisation ─────────────────────────────────────────────
    "GET /employees": (
        "reference: the company directory - names, titles, departments and work "
        "email, which every colleague may see. Personal fields are not in it."
    ),
    "GET /employees/{employee_id}": (
        "permissions: full record for self and HR; a line manager keeps "
        "employment fields; everybody else gets the directory view."
    ),
    "GET /org/departments": "reference: the organisation's structure.",
    "GET /org/legal-entities": "reference: the organisation's structure.",
    "GET /org/locations": "reference: the organisation's structure.",
    # ── Time ────────────────────────────────────────────────────────────────
    "GET /leave/types": "reference: the leave catalogue.",
    "GET /leave/holidays": "reference: the holiday calendar.",
    "GET /leave/requests": (
        "permissions: own requests, a manager's reporting line, or everybody "
        "for HR. Reasons are routinely medical."
    ),
    "POST /attendance/punch": "permissions: your own punch unless HR.",
    "GET /attendance/day": "permissions: your own attendance unless HR.",
    "POST /attendance/regularisation": "permissions: your own regularisation unless HR.",
    "GET /attendance/locks": "reference: which payroll periods are locked.",
    "GET /encashment/policies": "reference: the encashment rules.",
    "POST /encashment/requests": "permissions: your own encashment unless payroll.",
    "GET /encashment/requests": "permissions: your own encashments unless payroll.",
    # ── Tax ─────────────────────────────────────────────────────────────────
    "GET /tax/windows": "reference: declaration and proof windows.",
    "POST /tax/regime": "permissions: your own regime election unless payroll.",
    "GET /tax/regime/compare": "permissions: your own comparison unless payroll.",
    "GET /tax/declarations": "permissions: your own declarations unless payroll.",
    "PUT /tax/declarations": "permissions: your own declarations unless payroll.",
    "POST /tax/declarations/{declaration_id}/submit": (
        "permissions: the declaration must be yours unless payroll."
    ),
    "PUT /tax/house-rent": "permissions: your own HRA claim unless payroll.",
    "PUT /tax/previous-employer": "permissions: always the caller's own record.",
    "POST /tax/proofs": "permissions: the declaration must be yours unless payroll.",
    "GET /tax/projection": "permissions: your own projection unless payroll.",
    "GET /tax/snapshots": "permissions: your own computations unless payroll.",
    "GET /tax/form-12bb": "permissions: your own Form 12BB unless payroll.",
    # ── Letters ─────────────────────────────────────────────────────────────
    "GET /letter-templates": "reference: the template catalogue, not issued letters.",
    "GET /letters": "permissions: your own letters unless HR.",
    "GET /letters/{letter_id}/pdf": "permissions: your own letter unless HR.",
    "GET /letters/{letter_id}/verify": "permissions: your own letter unless HR.",
    # ── Hiring and recruiting ───────────────────────────────────────────────
    "GET /hiring/requisitions": (
        "reference: open roles - title, department, openings. No salary, no "
        "candidates."
    ),
    "GET /hiring/candidates": (
        "permissions: HR, or the hiring manager / recruiter on the requisition."
    ),
    "GET /recruiting/posture": "reference: what the module enforces.",
    "GET /recruiting/interviewer-framing": "reference: static guidance.",
    "GET /recruiting/share-link": "permissions: the referrer must be you unless HR.",
    "POST /recruiting/referrals": "permissions: the referrer must be you unless HR.",
    # ── Culture ─────────────────────────────────────────────────────────────
    "GET /culture/shoutouts": (
        "permissions: company shoutouts for all; department ones for that "
        "department and HR; private ones for the two people in them."
    ),
    "POST /culture/shoutouts": "own: always sent from the caller's own record.",
    "GET /culture/clubs": "reference: the club directory.",
    "GET /culture/posture": "reference: what the module enforces.",
    "GET /culture/instruments": "reference: survey instruments and the reporting floor.",
    "POST /culture/value-tags": "permissions: managers and HR tag values directly.",
    "POST /culture/nominations": "permissions: you nominate in your own name unless HR.",
    "POST /culture/nominations/{nomination_id}/score": (
        "scoped:juror_user_ids: only a juror on the cycle may score."
    ),
    "GET /culture/nominations/{nomination_id}/jury": (
        "permissions: jurors on the cycle and HR."
    ),
    "POST /culture/surveys/{survey_id}/responses": (
        "permissions: only the person the survey was sent to - HR included."
    ),
    "GET /culture/surveys/{survey_id}/results": (
        "scoped:min_responses: aggregates only, suppressed below the reporting floor."
    ),
    "POST /culture/action-plans": "permissions: managers and HR create action plans.",
    "POST /culture/action-plans/{plan_id}/complete": (
        "permissions: the plan's owner or HR."
    ),
    "POST /culture/announcements/{announcement_id}/acknowledge": (
        "permissions: only the recipient acknowledges - HR included."
    ),
    "PUT /culture/event-privacy": "permissions: your own privacy settings unless HR.",
    "GET /culture/calendar": (
        "scoped:EventPrivacyPreference: birthdays and anniversaries pass through "
        "each person's own privacy preference."
    ),
    # ── Expenses and GST ────────────────────────────────────────────────────
    "GET /expenses/posture": "reference: what the module enforces.",
    "POST /expenses/claims": "permissions: your own claims unless finance.",
    "POST /expenses/claims/{claim_id}/submit": "permissions: your own claim unless finance.",
    "POST /expenses/travel-requests": "permissions: your own trips unless finance.",
    "GET /gst/posture": "reference: what the module enforces.",
    "GET /gst/validate-gstin": "reference: a checksum on a string.",
    "POST /gst/credits": (
        "permissions: outside finance, only against your own expense claim."
    ),
    # ── Performance ─────────────────────────────────────────────────────────
    "GET /performance/posture": "reference: what the module enforces.",
    "POST /performance/pips/{pip_id}/check-ins": (
        "permissions: the employee's manager, HR, or whoever opened the plan - "
        "never the person on it."
    ),
    "GET /performance/cycles/{cycle_id}/budget": "permissions: managers and HR.",
    "POST /performance/recommendations": (
        "permissions: the employee's manager or HR, never for yourself."
    ),
    # ── Rewards ─────────────────────────────────────────────────────────────
    "GET /rewards/posture": "reference: what the module enforces.",
    "POST /rewards/awards": "permissions: managers and HR, never to yourself.",
    "POST /rewards/redemptions": "permissions: your own points unless HR.",
    "GET /rewards/statement/{employee_id}": "permissions: your own statement unless HR.",
    # ── Notifications ───────────────────────────────────────────────────────
    "GET /notifications": "own: the caller's own inbox.",
    "POST /notifications/{notification_id}/read": "own: only a notification sent to you.",
    "GET /notifications/stream": "own: the caller's own inbox.",
    "GET /notifications/preferences": "own: the caller's own preferences.",
    "PATCH /notifications/preferences": "own: the caller's own preferences.",
    "GET /notifications/whatsapp/consent": "own: the caller's own consent.",
    "PUT /notifications/whatsapp/consent": "own: the caller's own consent.",
    # ── Data protection and retention ───────────────────────────────────────
    "GET /dpdp/categories": "reference: the data category catalogue.",
    "GET /dpdp/purposes": "reference: the purpose catalogue.",
    "GET /dpdp/sub-processors": "reference: the published sub-processor list.",
    "GET /dpdp/me/notice": "reference: the tenant's privacy notice.",
    "GET /dpdp/me/consent": "own: the caller's own consent record.",
    "POST /dpdp/me/consent": "own: the caller's own consent record.",
    "POST /dpdp/me/consent/{purpose_key}/withdraw": "own: the caller's own consent record.",
    "GET /dpdp/me/export": "own: the caller's own data manifest.",
    "GET /dpdp/me/erasure-preview": "own: the caller's own erasure plan.",
    "POST /dpdp/me/requests": "own: a request raised by and about the caller.",
    "GET /retention/taxonomy": "reference: the retention catalogue.",
    "GET /retention/trust-centre": (
        "permissions: everybody sees that legal holds exist; HR sees why."
    ),
    # ── Security operations ─────────────────────────────────────────────────
    "GET /opsec/continuity": "reference: the published continuity posture.",
    "GET /opsec/sessions": "scoped:ADMIN_ROLES: your own sessions; others' only for admins.",
    "POST /opsec/sessions/revoke": (
        "scoped:ADMIN_ROLES: your own sessions; signing somebody else out is admin-only."
    ),
    "POST /opsec/step-up": "own: a challenge is issued to the caller.",
    "POST /opsec/step-up/{challenge_id}": "own: only the person the challenge was issued to.",
    "GET /opsec/step-up/actions": "reference: which actions need step-up.",
    "GET /opsec/siem/schema": "reference: the event schema.",
    "GET /opsec/pentest-scope": "reference: the published test scope.",
    # ── Agents, grounding, autonomy, BI ─────────────────────────────────────
    "GET /agent-eval/suites": "reference: the evaluation suite definitions.",
    "POST /agent-eval/feedback": "own: feedback submitted by the caller.",
    "GET /grounding/sources": "reference: which sources an agent may read.",
    "GET /grounding/scope": "scoped:resolve_scope: the caller's own retrieval scope.",
    "POST /grounding/retrieve": (
        "scoped:resolve_scope: refused for anybody outside the caller's scope."
    ),
    "POST /grounding/verify": "reference: checks citations in supplied text.",
    "POST /grounding/classify-refusal": (
        "scoped:refusal_engine: a pure classification over supplied facts; reads "
        "no records."
    ),
    "POST /grounding/present-score": "reference: formats a supplied score.",
    "POST /grounding/check-inference": "reference: checks supplied text.",
    "GET /autonomy/actions": "reference: the action catalogue.",
    "GET /autonomy/transparency": (
        "permissions: activity counts for everybody; the proposal queue and the "
        "decision record for HR."
    ),
    "GET /bi/posture": "reference: what the connector enforces.",
    "GET /bi/schema": "reference: the published schema, filtered to the caller's role.",
    "GET /bi/schema/entities": "reference: exposed entity names and the schema version.",
    "GET /bi/benchmark-terms": "reference: the benchmark contribution terms.",
}
