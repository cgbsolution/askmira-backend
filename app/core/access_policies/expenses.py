"""Expenses, travel, advances and GST input credit."""

ENDPOINTS: dict[str, str] = {
    "GET /expenses/posture": "reference: what the module enforces.",
    "POST /expenses/claims": "permissions: your own claims unless finance.",
    "POST /expenses/claims/{claim_id}/submit": "permissions: your own claim unless finance.",
    "POST /expenses/travel-requests": "permissions: your own trips unless finance.",
    "GET /gst/posture": "reference: what the module enforces.",
    "GET /gst/validate-gstin": "reference: a checksum on a string.",
    "POST /gst/credits": (
        "permissions: outside finance, only against your own expense claim."
    ),
}
