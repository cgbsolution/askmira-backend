"""V1 API surface. One router per FRD 7.1 service."""

from fastapi import APIRouter, Depends

from app.api.v1.routers import (
    admin,
    agenteval,
    appraisal,
    approvals,
    arrears,
    assets,
    attendance,
    auth,
    autonomy,
    bi,
    calibration,
    careers,
    company_documents,
    competency,
    culture,
    culture_depth,
    cycle_config,
    dashboard,
    devices,
    disbursement,
    documents,
    dpdp,
    employees,
    exit_benefits,
    exit_workflow,
    expenses,
    feedback,
    filings,
    goals,
    grounding,
    gst,
    helpdesk,
    hiring,
    interviews,
    leave,
    letters,
    lifecycle,
    loans,
    notifications,
    offers,
    onboarding,
    one_on_ones,
    opsec,
    org,
    payroll,
    payslips,
    performance_flow,
    positions,
    recruiting,
    reports,
    retention,
    rewards,
    salary,
    sso,
    statutory,
    tax,
)
from app.core.deps import require_module


def _module(key: str) -> list:
    """Router-level gate on the workspace's enabled modules.

    Applied only to routers whose every route is signed in: the gate needs a
    caller, and a public route (careers pages, webhooks, SSO callbacks) has none.
    """
    return [Depends(require_module(key))]


api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(dashboard.router)
api_router.include_router(employees.router)
api_router.include_router(org.router)
api_router.include_router(leave.router, dependencies=_module("leave"))
api_router.include_router(attendance.router, dependencies=_module("leave"))
api_router.include_router(bi.router, dependencies=_module("analytics"))
api_router.include_router(payroll.router, dependencies=_module("payroll"))
api_router.include_router(payslips.router, dependencies=_module("payroll"))
api_router.include_router(salary.router, dependencies=_module("payroll"))
api_router.include_router(tax.router, dependencies=_module("payroll"))
api_router.include_router(statutory.router, dependencies=_module("payroll"))
api_router.include_router(filings.router, dependencies=_module("payroll"))
api_router.include_router(disbursement.router, dependencies=_module("payroll"))
api_router.include_router(arrears.router, dependencies=_module("payroll"))
api_router.include_router(exit_workflow.router)
api_router.include_router(loans.router, dependencies=_module("payroll"))
api_router.include_router(appraisal.router)
api_router.include_router(competency.router)
api_router.include_router(calibration.router)
api_router.include_router(cycle_config.router)
api_router.include_router(offers.router, dependencies=_module("hiring"))
api_router.include_router(careers.router)
api_router.include_router(interviews.router)
api_router.include_router(reports.router)
api_router.include_router(positions.router)
api_router.include_router(lifecycle.router)
api_router.include_router(dpdp.router)
api_router.include_router(retention.router)
api_router.include_router(agenteval.router)
api_router.include_router(grounding.router)
api_router.include_router(autonomy.router)
api_router.include_router(culture_depth.router, dependencies=_module("culture"))
api_router.include_router(expenses.router)
api_router.include_router(gst.router)
api_router.include_router(opsec.router)
api_router.include_router(performance_flow.router)
api_router.include_router(goals.router)
api_router.include_router(one_on_ones.router)
api_router.include_router(feedback.router)
api_router.include_router(recruiting.router, dependencies=_module("hiring"))
api_router.include_router(rewards.router, dependencies=_module("rewards"))
api_router.include_router(exit_benefits.router, dependencies=_module("payroll"))
api_router.include_router(letters.router)
api_router.include_router(company_documents.router, dependencies=_module("core_hr"))
api_router.include_router(documents.router, dependencies=_module("core_hr"))
api_router.include_router(sso.router)
api_router.include_router(devices.router)
api_router.include_router(notifications.router)
api_router.include_router(culture.router, dependencies=_module("culture"))
api_router.include_router(hiring.router, dependencies=_module("hiring"))
api_router.include_router(admin.router)
api_router.include_router(approvals.router)
api_router.include_router(assets.router)
api_router.include_router(onboarding.router)
api_router.include_router(helpdesk.router)
