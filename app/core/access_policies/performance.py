"""Appraisals, calibration, PIPs, increments and promotions."""

ENDPOINTS: dict[str, str] = {
    "GET /performance/posture": "reference: what the module enforces.",
    "POST /performance/pips/{pip_id}/check-ins": (
        "permissions: the employee's manager, HR, or whoever opened the plan - "
        "never the person on it."
    ),
    "GET /performance/cycles/{cycle_id}/budget": "permissions: managers and HR.",
    "POST /performance/recommendations": (
        "permissions: the employee's manager or HR, never for yourself."
    ),
}
