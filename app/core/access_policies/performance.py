"""Reviews, goals, calibration, PIPs, increments and promotions.

Everything admin-shaped in these routers is role-gated. What is left here is
reachable by every signed-in person, and each entry says how the service
decides whose record it touches.
"""

_OWN_OR_LINE = "the person, their reporting line (or a delegate/escalation owner) or HR."
_PARTICIPANTS = (
    "scoped:_participant_meeting: only the two people in the 1:1; anybody else, HR "
    "included, gets a 404."
)
_FEEDBACK_READER = (
    "scoped:_viewer_role: the person (as the request's visibility allows), their manager "
    "line, HR or whoever asked; anybody else gets a 404."
)
_REVIEWER = "scoped:_my_reviewer_row: only the colleague who was asked; anybody else gets a 404."

ENDPOINTS: dict[str, str] = {
    # ── Posture and pickers ──────────────────────────────────────────────────
    "GET /performance/posture": "reference: what the module enforces.",
    "GET /performance/people": (
        "permissions: the caller's own record and reporting line, or everybody for HR."
    ),
    # ── Reviews ──────────────────────────────────────────────────────────────
    "GET /appraisal/my": "permissions: the caller's own reviews, from their own employee id.",
    "GET /appraisal/team": (
        "permissions: reviews in the caller's reporting line, delegated to them or escalated "
        "to them; everybody for HR. Never the caller's own."
    ),
    "GET /appraisal/appraisals": (
        "permissions: own reviews, the reporting line's, or everybody's for HR; an "
        "employee_id outside that is refused."
    ),
    "GET /appraisal/appraisals/{appraisal_id}": (
        "permissions: " + _OWN_OR_LINE + " Entries and the outcome are filtered by stage "
        "visibility and release."
    ),
    "POST /appraisal/appraisals/{appraisal_id}/score-preview": "permissions: " + _OWN_OR_LINE,
    "POST /appraisal/appraisals/{appraisal_id}/draft": (
        "permissions: the current step's owner - the person for the self review; their line, "
        "a delegate, an escalation owner or HR for other steps, never the person rated."
    ),
    "POST /appraisal/appraisals/{appraisal_id}/submit": (
        "permissions: the current step's owner - the person for the self review; their line, "
        "a delegate, an escalation owner or HR for other steps, never the person rated."
    ),
    "POST /appraisal/appraisals/{appraisal_id}/return": (
        "permissions: the current step's owner, never the person being reviewed."
    ),
    "POST /appraisal/appraisals/{appraisal_id}/acknowledge": (
        "permissions: only the person reviewed (require_self) - HR cannot acknowledge for them."
    ),
    # ── Goals ────────────────────────────────────────────────────────────────
    "GET /goals": (
        "permissions: own goals, goals shared with the caller's reporting line (above and "
        "below), company goals, or everything for HR."
    ),
    "POST /goals": (
        "permissions: a goal for yourself, or for somebody in your reporting line; company "
        "goals are HR only."
    ),
    "GET /goals/alignable": (
        "permissions: for yourself or somebody in your reporting line (or HR)."
    ),
    "GET /goals/score": "permissions: the person, their reporting line or HR.",
    "GET /goals/{goal_id}": "permissions: visible to the owner, their line per visibility, HR.",
    "PATCH /goals/{goal_id}": (
        "permissions: the owner, somebody above them in the line, or HR; company goals HR."
    ),
    "POST /goals/{goal_id}/key-results": (
        "permissions: the owner, somebody above them in the line, or HR."
    ),
    "POST /goals/{goal_id}/check-ins": (
        "permissions: the owner, somebody above them in the line, or HR."
    ),
    "GET /goals/{goal_id}/check-ins": "permissions: anybody the goal is visible to.",
    "GET /goals/cycles": (
        "reference: the review cycles that are not closed - name, type and period, the "
        "choices a goal may count towards. About nobody in particular."
    ),
    "POST /goals/{goal_id}/check-ins/{check_in_id}/comments": (
        "permissions: the goal's owner, somebody above them in the line, or HR."
    ),
    # ── 1:1s ─────────────────────────────────────────────────────────────────
    "GET /one-on-ones": (
        "own: 1:1s the caller is in, from their own employee id; with_employee_id only "
        "narrows that list."
    ),
    "GET /one-on-ones/reports": "own: the caller's own direct reports and their 1:1s with them.",
    "GET /one-on-ones/partners": (
        "own: the caller's own manager and direct reports, from their own employee id."
    ),
    "POST /one-on-ones": (
        "permissions: with somebody in the caller's reporting line, or with the caller's own "
        "manager."
    ),
    "GET /one-on-ones/{meeting_id}": _PARTICIPANTS,
    "PATCH /one-on-ones/{meeting_id}": _PARTICIPANTS,
    "POST /one-on-ones/{meeting_id}/items": _PARTICIPANTS,
    "PATCH /one-on-ones/{meeting_id}/items/{item_id}": _PARTICIPANTS,
    "DELETE /one-on-ones/{meeting_id}/items/{item_id}": (
        "scoped:_participant_item: one of the two participants, and only for an item they "
        "added themselves."
    ),
    "PUT /one-on-ones/{meeting_id}/my-note": (
        _PARTICIPANTS + " The note is keyed to the caller's login and returned to nobody else."
    ),
    "POST /one-on-ones/{meeting_id}/complete": _PARTICIPANTS,
    "POST /one-on-ones/{meeting_id}/cancel": _PARTICIPANTS,
    # ── 360 and private feedback ─────────────────────────────────────────────
    "GET /feedback/requests": (
        "permissions: requests about the caller, about their reporting line or asked by them; "
        "every request only for HR."
    ),
    "POST /feedback/requests": (
        "permissions: about yourself, about somebody in your reporting line, or anybody for HR."
    ),
    "GET /feedback/requests/{request_id}": _FEEDBACK_READER,
    "POST /feedback/requests/{request_id}/remind": _FEEDBACK_READER,
    "POST /feedback/requests/{request_id}/close": _FEEDBACK_READER,
    "GET /feedback/inbox": "own: requests where the caller's own employee id is a reviewer.",
    "GET /feedback/responses/{reviewer_id}": _REVIEWER,
    "POST /feedback/responses/{reviewer_id}/submit": _REVIEWER,
    "POST /feedback/responses/{reviewer_id}/decline": _REVIEWER,
    "GET /feedback/private": (
        "permissions: feedback the caller received or gave, or received by their reporting line."
    ),
    "POST /feedback/private": (
        "permissions: written as the caller's own employee record, to somebody else."
    ),
    # ── PIPs ─────────────────────────────────────────────────────────────────
    "GET /performance/pips": (
        "permissions: plans in the caller's reporting line, plans they opened, their own "
        "plan, or everything for HR."
    ),
    "POST /performance/pips": ("permissions: the employee's manager or HR, never for yourself."),
    "GET /performance/pips/{pip_id}": (
        "permissions: the person on the plan, their line manager, whoever opened it, or HR."
    ),
    "POST /performance/pips/{pip_id}/check-ins": (
        "permissions: the employee's manager, HR, or whoever opened the plan - "
        "never the person on it."
    ),
    "POST /performance/pips/{pip_id}/milestones/{milestone_id}/assess": (
        "permissions: the employee's manager, HR, or whoever opened the plan - "
        "never the person on it."
    ),
    "POST /performance/pips/{pip_id}/statements": (
        "permissions: only the person on the plan (require_self)."
    ),
    # ── Increments ───────────────────────────────────────────────────────────
    "GET /performance/cycles/{cycle_id}/budget": "permissions: managers and HR.",
    "POST /performance/recommendations": (
        "permissions: the employee's manager or HR, never for yourself."
    ),
}
