"""Recognition points, redemptions and statements."""

ENDPOINTS: dict[str, str] = {
    "GET /rewards/posture": "reference: what the module enforces.",
    "GET /rewards/expiry-policy": (
        "reference: the expiry policy and its disclosure, which every employee is "
        "entitled to read before their points expire."
    ),
    "POST /rewards/awards": "permissions: managers and HR, never to yourself.",
    "POST /rewards/redemptions": "permissions: your own points unless HR.",
    "GET /rewards/statement/{employee_id}": "permissions: your own statement unless HR.",
    "GET /rewards/me/statement": (
        "permissions: the caller's own statement (permissions.own_employee_id)."
    ),
}
