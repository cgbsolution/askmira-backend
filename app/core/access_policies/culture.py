"""Shoutouts, clubs, values, awards, surveys, announcements and celebrations."""

ENDPOINTS: dict[str, str] = {
    # ── Feed ────────────────────────────────────────────────────────────────
    "GET /culture/shoutouts": (
        "permissions: company shoutouts for all; department ones for that "
        "department and HR; private ones for the two people in them. Decided in "
        "the query, and the total counts only what is visible."
    ),
    "POST /culture/shoutouts": (
        "permissions: always sent from the caller's own record "
        "(permissions.own_employee_id), to a current colleague."
    ),
    "DELETE /culture/shoutouts/{shoutout_id}": (
        "permissions: the sender or HR, and only on a shoutout the caller can see - "
        "an invisible one is a 404."
    ),
    "PUT /culture/shoutouts/{shoutout_id}/reactions": (
        "permissions: the caller's own reaction, only on a shoutout the caller can see."
    ),
    "GET /culture/values": (
        "reference: the company's values, which every colleague recognises against."
    ),
    # ── Polls and events ────────────────────────────────────────────────────
    "GET /culture/polls": (
        "permissions: polls whose audience includes the caller (HR sees all); results "
        "only after voting or closing, never who voted on an anonymous poll."
    ),
    "POST /culture/polls/{poll_id}/vote": (
        "permissions: the caller's own vote (permissions.own_employee_id), only on a "
        "poll whose audience includes them."
    ),
    "GET /culture/events": (
        "permissions: events whose audience includes the caller; who is coming is "
        "listed for HR only."
    ),
    "POST /culture/events/{event_id}/rsvp": (
        "permissions: the caller's own answer (permissions.own_employee_id), only on "
        "an event whose audience includes them."
    ),
    # ── Clubs ───────────────────────────────────────────────────────────────
    "GET /culture/clubs": (
        "scoped:is_private: public clubs for everybody; a private club only for "
        "its members."
    ),
    "POST /culture/clubs": (
        "permissions: created by the caller, who becomes its organiser."
    ),
    "POST /culture/clubs/{club_id}/join": (
        "permissions: the caller joins; a private club is invisible to non-members."
    ),
    "POST /culture/clubs/{club_id}/leave": "permissions: the caller leaves.",
    "GET /culture/clubs/{club_id}/members": (
        "permissions: the club's members only - HR included in the refusal. HR "
        "sees participation in aggregate."
    ),
    # ── Reference ───────────────────────────────────────────────────────────
    "GET /culture/posture": "reference: what the module enforces.",
    "GET /culture/instruments": "reference: survey instruments and the reporting floor.",
    "POST /culture/value-tags": "permissions: managers and HR tag values directly.",
    # ── Awards ──────────────────────────────────────────────────────────────
    "GET /culture/award-programmes": (
        "permissions: active programmes for everybody; inactive ones for HR."
    ),
    "GET /culture/award-cycles": (
        "permissions: open and decided cycles for everybody; drafts and cancelled "
        "cycles for HR."
    ),
    "GET /culture/award-cycles/{cycle_id}/nominations": (
        "permissions: every nomination for the cycle's jurors and HR; otherwise "
        "only the nominations the caller made. No juror sees another's score here."
    ),
    "POST /culture/nominations": (
        "permissions: always in the caller's own name (permissions.own_employee_id), "
        "never for yourself, inside the window by today's server date."
    ),
    "POST /culture/nominations/{nomination_id}/score": (
        "scoped:juror_user_ids: only a juror on the cycle may score."
    ),
    "GET /culture/nominations/{nomination_id}/jury": (
        "permissions: jurors on the cycle and HR; another juror's score only after "
        "submitting your own."
    ),
    "GET /culture/award-results": (
        "scoped:announced_at: only results that have been announced - public "
        "recognition."
    ),
    # ── Surveys and action plans ────────────────────────────────────────────
    "GET /culture/surveys/mine": (
        "permissions: surveys sent to the caller's own record."
    ),
    "POST /culture/surveys/{survey_id}/responses": (
        "permissions: only the person the survey was sent to - HR included - once, "
        "while it is open."
    ),
    "GET /culture/surveys/{survey_id}/results": (
        "scoped:min_responses: aggregates only, suppressed below the reporting floor."
    ),
    "GET /culture/action-plans": (
        "permissions: every plan for HR; otherwise plans the caller owns or holds "
        "an item in."
    ),
    "POST /culture/action-plans": "permissions: managers and HR create action plans.",
    "PATCH /culture/action-plan-items/{item_id}": (
        "permissions: the item's owner, the plan's owner or HR."
    ),
    "POST /culture/action-plans/{plan_id}/notes": (
        "permissions: the plan's owner or HR."
    ),
    "POST /culture/action-plans/{plan_id}/complete": (
        "permissions: the plan's owner or HR."
    ),
    # ── Announcements and celebrations ──────────────────────────────────────
    "GET /culture/announcements": (
        "scoped:_includes: only live announcements whose audience includes the caller."
    ),
    "POST /culture/announcements/{announcement_id}/acknowledge": (
        "permissions: only the recipient acknowledges - HR included - and only an "
        "announcement whose audience includes them."
    ),
    "GET /culture/event-privacy": "permissions: your own celebration settings.",
    "PUT /culture/event-privacy": (
        "permissions: your own celebration settings only - nobody consents on "
        "anybody's behalf."
    ),
    "GET /culture/calendar": (
        "scoped:EventPrivacyPreference: birthdays and anniversaries pass through "
        "each person's own privacy preference, and a team-scoped birthday reaches "
        "only that person's department and HR."
    ),
}
