"""Recognition points, redemptions and statements."""

ENDPOINTS: dict[str, str] = {
    "GET /rewards/posture": "reference: what the module enforces.",
    "POST /rewards/awards": "permissions: managers and HR, never to yourself.",
    "POST /rewards/redemptions": "permissions: your own points unless HR.",
    "GET /rewards/statement/{employee_id}": "permissions: your own statement unless HR.",
}
