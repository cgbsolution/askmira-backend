"""Points liability, budgets, tax treatment and reconciliation (slice 33).

`GET /rewards/liability` is the endpoint finance opens: outstanding points in
rupees at the rate each award was issued at, aged, split by cost centre, with an
expected redemption rate for provisioning and a chain-integrity check beside it.

`GET /rewards/posture` says what is enforced and — first in the list — that **no
tax advisor has reviewed the thresholds**, which gates the whole tax section.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.rewards import TaxTreatmentEnum
from app.models.user import UserRole
from app.services.rewards import service as rewards
from app.services.rewards import tax as tax_engine

router = APIRouter(tags=["rewards"])

FINANCE_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)


# ── Contracts ───────────────────────────────────────────────────────────────


class ValuationIn(BaseModel):
    points_per_rupee: Decimal = Field(gt=0)
    effective_from: date
    note: str | None = Field(default=None, max_length=2000)


class ExpiryPolicyIn(BaseModel):
    enabled: bool
    #: A floor of 90 days in the engine and a check constraint in the database.
    #: Shorter and people lose points they were still choosing how to spend.
    expire_after_days: int = Field(default=365, ge=90, le=3650)
    notify_days_before: int = Field(default=30, ge=7, le=365)
    #: Required. Points that quietly vanish teach people the recognition was
    #: not real.
    disclosure: str = Field(min_length=1, max_length=2000)


class TaxRuleIn(BaseModel):
    reward_type: str = Field(min_length=1, max_length=64)
    treatment: TaxTreatmentEnum
    exemption_threshold_per_year: Decimal = Field(ge=0)
    effective_from: date
    basis: str | None = Field(default=None, max_length=4000)
    #: False unless a tax advisor is named. "Reviewed" with nobody attached is
    #: the state this flag exists to prevent.
    reviewed: bool = False
    reviewed_by: str | None = Field(default=None, max_length=160)
    reviewed_on: date | None = None


class AwardIn(BaseModel):
    employee_id: uuid.UUID
    points: int = Field(gt=0, le=1_000_000)
    awarded_on: date
    source_type: str = Field(default="recognition", max_length=48)
    source_id: uuid.UUID | None = None
    manager_key: str | None = Field(default=None, max_length=120)


class RedeemIn(BaseModel):
    employee_id: uuid.UUID
    points: int = Field(gt=0, le=1_000_000)
    requested_on: date
    item_sku: str | None = Field(default=None, max_length=64)
    item_description: str | None = Field(default=None, max_length=2000)


class ReverseIn(BaseModel):
    entry_id: uuid.UUID
    #: Shown on the employee's statement. A balance that dropped for no stated
    #: reason is a support conversation nobody can resolve.
    reason: str = Field(min_length=1, max_length=2000)
    occurred_on: date


class ReconcileIn(BaseModel):
    period_start: date
    period_end: date
    period_label: str = Field(min_length=1, max_length=16)


# ── Posture ─────────────────────────────────────────────────────────────────


@router.get("/rewards/posture")
async def posture(user: CurrentUser) -> dict[str, Any]:
    """What is enforced, and what is not built.

    Not role-gated. The first `not_built` line says no tax advisor has reviewed
    the thresholds, and an employee reading their statement is entitled to know
    that the figure marked provisional is genuinely provisional.
    """
    return rewards.rewards_posture()


@router.get("/rewards/tax-rules/defaults")
async def default_tax_rules(
    user: CurrentUser,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """The shipped defaults, every one unreviewed, with the reading behind each.

    Written out so an advisor can disagree with the basis rather than only with
    the number.
    """
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


@router.post("/rewards/valuations", status_code=status.HTTP_201_CREATED)
async def set_valuation(
    body: ValuationIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Add a valuation from a date.

    The response includes what the change does **not** do: issued points keep
    the rate they were awarded at, so the liability does not move.
    """
    return await rewards.set_valuation(
        session,
        user,
        points_per_rupee=body.points_per_rupee,
        effective_from=body.effective_from,
        note=body.note,
        request=request,
    )


@router.put("/rewards/expiry-policy")
async def set_expiry_policy(
    body: ExpiryPolicyIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    row = await rewards.set_expiry_policy(
        session,
        user,
        enabled=body.enabled,
        expire_after_days=body.expire_after_days,
        notify_days_before=body.notify_days_before,
        disclosure=body.disclosure,
        request=request,
    )
    return {
        "enabled": row.enabled,
        "expire_after_days": row.expire_after_days,
        "notify_days_before": row.notify_days_before,
        "disclosure": row.disclosure,
    }


@router.post("/rewards/tax-rules", status_code=status.HTTP_201_CREATED)
async def set_tax_rule(
    body: TaxRuleIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    row = await rewards.set_tax_rule(
        session,
        user,
        reward_type=body.reward_type,
        treatment=body.treatment,
        exemption_threshold_per_year=body.exemption_threshold_per_year,
        effective_from=body.effective_from,
        basis=body.basis,
        reviewed=body.reviewed,
        reviewed_by=body.reviewed_by,
        reviewed_on=body.reviewed_on,
        request=request,
    )
    return {
        "rule_id": str(row.id),
        "reward_type": row.reward_type,
        "treatment": row.treatment.value,
        "reviewed": row.reviewed,
        "note": (
            f"Reviewed by {row.reviewed_by}."
            if row.reviewed
            else "Not reviewed. Payroll will refuse a taxable figure from this "
            "rule until a tax advisor has signed it off."
        ),
    }


# ── The ledger ──────────────────────────────────────────────────────────────


@router.post("/rewards/awards", status_code=status.HTTP_201_CREATED)
async def award_points(
    body: AwardIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """Award points.

    Returns 409 when a hard-stop budget is exhausted, naming the scope and the
    remaining allocation. The expiry disclosure is in the response so it appears
    at the point of award rather than in a policy document.
    """
    return await rewards.award_points(
        session,
        user,
        employee_id=body.employee_id,
        points=body.points,
        awarded_on=body.awarded_on,
        source_type=body.source_type,
        source_id=body.source_id,
        manager_key=body.manager_key,
        request=request,
    )


@router.post("/rewards/redemptions", status_code=status.HTTP_201_CREATED)
async def redeem_points(
    body: RedeemIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """Spend points, valued at the rate of the lots actually consumed."""
    return await rewards.redeem_points(
        session,
        user,
        employee_id=body.employee_id,
        points=body.points,
        requested_on=body.requested_on,
        item_sku=body.item_sku,
        item_description=body.item_description,
        request=request,
    )


@router.post("/rewards/reversals", status_code=status.HTTP_201_CREATED)
async def reverse_entry(
    body: ReverseIn,
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
        reason=body.reason,
        occurred_on=body.occurred_on,
        request=request,
    )


@router.post("/rewards/expiry-run")
async def expire_points(
    session: DbSession,
    user: CurrentUser,
    request: Request,
    as_of: date | None = None,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Write EXPIRED rows for lots past the policy.

    Called by a person or an external scheduler. **Nothing schedules it here**,
    which the posture says.
    """
    return await rewards.expire_points(
        session, user, as_of=as_of or date.today(), request=request
    )


# ── Reporting ───────────────────────────────────────────────────────────────


@router.get("/rewards/liability")
async def liability(
    session: DbSession,
    user: CurrentUser,
    as_of: date | None = None,
    group_by: str = "department_key",
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Outstanding points in rupees, aged, split, with a chain-integrity check.

    Valued at the rate each award was issued at. A revaluation does not move
    this number, which is the whole point.
    """
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
    """Opening, awarded, redeemed, expired, adjusted, closing.

    The arithmetic is checked against the ledger rather than asserted: a
    movement report that does not reconcile raises rather than being signed off.
    """
    return await rewards.movement(
        session, user, period_start=period_start, period_end=period_end
    )


@router.get("/rewards/budgets")
async def budget_utilisation(
    session: DbSession,
    user: CurrentUser,
    as_of: date | None = None,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    return await rewards.budget_utilisation(
        session, user, as_of=as_of or date.today()
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
    """What should flow into payroll and TDS.

    Returns 400 for a taxable figure from an unreviewed rule unless
    `allow_provisional` is set — and even then, nothing in payroll consumes it.
    """
    return await rewards.perquisites(
        session,
        user,
        employee_id=employee_id,
        financial_year=financial_year,
        as_of=as_of or date.today(),
        allow_provisional=allow_provisional,
    )


@router.get("/rewards/statement/{employee_id}")
async def employee_statement(
    employee_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    financial_year: str,
    as_of: date | None = None,
) -> dict[str, Any]:
    """One employee's balance, rows, expiry and tax position.

    Shown before the payslip rather than in it. A recognition programme that
    produces an unexplained deduction has spent money to make somebody feel
    worse.
    """
    return await rewards.employee_statement(
        session,
        user,
        employee_id=employee_id,
        financial_year=financial_year,
        as_of=as_of or date.today(),
    )


@router.post("/rewards/reconciliations", status_code=status.HTTP_201_CREATED)
async def run_reconciliation(
    body: ReconcileIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Match redemptions, fulfilments and invoices for a month.

    Every kind of gap is reported separately. A fulfilment failure and a billing
    error of the same size would cancel to zero in a net variance, and both are
    real.
    """
    return await rewards.run_reconciliation(
        session,
        user,
        period_start=body.period_start,
        period_end=body.period_end,
        period_label=body.period_label,
        request=request,
    )
