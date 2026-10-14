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
    "POST /analytics/ask": (
        "permissions: the definition runs through the report compiler, which refuses "
        "restricted fields and scopes rows by permissions.scope_role."
    ),
}
