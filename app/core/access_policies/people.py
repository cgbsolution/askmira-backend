"""People, organisation structure and positions."""

ENDPOINTS: dict[str, str] = {
    "GET /employees": (
        "reference: the company directory - names, titles, departments and work "
        "email, which every colleague may see. Personal fields are not in it."
    ),
    "GET /employees/me": (
        "own: the caller's own record, found through the login's employee link."
    ),
    "PATCH /employees/me": (
        "own: the caller's own display name and personal contact details; no "
        "employment field is in the contract."
    ),
    "GET /employees/{employee_id}": (
        "permissions: full record for self and HR; a line manager keeps "
        "employment fields; everybody else gets the directory view."
    ),
    "GET /employees/celebrations": (
        "scoped:culture_service.calendar: the culture calendar's own opt-in rule "
        "decides who appears - only people who chose to share, each kind "
        "separately, and a team-scoped birthday only for that team, the person "
        "and HR."
    ),
    "GET /employees/me/profile": "own: the caller's own extended profile.",
    "PUT /employees/me/profile": (
        "own: the caller's own non-sensitive profile sections; sensitive ones are "
        "refused and go through a change request."
    ),
    "GET /employees/me/change-requests": "own: the caller's own change requests.",
    "POST /employees/me/change-requests": (
        "own: a change request about the caller's own record, found through the "
        "login's employee link."
    ),
    "PUT /employees/me/photo": "own: the caller's own profile photo.",
    "DELETE /employees/me/photo": "own: the caller's own profile photo.",
    "POST /employees/change-requests/{request_id}/withdraw": (
        "own: only the requester withdraws; anybody else's request is a 404."
    ),
    "GET /employees/{employee_id}/profile": (
        "permissions: the person themselves and HR; identifiers masked for "
        "everyone but HR."
    ),
    "GET /employees/{employee_id}/change-requests": (
        "permissions: the person's own requests, or HR."
    ),
    "GET /employees/{employee_id}/photo": (
        "scoped:tenant_scoped: a directory photo, shown to every colleague beside "
        "the name they can already see; another workspace's id is a 404."
    ),
    "POST /exits/resign": (
        "own: the caller's own resignation; whose it is comes from the login."
    ),
    "GET /exits/mine": "own: the caller's own exit record.",
    "GET /exits/clearance/mine": (
        "own: clearance items naming the caller as owner, or waiting on them as the "
        "leaver's line manager; never the caller's own exit."
    ),
    "GET /exits/{exit_id}/clearance": (
        "scoped:visible_exit: HR, the leaver and their reporting line, or a named "
        "owner of an item on this exit; anyone else is told it does not exist."
    ),
    "PATCH /exits/clearance/{item_id}": (
        "scoped:require_item_permission: the item's named owner, the leaver's line "
        "manager for a manager item, or HR - never on the caller's own exit."
    ),
    "POST /exits/{exit_id}/withdraw": (
        "own: the caller withdraws their own unaccepted resignation; anybody "
        "else's exit is a 404."
    ),
    "GET /org/departments": "reference: the organisation's structure.",
    "GET /org/legal-entities": "reference: the organisation's structure.",
    "GET /org/locations": "reference: the organisation's structure.",
}
