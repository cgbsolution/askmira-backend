"""Notifications, data protection, retention, security operations, agents and BI."""

ENDPOINTS: dict[str, str] = {
    # ── Approvals inbox ─────────────────────────────────────────────────────
    "GET /approvals": (
        "permissions: each source applies its module's own rule — HR for the whole "
        "tenant, a manager for their reporting line, never an item about yourself."
    ),
    "GET /approvals/count": (
        "permissions: the same per-source rule as the inbox, reduced to a number."
    ),
    "GET /approvals/delegates": (
        "reference: names of colleagues who can sign in, to choose a stand-in from."
    ),
    "GET /approvals/delegations": (
        "scoped:from_user_id: the caller's own hand-overs, both ways; HR sees everybody's."
    ),
    "POST /approvals/delegations": (
        "scoped:from_user_id: hands over the caller's own approvals; only HR sets one up "
        "for somebody else."
    ),
    "DELETE /approvals/delegations/{delegation_id}": (
        "scoped:from_user_id: only the person who handed over, or HR, ends it."
    ),
    # ── Linked products ─────────────────────────────────────────────────────
    "POST /products/{product}/launch": (
        "own: signs the caller in to the product as themselves (identity from the token); "
        "the product's role rule refuses anyone it does not admit."
    ),
    "GET /products/{product}/views/{view}": (
        "scoped:role_in: only someone AskMira gives a role in the linked product is answered; "
        "the product is asked as them (identity from the token, workspace from tid), answers "
        "only what that role there may see, and a page's fixed scope cannot be widened."
    ),
    "GET /products/helpdesk/ticket-options": (
        "scoped:_ask: through products.data, which answers only someone AskMira gives a role "
        "in the linked helpdesk; it is asked as the caller (identity from the token, workspace "
        "from tid) and offers only their own assets."
    ),
    "GET /products/helpdesk/tickets/{ticket_id}": (
        "scoped:_ask: through products.data, as the caller; the helpdesk opens a ticket only "
        "for its requester or the company's IT staff who may work it, and refuses anyone else."
    ),
    "POST /products/helpdesk/tickets": (
        "scoped:_act: through products.data, signed use=act, as the caller: the ticket is "
        "always the caller's own, and a linked asset must be one they hold."
    ),
    "POST /products/helpdesk/tickets/{ticket_id}/replies": (
        "scoped:_act: through products.data, signed use=act, as the caller; the helpdesk "
        "accepts a reply only on a ticket the caller may open."
    ),
    # ── Notifications ───────────────────────────────────────────────────────
    "GET /notifications/unread-count": "own: the caller's own unread count.",
    "GET /oauth/authorize/details": (
        "reference: a registered client's name and the scopes on offer; no individual's record."
    ),
    "POST /oauth/authorize": "own: a code acting as the caller, within their own role's scopes.",
    "GET /oauth/connections": "own: the caller's own connected clients (user.id from the token).",
    "POST /oauth/connections/{grant_id}/revoke": "own: only the caller's own grant is found.",
    "POST /notifications/devices": (
        "own: registers a phone for the caller (user.id from the token)."
    ),
    "POST /notifications/devices/unregister": "own: only the caller's own phone is unregistered.",
    "POST /notifications/read-all": "own: marks only notifications sent to the caller.",
    "GET /notifications/channels": "reference: which channels deliver in this workspace.",
    "GET /notifications/events": "reference: the event catalogue and shipped defaults.",
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
    "GET /dpdp/me/export": "own: the caller's own data, organised by category.",
    "GET /dpdp/me/export/download": "own: the caller's own data as a downloadable file.",
    "GET /dpdp/me/requests": "own: requests about the caller.",
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
        "scoped:refusal_engine: a pure classification over supplied facts; reads no records."
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
    "GET /operator/session": (
        "reference: whether this session is an operator inside a client's workspace; "
        "no person data."
    ),
    "POST /operator/exit": (
        "own: returns the caller to their own workspace, re-reading their own login by user.id."
    ),
}
