"""Payroll runs, payslips and salary self-service.

Tax declarations and encashment are declared in `payroll.py`. Everything about
running payroll is HR-gated at the router; only these read-your-own routes are
open to every signed-in person.
"""

ENDPOINTS: dict[str, str] = {
    "GET /payroll/my-payslips": (
        "own: lists only the caller's own published payslips, found by user.employee_id "
        "from the token; takes no person id."
    ),
    "GET /payroll/my-payslips/{payslip_id}": (
        "own: the payslip must belong to user.employee_id and be published; any other id, "
        "including another tenant's, is a 404."
    ),
    "GET /payroll/my-salary": (
        "own: the caller's own salary history, by user.employee_id; takes no person id."
    ),
}
