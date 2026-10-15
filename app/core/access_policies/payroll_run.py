"""Payroll runs, payslips and salary self-service.

Tax declarations and encashment are declared in `payroll.py`. Everything about
running payroll is HR-gated at the router; only these read-your-own routes are
open to every signed-in person.
"""

ENDPOINTS: dict[str, str] = {
    "GET /payroll/payslip-protection": (
        "reference: whether the workspace's payslip PDFs open with a password, and the rule."
    ),
    "GET /payroll/my-payslips": (
        "own: lists only the caller's own published payslips, found by user.employee_id "
        "from the token; takes no person id."
    ),
    "GET /payroll/my-payslips/{payslip_id}": (
        "own: the payslip must belong to user.employee_id and be published; any other id, "
        "including another tenant's, is a 404."
    ),
    "GET /payroll/my-payslips/{payslip_id}/pdf": (
        "own: the PDF of a payslip that belongs to user.employee_id and is published; any "
        "other id is a 404."
    ),
    "GET /payroll/my-total-rewards": (
        "own: the caller's own statement, by user.employee_id from the token; takes no "
        "person id."
    ),
    "GET /payroll/my-salary": (
        "own: the caller's own salary history, by user.employee_id; takes no person id."
    ),
}
