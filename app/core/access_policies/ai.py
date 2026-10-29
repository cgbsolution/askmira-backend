"""AI features: the assistant, drafting help and plain-language analytics."""

ENDPOINTS: dict[str, str] = {
    "GET /ai/status": (
        "own: whether AI is on for the workspace, and the caller's own consent by user.employee_id."
    ),
    "POST /assistant/ask": (
        "own: answers the caller's question; a question about their own record is looked "
        "up by user.employee_id and never sent to the model."
    ),
    "GET /assistant/articles": (
        "permissions: HR sees drafts; everyone else sees only published policy articles."
    ),
    "POST /culture/shoutouts/draft": (
        "own: a draft for the caller to edit (user.employee_id); nothing is posted."
    ),
    # ── Ask Mira (the AI companion) ─────────────────────────────────────────
    "GET /companion/status": (
        "own: the caller's own availability and messages sent today (user.id); the "
        "workspace's companion name and greeting are the same for everyone."
    ),
    "GET /companion/conversations": (
        "own: only conversations whose user_id is the caller's (user.id from the token)."
    ),
    "GET /companion/conversations/{conversation_id}": (
        "own: found only among the caller's own conversations (user.id); anyone else's is 404."
    ),
    "DELETE /companion/conversations/{conversation_id}": (
        "own: only the caller's own conversation (user.id) can be deleted."
    ),
    "POST /companion/messages": (
        "own: the caller's own conversation (user.id); each tool runs as the caller, offered "
        "only within oauth.allowed_scopes for their role; the model reads a trimmed result "
        "only within the categories the DPDP register lets the model provider receive."
    ),
    "POST /companion/actions/{action_id}/confirm": (
        "own: only an offer whose user_id is the caller's (user.id); it runs the module's own "
        "service as the caller, with that service's permission checks."
    ),
    "POST /companion/actions/{action_id}/cancel": (
        "own: only an offer whose user_id is the caller's (user.id) can be declined."
    ),
    "POST /analytics/ask": (
        "permissions: the definition runs through the report compiler, which refuses "
        "restricted fields and scopes rows by permissions.scope_role."
    ),
}
