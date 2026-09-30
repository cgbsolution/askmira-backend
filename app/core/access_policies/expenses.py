"""Expenses, travel, advances and GST input credit."""

ENDPOINTS: dict[str, str] = {
    "GET /expenses/posture": "reference: what the module enforces.",
    "GET /expenses/meta": (
        "reference: categories, vehicles and currencies from the tenant's configuration."
    ),
    "POST /expenses/receipts": "permissions: a receipt for your own claims unless finance.",
    "GET /expenses/receipts/{receipt_id}": (
        "permissions: the employee the receipt belongs to, or finance."
    ),
    "POST /expenses/claims": "permissions: your own claims unless finance.",
    "GET /expenses/claims": (
        "permissions: your own claims; everybody's only for finance (scope=all)."
    ),
    "GET /expenses/claims/{claim_id}": (
        "permissions: your own claim, one from your reporting line for its line manager, "
        "or any for finance."
    ),
    "POST /expenses/claims/{claim_id}/submit": "permissions: your own claim unless finance.",
    "POST /expenses/claims/{claim_id}/cancel": "permissions: your own draft claim unless finance.",
    "POST /expenses/reports": "permissions: your own expense reports unless finance.",
    "GET /expenses/reports": (
        "permissions: your own expense reports; everybody's only for finance (scope=all)."
    ),
    "GET /expenses/reports/{report_id}": "permissions: your own expense report unless finance.",
    "POST /expenses/reports/{report_id}/submit": (
        "permissions: your own expense report unless finance."
    ),
    "POST /expenses/travel-requests": "permissions: your own trips unless finance.",
    "GET /expenses/travel-requests": (
        "permissions: your own trips; everybody's only for finance (scope=all)."
    ),
    "POST /expenses/travel-requests/{travel_request_id}/cancel": (
        "permissions: your own trip, before a decision, unless finance."
    ),
    "GET /expenses/advances": (
        "permissions: your own advances; everybody's only for finance (scope=all)."
    ),
    "GET /expenses/advances/block-status": (
        "permissions: whether your own next advance would be blocked, unless finance."
    ),
    "GET /gst/posture": "reference: what the module enforces.",
    "GET /gst/validate-gstin": "reference: a checksum on a string.",
    "POST /gst/credits": (
        "permissions: outside finance, only against your own expense claim."
    ),
    "GET /gst/claims/{claim_id}/credits": (
        "permissions: GST invoices on your own expense claim, or any claim for finance."
    ),
}
