"""The finance provider: where AskMira writes the P&L an order is expected to make.

When an order is won (Ask Mira's order win, companion/order_win.py), its
expected billing and cost, month by month, have to land in the company's
finance layer as a *prospective* P&L — not as actuals, which only ever record
what happened. That layer is behind one small interface, so the order win
never knows which ledger it writes to:

    write_forecast(plan)     record the forecast; idempotent on the order's
                             reference (a second write returns the first)
    withdraw_forecast(...)   take it back, when a later step of the order failed

One provider exists: **AskMira's own ledger** — Account P&L (services/accounts),
where the forecast sits beside the account's actual billing and payroll cost
and the P&L page shows both. A ledger such as Tally does not hold forecasts:
actual payroll already goes to Tally as a journal (payroll/journal.py), and a
forecast becomes Tally's business only once it is billed. A provider that does
take forecasts (a budgeting tool, an ERP) is one more class with these two
methods, chosen in `provider_for`.

Money is Decimal throughout and Numeric in the database. Every write and every
withdrawal is audited in the same transaction as the change.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.accounts import (
    AccountEntry,
    AccountForecast,
    AccountForecastKind,
    AccountForecastLine,
    ClientAccount,
)
from app.models.audit import AuditAction
from app.models.user import User
from app.services import audit

__all__ = [
    "AskMiraLedger",
    "FinanceProvider",
    "ForecastLineIn",
    "ForecastPlan",
    "ForecastWritten",
    "list_forecasts",
    "provider_for",
    "withdraw_forecast_by_id",
]

CENT = Decimal("0.01")
#: Words that say what kind of company it is, not which one: left out of a code.
_FILLER = {"PVT", "PRIVATE", "LTD", "LIMITED", "LLP", "INC", "CO", "COMPANY", "THE", "AND", "OF"}


@dataclass(frozen=True, slots=True)
class ForecastLineIn:
    kind: str
    label: str
    #: The amount in each month the line covers (the same every month).
    monthly: Decimal
    #: Which months it covers, counted from the start: 0 = the first month.
    first: int = 0
    count: int = 1


@dataclass(frozen=True, slots=True)
class ForecastPlan:
    #: The account to write to, by name: found, or opened under that name.
    account_name: str
    reference: str
    title: str
    start_month: date
    months: int
    lines: list[ForecastLineIn]
    order_value: Decimal | None = None
    source: str = "order_win"
    source_ref: str | None = None
    notes: str | None = None


@dataclass(slots=True)
class ForecastWritten:
    forecast_id: uuid.UUID
    account_id: uuid.UUID
    account_code: str
    account_name: str
    #: The account was opened for this forecast (and is closed again if it is withdrawn).
    account_created: bool
    #: It was already there: the same order written before.
    existing: bool = False
    totals: dict[str, str] = field(default_factory=dict)


class FinanceProvider(Protocol):
    key: str
    name: str

    async def write_forecast(
        self, session: AsyncSession, user: User, plan: ForecastPlan, *, request: Request | None
    ) -> ForecastWritten: ...

    async def withdraw_forecast(
        self,
        session: AsyncSession,
        user: User,
        written: ForecastWritten,
        *,
        reason: str,
        request: Request | None,
    ) -> None: ...


def _require(user: User) -> None:
    permissions.require_hr(
        user,
        detail="Client accounts and their P&L are for admins and sub-admins with payroll rights.",
    )


def _month_after(start: date, offset: int) -> tuple[int, int]:
    index = start.year * 12 + (start.month - 1) + offset
    return index // 12, index % 12 + 1


def _money(value: Decimal) -> str:
    return f"{value.quantize(CENT)}"


def totals_of(plan_lines: list[ForecastLineIn]) -> dict[str, Decimal]:
    """Billing, cost and margin over the whole forecast."""
    billing = sum(
        (
            line.monthly * line.count
            for line in plan_lines
            if line.kind == AccountForecastKind.BILLING
        ),
        Decimal(0),
    )
    cost = sum(
        (
            line.monthly * line.count
            for line in plan_lines
            if line.kind in AccountForecastKind.COSTS
        ),
        Decimal(0),
    )
    return {"billing": billing, "cost": cost, "margin": billing - cost}


def _code_from(name: str) -> str:
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name.upper()) if w and w not in _FILLER]
    code = "-".join(words[:3])[:24] or "ACCOUNT"
    return code.strip("-") or "ACCOUNT"


class AskMiraLedger:
    """AskMira's Account P&L: forecasts beside the actuals, per client account."""

    key = "askmira"
    name = "AskMira Account P&L"

    async def _account(
        self, session: AsyncSession, user: User, name: str
    ) -> tuple[ClientAccount, bool]:
        wanted = name.strip()
        found = await session.scalar(
            tenant_scoped(
                select(ClientAccount)
                .where(
                    (func.lower(ClientAccount.name) == wanted.lower())
                    | (func.lower(ClientAccount.client_name) == wanted.lower())
                )
                .order_by(ClientAccount.is_active.desc(), ClientAccount.created_at),
                ClientAccount,
                user,
            )
        )
        if found is not None:
            if not found.is_active:
                found.is_active = True
            return found, False
        base = _code_from(wanted)
        code = base
        for n in range(2, 100):
            taken = await session.scalar(
                tenant_scoped(
                    select(ClientAccount.id).where(func.upper(ClientAccount.code) == code),
                    ClientAccount,
                    user,
                )
            )
            if taken is None:
                break
            code = f"{base[:20]}-{n}"
        row = ClientAccount(
            tenant_id=user.tenant_id,
            code=code,
            name=wanted[:160],
            client_name=wanted[:160],
            notes="Opened by an order win in Ask Mira.",
        )
        session.add(row)
        await session.flush()
        return row, True

    async def write_forecast(
        self, session: AsyncSession, user: User, plan: ForecastPlan, *, request: Request | None
    ) -> ForecastWritten:
        _require(user)
        if plan.start_month.day != 1:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "A forecast starts on the 1st."
            )
        if not 1 <= plan.months <= 120:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "A forecast runs 1 to 120 months."
            )
        sums = totals_of(plan.lines)

        live = await session.scalar(
            tenant_scoped(
                select(AccountForecast).where(
                    AccountForecast.reference == plan.reference,
                    AccountForecast.status == "active",
                ),
                AccountForecast,
                user,
            )
        )
        if live is not None:
            account = await session.get(ClientAccount, live.account_id)
            return ForecastWritten(
                forecast_id=live.id,
                account_id=live.account_id,
                account_code=account.code if account else "",
                account_name=account.name if account else plan.account_name,
                account_created=False,
                existing=True,
                totals={k: _money(v) for k, v in sums.items()},
            )

        account, created = await self._account(session, user, plan.account_name)
        forecast = AccountForecast(
            tenant_id=user.tenant_id,
            account_id=account.id,
            title=plan.title[:200],
            reference=plan.reference[:120],
            source=plan.source,
            source_ref=plan.source_ref,
            start_month=plan.start_month,
            months=plan.months,
            status="active",
            order_value=plan.order_value,
            created_by_user_id=user.id,
            notes=plan.notes,
        )
        session.add(forecast)
        await session.flush()
        for line in plan.lines:
            amount = line.monthly.quantize(CENT)
            if amount <= 0:
                continue
            for offset in range(line.first, min(line.first + line.count, plan.months)):
                year, month = _month_after(plan.start_month, offset)
                session.add(
                    AccountForecastLine(
                        tenant_id=user.tenant_id,
                        forecast_id=forecast.id,
                        kind=line.kind,
                        period_year=year,
                        period_month=month,
                        amount=amount,
                        label=line.label[:160],
                    )
                )
        await session.flush()
        if created:
            await audit.record(
                session,
                tenant_id=user.tenant_id,
                action=AuditAction.CREATE,
                entity_type="client_account",
                entity_id=account.id,
                actor=user,
                changes={"code": account.code, "name": account.name, "via": "order_win"},
                summary=f"Client account {account.code} ({account.name}) opened by an order win",
                request=request,
            )
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.CREATE,
            entity_type="account_forecast",
            entity_id=forecast.id,
            actor=user,
            changes={
                "account": account.code,
                "reference": plan.reference,
                "start_month": plan.start_month.isoformat(),
                "months": plan.months,
                **{k: _money(v) for k, v in sums.items()},
            },
            summary=(
                f"Prospective P&L for {account.name} ({plan.reference}): billing "
                f"{_money(sums['billing'])}, cost {_money(sums['cost'])} over {plan.months} months"
            ),
            request=request,
        )
        await session.commit()
        return ForecastWritten(
            forecast_id=forecast.id,
            account_id=account.id,
            account_code=account.code,
            account_name=account.name,
            account_created=created,
            totals={k: _money(v) for k, v in sums.items()},
        )

    async def withdraw_forecast(
        self,
        session: AsyncSession,
        user: User,
        written: ForecastWritten,
        *,
        reason: str,
        request: Request | None,
    ) -> None:
        _require(user)
        forecast = await session.scalar(
            tenant_scoped(
                select(AccountForecast).where(AccountForecast.id == written.forecast_id),
                AccountForecast,
                user,
            )
        )
        if forecast is None or forecast.status != "active":
            return
        forecast.status = "withdrawn"
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="account_forecast",
            entity_id=forecast.id,
            actor=user,
            changes={
                "before": {"status": "active"},
                "after": {"status": "withdrawn"},
                "reason": reason,
            },
            summary=f"Prospective P&L {forecast.reference} withdrawn: {reason}",
            request=request,
        )
        if written.account_created:
            account = await session.get(ClientAccount, written.account_id)
            used = await session.scalar(
                tenant_scoped(
                    select(func.count(AccountEntry.id)).where(
                        AccountEntry.account_id == written.account_id
                    ),
                    AccountEntry,
                    user,
                )
            )
            others = await session.scalar(
                tenant_scoped(
                    select(func.count(AccountForecast.id)).where(
                        AccountForecast.account_id == written.account_id,
                        AccountForecast.status == "active",
                        AccountForecast.id != forecast.id,
                    ),
                    AccountForecast,
                    user,
                )
            )
            if account is not None and not used and not others:
                # Opened for this order alone, and nothing else uses it: closed again.
                account.is_active = False
                await audit.record(
                    session,
                    tenant_id=user.tenant_id,
                    action=AuditAction.UPDATE,
                    entity_type="client_account",
                    entity_id=account.id,
                    actor=user,
                    changes={"before": {"is_active": True}, "after": {"is_active": False}},
                    summary=(
                        f"Client account {account.code} closed: the order it was opened for "
                        "was undone"
                    ),
                    request=request,
                )
        await session.commit()


def provider_for(session: AsyncSession, user: User) -> FinanceProvider:
    """The finance layer this workspace writes forecasts to. Today: AskMira's own ledger."""
    return AskMiraLedger()


# ── Reading forecasts (the Account P&L page, Ask Mira) ───────────────────────


async def list_forecasts(
    session: AsyncSession,
    user: User,
    *,
    account_id: uuid.UUID | None = None,
    include_withdrawn: bool = False,
) -> list[dict[str, Any]]:
    """Each forecast with its month-by-month billing, cost and margin."""
    _require(user)
    stmt = select(AccountForecast, ClientAccount).join(
        ClientAccount, ClientAccount.id == AccountForecast.account_id
    )
    if account_id is not None:
        stmt = stmt.where(AccountForecast.account_id == account_id)
    if not include_withdrawn:
        stmt = stmt.where(AccountForecast.status == "active")
    rows = (
        await session.execute(
            tenant_scoped(stmt, AccountForecast, user).order_by(
                AccountForecast.start_month, AccountForecast.created_at
            )
        )
    ).all()
    if not rows:
        return []
    ids = [forecast.id for forecast, _ in rows]
    lines = (
        await session.scalars(
            tenant_scoped(
                select(AccountForecastLine).where(AccountForecastLine.forecast_id.in_(ids)),
                AccountForecastLine,
                user,
            )
        )
    ).all()
    by_forecast: dict[uuid.UUID, list[AccountForecastLine]] = defaultdict(list)
    for line in lines:
        by_forecast[line.forecast_id].append(line)

    out: list[dict[str, Any]] = []
    for forecast, account in rows:
        months: dict[str, dict[str, Decimal]] = defaultdict(
            lambda: {
                "billing": Decimal(0),
                "people_cost": Decimal(0),
                "recruitment": Decimal(0),
                "other_cost": Decimal(0),
            }
        )
        labels: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
        for line in by_forecast[forecast.id]:
            key = f"{line.period_year:04d}-{line.period_month:02d}"
            months[key][line.kind] += line.amount
            labels[(line.kind, line.label)] += line.amount
        month_rows = []
        for key in sorted(months):
            m = months[key]
            cost = m["people_cost"] + m["recruitment"] + m["other_cost"]
            month_rows.append(
                {
                    "month": key,
                    "billing": _money(m["billing"]),
                    "people_cost": _money(m["people_cost"]),
                    "recruitment": _money(m["recruitment"]),
                    "other_cost": _money(m["other_cost"]),
                    "cost": _money(cost),
                    "margin": _money(m["billing"] - cost),
                }
            )
        billing = sum((Decimal(r["billing"]) for r in month_rows), Decimal(0))
        cost = sum((Decimal(r["cost"]) for r in month_rows), Decimal(0))
        margin = billing - cost
        out.append(
            {
                "id": str(forecast.id),
                "account_id": str(account.id),
                "account_code": account.code,
                "account_name": account.name,
                "title": forecast.title,
                "reference": forecast.reference,
                "source": forecast.source,
                "status": forecast.status,
                "start_month": forecast.start_month.isoformat()[:7],
                "months": forecast.months,
                "order_value": _money(forecast.order_value)
                if forecast.order_value is not None
                else None,
                "billing": _money(billing),
                "cost": _money(cost),
                "margin": _money(margin),
                "margin_pct": (
                    f"{(margin / billing * 100).quantize(Decimal('0.1'))}" if billing else None
                ),
                "by_month": month_rows,
                "lines": [
                    {"kind": kind, "label": label, "total": _money(total)}
                    for (kind, label), total in sorted(labels.items())
                ],
                "created_at": forecast.created_at.isoformat() if forecast.created_at else None,
            }
        )
    return out


async def withdraw_forecast_by_id(
    session: AsyncSession, user: User, forecast_id: uuid.UUID, *, request: Request | None
) -> None:
    """HR takes a forecast back by hand (the order fell through after all)."""
    _require(user)
    forecast = await session.scalar(
        tenant_scoped(
            select(AccountForecast).where(AccountForecast.id == forecast_id), AccountForecast, user
        )
    )
    if forecast is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such forecast.")
    if forecast.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "This forecast was already withdrawn.")
    await AskMiraLedger().withdraw_forecast(
        session,
        user,
        ForecastWritten(
            forecast_id=forecast.id,
            account_id=forecast.account_id,
            account_code="",
            account_name="",
            account_created=False,
        ),
        reason=f"withdrawn by {user.full_name} on {datetime.now(UTC):%d %b %Y}",
        request=request,
    )
