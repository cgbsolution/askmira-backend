"""Time off and attendance."""

ENDPOINTS: dict[str, str] = {
    # ── Timesheets ──────────────────────────────────────────────────────────
    "GET /timesheets/projects": "reference: the projects open for logging time.",
    "GET /timesheets/me": (
        "own: the caller's own week, by user.employee_id from the token; takes no person id."
    ),
    "PUT /timesheets/me": "own: the caller's own week, by user.employee_id from the token.",
    "POST /timesheets/me/submit": (
        "own: submits the caller's own week, by user.employee_id from the token."
    ),
    "GET /timesheets/pending": (
        "permissions: weeks whose approval chain's current step names the caller or someone "
        "they stand in for, older weeks from their reporting line, everything for HR; never "
        "the caller's own."
    ),
    "POST /timesheets/{timesheet_id}/decide": (
        "permissions: the current step's approver, their delegate or HR (the line manager "
        "or HR for a week from before chains); never the caller's own week."
    ),
    "GET /attendance/regularisations/pending": (
        "permissions: corrections whose approval chain's current step names the caller or "
        "someone they stand in for, older ones from their reporting line, everything for HR; "
        "never the caller's own."
    ),
    "PATCH /attendance/regularisation/{regularisation_id}": (
        "permissions: the current step's approver, their delegate or HR (the manager or HR "
        "for a request from before chains); never the caller's own."
    ),
    "GET /attendance/punches/{punch_id}/photo": (
        "scoped:require_manager_or_hr: the person's own punch photo, or their manager line and HR."
    ),
    "GET /attendance/kiosk-pin": (
        "own: whether the caller has a kiosk PIN, by user.employee_id from the token; "
        "takes no person id."
    ),
    "PUT /attendance/kiosk-pin": (
        "own: sets the caller's own kiosk PIN, by user.employee_id from the token."
    ),
    "GET /attendance/shift-swaps": (
        "own: swaps the caller asked for or was asked into, by user.employee_id from the "
        "token; takes no person id."
    ),
    "POST /attendance/shift-swaps": (
        "own: the requester is always the caller (user.employee_id from the token); the "
        "body names only the colleague asked."
    ),
    "POST /attendance/shift-swaps/{swap_id}/respond": (
        "permissions: only the colleague named on the swap may answer it; the service "
        "compares the token's employee id with the swap."
    ),
    "POST /attendance/shift-swaps/{swap_id}/cancel": (
        "permissions: only the requester may withdraw; the service compares the token's "
        "employee id with the swap."
    ),
    "GET /attendance/overtime/mine": (
        "own: the caller's own overtime, by user.employee_id from the token; takes no person id."
    ),
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
    "GET /leave/settings": (
        "reference: the optional-holiday quota and comp-off expiry, about nobody in particular."
    ),
    "GET /leave/optional-holidays": (
        "permissions: the caller's own optional holidays and choices; needs an employee record."
    ),
    "POST /leave/optional-holidays/{holiday_id}": (
        "permissions: only for yourself - a floating holiday is the employee's own choice."
    ),
    "DELETE /leave/optional-holidays/{holiday_id}": (
        "permissions: only for yourself - a floating holiday is the employee's own choice."
    ),
    "GET /leave/balances/team": (
        "permissions: a manager's reporting line, or everybody for HR; refused to "
        "somebody with no reports."
    ),
    "GET /leave/balances/team.csv": (
        "permissions: a manager's reporting line, or everybody for HR; recorded as an export."
    ),
    "GET /leave/balances/history": (
        "permissions: your own balance history, a manager's reporting line, or anybody's for HR."
    ),
    "POST /leave/attachments": (
        "permissions: a signed-in employee (or HR) stores a file under their own tenant; "
        "the key it returns is refused on any other tenant's request."
    ),
    "GET /leave/requests/{request_id}/document": (
        "permissions: the person, their reporting line, the approvers the chain names, "
        "or HR. Every download is audited."
    ),
    "GET /leave/comp-off": (
        "permissions: own claims, a manager's reporting line, or everybody for HR."
    ),
    "POST /leave/comp-off": (
        "permissions: your own claim unless HR is raising one on somebody's behalf."
    ),
    "GET /leave/comp-off/approvals": (
        "permissions: pending claims naming the caller as approver, or any for HR - never "
        "the caller's own."
    ),
    "POST /leave/comp-off/{claim_id}/approve": (
        "permissions: the named approver or HR, never the claimant."
    ),
    "POST /leave/comp-off/{claim_id}/reject": (
        "permissions: the named approver or HR, never the claimant."
    ),
    "POST /leave/comp-off/{claim_id}/cancel": (
        "permissions: the claimant while it is pending, or HR."
    ),
    "POST /attendance/punch": (
        "permissions: your own punch unless HR; a punch you record for yourself is "
        "timestamped by the server and cannot claim a device source."
    ),
    "GET /attendance/punches": (
        "permissions: your own punches, your reporting line's as their manager, or HR."
    ),
    "GET /attendance/day": (
        "permissions: your own attendance, your reporting line's as their manager, or HR."
    ),
    "GET /attendance/period": (
        "permissions: your own period, your reporting line's as their manager, or HR."
    ),
    "POST /attendance/regularisation": "permissions: your own regularisation unless HR.",
    "POST /attendance/regularisations/range": (
        "permissions: your own on-duty or work-from-home days unless HR."
    ),
    "GET /attendance/regularisations": "permissions: your own regularisations unless HR.",
    "POST /attendance/regularisations/{regularisation_id}/cancel": (
        "permissions: only the person the regularisation is about may withdraw it."
    ),
    "GET /attendance/locks": "reference: which payroll periods are locked.",
    "GET /attendance/settings": (
        "reference: the workspace's punch-location rule, about nobody in particular."
    ),
}
