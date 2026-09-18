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
    "GET /org/departments": "reference: the organisation's structure.",
    "GET /org/legal-entities": "reference: the organisation's structure.",
    "GET /org/locations": "reference: the organisation's structure.",
}
