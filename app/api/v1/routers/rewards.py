"""Points liability, budgets, tax treatment and reconciliation (slice 33).

`GET /rewards/liability` is the endpoint finance opens: outstanding points in
rupees at the rate each award was issued at, aged, split by cost centre, with an
expected redemption rate for provisioning and a chain-integrity check beside it.

No endpoint takes a date that decides valuation, expiry or tax year from the
client. Awards and redemptions happen today; expiry runs as of today.

`GET /rewards/posture` says what is enforced and - first in the list - that **no
tax advisor has reviewed the thresholds**, which gates the whole tax section.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.rewards import RedemptionStatusEnum
from app.models.user import UserRole
from app.schemas.rewards import (
    BudgetCreate,
    ExpiryPolicyRead,
    ExpiryPolicyUpdate,
    ExpiryWarningRunRead,
    PointsAwardCreate,
    ReconciliationCreate,
    ReconciliationRunRead,
    RedemptionCreate,
    RedemptionRead,
    ReversalCreate,
    RewardsImportCreate,
    RewardsImportRead,
    TaxRuleCreate,
    TaxRuleRead,
    ValuationCreate,
    ValuationRead,
)
from app.services.rewards import catalogue, portal
from app.services.rewards import service as rewards
from app.services.rewards import tax as tax_engine

router = APIRouter(tags=["rewards"])

FINANCE_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
ADMIN_ROLES = (UserRole.ADMIN,)


# ── Posture ─────────────────────────────────────────────────────────────────


@router.get("/rewards/posture")
async def posture(user: CurrentUser) -> dict[str, Any]:
    """What is enforced, and what is not built. Not role-gated."""
    return rewards.rewards_posture()


@router.get("/rewards/tax-rules/defaults")
async def default_tax_rules(
    user: CurrentUser,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """The shipped defaults, every one unreviewed, with the reading behind each."""
    return {
        "rules": [
            {
                "reward_type": r.reward_type,
                "treatment": r.treatment.value,
                "exemption_threshold_per_year": str(r.exemption_threshold_per_year),
                "effective_from": r.effective_from.isoformat(),
                "basis": r.basis,
                "reviewed": r.reviewed,
            }
            for r in tax_engine.DEFAULT_RULES
        ],
        "warning": (
            "Every rule here is unreviewed and produces a provisional figure. "
            "Payroll refuses a provisional perquisite. Have these reviewed by "
            "the tenant's tax advisor before anything is filed."
        ),
    }


# ── Configuration ───────────────────────────────────────────────────────────


@router.get("/rewards/valuations", response_model=list[ValuationRead])
async def list_valuations(
    session: DbSession,
    user: CurrentUser,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> list[ValuationRead]:
    return await portal.list_valuations(session, user)


@router.post("/rewards/valuations", status_code=status.HTTP_201_CREATED)
async def set_valuation(
    body: ValuationCreate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Add a valuation from a date. Issued points keep the rate they were
    awarded at, so the liability does not move."""
    return await rewards.set_valuation(
        session,
        user,
        points_per_rupee=body.points_per_rupee,
        effective_from=body.effective_from,
        note=body.note,
        request=request,
    )


@router.get("/rewards/expiry-policy", response_model=ExpiryPolicyRead)
async def get_expiry_policy(session: DbSession, user: CurrentUser) -> ExpiryPolicyRead:
    """The expiry policy and its disclosure - which every employee may read."""
    return await portal.get_expiry_policy(session, user)


@router.put("/rewards/expiry-policy", response_model=ExpiryPolicyRead)
async def set_expiry_policy(
    body: ExpiryPolicyUpdate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> ExpiryPolicyRead:
    await rewards.set_expiry_policy(
        session,
        user,
        enabled=body.enabled,
        expire_after_days=body.expire_after_days,
        notify_days_before=body.notify_days_before,
        disclosure=body.disclosure.strip(),
        request=request,
    )
    return await portal.get_expiry_policy(session, user)


@router.get("/rewards/tax-rules", response_model=list[TaxRuleRead])
async def list_tax_rules(
    session: DbSession,
    user: CurrentUser,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> list[TaxRuleRead]:
    return await portal.list_tax_rules(session, user)


@router.post("/rewards/tax-rules", response_model=TaxRuleRead, status_code=status.HTTP_201_CREATED)
async def set_tax_rule(
    body: TaxRuleCreate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> TaxRuleRead:
    row = await rewards.set_tax_rule(
        session,
        user,
        reward_type=body.reward_type.strip(),
        treatment=body.treatment,
        exemption_threshold_per_year=body.exemption_threshold_per_year,
        effective_from=body.effective_from,
        basis=body.basis,
        reviewed=body.reviewed,
        reviewed_by=(body.reviewed_by or "").strip() or None,
        reviewed_on=body.reviewed_on,
        request=request,
    )
    return TaxRuleRead.model_validate(row)


# ── Budgets ─────────────────────────────────────────────────────────────────


@router.get("/rewards/budgets")
async def budget_utilisation(
    session: DbSession,
    user: CurrentUser,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    return await portal.budget_report(session, user, as_of=date.today())


@router.post("/rewards/budgets", status_code=status.HTTP_201_CREATED)
async def create_budget(
    body: BudgetCreate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    return await portal.create_budget(session, user, body, request=request)


# ── The ledger ──────────────────────────────────────────────────────────────


@router.post("/rewards/awards", status_code=status.HTTP_201_CREATED)
async def award_points(
    body: PointsAwardCreate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """Award points, today.

    Returns 409 when a hard-stop budget is exhausted. The expiry disclosure is
    in the response so it appears at the point of award.
    """
    permissions.require_role(
        user,
        permissions.HR_ROLES,
        line_managers=True,
        detail="Points are awarded by managers and HR.",
    )
    if body.employee_id == user.employee_id:
        raise permissions.forbidden("You cannot award points to yourself.")
    return await rewards.award_points(
        session,
        user,
        employee_id=body.employee_id,
        points=body.points,
        awarded_on=date.today(),
        source_type=body.source_type,
        source_id=body.source_id,
        request=request,
    )


@router.post("/rewards/redemptions", status_code=status.HTTP_201_CREATED)
async def redeem_points(
    body: RedemptionCreate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """Spend points today, valued at the rate of the lots actually consumed."""
    employee_id = permissions.resolve_employee(
        user, body.employee_id, detail="You can only redeem your own points."
    )
    if not permissions.is_hr(user) and await catalogue.has_catalogue(session, user):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Choose an item from the rewards catalogue.",
        )
    return await rewards.redeem_points(
        session,
        user,
        employee_id=employee_id,
        points=body.points,
        requested_on=date.today(),
        item_sku=body.item_sku,
        item_description=body.item_description.strip(),
        request=request,
    )


@router.get("/rewards/redemptions", response_model=list[RedemptionRead])
async def list_redemptions(
    session: DbSession,
    user: CurrentUser,
    redemption_status: RedemptionStatusEnum | None = None,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> list[RedemptionRead]:
    return await portal.list_redemptions(session, user, status_filter=redemption_status)


@router.post("/rewards/reversals", status_code=status.HTTP_201_CREATED)
async def reverse_entry(
    body: ReversalCreate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Undo an award with a new row. Nothing is edited and nothing is deleted."""
    return await rewards.reverse_entry(
        session,
        user,
        entry_id=body.entry_id,
        reason=body.reason.strip(),
        occurred_on=date.today(),
        request=request,
    )


@router.post("/rewards/expiry-run")
async def expire_points(
    session: DbSession,
    user: CurrentUser,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Write EXPIRED rows for lots past the policy, as of today.

    The same function a scheduler would call. No scheduler runs here.
    """
    return await portal.run_expiry(session, user.tenant_id)


@router.post("/rewards/expiry-warnings", response_model=ExpiryWarningRunRead)
async def expiry_warnings(
    session: DbSession,
    user: CurrentUser,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> ExpiryWarningRunRead:
    """Warn people whose points expire inside the notice window, once per date.

    The same function a scheduler would call. No scheduler runs here.
    """
    return await portal.send_expiry_warnings(session, user.tenant_id)


# ── Reporting ───────────────────────────────────────────────────────────────


@router.get("/rewards/liability")
async def liability(
    session: DbSession,
    user: CurrentUser,
    as_of: date | None = None,
    group_by: str = "department_key",
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Outstanding points in rupees, aged, split, with a chain-integrity check."""
    return await rewards.liability_report(
        session, user, as_of=as_of or date.today(), group_by=group_by
    )


@router.get("/rewards/movement")
async def movement(
    session: DbSession,
    user: CurrentUser,
    period_start: date,
    period_end: date,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Opening, awarded, redeemed, expired, adjusted, closing - checked."""
    return await rewards.movement(
        session, user, period_start=period_start, period_end=period_end
    )


@router.get("/rewards/perquisites/{employee_id}")
async def perquisites(
    employee_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    financial_year: str,
    as_of: date | None = None,
    allow_provisional: bool = False,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """What should flow into payroll and TDS. Nothing in payroll consumes it here."""
    return await rewards.perquisites(
        session,
        user,
        employee_id=employee_id,
        financial_year=financial_year,
        as_of=as_of or date.today(),
        allow_provisional=allow_provisional,
    )


@router.get("/rewards/me/statement")
async def my_statement(session: DbSession, user: CurrentUser) -> dict[str, Any]:
    """Your balance, statement, expiry and tax position for this financial year."""
    return await portal.my_statement(session, user)


@router.get("/rewards/statement/{employee_id}")
async def employee_statement(
    employee_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    financial_year: str,
    as_of: date | None = None,
) -> dict[str, Any]:
    """One employee's balance, rows, expiry and tax position."""
    permissions.require_self_or_hr(
        user, employee_id, detail="You can only see your own rewards statement."
    )
    return await rewards.employee_statement(
        session,
        user,
        employee_id=employee_id,
        financial_year=financial_year,
        as_of=as_of or date.today(),
    )


# ── Reconciliation ──────────────────────────────────────────────────────────


@router.post(
    "/rewards/partner-imports",
    response_model=RewardsImportRead,
    status_code=status.HTTP_201_CREATED,
)
async def import_partner_records(
    body: RewardsImportCreate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> RewardsImportRead:
    """Load the merchandise partner's fulfilment and invoice rows, as received."""
    return await portal.import_partner_records(session, user, body, request=request)


@router.get("/rewards/reconciliations", response_model=list[ReconciliationRunRead])
async def list_reconciliations(
    session: DbSession,
    user: CurrentUser,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> list[ReconciliationRunRead]:
    return await portal.list_reconciliations(session, user)


@router.post("/rewards/reconciliations", status_code=status.HTTP_201_CREATED)
async def run_reconciliation(
    body: ReconciliationCreate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Match redemptions, fulfilments and invoices for a month, every gap
    reported separately."""
    return await rewards.run_reconciliation(
        session,
        user,
        period_start=body.period_start,
        period_end=body.period_end,
        period_label=body.period_label,
        request=request,
    )



# ── Catalogue ───────────────────────────────────────────────────────────────


@router.get("/rewards/catalogue")
async def list_catalogue(
    session: DbSession, user: CurrentUser, include_inactive: bool = False
) -> list[dict[str, Any]]:
    """What points can be spent on, cheapest first in each category."""
    return await catalogue.list_items(session, user, include_inactive=include_inactive)


@router.post(
    "/rewards/catalogue",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_catalogue_item(
    body: catalogue.CatalogueItemIn, session: DbSession, user: CurrentUser, request: Request
) -> dict[str, Any]:
    return await catalogue.create_item(session, user, body, request=request)


@router.patch(
    "/rewards/catalogue/{item_id}", dependencies=[Depends(require_roles(*ADMIN_ROLES))]
)
async def update_catalogue_item(
    item_id: uuid.UUID,
    body: catalogue.CatalogueItemUpdate,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    return await catalogue.update_item(session, user, item_id, body, request=request)


@router.post("/rewards/catalogue/{item_id}/redeem", status_code=status.HTTP_201_CREATED)
async def redeem_catalogue_item(
    item_id: uuid.UUID, session: DbSession, user: CurrentUser, request: Request
) -> dict[str, Any]:
    """Spend your points on one item; one is taken from stock."""
    return await catalogue.redeem_item(session, user, item_id, request=request)
