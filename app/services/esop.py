"""ESOP plans, grants, vesting schedules, valuations, projections and exercises.

Vesting: nothing vests before the cliff. At the cliff, every tranche that would
have vested by then vests at once; after it, a tranche every `frequency_months`
until `vesting_months`. Tranches are equal, with any remainder in the last one,
so the schedule always adds up to the grant. A cancelled grant stops vesting
on its cancellation date.

A valuation is vested and unvested units at the latest fair value. A
**projection** is the same arithmetic at a price the person chooses — "if a
share were worth ₹500" — as each tranche vests: a what-if they set, not a
forecast AskMira makes. Tax is described, not computed: the perquisite on
exercise depends on the fair value on that day.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.esop import (
    EsopExercise,
    EsopGrant,
    EsopInstrument,
    EsopPlan,
    EsopValuation,
    GrantStatus,
)
from app.models.user import User
from app.services import audit

__all__ = [
    "ExerciseIn",
    "GrantIn",
    "PlanIn",
    "ValuationIn",
    "add_valuation",
    "cancel_grant",
    "create_grant",
    "create_plan",
    "list_plans",
    "portfolio",
    "projection",
    "record_exercise",
    "vesting_schedule",
]


class PlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=160)
    instrument: EsopInstrument = EsopInstrument.OPTIONS
    pool_units: int = Field(gt=0, le=1_000_000_000)
    default_cliff_months: int = Field(default=12, ge=0, le=120)
    default_vesting_months: int = Field(default=48, ge=1, le=240)
    default_frequency_months: int = Field(default=3, ge=1, le=60)


class GrantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: uuid.UUID
    grant_date: date
    units: int = Field(gt=0, le=100_000_000)
    exercise_price: Decimal = Field(ge=0, max_digits=14, decimal_places=4)
    vesting_start: date | None = None
    cliff_months: int | None = Field(default=None, ge=0, le=120)
    vesting_months: int | None = Field(default=None, ge=1, le=240)
    frequency_months: int | None = Field(default=None, ge=1, le=60)
    notes: str | None = Field(default=None, max_length=2000)


class ValuationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effective_from: date
    fmv_per_unit: Decimal = Field(ge=0, max_digits=14, decimal_places=4)
    source: str = Field(min_length=3, max_length=300)


class ExerciseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exercise_date: date
    units: int = Field(gt=0)


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def vesting_schedule(grant: EsopGrant) -> list[dict[str, Any]]:
    """Every vesting date and the units that vest on it. Sums to the grant.

    Vested after n of N tranches is floor(units x n / N), so each point on the
    schedule is the exact share of the grant and rounding never drifts. Tranches
    that fall before the cliff vest together on the cliff date.
    """
    tranches = max(1, -(-grant.vesting_months // grant.frequency_months))
    cliff_date = _add_months(grant.vesting_start, grant.cliff_months)
    by_date: dict[date, int] = {}
    previous = 0
    for n in range(1, tranches + 1):
        months = min(n * grant.frequency_months, grant.vesting_months)
        cumulative = grant.units * n // tranches
        when = (
            cliff_date
            if months < grant.cliff_months
            else _add_months(grant.vesting_start, months)
        )
        by_date[when] = by_date.get(when, 0) + cumulative - previous
        previous = cumulative
    return [{"date": day, "units": units} for day, units in sorted(by_date.items())]


def _vested(grant: EsopGrant, on: date) -> int:
    stop = grant.cancelled_on if grant.status is GrantStatus.CANCELLED else None
    cutoff = min(on, stop) if stop else on
    return sum(r["units"] for r in vesting_schedule(grant) if r["date"] <= cutoff)


async def _latest_fmv(
    session: AsyncSession, user: User, plan_id: uuid.UUID, on: date
) -> EsopValuation | None:
    return await session.scalar(
        tenant_scoped(
            select(EsopValuation)
            .where(EsopValuation.plan_id == plan_id, EsopValuation.effective_from <= on)
            .order_by(EsopValuation.effective_from.desc())
            .limit(1),
            EsopValuation,
            user,
        )
    )


async def _plan(session: AsyncSession, user: User, plan_id: uuid.UUID) -> EsopPlan:
    row = await session.scalar(
        tenant_scoped(select(EsopPlan).where(EsopPlan.id == plan_id), EsopPlan, user)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    return row


async def create_plan(
    session: AsyncSession, user: User, payload: PlanIn, *, request: Request | None
) -> dict[str, Any]:
    permissions.require_hr(user, detail="ESOP plans are set up by HR.")
    row = EsopPlan(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="esop_plan",
        entity_id=row.id,
        actor=user,
        changes=payload.model_dump(mode="json"),
        summary=f"Created the ESOP plan {row.name} ({row.pool_units} units)",
        request=request,
    )
    await session.commit()
    return (await list_plans(session, user, plan_id=row.id))[0]


async def list_plans(
    session: AsyncSession, user: User, *, plan_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    permissions.require_hr(user, detail="ESOP plans are for HR.")
    stmt = select(EsopPlan).order_by(EsopPlan.name)
    if plan_id:
        stmt = stmt.where(EsopPlan.id == plan_id)
    plans = (await session.scalars(tenant_scoped(stmt, EsopPlan, user))).all()
    out = []
    for plan in plans:
        granted = await session.scalar(
            tenant_scoped(
                select(func.coalesce(func.sum(EsopGrant.units), 0)).where(
                    EsopGrant.plan_id == plan.id, EsopGrant.status == GrantStatus.ACTIVE
                ),
                EsopGrant,
                user,
            )
        )
        fmv = await _latest_fmv(session, user, plan.id, date.today())
        out.append(
            {
                "id": str(plan.id),
                "name": plan.name,
                "instrument": plan.instrument.value,
                "pool_units": plan.pool_units,
                "granted_units": int(granted or 0),
                "available_units": plan.pool_units - int(granted or 0),
                "default_cliff_months": plan.default_cliff_months,
                "default_vesting_months": plan.default_vesting_months,
                "default_frequency_months": plan.default_frequency_months,
                "fmv_per_unit": str(fmv.fmv_per_unit) if fmv else None,
                "fmv_from": fmv.effective_from.isoformat() if fmv else None,
            }
        )
    return out


async def add_valuation(
    session: AsyncSession,
    user: User,
    plan_id: uuid.UUID,
    payload: ValuationIn,
    *,
    request: Request | None,
) -> dict[str, Any]:
    permissions.require_hr(user, detail="Valuations are entered by HR.")
    plan = await _plan(session, user, plan_id)
    taken = await session.scalar(
        tenant_scoped(
            select(EsopValuation.id).where(
                EsopValuation.plan_id == plan.id,
                EsopValuation.effective_from == payload.effective_from,
            ),
            EsopValuation,
            user,
        )
    )
    if taken is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A valuation already starts that day; a new one starts on another day.",
        )
    row = EsopValuation(tenant_id=user.tenant_id, plan_id=plan.id, **payload.model_dump())
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="esop_valuation",
        entity_id=row.id,
        actor=user,
        changes=payload.model_dump(mode="json"),
        summary=f"{plan.name}: fair value {row.fmv_per_unit} from {row.effective_from}",
        request=request,
    )
    await session.commit()
    return {
        "id": str(row.id),
        "effective_from": row.effective_from.isoformat(),
        "fmv_per_unit": str(row.fmv_per_unit),
        "source": row.source,
    }


async def create_grant(
    session: AsyncSession,
    user: User,
    plan_id: uuid.UUID,
    payload: GrantIn,
    *,
    request: Request | None,
) -> dict[str, Any]:
    permissions.require_hr(user, detail="Grants are made by HR.")
    plan = await _plan(session, user, plan_id)
    if payload.employee_id == user.employee_id:
        raise permissions.forbidden("Somebody else makes a grant to you.")
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == payload.employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    granted = await session.scalar(
        tenant_scoped(
            select(func.coalesce(func.sum(EsopGrant.units), 0)).where(
                EsopGrant.plan_id == plan.id, EsopGrant.status == GrantStatus.ACTIVE
            ),
            EsopGrant,
            user,
        )
    )
    if int(granted or 0) + payload.units > plan.pool_units:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only {plan.pool_units - int(granted or 0)} units are left in the pool.",
        )
    cliff = payload.cliff_months if payload.cliff_months is not None else plan.default_cliff_months
    months = (
        payload.vesting_months
        if payload.vesting_months is not None
        else plan.default_vesting_months
    )
    frequency = (
        payload.frequency_months
        if payload.frequency_months is not None
        else plan.default_frequency_months
    )
    if cliff > months:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The cliff cannot be longer than the vesting period.",
        )
    row = EsopGrant(
        tenant_id=user.tenant_id,
        plan_id=plan.id,
        employee_id=employee.id,
        grant_date=payload.grant_date,
        units=payload.units,
        exercise_price=payload.exercise_price,
        vesting_start=payload.vesting_start or payload.grant_date,
        cliff_months=cliff,
        vesting_months=months,
        frequency_months=frequency,
        notes=(payload.notes or "").strip() or None,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="esop_grant",
        entity_id=row.id,
        actor=user,
        changes={
            "employee": employee.employee_code,
            "units": row.units,
            "exercise_price": str(row.exercise_price),
            "cliff": cliff,
            "months": months,
        },
        summary=f"Granted {row.units} {plan.instrument.value} to {employee.employee_code}",
        request=request,
    )
    await session.commit()
    return await _grant_read(session, user, row, plan, date.today())


async def cancel_grant(
    session: AsyncSession,
    user: User,
    grant_id: uuid.UUID,
    *,
    on: date,
    request: Request | None,
) -> dict[str, Any]:
    permissions.require_hr(user, detail="Grants are cancelled by HR.")
    row = await _grant(session, user, grant_id)
    if row.status is GrantStatus.CANCELLED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already cancelled.")
    row.status = GrantStatus.CANCELLED
    row.cancelled_on = on
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="esop_grant",
        entity_id=row.id,
        actor=user,
        changes={"cancelled_on": on.isoformat()},
        summary=f"Cancelled a grant of {row.units} units from {on}; vested units stay",
        request=request,
    )
    await session.commit()
    plan = await _plan(session, user, row.plan_id)
    return await _grant_read(session, user, row, plan, date.today())


async def _grant(session: AsyncSession, user: User, grant_id: uuid.UUID) -> EsopGrant:
    row = await session.scalar(
        tenant_scoped(select(EsopGrant).where(EsopGrant.id == grant_id), EsopGrant, user)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grant not found")
    return row


async def _exercised(session: AsyncSession, user: User, grant_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            tenant_scoped(
                select(func.coalesce(func.sum(EsopExercise.units), 0)).where(
                    EsopExercise.grant_id == grant_id
                ),
                EsopExercise,
                user,
            )
        )
        or 0
    )


async def _grant_read(
    session: AsyncSession, user: User, grant: EsopGrant, plan: EsopPlan, on: date
) -> dict[str, Any]:
    vested = _vested(grant, on)
    exercised = await _exercised(session, user, grant.id)
    fmv = await _latest_fmv(session, user, plan.id, on)
    unvested = 0 if grant.status is GrantStatus.CANCELLED else grant.units - vested
    spread = (
        max(Decimal(0), Decimal(str(fmv.fmv_per_unit)) - Decimal(str(grant.exercise_price)))
        if fmv
        else None
    )

    def worth(units: int) -> str | None:
        if spread is None:
            return None
        return str((spread * units).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return {
        "id": str(grant.id),
        "plan": plan.name,
        "instrument": plan.instrument.value,
        "employee_id": str(grant.employee_id),
        "grant_date": grant.grant_date.isoformat(),
        "units": grant.units,
        "exercise_price": str(grant.exercise_price),
        "status": grant.status.value,
        "cancelled_on": grant.cancelled_on.isoformat() if grant.cancelled_on else None,
        "vested": vested,
        "exercised": exercised,
        "exercisable": max(0, vested - exercised),
        "unvested": unvested,
        "schedule": [
            {"date": r["date"].isoformat(), "units": r["units"], "vested": r["date"] <= on}
            for r in vesting_schedule(grant)
        ],
        "fmv_per_unit": str(fmv.fmv_per_unit) if fmv else None,
        "fmv_from": fmv.effective_from.isoformat() if fmv else None,
        "value_vested_unexercised": worth(max(0, vested - exercised)),
        "value_unvested": worth(unvested),
    }


async def portfolio(
    session: AsyncSession, user: User, employee_id: uuid.UUID, *, on: date | None = None
) -> dict[str, Any]:
    """One person's grants: schedule, vested, exercised, and worth at today's fair value."""
    if not (permissions.is_hr(user) or user.employee_id == employee_id):
        raise permissions.forbidden("A person's grants are for them and HR.")
    day = on or date.today()
    grants = (
        await session.scalars(
            tenant_scoped(
                select(EsopGrant)
                .where(EsopGrant.employee_id == employee_id)
                .order_by(EsopGrant.grant_date),
                EsopGrant,
                user,
            )
        )
    ).all()
    plans = {}
    out = []
    for grant in grants:
        if grant.plan_id not in plans:
            plans[grant.plan_id] = await _plan(session, user, grant.plan_id)
        out.append(await _grant_read(session, user, grant, plans[grant.plan_id], day))
    return {"employee_id": str(employee_id), "as_of": day.isoformat(), "grants": out}


async def projection(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    *,
    price_per_unit: Decimal,
    on: date | None = None,
) -> dict[str, Any]:
    """What the person's options would be worth at a price they choose, as they vest."""
    if price_per_unit < 0:
        raise HTTPException(status_code=422, detail="A price per share cannot be negative.")
    held = await portfolio(session, user, employee_id, on=on)
    day = on or date.today()
    grants = []
    timeline: dict[date, Decimal] = {}
    total_now = total_all = Decimal(0)
    for grant in held["grants"]:
        if grant["status"] == "cancelled" and grant["vested"] == 0:
            continue
        spread = max(Decimal(0), price_per_unit - Decimal(grant["exercise_price"]))
        exercised = grant["exercised"]
        vested_open = max(0, grant["vested"] - exercised)
        all_open = vested_open + grant["unvested"]
        now_value = (spread * vested_open).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        all_value = (spread * all_open).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total_now += now_value
        total_all += all_value
        # Each future tranche adds its units' worth on its vesting date.
        for tranche in grant["schedule"]:
            vests_on = date.fromisoformat(tranche["date"])
            if vests_on > day and grant["status"] != "cancelled":
                timeline[vests_on] = timeline.get(vests_on, Decimal(0)) + spread * tranche["units"]
        grants.append(
            {
                "id": grant["id"],
                "plan": grant["plan"],
                "exercise_price": grant["exercise_price"],
                "spread_per_unit": str(spread.quantize(Decimal("0.01"))),
                "vested_unexercised": vested_open,
                "unvested": grant["unvested"],
                "value_now": str(now_value),
                "value_when_fully_vested": str(all_value),
            }
        )
    running = total_now
    points = [{"date": day.isoformat(), "value": str(total_now)}]
    for vests_on in sorted(timeline):
        running += timeline[vests_on]
        points.append(
            {
                "date": vests_on.isoformat(),
                "value": str(running.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            }
        )
    return {
        "employee_id": str(employee_id),
        "as_of": day.isoformat(),
        "price_per_unit": str(price_per_unit),
        "grants": grants,
        "value_now": str(total_now.quantize(Decimal("0.01"))),
        "value_when_fully_vested": str(total_all.quantize(Decimal("0.01"))),
        "timeline": points,
        "note": (
            "Before tax, at the price you entered. On exercise, the difference between the "
            "fair value that day and the exercise price is taxed as salary (a perquisite), and "
            "a later sale is taxed as capital gains. This is a what-if, not advice."
        ),
    }


async def record_exercise(
    session: AsyncSession,
    user: User,
    grant_id: uuid.UUID,
    payload: ExerciseIn,
    *,
    request: Request | None,
) -> dict[str, Any]:
    """HR records an exercise; its perquisite reaches payroll's next run as taxable income."""
    permissions.require_hr(user, detail="Exercises are recorded by HR.")
    grant = await _grant(session, user, grant_id)
    vested = _vested(grant, payload.exercise_date)
    exercised = await _exercised(session, user, grant.id)
    if payload.units > vested - exercised:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Only {max(0, vested - exercised)} vested units can be exercised then.",
        )
    fmv = await _latest_fmv(session, user, grant.plan_id, payload.exercise_date)
    if fmv is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Enter the fair market value for that date first; the perquisite needs it.",
        )
    spread = max(Decimal(0), Decimal(str(fmv.fmv_per_unit)) - Decimal(str(grant.exercise_price)))
    perquisite = (spread * payload.units).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    row = EsopExercise(
        tenant_id=user.tenant_id,
        grant_id=grant.id,
        employee_id=grant.employee_id,
        exercise_date=payload.exercise_date,
        units=payload.units,
        fmv_per_unit=fmv.fmv_per_unit,
        exercise_price=grant.exercise_price,
        perquisite_value=perquisite,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="esop_exercise",
        entity_id=row.id,
        actor=user,
        changes={"units": row.units, "fmv": str(row.fmv_per_unit), "perquisite": str(perquisite)},
        summary=f"Recorded the exercise of {row.units} units; perquisite {perquisite}",
        request=request,
    )
    await session.commit()
    return {
        "id": str(row.id),
        "units": row.units,
        "fmv_per_unit": str(row.fmv_per_unit),
        "perquisite_value": str(perquisite),
    }
