"""Asset management: what everybody holds, acknowledges and asks for."""

ENDPOINTS: dict[str, str] = {
    "GET /assets/me": "permissions: the assets held by your own employee record.",
    "POST /assets/assignments/{assignment_id}/acknowledge": (
        "permissions: only the holder acknowledges receipt (require_self), HR included."
    ),
    "GET /assets/categories": (
        "reference: the tenant's asset categories; inactive ones only for HR."
    ),
    "GET /assets/requests": (
        "permissions: your own requests; scope=review is your reporting line for a "
        "manager and everybody for HR, never your own."
    ),
    "POST /assets/requests": "permissions: always a request for your own employee record.",
    "POST /assets/requests/{request_id}/cancel": (
        "permissions: only the person who asked can withdraw it (require_self)."
    ),
}
