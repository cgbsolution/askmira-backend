"""Time off and attendance."""

ENDPOINTS: dict[str, str] = {
    "GET /leave/types": "reference: the leave catalogue.",
    "GET /leave/holidays": "reference: the holiday calendar.",
    "GET /leave/requests": (
        "permissions: own requests, a manager's reporting line, or everybody "
        "for HR. Reasons are routinely medical."
    ),
    "GET /leave/policies": (
        "reference: the policy catalogue - quotas, accrual and notice rules, "
        "about nobody in particular."
    ),
    "GET /leave/balances": (
        "permissions: your own balances, a manager's reporting line, or anybody's for HR."
    ),
    "POST /leave/requests/preview": (
        "permissions: your own request unless HR is raising one on somebody's behalf."
    ),
    "POST /leave/requests": (
        "permissions: your own request unless HR is raising one on somebody's behalf."
    ),
    "GET /leave/approvals": (
        "permissions: pending requests whose current step names the caller, or any "
        "pending request for HR - never the caller's own."
    ),
    "POST /leave/requests/{request_id}/approve": (
        "permissions: the current step's named approver or HR, never the person on leave."
    ),
    "POST /leave/requests/{request_id}/reject": (
        "permissions: the current step's named approver or HR, never the person on leave."
    ),
    "POST /leave/requests/{request_id}/cancel": (
        "permissions: the owner before the leave starts, or HR at any time."
    ),
    "GET /leave/who-is-out": (
        "permissions: everybody sees who is out on approved leave; the leave type and "
        "pending requests only for self, the reporting line and HR."
    ),
    "POST /attendance/punch": (
        "permissions: your own punch unless HR; a punch you record for yourself is "
        "timestamped by the server and cannot claim a device source."
    ),
    "GET /attendance/punches": "permissions: your own punches unless HR.",
    "GET /attendance/day": "permissions: your own attendance unless HR.",
    "GET /attendance/period": "permissions: your own attendance period unless HR.",
    "POST /attendance/regularisation": "permissions: your own regularisation unless HR.",
    "GET /attendance/regularisations": "permissions: your own regularisations unless HR.",
    "POST /attendance/regularisations/{regularisation_id}/cancel": (
        "permissions: only the person the regularisation is about may withdraw it."
    ),
    "GET /attendance/locks": "reference: which payroll periods are locked.",
}
