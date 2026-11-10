"""Each department's year against its budget, across AskMiraa and the linked products.

One view for the questions a department head asks: how many seats and how much
people cost were approved, what is committed, what payroll has paid so far this
financial year, what was spent on expenses (Expense Genie when it is linked,
else AskMiraa's own claims), what purchasing has committed and spent against the
department's cost centre in ProcureFlow, and where the sales pipeline stands in
Saleinx.

Each figure comes from the system that owns it and is shown beside the others,
never added into one "remaining" number: a people-cost budget, an expense line
and a purchasing budget are different budgets, and summing them would invent a
figure nobody approved.

Who sees what:
* headcount and the budget itself — admins and sub-admins;
* payroll paid — only with payroll access (`Sensitivity.PAYROLL_ONLY`);
* Expense Genie — company totals for someone Expense Genie calls admin;
* ProcureFlow and Saleinx — as the person's own role there allows.
A source that cannot answer says why, and the rest still show.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.employee import Employee
from app.models.expenses import ExpenseClaim, ExpenseClaimStatusEnum
from app.models.org import Department
from app.models.payroll import PayrollRun, PayrollRunStatus, Payslip
from app.models.tenant import Tenant
from app.models.user import User
from app.services.positions import service as positions
from app.services.reporting.ranges import financial_year_bounds
from app.services.reporting.semantic import Sensitivity, may_see

__all__ = ["department_year"]

ZERO = Decimal("0.00")
UNMATCHED = "Not in a department"


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return ZERO


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(Decimal("0.01")))


async def _linked(session: AsyncSession, user: User, product: str) -> bool:
    tenant = await session.get(Tenant, user.tenant_id)
    return bool(tenant and (tenant.linked_products or {}).get(product))


async def _departments(session: AsyncSession, user: User) -> dict[str, str]:
    rows = (
        await session.execute(
            tenant_scoped(select(Department.id, Department.name), Department, user)
        )
    ).all()
    return {str(i): n for i, n in rows}


async def _people(session: AsyncSession, user: User) -> dict[str, str | None]:
    """Work email (lowercase) → department id, for everyone in the directory."""
    rows = (
        await session.execute(
            tenant_scoped(select(Employee.work_email, Employee.department_id), Employee, user)
        )
    ).all()
    out = {str(e).lower(): (str(d) if d else None) for e, d in rows if e}
    logins = (
        await session.execute(
            tenant_scoped(
                select(User.email, Employee.department_id).join(
                    Employee, Employee.id == User.employee_id
                ),
                User,
                user,
            )
        )
    ).all()
    for email, dept in logins:
        if email:
            out.setdefault(str(email).lower(), str(dept) if dept else None)
    return out


async def _payroll(
    session: AsyncSession, user: User, start: date, end: date
) -> dict[str, Decimal] | None:
    """Employer cost paid so far this year, by department. None without payroll access."""
    if not may_see(permissions.scope_role(user), Sensitivity.PAYROLL_ONLY):
        return None
    rows = (
        await session.execute(
            tenant_scoped(
                select(Employee.department_id, func.sum(Payslip.employer_cost))
                .join(PayrollRun, PayrollRun.id == Payslip.run_id)
                .join(Employee, Employee.id == Payslip.employee_id)
                .where(
                    PayrollRun.status.in_((PayrollRunStatus.APPROVED, PayrollRunStatus.PAID)),
                    func.make_date(PayrollRun.period_year, PayrollRun.period_month, 1) >= start,
                    func.make_date(PayrollRun.period_year, PayrollRun.period_month, 1) <= end,
                )
                .group_by(Employee.department_id),
                Payslip,
                user,
            )
        )
    ).all()
    return {(str(d) if d else UNMATCHED): _money(total) for d, total in rows}


async def _expenses(
    session: AsyncSession, user: User, start: date, end: date
) -> tuple[dict[str, Decimal] | None, str, str | None]:
    """Expense claims this year by department: (totals, source, note)."""
    from app.services.products import data as products

    if await _linked(session, user, "expense"):
        role = await products.role_in(session, user, "expense")
        if role != "admin":
            return None, "Expense Genie", "Company expense totals are for Expense Genie's admins."
        to = min(datetime.combine(end + timedelta(days=1), time(), UTC), datetime.now(UTC))
        try:
            answer = await products.ask(
                session,
                user,
                "expense",
                "summary",
                {
                    "scope": "company",
                    "status": "approved",
                    "from": datetime.combine(start, time(), UTC).isoformat(),
                    "to": to.isoformat(),
                },
            )
        except products.ProductUnavailableError as exc:
            return None, "Expense Genie", str(exc)
        people = await _people(session, user)
        totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
        unknown = 0
        for row in answer.get("by_submitter") or []:
            if not isinstance(row, dict) or str(row.get("currency") or "INR").upper() != "INR":
                continue
            email = str(row.get("email") or "").lower()
            if not email:
                unknown += 1
            dept = people.get(email) if email else None
            totals[dept or UNMATCHED] += _money(row.get("amount"))
        note = (
            "This Expense Genie does not say who submitted what yet, so its approved claims "
            "show under 'Not in a department'."
            if unknown
            else None
        )
        return dict(totals), "Expense Genie", note

    rows = (
        await session.execute(
            tenant_scoped(
                select(
                    func.coalesce(ExpenseClaim.department_id, Employee.department_id),
                    func.sum(ExpenseClaim.amount),
                )
                .join(Employee, Employee.id == ExpenseClaim.employee_id)
                .where(
                    ExpenseClaim.status.in_(
                        (ExpenseClaimStatusEnum.APPROVED, ExpenseClaimStatusEnum.REIMBURSED)
                    ),
                    ExpenseClaim.incurred_on >= start,
                    ExpenseClaim.incurred_on <= end,
                )
                .group_by(func.coalesce(ExpenseClaim.department_id, Employee.department_id)),
                ExpenseClaim,
                user,
            )
        )
    ).all()
    return {(str(d) if d else UNMATCHED): _money(t) for d, t in rows}, "AskMiraa", None


async def _purchasing(session: AsyncSession, user: User) -> dict[str, Any] | None:
    """ProcureFlow's cost-centre budgets this period, as the person's role there allows."""
    from app.services.products import data as products

    if not await _linked(session, user, "procurement"):
        return None
    if not await products.role_in(session, user, "procurement"):
        return {"budgets": [], "note": "You have no role in ProcureFlow."}
    try:
        answer = await products.ask(session, user, "procurement", "budgets", {})
    except products.ProductUnavailableError as exc:
        return {"budgets": [], "note": str(exc)}
    return {
        "budgets": [b for b in answer.get("budgets") or [] if isinstance(b, dict)],
        "note": None,
    }


async def _pipeline(session: AsyncSession, user: User) -> dict[str, Any] | None:
    """Saleinx's pipeline by stage, as the person's role there allows."""
    from app.services.products import data as products

    if not await _linked(session, user, "saleinx"):
        return None
    if not await products.role_in(session, user, "saleinx"):
        return {"stages": [], "note": "You have no role in Saleinx."}
    try:
        answer = await products.ask(session, user, "saleinx", "overview", {})
    except products.ProductUnavailableError as exc:
        return {"stages": [], "note": str(exc)}
    stages = [
        {
            "stage": s.get("stage"),
            "label": s.get("stage_label"),
            "deals": s.get("deals"),
            "amount": s.get("amount"),
            "currency": s.get("currency") or "INR",
        }
        for s in answer.get("stages") or []
        if isinstance(s, dict)
    ]
    open_total = sum(
        (_money(s["amount"]) for s in stages if s["stage"] not in ("won", "lost")), ZERO
    )
    won = sum((_money(s["amount"]) for s in stages if s["stage"] == "won"), ZERO)
    sees = (answer.get("actor") or {}).get("sees")
    return {
        "stages": stages,
        "open_pipeline": _s(open_total),
        "won": _s(won),
        "scope": "your deals and unassigned ones" if sees == "own_and_unassigned" else "everyone's",
        "note": None,
    }


async def department_year(
    session: AsyncSession, user: User, *, on: date | None = None
) -> dict[str, Any]:
    """Every department's financial year so far, from each system that owns a figure."""
    if not permissions.is_hr(user):
        raise permissions.forbidden("Department budgets are for admins and sub-admins.")
    today = on or date.today()
    start, end = financial_year_bounds(today)
    budgets = await positions.budget_room(session, user, on=today)
    names = await _departments(session, user)
    payroll = await _payroll(session, user, start, min(end, today))
    expenses, expense_source, expense_note = await _expenses(session, user, start, min(end, today))
    purchasing = await _purchasing(session, user)
    pipeline = await _pipeline(session, user)

    by_centre = {
        str(b.get("cost_center") or "").strip().lower(): b
        for b in (purchasing or {}).get("budgets") or []
        if b.get("cost_center")
    }
    keys: list[str] = []
    for b in budgets["budgets"]:
        if b["department_id"] and b["department_id"] not in keys:
            keys.append(b["department_id"])
    for source in (payroll or {}, expenses or {}):
        for key in source:
            if key != UNMATCHED and key not in keys:
                keys.append(key)

    budget_of = {b["department_id"]: b for b in budgets["budgets"] if b["department_id"]}
    rows = []
    for key in keys:
        name = names.get(key, "A department")
        b = budget_of.get(key)
        centre = by_centre.get(name.strip().lower())
        rows.append(
            {
                "department_id": key,
                "department": name,
                "approved_positions": b["approved_positions"] if b else None,
                "positions_filled": b["positions_filled"] if b else None,
                "positions_left": b["positions_left"] if b else None,
                "approved_cost": b["approved_cost"] if b else None,
                "committed_on_positions": b["cost_committed"] if b else None,
                "people_cost_left": b["cost_left"] if b else None,
                "payroll_paid": _s(payroll.get(key, ZERO)) if payroll is not None else None,
                "expenses": _s(expenses.get(key, ZERO)) if expenses is not None else None,
                "purchase_budget": (
                    {
                        "cost_center": centre.get("cost_center"),
                        "allocated": centre.get("allocated"),
                        "committed": centre.get("committed"),
                        "consumed": centre.get("consumed"),
                        "available": centre.get("available"),
                    }
                    if centre
                    else None
                ),
            }
        )
    rows.sort(key=lambda r: r["department"].lower())
    matched = {
        r["purchase_budget"]["cost_center"].strip().lower() for r in rows if r["purchase_budget"]
    }
    company = next((b for b in budgets["budgets"] if b["department_id"] is None), None)
    return {
        "financial_year": budgets["financial_year"],
        "as_of": today.isoformat(),
        "departments": rows,
        "company_budget": company,
        "payroll_visible": payroll is not None,
        "payroll_unassigned": _s(payroll.get(UNMATCHED))
        if payroll and UNMATCHED in payroll
        else None,
        "expenses": {
            "source": expense_source,
            "note": expense_note,
            "unassigned": _s(expenses.get(UNMATCHED))
            if expenses and UNMATCHED in expenses
            else None,
        },
        "purchasing": (
            None
            if purchasing is None
            else {
                "note": purchasing["note"],
                "other_cost_centres": [
                    b
                    for b in purchasing["budgets"]
                    if str(b.get("cost_center") or "").strip().lower() not in matched
                ],
            }
        ),
        "sales": pipeline,
    }
