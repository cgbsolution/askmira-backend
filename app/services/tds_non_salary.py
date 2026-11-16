"""TDS on non-salary payments (194C contractors, 194J professionals).

One calculation shared by contractor invoices and consultant payouts. The rules
come from the approved 'tds_non_salary' rule set; nothing here knows a rate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.contractors import ContractorKind, TdsSection
from app.models.payroll import PayrollRuleSet
from app.models.user import User

RULE_TYPE = "tds_non_salary"

_ONE = Decimal(1)
_HUNDRED = Decimal(100)


@dataclass(frozen=True)
class Payee:
    pan: str | None
    kind: ContractorKind
    section: TdsSection


@dataclass(frozen=True)
class YearToDate:
    """What this payee was paid so far this financial year under one section."""

    total: Decimal
    taxed_base: Decimal

    @property
    def untaxed(self) -> Decimal:
        return max(Decimal(0), self.total - self.taxed_base)


@dataclass(frozen=True)
class TdsResult:
    base: Decimal
    rate: Decimal
    tds: Decimal
    note: str | None
    rule_set_id: uuid.UUID


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def fy_bounds(day: date) -> tuple[date, date]:
    """The financial year (1 April to 31 March) that contains the day."""
    start = date(day.year if day.month >= 4 else day.year - 1, 4, 1)
    return start, date(start.year + 1, 3, 31)


async def rules_on(session: AsyncSession, user: User, day: date) -> PayrollRuleSet:
    """The approved rule set in force on the day, or a 409 saying to add one."""
    row = await session.scalar(
        tenant_scoped(
            select(PayrollRuleSet)
            .where(
                PayrollRuleSet.rule_type == RULE_TYPE,
                PayrollRuleSet.approved_at.is_not(None),
                PayrollRuleSet.effective_from <= day,
                or_(PayrollRuleSet.effective_to.is_(None), PayrollRuleSet.effective_to >= day),
            )
            .order_by(PayrollRuleSet.effective_from.desc())
            .limit(1),
            PayrollRuleSet,
            user,
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"No approved TDS rule set for non-salary payments covers {day}. Add a "
                f"'{RULE_TYPE}' rule set under Payroll > Rule sets first."
            ),
        )
    return row


def rate_for(rules: dict[str, Any], payee: Payee) -> Decimal:
    """The TDS rate in percent: the no-PAN rate, or the section's rate."""
    if not payee.pan:
        return _d(rules["no_pan_rate"])
    spec = rules["sections"][payee.section.value]
    if payee.section is TdsSection.S194C:
        individual = payee.kind in (ContractorKind.INDIVIDUAL, ContractorKind.HUF)
        return _d(spec["rate_individual"] if individual else spec["rate_other"])
    return _d(spec["rate"])


def compute(rule_set: PayrollRuleSet, payee: Payee, amount: Decimal, ytd: YearToDate) -> TdsResult:
    """TDS for one new payment, catching up earlier untaxed payments once the
    year's threshold is crossed."""
    rules = rule_set.rules or {}
    section = payee.section
    try:
        spec = rules["sections"][section.value]
        rate = rate_for(rules, payee)
    except (KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"The '{RULE_TYPE}' rule set has no entry for section {section.value}.",
        ) from exc
    untaxed_before = ytd.untaxed
    annual = _d(spec.get("annual_threshold", "0"))
    single = _d(spec["single_threshold"]) if "single_threshold" in spec else None
    after = ytd.total + amount
    if single is not None and amount > single:
        base, note = amount, None
        if after > annual:
            base, note = amount + untaxed_before, None
    elif after > annual:
        base = amount + untaxed_before
        note = (
            "The year's total crossed the threshold, so earlier untaxed invoices are taxed now."
            if untaxed_before > 0
            else None
        )
    else:
        return TdsResult(
            base=Decimal(0),
            rate=Decimal(0),
            tds=Decimal(0),
            note=f"No TDS: below the {section.value} threshold so far this year.",
            rule_set_id=rule_set.id,
        )
    tds = (base * rate / _HUNDRED).quantize(_ONE, rounding=ROUND_HALF_UP)
    if not payee.pan:
        note = (note + " " if note else "") + "No PAN on file: the higher rate applies."
    return TdsResult(base=base, rate=rate, tds=tds, note=note, rule_set_id=rule_set.id)
