"""Ad-hoc payroll adjustments: keyed in, approved by somebody else, paid by a run.

Rules held here rather than in a router:

- **Maker-checker.** Whoever keys an adjustment in cannot approve it. An
  incentive is money; it gets a second pair of eyes like every payroll figure.
- **Nobody adjusts their own pay.** HR keying an incentive for themselves is
  refused, whatever their role.
- **The category decides the kind.** An incentive is an earning; a canteen
  recovery is a deduction. A deduction is never taxable income and never wages.
- **A court attachment names its order.** A recovery nobody can trace to a
  decree is a recovery nobody can defend.
- **Approved is not paid.** Payment happens when a run that includes it is
  approved (`inputs.AdjustmentProvider`), and is recorded per period, so the
  same adjustment cannot be paid twice by two runs.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.payroll_inputs import (
    AdjustmentCategory,
    AdjustmentKind,
    AdjustmentRecurrence,
    AdjustmentStatus,
    PayrollAdjustment,
    PayrollAdjustmentApplication,
)
from app.models.user import User
from app.schemas.payroll_inputs import (
    AdjustmentApplicationRead,
    AdjustmentCreate,
    AdjustmentImportResult,
    AdjustmentRead,
)
from app.services import audit

__all__ = [
    "EARNING_CATEGORIES",
    "approve",
    "cancel",
    "create",
    "get",
    "import_csv",
    "list_adjustments",
    "pending_for",
    "reject",
]

EARNING_CATEGORIES = frozenset(
    {
        AdjustmentCategory.INCENTIVE,
        AdjustmentCategory.BONUS,
        AdjustmentCategory.COMMISSION,
        AdjustmentCategory.OVERTIME,
        AdjustmentCategory.SHIFT_ALLOWANCE,
        AdjustmentCategory.ARREAR,
        AdjustmentCategory.OTHER_EARNING,
    }
)
#: Pay for work by its nature: counts as wages for ESI and PT whatever the form says.
_WAGE_CATEGORIES = frozenset({AdjustmentCategory.OVERTIME, AdjustmentCategory.SHIFT_ALLOWANCE})

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Adjustment not found")


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def kind_for(category: AdjustmentCategory) -> AdjustmentKind:
    return AdjustmentKind.EARNING if category in EARNING_CATEGORIES else AdjustmentKind.DEDUCTION


async def _employee(session: AsyncSession, user: User, employee_id: uuid.UUID) -> Employee:
    employee = await session.scalar(
        tenant_scoped(
            select(Employee).where(Employee.id == employee_id, Employee.deleted_at.is_(None)),
            Employee,
            user,
        )
    )
    if employee is None:
        raise _unprocessable("That employee is not in this workspace.")
    return employee


def _validated(payload: AdjustmentCreate) -> dict:
    """The row's values after the rules the form cannot express."""
    kind = kind_for(payload.category)
    values = payload.model_dump()
    values["kind"] = kind
    values["name"] = payload.name.strip()
    values["reason"] = payload.reason.strip()
    values["reference"] = (payload.reference or "").strip() or None
    if kind is AdjustmentKind.DEDUCTION:
        values["taxable"] = False
        values["counts_as_wages"] = False
    else:
        values["within_net_pay"] = False
        if payload.category in _WAGE_CATEGORIES:
            values["counts_as_wages"] = True
    if payload.recurrence is AdjustmentRecurrence.ONE_TIME:
        if payload.end_year is not None or payload.total_cap is not None:
            raise _unprocessable(
                "A one-time adjustment is paid once: it has no end month and no total cap."
            )
    elif payload.end_year is not None and (payload.end_year, payload.end_month) < (
        payload.start_year,
        payload.start_month,
    ):
        raise _unprocessable("The last month is before the first month.")
    if payload.category is AdjustmentCategory.COURT_ATTACHMENT:
        if not values["reference"]:
            raise _unprocessable(
                "A court attachment needs the order's reference number, so every recovery "
                "can be traced to the decree that allows it."
            )
        if payload.recurrence is AdjustmentRecurrence.MONTHLY and payload.total_cap is None:
            raise _unprocessable(
                "A monthly court attachment needs the decree's total, so recovery stops when "
                "it is satisfied."
            )
    return values


async def _names(session: AsyncSession, user: User, ids: set) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(select(User.id, User.full_name).where(User.id.in_(wanted)), User, user)
        )
    ).all()
    return {row[0]: row[1] for row in rows}


async def _reads(
    session: AsyncSession,
    user: User,
    rows: list[PayrollAdjustment],
    *,
    with_applications: bool = False,
) -> list[AdjustmentRead]:
    if not rows:
        return []
    employees = {
        row.id: row
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(Employee).where(Employee.id.in_({r.employee_id for r in rows})),
                    Employee,
                    user,
                )
            )
        ).all()
    }
    names = await _names(
        session,
        user,
        {r.created_by_user_id for r in rows} | {r.decided_by_user_id for r in rows},
    )
    applications: dict[uuid.UUID, list[AdjustmentApplicationRead]] = {}
    if with_applications:
        for app in (
            await session.scalars(
                tenant_scoped(
                    select(PayrollAdjustmentApplication)
                    .where(PayrollAdjustmentApplication.adjustment_id.in_([r.id for r in rows]))
                    .order_by(
                        PayrollAdjustmentApplication.period_year,
                        PayrollAdjustmentApplication.period_month,
                    ),
                    PayrollAdjustmentApplication,
                    user,
                )
            )
        ).all():
            applications.setdefault(app.adjustment_id, []).append(
                AdjustmentApplicationRead(
                    run_id=app.run_id,
                    period_year=app.period_year,
                    period_month=app.period_month,
                    amount=Decimal(str(app.amount)),
                    created_at=app.created_at,
                )
            )
    reads = []
    for row in rows:
        employee = employees.get(row.employee_id)
        reads.append(
            AdjustmentRead(
                id=row.id,
                employee_id=row.employee_id,
                employee_code=employee.employee_code if employee else None,
                employee_name=employee.full_name if employee else None,
                kind=row.kind,
                category=row.category,
                name=row.name,
                amount=Decimal(str(row.amount)),
                taxable=row.taxable,
                counts_as_wages=row.counts_as_wages,
                within_net_pay=row.within_net_pay,
                recurrence=row.recurrence,
                start_year=row.start_year,
                start_month=row.start_month,
                end_year=row.end_year,
                end_month=row.end_month,
                total_cap=Decimal(str(row.total_cap)) if row.total_cap is not None else None,
                amount_applied=Decimal(str(row.amount_applied)),
                reference=row.reference,
                reason=row.reason,
                status=row.status,
                created_by_user_id=row.created_by_user_id,
                created_by_name=names.get(row.created_by_user_id),  # type: ignore[arg-type]
                decided_by_name=names.get(row.decided_by_user_id),  # type: ignore[arg-type]
                decided_at=row.decided_at,
                decision_note=row.decision_note,
                created_at=row.created_at,
                caller_may_decide=(
                    row.status is AdjustmentStatus.PENDING
                    and row.created_by_user_id != user.id
                    and user.employee_id != row.employee_id
                    and permissions.is_admin(user)
                ),
                applications=applications.get(row.id, []),
            )
        )
    return reads


async def _get_row(
    session: AsyncSession, user: User, adjustment_id: uuid.UUID
) -> PayrollAdjustment:
    row = await session.scalar(
        tenant_scoped(
            select(PayrollAdjustment).where(PayrollAdjustment.id == adjustment_id),
            PayrollAdjustment,
            user,
        )
    )
    if row is None:
        raise _NOT_FOUND
    return row


async def get(session: AsyncSession, user: User, adjustment_id: uuid.UUID) -> AdjustmentRead:
    row = await _get_row(session, user, adjustment_id)
    return (await _reads(session, user, [row], with_applications=True))[0]


async def list_adjustments(
    session: AsyncSession,
    user: User,
    *,
    adjustment_status: AdjustmentStatus | None = None,
    employee_id: uuid.UUID | None = None,
    category: AdjustmentCategory | None = None,
    period_year: int | None = None,
    period_month: int | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[AdjustmentRead], int]:
    conditions = []
    if adjustment_status is not None:
        conditions.append(PayrollAdjustment.status == adjustment_status)
    if employee_id is not None:
        conditions.append(PayrollAdjustment.employee_id == employee_id)
    if category is not None:
        conditions.append(PayrollAdjustment.category == category)
    if period_year is not None and period_month is not None:
        here = period_year * 12 + period_month
        start = PayrollAdjustment.start_year * 12 + PayrollAdjustment.start_month
        end = PayrollAdjustment.end_year * 12 + PayrollAdjustment.end_month
        conditions.append(start <= here)
        conditions.append(
            or_(
                (PayrollAdjustment.recurrence == AdjustmentRecurrence.ONE_TIME) & (start == here),
                (PayrollAdjustment.recurrence == AdjustmentRecurrence.MONTHLY)
                & (or_(PayrollAdjustment.end_year.is_(None), end >= here)),
            )
        )
    total = int(
        await session.scalar(
            tenant_scoped(
                select(func.count(PayrollAdjustment.id)).where(*conditions),
                PayrollAdjustment,
                user,
            )
        )
        or 0
    )
    rows = (
        await session.scalars(
            tenant_scoped(
                select(PayrollAdjustment)
                .where(*conditions)
                .order_by(PayrollAdjustment.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size),
                PayrollAdjustment,
                user,
            )
        )
    ).all()
    return await _reads(session, user, list(rows)), total


async def pending_for(session: AsyncSession, user: User) -> list[PayrollAdjustment]:
    """Adjustments waiting on this admin's decision: not their own, not about them."""
    if not permissions.is_admin(user):
        return []
    rows = (
        await session.scalars(
            tenant_scoped(
                select(PayrollAdjustment)
                .where(PayrollAdjustment.status == AdjustmentStatus.PENDING)
                .order_by(PayrollAdjustment.created_at),
                PayrollAdjustment,
                user,
            )
        )
    ).all()
    return [
        r
        for r in rows
        if r.created_by_user_id != user.id
        and (user.employee_id is None or r.employee_id != user.employee_id)
    ]


def _refuse_own(user: User, employee_id: uuid.UUID) -> None:
    if user.employee_id is not None and user.employee_id == employee_id:
        raise permissions.forbidden(
            "Nobody keys in or decides an adjustment to their own pay. Ask a colleague in payroll."
        )


async def create(
    session: AsyncSession,
    user: User,
    payload: AdjustmentCreate,
    *,
    request: Request | None = None,
    commit: bool = True,
) -> AdjustmentRead:
    _refuse_own(user, payload.employee_id)
    employee = await _employee(session, user, payload.employee_id)
    values = _validated(payload)
    row = PayrollAdjustment(
        tenant_id=user.tenant_id,
        status=AdjustmentStatus.PENDING,
        created_by_user_id=user.id,
        **values,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="payroll_adjustment",
        entity_id=row.id,
        actor=user,
        changes={
            "employee_code": employee.employee_code,
            "category": row.category.value,
            "amount": str(row.amount),
            "recurrence": row.recurrence.value,
            "start": f"{row.start_year}-{row.start_month:02d}",
        },
        summary=(
            f"Keyed in {row.category.value.replace('_', ' ')} of {row.amount} for "
            f"{employee.employee_code}"
        ),
        request=request,
    )
    if not commit:
        return (await _reads(session, user, [row]))[0]
    await session.commit()
    await session.refresh(row)
    return (await _reads(session, user, [row]))[0]


async def _decide(
    session: AsyncSession,
    user: User,
    adjustment_id: uuid.UUID,
    *,
    approve_it: bool,
    note: str | None,
    request: Request | None,
) -> AdjustmentRead:
    if not permissions.is_admin(user):
        raise permissions.forbidden("Only an admin approves payroll adjustments.")
    row = await _get_row(session, user, adjustment_id)
    _refuse_own(user, row.employee_id)
    if row.status is not AdjustmentStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This adjustment is {row.status.value}; only a pending one is decided.",
        )
    if row.created_by_user_id is not None and row.created_by_user_id == user.id:
        raise permissions.forbidden(
            "You keyed this adjustment in, so somebody else has to approve it. Payroll is "
            "maker-checker."
        )
    clean_note = (note or "").strip() or None
    if not approve_it and clean_note is None:
        raise _unprocessable("Say why this adjustment is rejected.")
    row.status = AdjustmentStatus.APPROVED if approve_it else AdjustmentStatus.REJECTED
    row.decided_by_user_id = user.id
    row.decided_at = datetime.now(UTC)
    row.decision_note = clean_note
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve_it else AuditAction.REJECT,
        entity_type="payroll_adjustment",
        entity_id=row.id,
        actor=user,
        changes={"status": row.status.value, "note": clean_note},
        summary=f"{'Approved' if approve_it else 'Rejected'} payroll adjustment '{row.name}'",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return (await _reads(session, user, [row], with_applications=True))[0]


async def approve(
    session: AsyncSession,
    user: User,
    adjustment_id: uuid.UUID,
    *,
    note: str | None = None,
    request: Request | None = None,
) -> AdjustmentRead:
    return await _decide(session, user, adjustment_id, approve_it=True, note=note, request=request)


async def reject(
    session: AsyncSession,
    user: User,
    adjustment_id: uuid.UUID,
    *,
    note: str | None,
    request: Request | None = None,
) -> AdjustmentRead:
    return await _decide(session, user, adjustment_id, approve_it=False, note=note, request=request)


async def cancel(
    session: AsyncSession,
    user: User,
    adjustment_id: uuid.UUID,
    *,
    note: str | None = None,
    request: Request | None = None,
) -> AdjustmentRead:
    """Stop an adjustment. What a run already paid stays paid and on record."""
    row = await _get_row(session, user, adjustment_id)
    _refuse_own(user, row.employee_id)
    if row.status not in (AdjustmentStatus.PENDING, AdjustmentStatus.APPROVED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This adjustment is already {row.status.value}.",
        )
    row.status = AdjustmentStatus.CANCELLED
    row.cancelled_at = datetime.now(UTC)
    row.decision_note = (note or "").strip() or row.decision_note
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="payroll_adjustment",
        entity_id=row.id,
        actor=user,
        changes={"status": "cancelled", "amount_applied": str(row.amount_applied)},
        summary=f"Cancelled payroll adjustment '{row.name}'",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return (await _reads(session, user, [row], with_applications=True))[0]


# ── Bulk import ─────────────────────────────────────────────────────────────

_IMPORT_COLUMNS = ("employee_code", "category", "name", "amount", "period", "reason")
_TRUE = {"1", "true", "yes", "y"}


async def import_csv(
    session: AsyncSession,
    user: User,
    data: bytes,
    *,
    request: Request | None = None,
) -> AdjustmentImportResult:
    """One-time adjustments from a spreadsheet, all or nothing.

    Columns: employee_code, category, name, amount, period (YYYY-MM), reason, and
    optionally taxable, reference. Every row is checked before any is written:
    half an incentive file applied is worse than none.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return AdjustmentImportResult(created=0, errors=["The file is not UTF-8 CSV."])
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in _IMPORT_COLUMNS if c not in header]
    if missing:
        return AdjustmentImportResult(
            created=0, errors=[f"Missing column(s): {', '.join(missing)}."]
        )

    codes = {
        row.employee_code.upper(): row
        for row in (
            await session.scalars(
                tenant_scoped(select(Employee).where(Employee.deleted_at.is_(None)), Employee, user)
            )
        ).all()
    }
    payloads: list[AdjustmentCreate] = []
    errors: list[str] = []
    for number, raw in enumerate(reader, start=2):
        row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        if not any(row.values()):
            continue
        problems: list[str] = []
        employee = codes.get(row.get("employee_code", "").upper())
        if employee is None:
            problems.append(f"no employee with code '{row.get('employee_code')}'")
        try:
            category = AdjustmentCategory(row.get("category", "").lower())
        except ValueError:
            problems.append(f"unknown category '{row.get('category')}'")
            category = None
        try:
            amount = Decimal(row.get("amount", "").replace(",", ""))
            if amount <= 0:
                problems.append("the amount must be more than zero")
        except InvalidOperation:
            problems.append(f"'{row.get('amount')}' is not an amount")
            amount = None
        period = row.get("period", "")
        try:
            year_text, month_text = period.split("-")
            year, month = int(year_text), int(month_text)
            if not 1 <= month <= 12:
                raise ValueError
        except ValueError:
            problems.append(f"'{period}' is not a period like 2026-10")
            year = month = None
        if problems:
            errors.append(f"Row {number}: " + "; ".join(problems) + ".")
            continue
        try:
            payloads.append(
                AdjustmentCreate(
                    employee_id=employee.id,  # type: ignore[union-attr]
                    category=category,  # type: ignore[arg-type]
                    name=row.get("name") or category.value.replace("_", " ").title(),  # type: ignore[union-attr]
                    amount=amount,  # type: ignore[arg-type]
                    taxable=row.get("taxable", "yes").lower() in _TRUE,
                    start_year=year,  # type: ignore[arg-type]
                    start_month=month,  # type: ignore[arg-type]
                    reference=row.get("reference") or None,
                    reason=row.get("reason") or "",
                )
            )
        except ValueError as exc:
            errors.append(f"Row {number}: {exc}")
    if errors:
        return AdjustmentImportResult(created=0, errors=errors)
    if not payloads:
        return AdjustmentImportResult(created=0, errors=["The file has no rows."])

    try:
        for payload in payloads:
            await create(session, user, payload, request=request, commit=False)
    except HTTPException as exc:
        await session.rollback()
        return AdjustmentImportResult(created=0, errors=[str(exc.detail)])
    await session.commit()
    return AdjustmentImportResult(created=len(payloads), errors=[])
