"""Hiring, offers, interviews and recruiting.

Almost every hiring endpoint carries a role gate. These are the ones every
signed-in person reaches, and the rule that makes each safe.
"""

ENDPOINTS: dict[str, str] = {
    "GET /hiring/requisitions": (
        "scoped:visible_requisitions: HR lists every requisition; a manager lists open "
        "roles plus the ones they are on the hiring team of; anybody else lists open "
        "roles only. The list carries no budget and no candidates."
    ),
    "GET /recruiting/posture": "reference: what the module enforces.",
    "GET /recruiting/interviewer-framing": "reference: static guidance.",
    "GET /recruiting/open-roles": (
        "reference: open roles - title, department, location, openings. No budget, "
        "no candidates."
    ),
    "GET /recruiting/referral-programmes": (
        "reference: active referral programme terms, identical for everyone."
    ),
    "GET /recruiting/my-referrals": (
        "own: the caller's own referrals, found by their own employee id from the token."
    ),
    "GET /recruiting/share-link": "permissions: the referrer must be you unless HR.",
    "POST /recruiting/referrals": "permissions: the referrer must be you unless HR.",
    "POST /recruiting/refer": (
        "permissions: the referrer is always the caller's own employee record "
        "(permissions.own_employee_id); PIP and hiring-team eligibility are read from "
        "the records, never from the request."
    ),
}
