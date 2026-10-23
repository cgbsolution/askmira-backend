"""The Salary master: one employee's salary revisions, newest first.

HR searches a person, sees every revision as a card, and can correct or delete
one — but only while payroll has not used it. A revision with a payslip in an
approved or paid run, or with arrears raised against it, is history: what was
paid on it must stay reproducible, so it is locked and the screen says why.
A correction to a paid month is a new revision plus arrears, never an edit.

Every change here writes its audit entry in the same transaction as the change.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.arrears import SalaryRevision
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.payroll import (
    ComponentType,
    EmployeeSalary,
    PayrollRun,
    PayrollRunStatus,
    Payslip,
)
from app.models.user import User
from app.schemas.payroll import (
    EmployeeSalaryComponentRead,
    EmployeeSalaryUpdate,
    SalaryRevisionRead,
)
from app.services import audit
from app.services.documents.vault import _checked, _download_name
from app.services.payroll.service import (
    ResolvedSalary,
    get_structure,
    manual_summary,
    materialise,
    resolve_amounts,
    resolve_manual,
    type_totals,
)
from app.services.storage import StorageError, get_storage

_PAISE = Decimal("0.01")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

#: A run in one of these states has been signed off; what it paid is history.
_CLOSED = (PayrollRunStatus.APPROVED, PayrollRunStatus.PAID)
#: A run in one of these states can still be recomputed.
_OPEN = (
    PayrollRunStatus.DRAFT,
    PayrollRunStatus.PROCESSING,
    PayrollRunStatus.PENDING_REVIEW,
    PayrollRunStatus.FAILED,
)


def _money(value: object) -> Decimal:
    return Decimal(str(value)).quantize(_PAISE)


def _period(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


def _period_label(year: int, month: int) -> str:
    return f"{_MONTHS[month - 1]} {year}"


def _day(value: date) -> str:
    return f"{value.day:02d} {_MONTHS[value.month - 1]} {value.year}"


def _not_found(detail: str = "Salary revision not found") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


# ── Reading ─────────────────────────────────────────────────────────────────


async def _employee(session: AsyncSession, user: User, employee_id: uuid.UUID) -> Employee:
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise _not_found("Employee not found")
    return employee


async def _series(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> list[EmployeeSalary]:
    """Every revision, oldest first."""
    stmt = tenant_scoped(
        select(EmployeeSalary)
        .where(EmployeeSalary.employee_id == employee_id)
        .order_by(EmployeeSalary.effective_from),
        EmployeeSalary,
        user,
    )
    return list((await session.scalars(stmt)).all())


async def revision_locks(
    session: AsyncSession, user: User, salary_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Why each locked revision is locked. A revision missing here may be changed."""
    if not salary_ids:
        return {}
    paid: dict[uuid.UUID, list[tuple[int, int]]] = {}
    rows = await session.execute(
        tenant_scoped(
            select(Payslip.employee_salary_id, PayrollRun.period_year, PayrollRun.period_month)
            .join(PayrollRun, Payslip.run_id == PayrollRun.id)
            .where(
                Payslip.employee_salary_id.in_(salary_ids),
                PayrollRun.status.in_(_CLOSED),
            )
            .distinct(),
            Payslip,
            user,
        )
    )
    for salary_id, year, month in rows.all():
        paid.setdefault(salary_id, []).append((year, month))

    locks: dict[uuid.UUID, str] = {}
    for salary_id, periods in paid.items():
        periods.sort()
        first, last = _period_label(*periods[0]), _period_label(*periods[-1])
        span = first if first == last else f"{first} to {last}"
        locks[salary_id] = f"payroll for {span} was approved on this salary"

    arrears = await session.execute(
        tenant_scoped(
            select(SalaryRevision.previous_salary_id, SalaryRevision.new_salary_id).where(
                or_(
                    SalaryRevision.previous_salary_id.in_(salary_ids),
                    SalaryRevision.new_salary_id.in_(salary_ids),
                )
            ),
            SalaryRevision,
            user,
        )
    )
    for previous_id, new_id in arrears.all():
        for salary_id in (previous_id, new_id):
            if salary_id in salary_ids and salary_id not in locks:
                locks[salary_id] = "arrears were raised against this salary"
    return locks


def _component_type(value: object) -> ComponentType:
    return value if isinstance(value, ComponentType) else ComponentType(str(value))


def revision_read(salary: EmployeeSalary, lock_reason: str | None) -> SalaryRevisionRead:
    components = sorted(salary.components, key=lambda c: (c.display_order, c.component_code))
    earnings = [c for c in components if _component_type(c.component_type) is ComponentType.EARNING]
    basic = next(
        (
            c
            for c in earnings
            if c.component_code.upper() == "BASIC" or c.component_name.lower().startswith("basic")
        ),
        None,
    )
    return SalaryRevisionRead(
        id=salary.id,
        employee_id=salary.employee_id,
        structure_id=salary.structure_id,
        structure_name=salary.structure.name if salary.structure else None,
        ctc_annual=_money(salary.ctc_annual),
        ctc_monthly=_money(Decimal(str(salary.ctc_annual)) / 12),
        gross_monthly=_money(sum((Decimal(str(c.monthly_amount)) for c in earnings), Decimal(0))),
        basic_monthly=_money(basic.monthly_amount) if basic else None,
        currency=salary.currency,
        effective_from=salary.effective_from,
        effective_to=salary.effective_to,
        revision_reason=salary.revision_reason,
        attachment_name=salary.attachment_name if salary.attachment_key else None,
        components=[EmployeeSalaryComponentRead.model_validate(c) for c in components],
        totals=type_totals(
            [
                (_component_type(c.component_type), c.monthly_amount, c.annual_amount)
                for c in components
            ]
        ),
        locked=lock_reason is not None,
        locked_reason=lock_reason,
        updated_at=salary.updated_at,
    )


async def list_revisions(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> list[SalaryRevisionRead]:
    """The Salary master's timeline for one person, newest first."""
    await _employee(session, user, employee_id)
    series = await _series(session, user, employee_id)
    locks = await revision_locks(session, user, [s.id for s in series])
    return [revision_read(s, locks.get(s.id)) for s in reversed(series)]


async def _open_runs_from(
    session: AsyncSession, user: User, employee_id: uuid.UUID, since: date
) -> list[str]:
    """Open runs, from `since`, that already worked this person out on old amounts."""
    rows = await session.execute(
        tenant_scoped(
            select(PayrollRun.period_year, PayrollRun.period_month)
            .join(Payslip, Payslip.run_id == PayrollRun.id)
            .where(
                Payslip.employee_id == employee_id,
                PayrollRun.status.in_(_OPEN),
                PayrollRun.effective_date >= since,
            )
            .distinct()
            .order_by(PayrollRun.period_year, PayrollRun.period_month),
            PayrollRun,
            user,
        )
    )
    return [_period(year, month) for year, month in rows.all()]


async def _paid_months_between(
    session: AsyncSession, user: User, employee_id: uuid.UUID, start: date, end: date | None
) -> list[str]:
    """Months in [start, end] an approved run already paid this person."""
    stmt = (
        select(PayrollRun.period_year, PayrollRun.period_month)
        .join(Payslip, Payslip.run_id == PayrollRun.id)
        .where(
            Payslip.employee_id == employee_id,
            Payslip.is_supplementary.is_(False),
            PayrollRun.status.in_(_CLOSED),
            PayrollRun.effective_date >= start,
        )
    )
    if end is not None:
        stmt = stmt.where(PayrollRun.effective_date <= end)
    rows = await session.execute(
        tenant_scoped(
            stmt.distinct().order_by(PayrollRun.period_year, PayrollRun.period_month),
            PayrollRun,
            user,
        )
    )
    return [_period(year, month) for year, month in rows.all()]


# ── Correcting a revision ───────────────────────────────────────────────────


def _snapshot(salary: EmployeeSalary, structure_name: str | None) -> dict:
    """A revision as an audit entry records it."""
    return {
        "ctc_annual": str(_money(salary.ctc_annual)),
        "effective_from": salary.effective_from.isoformat(),
        "structure": structure_name,
        "reason": salary.revision_reason,
        "typed_amounts": {
            c.component_code: str(_money(c.annual_amount)) for c in salary.components if c.is_manual
        },
        "remarks": {c.component_code: c.remarks for c in salary.components if c.remarks},
    }


#: Per wage type: the typed annual amount (None if calculated), the remark, the basis.
Typed = dict[str, tuple[Decimal | None, str | None, str]]
_UNTYPED: tuple[Decimal | None, str | None, str] = (None, None, "monthly")


def _typed(salary: EmployeeSalary) -> Typed:
    return {
        c.component_code: (
            Decimal(str(c.annual_amount)) if c.is_manual else None,
            c.remarks,
            c.pay_basis if c.is_manual else "monthly",
        )
        for c in salary.components
        if c.is_manual or c.remarks
    }


def _typed_after(resolved: ResolvedSalary) -> Typed:
    out: Typed = {}
    for component in resolved.breakdown.components:
        remark = resolved.remarks.get(component.code)
        if component.is_manual or remark:
            out[component.code] = (
                component.annual_amount if component.is_manual else None,
                remark,
                resolved.bases.get(component.code, "monthly") if component.is_manual else "monthly",
            )
    return out


async def _replace_components(
    session: AsyncSession, salary: EmployeeSalary, resolved: ResolvedSalary, tenant_id: uuid.UUID
) -> None:
    """Swap a revision's amounts. Flushed in two steps so the old rows are gone
    before the new ones arrive (each wage type appears once per revision)."""
    salary.components.clear()
    await session.flush()
    materialise(salary, resolved, tenant_id)


@dataclass
class SalaryChange:
    salary: EmployeeSalary
    paid_months_affected: list[str] = field(default_factory=list)
    runs_to_recompute: list[str] = field(default_factory=list)
    carried_forward_to: list[date] = field(default_factory=list)


async def update_salary(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    salary_id: uuid.UUID,
    payload: EmployeeSalaryUpdate,
    request: Request | None = None,
) -> SalaryChange:
    """Correct a revision payroll has not used yet.

    The date may move, but only between the revisions either side of it. With
    `apply_to_later_revisions`, the typed amounts and remarks changed here are
    carried into every later revision too, each re-resolved on its own CTC.
    Everything is checked before anything is written.
    """
    employee = await _employee(session, user, employee_id)
    series = await _series(session, user, employee_id)
    index = next((i for i, s in enumerate(series) if s.id == salary_id), None)
    if index is None:
        raise _not_found()
    salary = series[index]
    previous = series[index - 1] if index > 0 else None
    later = series[index + 1 :]
    following = later[0] if later else None

    locks = await revision_locks(session, user, [s.id for s in series])
    if salary.id in locks:
        raise _conflict(
            f"This revision can't be changed: {locks[salary.id]}. Add a new revision "
            "instead, and raise arrears for any month already paid."
        )
    if payload.apply_to_later_revisions:
        locked_later = [s for s in later if s.id in locks]
        if locked_later:
            raise _conflict(
                f"The revision from {_day(locked_later[0].effective_from)} can't be changed "
                f"({locks[locked_later[0].id]}), so later revisions can't be updated from here."
            )

    structure = await get_structure(session, user, payload.structure_id)
    if payload.effective_from < structure.effective_from:
        raise _unprocessable(
            f"Salary is effective {payload.effective_from} but the structure "
            f"'{structure.name}' only starts on {structure.effective_from}"
        )
    if previous is not None and payload.effective_from <= previous.effective_from:
        raise _unprocessable(
            f"This revision must start after the one before it ({_day(previous.effective_from)})."
        )
    if following is not None and payload.effective_from >= following.effective_from:
        raise _unprocessable(
            f"This revision must start before the next one ({_day(following.effective_from)})."
        )

    resolved = await resolve_amounts(session, user, structure, payload)

    # ── Carry forward: resolve every later revision before writing anything ──
    carried: list[tuple[EmployeeSalary, ResolvedSalary]] = []
    if payload.apply_to_later_revisions and later:
        before, after = _typed(salary), _typed_after(resolved)
        changed = {
            code: after.get(code, _UNTYPED)
            for code in set(before) | set(after)
            if before.get(code, _UNTYPED) != after.get(code, _UNTYPED)
        }
        if changed:
            for revision in later:
                typed = _typed(revision)
                for code, value in changed.items():
                    typed[code] = value
                manual = {
                    code: amount for code, (amount, _, _) in typed.items() if amount is not None
                }
                remarks = {code: note for code, (_, note, _) in typed.items() if note}
                bases = {code: basis for code, (_, _, basis) in typed.items()}
                try:
                    carried.append(
                        (
                            revision,
                            await resolve_manual(
                                session,
                                user,
                                revision.structure,
                                Decimal(str(revision.ctc_annual)),
                                manual,
                                remarks,
                                bases,
                            ),
                        )
                    )
                except HTTPException as exc:
                    raise _unprocessable(
                        f"The revision from {_day(revision.effective_from)} could not take "
                        f"this change: {exc.detail}"
                    ) from exc

    # ── Write ───────────────────────────────────────────────────────────────
    old_from = salary.effective_from
    before_audit = _snapshot(salary, salary.structure.name if salary.structure else None)
    await _replace_components(session, salary, resolved, user.tenant_id)
    salary.structure_id = structure.id
    salary.structure = structure
    salary.ctc_annual = resolved.ctc_annual
    salary.effective_from = payload.effective_from
    salary.revision_reason = payload.revision_reason
    salary.approved_by_user_id = user.id
    if previous is not None:
        previous.effective_to = payload.effective_from - timedelta(days=1)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="employee_salary",
        entity_id=salary.id,
        actor=user,
        changes={
            "employee_id": str(employee_id),
            "before": before_audit,
            "after": {
                **_snapshot(salary, structure.name),
                "typed_amounts": manual_summary(resolved),
            },
            "carried_to_later_revisions": bool(carried),
        },
        summary=(
            f"Salary revision from {_day(payload.effective_from)} corrected for "
            f"{employee.employee_code}"
        ),
        request=request,
    )

    for revision, revision_resolved in carried:
        revision_before = _snapshot(
            revision, revision.structure.name if revision.structure else None
        )
        await _replace_components(session, revision, revision_resolved, user.tenant_id)
        revision.approved_by_user_id = user.id
        await session.flush()
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="employee_salary",
            entity_id=revision.id,
            actor=user,
            changes={
                "employee_id": str(employee_id),
                "before": revision_before,
                "after": _snapshot(
                    revision, revision.structure.name if revision.structure else None
                ),
                "carried_from": str(salary.id),
            },
            summary=(
                f"Salary revision from {_day(revision.effective_from)} updated for "
                f"{employee.employee_code}, carried from {_day(payload.effective_from)}"
            ),
            request=request,
        )

    since = min(old_from, payload.effective_from)
    end = following.effective_from - timedelta(days=1) if following else None
    runs = await _open_runs_from(session, user, employee_id, since)
    paid = await _paid_months_between(session, user, employee_id, payload.effective_from, end)
    await session.commit()
    await session.refresh(salary)
    return SalaryChange(
        salary=salary,
        paid_months_affected=paid,
        runs_to_recompute=runs,
        carried_forward_to=[revision.effective_from for revision, _ in carried],
    )


async def delete_salary(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    salary_id: uuid.UUID,
    request: Request | None = None,
) -> list[str]:
    """Delete a revision payroll has not used. The one before it runs on in its place.

    Returns the open payroll runs that should be recomputed.
    """
    employee = await _employee(session, user, employee_id)
    series = await _series(session, user, employee_id)
    index = next((i for i, s in enumerate(series) if s.id == salary_id), None)
    if index is None:
        raise _not_found()
    salary = series[index]
    previous = series[index - 1] if index > 0 else None

    locks = await revision_locks(session, user, [salary.id])
    if salary.id in locks:
        raise _conflict(
            f"This revision can't be deleted: {locks[salary.id]}. Payroll history stays "
            "as it was paid."
        )

    runs = await _open_runs_from(session, user, employee_id, salary.effective_from)
    snapshot = _snapshot(salary, salary.structure.name if salary.structure else None)
    attachment_key = salary.attachment_key
    if previous is not None:
        previous.effective_to = salary.effective_to

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="employee_salary",
        entity_id=salary.id,
        actor=user,
        changes={"employee_id": str(employee_id), "deleted": snapshot},
        summary=(
            f"Salary revision from {_day(salary.effective_from)} deleted for "
            f"{employee.employee_code}"
        ),
        request=request,
    )
    await session.delete(salary)
    await session.commit()

    if attachment_key:
        # The row is gone; a file left behind is only storage, never data.
        with contextlib.suppress(StorageError):
            await get_storage().delete(attachment_key)
    return runs


# ── Attachment ──────────────────────────────────────────────────────────────


async def _revision(
    session: AsyncSession, user: User, employee_id: uuid.UUID, salary_id: uuid.UUID
) -> EmployeeSalary:
    salary = await session.scalar(
        tenant_scoped(
            select(EmployeeSalary).where(
                EmployeeSalary.id == salary_id, EmployeeSalary.employee_id == employee_id
            ),
            EmployeeSalary,
            user,
        )
    )
    if salary is None:
        raise _not_found()
    return salary


def _shown_name(filename: str | None) -> str:
    """The file's own name, for display only. Never part of a storage key."""
    name = re.split(r"[\\/]", filename or "")[-1].strip()
    name = re.sub(r"[\x00-\x1f]+", "", name)
    return (name or "attachment")[:255]


async def attach_file(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    salary_id: uuid.UUID,
    *,
    filename: str | None,
    data: bytes,
    request: Request | None = None,
) -> EmployeeSalary:
    """Attach (or replace) the file behind a revision: a PDF, PNG or JPEG.

    Allowed on a locked revision too; the file documents the salary, it does
    not change what was paid.
    """
    salary = await _revision(session, user, employee_id, salary_id)
    kind = _checked(data)
    storage = get_storage()
    key = storage.new_key(user.tenant_id, "salary-attachments", kind.extension)
    try:
        await storage.put(key, data)
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    old_key = salary.attachment_key
    salary.attachment_key = key
    salary.attachment_name = _shown_name(filename)
    salary.attachment_mime = kind.mime_type
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="employee_salary",
        entity_id=salary.id,
        actor=user,
        changes={"attachment": salary.attachment_name, "replaced": old_key is not None},
        summary=f"File attached to the salary revision from {_day(salary.effective_from)}",
        request=request,
    )
    await session.commit()
    if old_key:
        with contextlib.suppress(StorageError):
            await storage.delete(old_key)
    await session.refresh(salary)
    return salary


async def read_attachment(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    salary_id: uuid.UUID,
    request: Request | None = None,
) -> tuple[str, str, bytes]:
    """The file, a safe download name and its type. Audited: who took a copy."""
    salary = await _revision(session, user, employee_id, salary_id)
    if not salary.attachment_key:
        raise _not_found("This revision has no attachment")
    try:
        data = await get_storage().get(salary.attachment_key)
    except StorageError as exc:
        raise _not_found(str(exc)) from exc
    if data is None:
        raise _not_found("The stored file is missing. Attach it again.")
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.EXPORT,
        entity_type="employee_salary",
        entity_id=salary.id,
        actor=user,
        summary=f"Downloaded the file on the salary revision from {_day(salary.effective_from)}",
        request=request,
    )
    await session.commit()
    mime = salary.attachment_mime or "application/octet-stream"
    stem = (salary.attachment_name or "salary-revision").rsplit(".", 1)[0]
    return _download_name(stem, mime), mime, data


async def remove_attachment(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    salary_id: uuid.UUID,
    request: Request | None = None,
) -> None:
    salary = await _revision(session, user, employee_id, salary_id)
    key = salary.attachment_key
    if not key:
        return
    name = salary.attachment_name
    salary.attachment_key = None
    salary.attachment_name = None
    salary.attachment_mime = None
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="employee_salary",
        entity_id=salary.id,
        actor=user,
        changes={"attachment_removed": name},
        summary=f"File removed from the salary revision from {_day(salary.effective_from)}",
        request=request,
    )
    await session.commit()
    with contextlib.suppress(StorageError):
        await get_storage().delete(key)


__all__ = [
    "SalaryChange",
    "attach_file",
    "delete_salary",
    "list_revisions",
    "read_attachment",
    "remove_attachment",
    "revision_locks",
    "revision_read",
    "update_salary",
]
