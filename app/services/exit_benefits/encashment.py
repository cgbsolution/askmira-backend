"""Leave encashment computation.

Balances are **read** from the leave ledger, never recomputed here. Two places
computing a leave balance is two places to disagree, and the one that disagrees
at exit is the one an employee disputes.

Tax treatment is configuration per policy rather than inferred from an employee
attribute. It differs between government and non-government employees and
between in-service and retirement encashment, and inferring it would be wrong
in exactly the cases that matter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.models.exit_benefits import EncashmentPolicy

__all__ = ["EncashmentError", "EncashmentResult", "compute_encashment"]

ZERO = Decimal(0)
RUPEE = Decimal("0.01")


class EncashmentError(ValueError):
    """The policy forbids this encashment, or the balance does not support it."""


def _money(value: Decimal) -> Decimal:
    return value.quantize(RUPEE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class EncashmentResult:
    days_requested: Decimal
    days_approved: Decimal
    daily_rate: Decimal
    gross_amount: Decimal
    exempt_amount: Decimal
    taxable_amount: Decimal
    # Which limit reduced the request, if any.
    limiting_factor: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_working(self) -> dict[str, Any]:
        return {
            "days_requested": str(self.days_requested),
            "days_approved": str(self.days_approved),
            "daily_rate": str(self.daily_rate),
            "gross_amount": str(self.gross_amount),
            "exempt_amount": str(self.exempt_amount),
            "taxable_amount": str(self.taxable_amount),
            "limiting_factor": self.limiting_factor,
            "notes": self.notes,
        }


def compute_encashment(
    *,
    policy: EncashmentPolicy,
    days_requested: Decimal,
    available_balance: Decimal,
    monthly_wages: Decimal,
    is_exit: bool,
    is_retirement: bool = False,
    days_already_encashed_this_year: Decimal = ZERO,
) -> EncashmentResult:
    """Compute an encashment against a policy.

    `available_balance` comes from the leave ledger. This function does not
    know how it was arrived at and must not.
    """
    if not isinstance(days_requested, Decimal) or not isinstance(monthly_wages, Decimal):
        raise EncashmentError("Days and wages must be Decimals, not floats")
    if days_requested <= ZERO:
        raise EncashmentError("Days requested must be greater than zero")
    if not policy.formula_basis:
        raise EncashmentError(
            f"No wage-basis components are configured for {policy.leave_type_code} "
            "encashment. The Compliance SME maps them before it can be computed."
        )
    if policy.days_per_month_divisor <= 0:
        raise EncashmentError("The encashment divisor must be greater than zero")

    if is_exit and not policy.encashable_on_exit:
        raise EncashmentError(
            f"{policy.leave_type_code} is not encashable on exit under this policy."
        )
    if not is_exit and not policy.encashable_in_service:
        raise EncashmentError(
            f"{policy.leave_type_code} is not encashable while in service under this "
            "policy. It can be encashed at exit."
        )

    notes: list[str] = []
    approved = days_requested
    limiting: str | None = None

    # ── Balance ─────────────────────────────────────────────────────────────
    if approved > available_balance:
        approved = available_balance
        limiting = "available_balance"
        notes.append(f"Requested {days_requested} days against a balance of {available_balance}.")

    # ── Minimum retained balance, in-service only ───────────────────────────
    if not is_exit and policy.min_balance_after_encashment > 0:
        floor = Decimal(str(policy.min_balance_after_encashment))
        allowed = max(ZERO, available_balance - floor)
        if approved > allowed:
            approved = allowed
            limiting = "min_balance_after_encashment"
            notes.append(
                f"This policy requires {floor} days to remain after an in-service encashment."
            )

    # ── Annual cap, in-service only ─────────────────────────────────────────
    # It rations encashment while someone stays. At exit the accumulated
    # balance is paid out, up to the per-event cap: holding a leaver to this
    # year's allowance would forfeit leave they earned.
    if not is_exit and policy.max_days_per_year is not None:
        annual = Decimal(str(policy.max_days_per_year))
        remaining = max(ZERO, annual - days_already_encashed_this_year)
        if approved > remaining:
            approved = remaining
            limiting = "max_days_per_year"
            notes.append(
                f"{days_already_encashed_this_year} of the {annual}-day annual limit "
                "has already been encashed this year."
            )

    # ── Per-event cap ───────────────────────────────────────────────────────
    if policy.cap_days is not None:
        cap = Decimal(str(policy.cap_days))
        if approved > cap:
            approved = cap
            limiting = "cap_days"
            notes.append(f"Capped at {cap} days by policy.")

    if approved <= ZERO:
        return EncashmentResult(
            days_requested=days_requested,
            days_approved=ZERO,
            daily_rate=ZERO,
            gross_amount=ZERO,
            exempt_amount=ZERO,
            taxable_amount=ZERO,
            limiting_factor=limiting,
            notes=notes or ["Nothing is encashable under this policy right now."],
        )

    # ── Amount ──────────────────────────────────────────────────────────────
    daily_rate = _money(monthly_wages / Decimal(policy.days_per_month_divisor))
    gross = _money(daily_rate * approved)

    # ── Tax ─────────────────────────────────────────────────────────────────
    # Retirement encashment may be exempt up to a ceiling; in-service
    # encashment is normally fully taxable. Both are policy settings.
    exempt = ZERO
    if is_retirement and policy.exempt_on_retirement:
        ceiling = (
            Decimal(str(policy.retirement_exemption_ceiling))
            if policy.retirement_exemption_ceiling is not None
            else gross
        )
        exempt = min(gross, ceiling)
        notes.append(
            "Exempt on retirement up to the configured ceiling."
            if policy.retirement_exemption_ceiling is not None
            else "Fully exempt on retirement under this policy."
        )
    elif not is_exit and policy.in_service_fully_taxable:
        notes.append("In-service encashment is fully taxable under this policy.")

    return EncashmentResult(
        days_requested=days_requested,
        days_approved=approved,
        daily_rate=daily_rate,
        gross_amount=gross,
        exempt_amount=_money(exempt),
        taxable_amount=_money(gross - exempt),
        limiting_factor=limiting,
        notes=notes,
    )
