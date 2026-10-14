"""Payroll self-service: tax declarations, encashment, loans and reimbursements.

Workstream W3. Every entry below resolves the person from the token through
`app.core.permissions` — the caller's own record unless payroll says otherwise —
or is catalogue material about nobody in particular.
"""

ENDPOINTS: dict[str, str] = {
    "GET /esop/me": "own: the caller's own grants, by user.employee_id from the token.",
    # ── Earned wage access ──────────────────────────────────────────────────
    "GET /ewa/policy": "reference: whether advances are on and the share allowed; no person data.",
    "GET /ewa/me": "own: the caller's own earned pay and advances, by user.employee_id.",
    "POST /ewa/requests": "own: an advance for the caller (user.employee_id from the token).",
    "GET /ewa/requests": "permissions: HR reads every request; anyone else only their own.",
    # ── Benefits ────────────────────────────────────────────────────────────
    "GET /benefits/plans": (
        "permissions: active plans for everyone; ended plans and enrolment counts for HR."
    ),
    "GET /benefits/me": (
        "own: the caller's own enrolments, by user.employee_id from the token."
    ),
    "POST /benefits/plans/{plan_id}/enrol": (
        "own: enrols the caller (user.employee_id from the token) in the plan."
    ),
    "GET /benefits/life-events": (
        "permissions: HR reads pending changes; anyone else only their own."
    ),
    "POST /benefits/life-events": (
        "own: the caller's own change, against their own enrolment (user.employee_id)."
    ),
    # ── Leave encashment ────────────────────────────────────────────────────
    "GET /encashment/policies": "reference: the encashment rules.",
    "POST /encashment/requests": "permissions: your own encashment unless payroll.",
    "GET /encashment/requests": (
        "permissions: your own encashments, a manager's reporting line, or everybody for "
        "payroll."
    ),
    # ── Tax declarations ────────────────────────────────────────────────────
    "GET /tax/windows": "reference: declaration and proof windows.",
    "GET /tax/regime": "permissions: your own regime election unless payroll.",
    "POST /tax/regime": "permissions: your own regime election unless payroll.",
    "GET /tax/regime/compare": "permissions: your own comparison unless payroll.",
    "GET /tax/declarations": "permissions: your own declarations unless payroll.",
    "PUT /tax/declarations": "permissions: your own declarations unless payroll.",
    "POST /tax/declarations/{declaration_id}/submit": (
        "permissions: the declaration must be yours unless payroll."
    ),
    "GET /tax/house-rent": "permissions: your own HRA claim unless payroll.",
    "PUT /tax/house-rent": "permissions: your own HRA claim unless payroll.",
    "GET /tax/previous-employer": "permissions: your own record unless payroll.",
    "PUT /tax/previous-employer": "permissions: always the caller's own record.",
    "POST /tax/previous-employer/document": (
        "permissions: always the caller's own Form 16, resolved from the token."
    ),
    "GET /tax/previous-employer/document": (
        "permissions: your own Form 16 unless payroll; payroll reads are audited."
    ),
    "POST /tax/proofs": "permissions: the declaration must be yours unless payroll.",
    "GET /tax/proofs/{proof_id}/document": (
        "permissions: the proof's declaration must be yours unless payroll; payroll reads "
        "are audited."
    ),
    "GET /tax/projection": "permissions: your own projection unless payroll.",
    "GET /tax/snapshots": "permissions: your own computations unless payroll.",
    "GET /tax/form-12bb": "permissions: your own Form 12BB unless payroll.",
    "POST /tax/form-12bb/issue": "permissions: your own Form 12BB unless payroll.",
    "GET /tax/form-12bb/documents": "permissions: your own Form 12BB unless payroll.",
    "GET /tax/form-12bb/{letter_id}/pdf": (
        "permissions: the issued form must be yours unless payroll."
    ),
    "GET /tax/form-16": "permissions: your own Form 16 years unless payroll.",
    "GET /tax/form-16/pdf": (
        "permissions: your own Form 16 Part B unless payroll; payroll downloads are audited."
    ),
    # ── Loans ───────────────────────────────────────────────────────────────
    "GET /loans/schemes": "reference: the loan catalogue - terms, about nobody in particular.",
    "GET /loans/mine": "permissions: the caller's own loans, from the token.",
    "POST /loans/mine": "permissions: an application for the caller, from the token.",
    "GET /loans/{loan_id}": "permissions: the borrower's own loan unless payroll.",
    # ── Reimbursement components ────────────────────────────────────────────
    "GET /reimbursements/components": (
        "reference: the reimbursement catalogue - entitlements, about nobody in particular."
    ),
    "POST /reimbursements/claims": (
        "permissions: a claim for yourself; payroll may claim on someone's behalf."
    ),
    "GET /reimbursements/claims": "permissions: your own claims unless payroll.",
    "GET /reimbursements/claims/{claim_id}/bill": (
        "permissions: the claim must be yours unless payroll; payroll reads are audited."
    ),
    "GET /reimbursements/position": "permissions: your own entitlement unless payroll.",
}
