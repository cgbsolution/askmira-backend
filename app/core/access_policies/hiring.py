"""Hiring, offers, interviews and recruiting."""

ENDPOINTS: dict[str, str] = {
    "GET /hiring/requisitions": (
        "reference: open roles - title, department, openings. No salary, no candidates."
    ),
    "GET /hiring/candidates": (
        "permissions: HR, or the hiring manager / recruiter on the requisition."
    ),
    "GET /recruiting/posture": "reference: what the module enforces.",
    "GET /recruiting/interviewer-framing": "reference: static guidance.",
    "GET /recruiting/share-link": "permissions: the referrer must be you unless HR.",
    "POST /recruiting/referrals": "permissions: the referrer must be you unless HR.",
}
