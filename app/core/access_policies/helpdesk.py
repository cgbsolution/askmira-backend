"""HR helpdesk: tickets, their thread, the agents' queue."""

_VISIBLE = (
    "scoped:_visible_ticket: the requester, the category owner, the assignee or HR; "
    "a confidential ticket only to its requester, owner, assignee and admins. "
    "Anyone else gets a 404, and the requester never receives an internal note."
)
_REQUESTER = "scoped:_requester_ticket: only the employee the ticket is about."
_AGENT = (
    "scoped:_agent_ticket: HR or the category owner (admins for a "
    "confidential ticket), never the ticket's own requester."
)
_QUEUE = (
    "scoped:_require_agent: HR or a category owner, narrowed to the tickets they may "
    "work and never their own."
)

ENDPOINTS: dict[str, str] = {
    "GET /helpdesk/categories": (
        "reference: the tenant's helpdesk categories; deactivated ones only for HR."
    ),
    "GET /helpdesk/me/summary": "own: the caller's own tickets.",
    "GET /helpdesk/tickets/mine": "own: the caller's own tickets.",
    "POST /helpdesk/tickets": (
        "permissions: a ticket for yourself, unless HR raises it on an employee's behalf."
    ),
    "GET /helpdesk/tickets/{ticket_id}": _VISIBLE,
    "POST /helpdesk/tickets/{ticket_id}/comments": _VISIBLE,
    "GET /helpdesk/attachments/{attachment_id}": _VISIBLE,
    "POST /helpdesk/tickets/{ticket_id}/cancel": _REQUESTER,
    "POST /helpdesk/tickets/{ticket_id}/reopen": _REQUESTER,
    "POST /helpdesk/tickets/{ticket_id}/rate": _REQUESTER,
    "GET /helpdesk/queue": _QUEUE,
    "GET /helpdesk/queue/summary": _QUEUE,
    "GET /helpdesk/agents": _QUEUE,
    "GET /helpdesk/tickets/{ticket_id}/assignees": _AGENT,
    "POST /helpdesk/tickets/{ticket_id}/assign": _AGENT,
    "PATCH /helpdesk/tickets/{ticket_id}": _AGENT,
}
