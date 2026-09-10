"""Persistence for salary structures and employee salary assignments.

The resolution arithmetic lives in `salary_structure.py`; this module is the
database side of it — creating structures, materialising resolved amounts onto
an employee, and keeping the effective-dated series honest.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.payroll import (
    EmployeeSalary,
    EmployeeSalaryComponent,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
)
from app.models.user import User
from app.schemas.payroll import (
    EmployeeSalaryCreate,
    SalaryComponentCreate,
    SalaryStructureCreate,
)
from app.services import audit
from app.services.payroll.salary_structure import (
    SalaryBreakdown,
    SalaryStructureError,
    resolve_ctc,
    validate_structure,
)

_PAISE = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    """One representation for money in audit records and messages."""
    return Decimal(str(value)).quantize(_PAISE)


def _bad_request(exc: SalaryStructureError) -> HTTPException:
    """Structure errors are configuration mistakes, so 422 with the real message.

    The messages from the engine name the offending component and the cycle;
    swallowing them into a generic error would make the CTC builder unusable.
    """
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


# ── Components ──────────────────────────────────────────────────────────────


async def create_component(
    session: AsyncSession,
    user: User,
    payload: SalaryComponentCreate,
    request: Request | None = None,
) -> SalaryComponent:
    existing = await session.scalar(
        tenant_scoped(
            select(SalaryComponent.id).where(SalaryComponent.code == payload.code),
            SalaryComponent,
            user,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A salary component with code {payload.code} already exists",
        )

    component = SalaryComponent(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(component)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="salary_component",
        entity_id=component.id,
        actor=user,
        changes=payload.model_dump(mode="json"),
        summary=f"Created salary component {component.code}",
        request=request,
    )
    await session.commit()
    await session.refresh(component)
    return component


async def list_components(session: AsyncSession, user: User) -> list[SalaryComponent]:
    stmt = tenant_scoped(
        select(SalaryComponent).order_by(SalaryComponent.display_order, SalaryComponent.code),
        SalaryComponent,
        user,
    )
    return list((await session.scalars(stmt)).all())


# ── Structures ──────────────────────────────────────────────────────────────


async def create_structure(
    session: AsyncSession,
    user: User,
    payload: SalaryStructureCreate,
    request: Request | None = None,
) -> SalaryStructure:
    """Create a structure, rejecting anything that cannot resolve.

    Validation runs before the commit, so an invalid structure never reaches
    the database and cannot be assigned to anyone later.
    """
    codes = [item.component_code for item in payload.components]
    stmt = tenant_scoped(
        select(SalaryComponent).where(SalaryComponent.code.in_(codes)),
        SalaryComponent,
        user,
    )
    components = {c.code: c for c in (await session.scalars(stmt)).all()}

    missing = set(codes) - set(components)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown salary component(s): {', '.join(sorted(missing))}",
        )

    structure = SalaryStructure(
        tenant_id=user.tenant_id,
        **payload.model_dump(exclude={"components"}),
    )
    for item in payload.components:
        structure.components.append(
            SalaryStructureComponent(
                tenant_id=user.tenant_id,
                component_id=components[item.component_code].id,
                component=components[item.component_code],
                override_value=item.override_value,
                override_formula=item.override_formula,
                override_base_component_code=item.override_base_component_code,
                display_order=item.display_order,
            )
        )

    try:
        validate_structure(structure)
    except SalaryStructureError as exc:
        raise _bad_request(exc) from exc

    session.add(structure)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="salary_structure",
        entity_id=structure.id,
        actor=user,
        changes={"name": structure.name, "components": codes},
        summary=f"Created salary structure {structure.name}",
        request=request,
    )
    await session.commit()
    await session.refresh(structure)
    return structure


async def get_structure(
    session: AsyncSession, user: User, structure_id: uuid.UUID
) -> SalaryStructure:
    structure = await session.scalar(
        tenant_scoped(
            select(SalaryStructure).where(SalaryStructure.id == structure_id),
            SalaryStructure,
            user,
        )
    )
    if structure is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Salary structure not found"
        )
    return structure


async def preview_structure(
    session: AsyncSession, user: User, structure_id: uuid.UUID, ctc: Decimal
) -> tuple[SalaryStructure, SalaryBreakdown]:
    """Resolve a CTC without persisting anything.

    This is the screen HR actually uses, so it does one query and no writes.
    """
    structure = await get_structure(session, user, structure_id)
    try:
        return structure, resolve_ctc(structure, ctc)
    except SalaryStructureError as exc:
        raise _bad_request(exc) from exc


async def list_structures(session: AsyncSession, user: User) -> list[SalaryStructure]:
    stmt = tenant_scoped(
        select(SalaryStructure).order_by(SalaryStructure.effective_from.desc()),
        SalaryStructure,
        user,
    )
    return list((await session.scalars(stmt)).all())


# ── Employee salary ─────────────────────────────────────────────────────────


async def assign_salary(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    payload: EmployeeSalaryCreate,
    request: Request | None = None,
    commit: bool = True,
) -> EmployeeSalary:
    """Assign or revise an employee's salary, effective-dated.

    Materialises the resolved component amounts onto `EmployeeSalaryComponent`
    rows. Those rows are the payroll engine's input and are never rewritten, so
    a later edit to the structure leaves this assignment untouched.
    """
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    structure = await get_structure(session, user, payload.structure_id)

    if payload.effective_from < structure.effective_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Salary is effective {payload.effective_from} but the structure "
                f"'{structure.name}' only starts on {structure.effective_from}"
            ),
        )

    try:
        breakdown = resolve_ctc(structure, payload.ctc_annual)
    except SalaryStructureError as exc:
        raise _bad_request(exc) from exc

    # ── Close the previous revision ─────────────────────────────────────────
    current = await session.scalar(
        tenant_scoped(
            select(EmployeeSalary)
            .where(
                EmployeeSalary.employee_id == employee_id,
                EmployeeSalary.effective_to.is_(None),
            )
            .order_by(EmployeeSalary.effective_from.desc()),
            EmployeeSalary,
            user,
        )
    )
    previous_ctc: Decimal | None = None
    if current is not None:
        if current.effective_from >= payload.effective_from:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"This employee already has a salary effective "
                    f"{current.effective_from}. A revision must start after it — "
                    "backdated corrections are an arrears run, not an edit."
                ),
            )
        previous_ctc = Decimal(str(current.ctc_annual))
        current.effective_to = payload.effective_from - timedelta(days=1)

    salary = EmployeeSalary(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        structure_id=structure.id,
        ctc_annual=payload.ctc_annual,
        effective_from=payload.effective_from,
        revision_reason=payload.revision_reason,
        approved_by_user_id=user.id,
    )
    for resolved in breakdown.components:
        salary.components.append(
            EmployeeSalaryComponent(
                tenant_id=user.tenant_id,
                component_code=resolved.code,
                component_name=resolved.name,
                component_type=resolved.component_type,
                annual_amount=resolved.annual_amount,
                monthly_amount=resolved.monthly_amount,
                taxable=resolved.taxable,
                part_of_ctc=resolved.part_of_ctc,
                pro_rate_on_lop=resolved.pro_rate_on_lop,
                counts_toward_pf=resolved.counts_toward_pf,
                counts_toward_esi=resolved.counts_toward_esi,
                display_order=resolved.display_order,
            )
        )

    session.add(salary)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE if current is not None else AuditAction.CREATE,
        entity_type="employee_salary",
        entity_id=salary.id,
        actor=user,
        changes={
            "employee_id": str(employee_id),
            # Both normalised to paise. The old value comes back from a
            # Numeric(14,2) column and the new one from the request body; left
            # alone they serialise as "1200000.00" and "1500000", which reads
            # like two different kinds of number in an audit export.
            "old_ctc": str(_money(previous_ctc)) if previous_ctc is not None else None,
            "new_ctc": str(_money(payload.ctc_annual)),
            "structure": structure.name,
            "effective_from": payload.effective_from.isoformat(),
            "reason": payload.revision_reason,
        },
        summary=(
            f"Salary revised for {employee.employee_code}: "
            f"{_money(previous_ctc)} to {_money(payload.ctc_annual)}"
            if previous_ctc is not None
            else f"Salary assigned to {employee.employee_code}: {_money(payload.ctc_annual)}"
        ),
        request=request,
    )
    # `commit=False` for a caller that owns the transaction — a lifecycle
    # cascade runs inside a savepoint, and committing here would close it out
    # from under the caller, taking the atomicity guarantee with it.
    if commit:
        await session.commit()
        await session.refresh(salary)
    return salary


async def salary_history(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> list[EmployeeSalary]:
    """The full effective-dated series, newest first."""
    stmt = tenant_scoped(
        select(EmployeeSalary)
        .where(EmployeeSalary.employee_id == employee_id)
        .order_by(EmployeeSalary.effective_from.desc()),
        EmployeeSalary,
        user,
    )
    return list((await session.scalars(stmt)).all())


async def active_salary_on(
    session: AsyncSession, user: User, employee_id: uuid.UUID, on_date: date
) -> EmployeeSalary | None:
    """The salary in force on a given date.

    This is what a payroll run resolves against — by the run's effective date,
    never by today, so a re-run months later produces the identical result.
    """
    stmt = tenant_scoped(
        select(EmployeeSalary)
        .where(
            EmployeeSalary.employee_id == employee_id,
            EmployeeSalary.effective_from <= on_date,
            (EmployeeSalary.effective_to.is_(None)) | (EmployeeSalary.effective_to >= on_date),
        )
        .order_by(EmployeeSalary.effective_from.desc()),
        EmployeeSalary,
        user,
    )
    return await session.scalar(stmt)
