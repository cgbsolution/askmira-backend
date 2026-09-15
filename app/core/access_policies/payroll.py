"""Payroll self-service: tax declarations, encashment, payslips."""

ENDPOINTS: dict[str, str] = {
    "GET /encashment/policies": "reference: the encashment rules.",
    "POST /encashment/requests": "permissions: your own encashment unless payroll.",
    "GET /encashment/requests": "permissions: your own encashments unless payroll.",
    "GET /tax/windows": "reference: declaration and proof windows.",
    "POST /tax/regime": "permissions: your own regime election unless payroll.",
    "GET /tax/regime/compare": "permissions: your own comparison unless payroll.",
    "GET /tax/declarations": "permissions: your own declarations unless payroll.",
    "PUT /tax/declarations": "permissions: your own declarations unless payroll.",
    "POST /tax/declarations/{declaration_id}/submit": (
        "permissions: the declaration must be yours unless payroll."
    ),
    "PUT /tax/house-rent": "permissions: your own HRA claim unless payroll.",
    "PUT /tax/previous-employer": "permissions: always the caller's own record.",
    "POST /tax/proofs": "permissions: the declaration must be yours unless payroll.",
    "GET /tax/projection": "permissions: your own projection unless payroll.",
    "GET /tax/snapshots": "permissions: your own computations unless payroll.",
    "GET /tax/form-12bb": "permissions: your own Form 12BB unless payroll.",
}
