"""Ask Mira's trend questions: payroll cost and headcount month by month, and account P&L.

Each answer is a series a person can read twice — a chart and the table under
it — and the model reads the same figures to say what changed and why. The
figures come from payroll's own payslips and the employee records, as the
person asking: a sub-admin narrowed to some departments sees their people only,
and the card says so.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import hr_scope
from app.core.deps import tenant_scoped
from app.models.employee import Employee
from app.models.org import Department, LegalEntity, Location
from app.models.payroll import PayrollRun, PayrollRunStatus, Payslip
from app.models.user import User
from app.services.accounts import pnl as pnl_math
from app.services.accounts import service as accounts
from app.services.companion.people_tools import may_view
from app.services.documents.indian_numbers import format_inr

__all__ = ["GROUPS", "account_pnl", "headcount_trend", "payroll_trend"]

#: How a series may be split.
GROUPS = ("none", "department", "entity", "location")
_ZERO = Decimal(0)
_NOT_A_COST = (PayrollRunStatus.CANCELLED, PayrollRunStatus.FAILED)
_FINAL = (PayrollRunStatus.APPROVED, PayrollRunStatus.PAID)
#: Groups drawn as their own line; the rest are added up as "Others".
_MAX_SERIES = 6


def _notice(text: str, tone: str = "info") -> dict[str, Any]:
    return {"type": "notice", "tone": tone, "text": text}


def _parse(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def _label(year: int, month: int) -> str:
    return f"{date(year, month, 1):%b %Y}"


def _pct(new: Decimal, old: Decimal) -> str:
    if old == 0:
        return "—"
    change = ((new - old) / old * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if change == 0:
        return "0.0%"
    return f"{'+' if change > 0 else ''}{change}%"


def _months(
    inputs: dict[str, Any], today: date, *, latest: tuple[int, int] | None
) -> list[tuple[int, int]]:
    """The months asked about; by default the twelve up to the latest month with data."""
    start, end = _parse(inputs.get("from")), _parse(inputs.get("to"))
    end_month = (end.year, end.month) if end else (latest or (today.year, today.month))
    if start is None:
        year, month = end_month
        month -= 11
        while month < 1:
            month += 12
            year -= 1
        start_month = (year, month)
    else:
        start_month = (start.year, start.month)
    if start_month > end_month:
        start_month, end_month = end_month, start_month
    months = pnl_math.months_between(start_month, end_month)
    return months[-24:]


def _chart(
    title: str,
    months: list[tuple[int, int]],
    series: list[tuple[str, list[Decimal | int | None]]],
    *,
    kind: str,
    unit: str,
    caption: str,
) -> dict[str, Any]:
    """A line or bar chart card: one point per month, one value per series."""
    return {
        "type": "chart",
        "title": title,
        "caption": caption,
        "chart": kind,
        "unit": unit,
        "x": [_label(y, m) for y, m in months],
        "series": [
            {"name": name, "values": ["" if v is None else str(v) for v in values]}
            for name, values in series
        ],
    }


def _scoped_note() -> list[str]:
    if hr_scope.current_scope() is not None:
        return ["You see only the people in your scope; others are left out."]
    return []


# ── Payroll cost ────────────────────────────────────────────────────────────


async def payroll_trend(
    session: AsyncSession, user: User, inputs: dict[str, Any], *, today: date
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Payroll cost month by month: cost to company, gross, net, people paid; split if asked."""
    if not await may_view(session, user, "payroll"):
        return [
            _notice(
                "Payroll figures are for admins and sub-admins who may view payroll.", "warning"
            )
        ], {}
    group = str(inputs.get("group") or "none")
    if group not in GROUPS:
        group = "none"
    latest = (
        await session.execute(
            tenant_scoped(
                select(PayrollRun.period_year, PayrollRun.period_month)
                .where(PayrollRun.status.notin_(_NOT_A_COST))
                .order_by(PayrollRun.period_year.desc(), PayrollRun.period_month.desc())
                .limit(1),
                PayrollRun,
                user,
            )
        )
    ).first()
    if latest is None:
        return [_notice("No payroll has been run yet, so there is no trend to show.")], {}
    try:
        months = _months(inputs, today, latest=(latest[0], latest[1]))
    except ValueError as exc:
        return [_notice(str(exc), "warning")], {}
    used = {
        "from": date(*months[0], 1).isoformat(),
        "to": pnl_math.month_bounds(*months[-1])[1].isoformat(),
        "group": group,
    }
    period = or_(
        *[and_(PayrollRun.period_year == y, PayrollRun.period_month == m) for y, m in months]
    )
    group_column = {
        "department": Employee.department_id,
        "entity": PayrollRun.legal_entity_id,
        "location": Employee.location_id,
    }.get(group)
    columns = [
        PayrollRun.period_year,
        PayrollRun.period_month,
        func.count(func.distinct(Payslip.employee_id)),
        func.coalesce(func.sum(Payslip.employer_cost), 0),
        func.coalesce(func.sum(Payslip.gross_earnings), 0),
        func.coalesce(func.sum(Payslip.net_pay), 0),
        func.coalesce(func.sum(Payslip.pf_employer + Payslip.esi_employer), 0),
    ]
    stmt = (
        select(*columns, *([group_column] if group_column is not None else []))
        .join(PayrollRun, PayrollRun.id == Payslip.run_id)
        .where(period, PayrollRun.status.notin_(_NOT_A_COST))
        .group_by(
            PayrollRun.period_year,
            PayrollRun.period_month,
            *([group_column] if group_column is not None else []),
        )
    )
    if group in ("department", "location"):
        stmt = stmt.join(Employee, Employee.id == Payslip.employee_id)
    rows = (await session.execute(tenant_scoped(stmt, Payslip, user))).all()
    pending = {
        _label(r[0], r[1])
        for r in await session.execute(
            tenant_scoped(
                select(PayrollRun.period_year, PayrollRun.period_month).where(
                    period, PayrollRun.status.notin_((*_NOT_A_COST, *_FINAL))
                ),
                PayrollRun,
                user,
            )
        )
    }

    totals: dict[tuple[int, int], dict[str, Decimal]] = defaultdict(
        lambda: {"people": _ZERO, "cost": _ZERO, "gross": _ZERO, "net": _ZERO, "employer": _ZERO}
    )
    by_group: dict[Any, dict[tuple[int, int], Decimal]] = defaultdict(
        lambda: defaultdict(lambda: _ZERO)
    )
    for row in rows:
        key = (row[0], row[1])
        figures = totals[key]
        figures["people"] += Decimal(row[2])
        figures["cost"] += Decimal(row[3])
        figures["gross"] += Decimal(row[4])
        figures["net"] += Decimal(row[5])
        figures["employer"] += Decimal(row[6])
        if group_column is not None:
            by_group[row[7]][key] += Decimal(row[3])

    with_data = [m for m in months if totals.get(m, {}).get("cost")]
    if not with_data:
        span = f"{_label(*months[0])} to {_label(*months[-1])}"
        return [_notice(f"No payroll was run from {span}.")], used
    if not inputs.get("from") and not inputs.get("to"):
        # Nothing asked for: the months that have payroll, not empty ones round them.
        months = months[months.index(with_data[0]) : months.index(with_data[-1]) + 1]
        used = {
            **used,
            "from": date(*months[0], 1).isoformat(),
            "to": pnl_math.month_bounds(*months[-1])[1].isoformat(),
        }

    cost = [pnl_math.rupees(totals[m]["cost"]) if m in totals else _ZERO for m in months]
    first = cost[months.index(with_data[0])]
    last = cost[months.index(with_data[-1])]
    peak_index = max(range(len(months)), key=lambda i: cost[i])
    total_cost = sum(cost, _ZERO)
    average = (total_cost / len(with_data)).quantize(Decimal("0.01"))
    span = f"{_label(*months[0])} to {_label(*months[-1])}"

    # A month with no payroll is a gap on the chart, not a fall to zero.
    series: list[tuple[str, list[Decimal | int | None]]] = [
        (
            "Cost to company",
            [c if m in with_data else None for c, m in zip(cost, months, strict=True)],
        )
    ]
    if group_column is not None:
        names = await _group_names(session, user, group, set(by_group))
        ranked = sorted(by_group, key=lambda g: -sum(by_group[g].values(), _ZERO))
        series = []
        for g in ranked[:_MAX_SERIES]:
            series.append(
                (
                    names.get(g, "Not set"),
                    [pnl_math.rupees(by_group[g].get(m, _ZERO)) for m in months],
                )
            )
        if len(ranked) > _MAX_SERIES:
            rest = ranked[_MAX_SERIES:]
            series.append(
                (
                    "Others",
                    [
                        pnl_math.rupees(sum((by_group[g].get(m, _ZERO) for g in rest), _ZERO))
                        for m in months
                    ],
                )
            )

    table_rows = []
    previous: Decimal | None = None
    for index, month in enumerate(months):
        figures = totals.get(month)
        if not figures or not figures["cost"]:
            table_rows.append([_label(*month), "—", "—", "—", "—", "—"])
            continue
        table_rows.append(
            [
                _label(*month) + (" *" if _label(*month) in pending else ""),
                int(figures["people"]),
                format_inr(pnl_math.rupees(figures["gross"])),
                format_inr(pnl_math.rupees(figures["net"])),
                format_inr(cost[index]),
                _pct(cost[index], previous) if previous is not None else "—",
            ]
        )
        previous = cost[index]
    notes = [
        "Cost to company is added up from every payslip of the month's runs (regular, arrears, "
        "bonus, full & final), cancelled runs left out. Arrears count in the month they were paid."
    ]
    if pending:
        notes.append("* Payroll for " + ", ".join(sorted(pending)) + " is not approved yet.")
    if group == "department":
        notes.append("Split by each person's department today.")
    notes += _scoped_note()
    return [
        _chart(
            "Payroll cost" + ("" if group == "none" else f" by {group}"),
            months,
            series,
            kind="line" if group == "none" else "bar",
            unit="inr",
            caption=span,
        ),
        {
            "type": "record",
            "title": "Payroll cost, month by month",
            "subtitle": "",
            "heading": span,
            "figures": [
                {"label": "Total cost", "value": format_inr(total_cost)},
                {"label": "Average a month", "value": format_inr(average)},
                {
                    "label": f"{_label(*with_data[0])} → {_label(*with_data[-1])}",
                    "value": _pct(last, first),
                },
                {
                    "label": "Highest",
                    "value": f"{_label(*months[peak_index])}: {format_inr(cost[peak_index])}",
                },
            ],
            "table": {
                "columns": [
                    "Month",
                    "People paid",
                    "Gross",
                    "Net pay",
                    "Cost to company",
                    "Change",
                ],
                "rows": table_rows,
            },
            "note": " ".join(notes),
            "open": "/payroll/runs",
            "open_label": "Open payroll runs",
        },
    ], used


async def _group_names(
    session: AsyncSession, user: User, group: str, ids: set[Any]
) -> dict[Any, str]:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    model = {"department": Department, "entity": LegalEntity, "location": Location}[group]
    rows = await session.execute(
        tenant_scoped(select(model.id, model.name).where(model.id.in_(ids)), model, user)
    )
    return {row.id: row.name for row in rows}


# ── Headcount ───────────────────────────────────────────────────────────────


async def headcount_trend(
    session: AsyncSession, user: User, inputs: dict[str, Any], *, today: date
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Headcount at each month's end, who joined and who left, and the attrition rate."""
    if not await may_view(session, user, "people"):
        return [
            _notice("Headcount is for admins and sub-admins who may view people.", "warning")
        ], {}
    try:
        months = _months(inputs, today, latest=(today.year, today.month))
    except ValueError as exc:
        return [_notice(str(exc), "warning")], {}
    used = {
        "from": date(*months[0], 1).isoformat(),
        "to": pnl_math.month_bounds(*months[-1])[1].isoformat(),
    }
    people = (
        await session.execute(
            tenant_scoped(
                select(Employee.date_of_joining, Employee.date_of_exit).where(
                    Employee.deleted_at.is_(None), Employee.date_of_joining.is_not(None)
                ),
                Employee,
                user,
            )
        )
    ).all()
    heads, joined, left, attrition = [], [], [], []
    rows = []
    for year, month in months:
        first, last = pnl_math.month_bounds(year, month)
        if first > today:
            break
        cut = min(last, today)
        opening = sum(1 for j, x in people if j < first and (x is None or x >= first))
        closing = sum(1 for j, x in people if j <= cut and (x is None or x > cut))
        joiners = sum(1 for j, _ in people if first <= j <= cut)
        leavers = sum(1 for _, x in people if x is not None and first <= x <= cut)
        average = Decimal(opening + closing) / 2
        rate = (
            (Decimal(leavers) / average * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            if average
            else _ZERO
        )
        heads.append(closing)
        joined.append(joiners)
        left.append(leavers)
        attrition.append(rate)
        rows.append([_label(year, month), opening, joiners, leavers, closing, f"{rate}%"])
    shown = months[: len(rows)]
    if not rows:
        return [_notice("Those months are still ahead.")], used
    span = f"{_label(*shown[0])} to {_label(*shown[-1])}"
    total_leavers = sum(left)
    average_heads = Decimal(sum(heads)) / len(heads) if heads else _ZERO
    yearly = (
        (Decimal(total_leavers) / average_heads * 100 * 12 / len(rows)).quantize(Decimal("0.1"))
        if average_heads
        else _ZERO
    )
    return [
        _chart(
            "Headcount",
            shown,
            [("Headcount", heads), ("Joined", joined), ("Left", left)],
            kind="line",
            unit="count",
            caption=span,
        ),
        {
            "type": "record",
            "title": "Headcount, month by month",
            "subtitle": "",
            "heading": span,
            "figures": [
                {"label": f"Headcount {_label(*shown[0])}", "value": str(rows[0][1])},
                {"label": f"Headcount {_label(*shown[-1])}", "value": str(heads[-1])},
                {"label": "Joined", "value": str(sum(joined))},
                {"label": "Left", "value": str(total_leavers)},
                {"label": "Attrition (yearly rate)", "value": f"{yearly}%"},
            ],
            "table": {
                "columns": ["Month", "At start", "Joined", "Left", "At end", "Attrition"],
                "rows": rows,
            },
            "note": " ".join(
                [
                    "From each person's joining and exit dates. Attrition for a month is the "
                    "people who left over the average of its opening and closing headcount; the "
                    "yearly rate scales the period's total to twelve months.",
                    *_scoped_note(),
                ]
            ),
            "open": "/people",
            "open_label": "Open People",
        },
    ], used


# ── Account P&L ─────────────────────────────────────────────────────────────


def _inr(value: Decimal) -> str:
    return format_inr(value) if value >= 0 else f"-{format_inr(-value)}"


async def account_pnl(
    session: AsyncSession, user: User, inputs: dict[str, Any], *, today: date
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """An account's P&L (billed against mapped cost), or every account's side by side."""
    if not await may_view(session, user, "payroll"):
        return [
            _notice("Account P&L is for admins and sub-admins who may view payroll.", "warning")
        ], {}
    start, end = _parse(inputs.get("from")), _parse(inputs.get("to"))
    start_key = pnl_math.month_key(start.year, start.month) if start else None
    end_key = pnl_math.month_key(end.year, end.month) if end else None
    query = str(inputs.get("query") or "").strip()
    used: dict[str, Any] = {"query": query, "from": start_key or "", "to": end_key or ""}
    try:
        if query:
            found = await accounts.find_accounts(session, user, query)
            if not found:
                return [_notice(f"No client account matches “{query}”.")], used
            if len(found) > 1:
                names = ", ".join(f"{a.name} ({a.code})" for a in found[:10])
                return [_notice(f"Several accounts match “{query}”: {names}. Which one?")], used
            detail = await accounts.pnl_detail(
                session, user, found[0].id, start=start_key, end=end_key, checked=True
            )
        else:
            overview = await accounts.account_pnl(session, user, start=start_key, end=end_key)
    except Exception as exc:  # the service raises HTTP errors with readable details
        detail_text = getattr(exc, "detail", None)
        if isinstance(detail_text, str):
            return [_notice(detail_text, "warning")], used
        raise

    if query:
        months = [(int(m.month[:4]), int(m.month[5:])) for m in detail.months]
        span = f"{_label(*months[0])} to {_label(*months[-1])}"
        s = detail.summary
        rows = [
            [
                _label(int(m.month[:4]), int(m.month[5:])),
                _inr(m.billed),
                _inr(m.people_cost),
                _inr(m.other_cost),
                _inr(m.margin),
                f"{m.margin_pct}%" if m.margin_pct is not None else "—",
            ]
            for m in detail.months
        ]
        cards: list[dict[str, Any]] = [
            _chart(
                f"{detail.account.name}: billed and cost",
                months,
                [
                    ("Billed", [m.billed for m in detail.months]),
                    ("Total cost", [m.total_cost for m in detail.months]),
                    ("Margin", [m.margin for m in detail.months]),
                ],
                kind="bar",
                unit="inr",
                caption=span,
            ),
            {
                "type": "record",
                "title": f"P&L · {detail.account.name} ({detail.account.code})",
                "subtitle": detail.account.client_name or "",
                "heading": span,
                "figures": [
                    {"label": "Billed", "value": _inr(s.billed)},
                    {"label": "People cost", "value": _inr(s.people_cost)},
                    {"label": "Other direct cost", "value": _inr(s.other_cost)},
                    {"label": "Margin", "value": _inr(s.margin)},
                    {
                        "label": "Margin %",
                        "value": f"{s.margin_pct}%" if s.margin_pct is not None else "—",
                    },
                    {"label": "People on it", "value": str(s.people)},
                ],
                "table": {
                    "columns": [
                        "Month",
                        "Billed",
                        "People cost",
                        "Other cost",
                        "Margin",
                        "Margin %",
                    ],
                    "rows": rows,
                },
                "note": " ".join(detail.notes),
                "open": f"/payroll/accounts?account={detail.account.id}",
                "open_label": "Open the account",
            },
        ]
        if detail.people:
            cards.append(
                {
                    "type": "table",
                    "title": "People cost on this account",
                    "caption": f"{len(detail.people)} person(s) · {span}",
                    "columns": ["Name", "Code", "Average share", "Cost"],
                    "rows": [
                        [p.name, p.employee_code, f"{p.share_pct}%", _inr(p.cost)]
                        for p in detail.people[:25]
                    ],
                }
            )
        return cards, {**used, "from": detail.start, "to": detail.end}

    if not overview.accounts:
        return [_notice(" ".join(overview.notes))], used
    span = f"{overview.start} to {overview.end}"
    rows = [
        [
            f"{r.name} ({r.code})",
            _inr(r.billed),
            _inr(r.people_cost),
            _inr(r.other_cost),
            _inr(r.margin),
            f"{r.margin_pct}%" if r.margin_pct is not None else "—",
        ]
        for r in overview.accounts
    ]
    t = overview.totals
    figures = []
    if t is not None:
        figures = [
            {"label": "Billed", "value": _inr(t.billed)},
            {"label": "Cost on accounts", "value": _inr(t.total_cost)},
            {"label": "Margin", "value": _inr(t.margin)},
            {"label": "Margin %", "value": f"{t.margin_pct}%" if t.margin_pct is not None else "—"},
            {"label": "Payroll cost on no account", "value": _inr(overview.unmapped_cost)},
        ]
    return [
        {
            "type": "record",
            "title": "Account P&L",
            "subtitle": "",
            "heading": span,
            "figures": figures,
            "table": {
                "columns": ["Account", "Billed", "People cost", "Other cost", "Margin", "Margin %"],
                "rows": rows,
            },
            "note": " ".join(overview.notes),
            "open": "/payroll/accounts",
            "open_label": "Open Account P&L",
        }
    ], {**used, "from": overview.start, "to": overview.end}
