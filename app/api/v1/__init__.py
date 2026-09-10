"""V1 API surface. One router per FRD 7.1 service."""

from fastapi import APIRouter

from app.api.v1.routers import (
    admin,
    agenteval,
    appraisal,
    arrears,
    attendance,
    auth,
    autonomy,
    bi,
    calibration,
    careers,
    competency,
    culture,
    culture_depth,
    cycle_config,
    dashboard,
    devices,
    disbursement,
    dpdp,
    employees,
    exit_benefits,
    exit_workflow,
    expenses,
    filings,
    grounding,
    gst,
    hiring,
    interviews,
    leave,
    letters,
    lifecycle,
    loans,
    notifications,
    offers,
    opsec,
    org,
    payroll,
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

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(dashboard.router)
api_router.include_router(employees.router)
api_router.include_router(org.router)
api_router.include_router(leave.router)
api_router.include_router(attendance.router)
api_router.include_router(bi.router)
api_router.include_router(payroll.router)
api_router.include_router(salary.router)
api_router.include_router(tax.router)
api_router.include_router(statutory.router)
api_router.include_router(filings.router)
api_router.include_router(disbursement.router)
api_router.include_router(arrears.router)
api_router.include_router(exit_workflow.router)
api_router.include_router(loans.router)
api_router.include_router(appraisal.router)
api_router.include_router(competency.router)
api_router.include_router(calibration.router)
api_router.include_router(cycle_config.router)
api_router.include_router(offers.router)
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
api_router.include_router(culture_depth.router)
api_router.include_router(expenses.router)
api_router.include_router(gst.router)
api_router.include_router(opsec.router)
api_router.include_router(performance_flow.router)
api_router.include_router(recruiting.router)
api_router.include_router(rewards.router)
api_router.include_router(exit_benefits.router)
api_router.include_router(letters.router)
api_router.include_router(sso.router)
api_router.include_router(devices.router)
api_router.include_router(notifications.router)
api_router.include_router(culture.router)
api_router.include_router(hiring.router)
api_router.include_router(admin.router)

# V2+ routers mount here as they land: analytics, ai (Claude Companion),
# benefits, rewards, integrations, and the MCP server (V3), which mounts on the
# same permission gateway as the AI Companion rather than a parallel one.

__all__ = ["api_router"]
