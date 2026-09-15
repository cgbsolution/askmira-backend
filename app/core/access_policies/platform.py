"""Notifications, data protection, retention, security operations, agents and BI."""

ENDPOINTS: dict[str, str] = {
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
