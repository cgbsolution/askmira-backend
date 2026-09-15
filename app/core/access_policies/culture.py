"""Shoutouts, clubs, values, awards, surveys, announcements and celebrations."""

ENDPOINTS: dict[str, str] = {
    "GET /culture/shoutouts": (
        "permissions: company shoutouts for all; department ones for that "
        "department and HR; private ones for the two people in them."
    ),
    "POST /culture/shoutouts": "own: always sent from the caller's own record.",
    "GET /culture/clubs": "reference: the club directory.",
    "GET /culture/posture": "reference: what the module enforces.",
    "GET /culture/instruments": "reference: survey instruments and the reporting floor.",
    "POST /culture/value-tags": "permissions: managers and HR tag values directly.",
    "POST /culture/nominations": "permissions: you nominate in your own name unless HR.",
    "POST /culture/nominations/{nomination_id}/score": (
        "scoped:juror_user_ids: only a juror on the cycle may score."
    ),
    "GET /culture/nominations/{nomination_id}/jury": (
        "permissions: jurors on the cycle and HR."
    ),
    "POST /culture/surveys/{survey_id}/responses": (
        "permissions: only the person the survey was sent to - HR included."
    ),
    "GET /culture/surveys/{survey_id}/results": (
        "scoped:min_responses: aggregates only, suppressed below the reporting floor."
    ),
    "POST /culture/action-plans": "permissions: managers and HR create action plans.",
    "POST /culture/action-plans/{plan_id}/complete": (
        "permissions: the plan's owner or HR."
    ),
    "POST /culture/announcements/{announcement_id}/acknowledge": (
        "permissions: only the recipient acknowledges - HR included."
    ),
    "PUT /culture/event-privacy": "permissions: your own privacy settings unless HR.",
    "GET /culture/calendar": (
        "scoped:EventPrivacyPreference: birthdays and anniversaries pass through "
        "each person's own privacy preference."
    ),
}
