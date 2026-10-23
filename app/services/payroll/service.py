"""Persistence for salary structures and employee salary assignments.

The resolution arithmetic lives in `salary_structure.py`; this module is the
database side of it — creating structures, materialising resolved amounts onto
an employee, and keeping the effective-dated series honest.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.payroll import (
    DEFAULT_WAGE_GROUP,
    ComponentType,
    EmployeeSalary,
    EmployeeSalaryComponent,
    PayrollRun,
    PayrollRunStatus,
    Payslip,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
    WageGroup,
    wage_group_of,
)
from app.models.user import User
from app.schemas.payroll import (
    EmployeeSalaryComponentRead,
    EmployeeSalaryCreate,
    SalaryAmountsInput,
    SalaryComponentCreate,
    SalaryGroupTotalRead,
    SalaryStructureCreate,
    SalaryTypeTotalRead,
)
from app.schemas.payroll_run import (
    SalaryOverviewRead,
    SalaryRecordRead,
    SalaryStructureDetailRead,
    StructureComponentDetailRead,
)
from app.services import audit
from app.services.payroll.salary_structure import (
    SalaryBreakdown,
    SalaryStructureError,
    fits_structure,
    resolve_ctc,
    round_to_structure,
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

    component = SalaryComponent(
        tenant_id=user.tenant_id,
        **payload.model_dump(exclude={"wage_group"}),
        wage_group=payload.wage_group.value if payload.wage_group else None,
    )
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


async def preview_salary(
    session: AsyncSession, user: User, structure_id: uuid.UUID, payload: SalaryAmountsInput
) -> tuple[SalaryStructure, ResolvedSalary]:
    """What the Salary master's editor shows as HR types. Writes nothing."""
    structure = await get_structure(session, user, structure_id)
    return structure, await resolve_amounts(session, user, structure, payload)


async def list_structures(session: AsyncSession, user: User) -> list[SalaryStructure]:
    stmt = tenant_scoped(
        select(SalaryStructure).order_by(SalaryStructure.effective_from.desc()),
        SalaryStructure,
        user,
    )
    return list((await session.scalars(stmt)).all())


# ── Resolving one employee's amounts ────────────────────────────────────────

#: The order kinds of component are listed and totalled in.
TYPE_ORDER = (
    ComponentType.EARNING,
    ComponentType.REIMBURSEMENT,
    ComponentType.DEDUCTION,
    ComponentType.EMPLOYER_CONTRIBUTION,
)


@dataclass
class ResolvedSalary:
    """A CTC resolved for one employee, with their typed amounts and remarks."""

    ctc_annual: Decimal
    breakdown: SalaryBreakdown
    remarks: dict[str, str] = field(default_factory=dict)
    #: How each typed amount was entered, "monthly" or "annual", by code.
    bases: dict[str, str] = field(default_factory=dict)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


async def resolve_manual(
    session: AsyncSession,
    user: User,
    structure: SalaryStructure,
    ctc_annual: Decimal,
    manual: dict[str, Decimal],
    remarks: dict[str, str],
    bases: dict[str, str] | None = None,
) -> ResolvedSalary:
    """Resolve a CTC with annual amounts pinned for some wage types.

    A wage type outside the structure is looked up among the tenant's own
    components; one that does not exist or is switched off is refused.
    """
    in_structure = {link.component.code for link in structure.components}
    extra_codes = set(manual) - in_structure
    extras: list[SalaryComponent] = []
    if extra_codes:
        found = {
            component.code: component
            for component in (
                await session.scalars(
                    tenant_scoped(
                        select(SalaryComponent).where(SalaryComponent.code.in_(extra_codes)),
                        SalaryComponent,
                        user,
                    )
                )
            ).all()
        }
        missing = sorted(extra_codes - set(found))
        if missing:
            raise _unprocessable(f"Unknown wage type(s): {', '.join(missing)}")
        inactive = sorted(c.name for c in found.values() if not c.active)
        if inactive:
            raise _unprocessable(f"{', '.join(inactive)} is switched off in payroll setup")
        extras = [found[code] for code in sorted(extra_codes)]
    try:
        breakdown = resolve_ctc(structure, ctc_annual, manual=manual, extras=extras)
    except SalaryStructureError as exc:
        raise _bad_request(exc) from exc
    present = {component.code for component in breakdown.components}
    return ResolvedSalary(
        ctc_annual=ctc_annual,
        breakdown=breakdown,
        remarks={code: note for code, note in remarks.items() if code in present},
        bases={code: basis for code, basis in (bases or {}).items() if code in manual},
    )


async def resolve_amounts(
    session: AsyncSession, user: User, structure: SalaryStructure, payload: SalaryAmountsInput
) -> ResolvedSalary:
    """Resolve what the Salary master sent: a CTC, typed amounts and remarks.

    Monthly figures become annual ones here, on the server, so a browser never
    multiplies money.
    """
    if payload.ctc_annual is not None:
        ctc = payload.ctc_annual
    else:
        assert payload.ctc_monthly is not None  # the schema requires one of the two
        ctc = round_to_structure(structure, payload.ctc_monthly * 12)

    in_structure = {link.component.code for link in structure.components}
    manual: dict[str, Decimal] = {}
    remarks: dict[str, str] = {}
    bases: dict[str, str] = {}
    for item in payload.components:
        if item.remarks:
            remarks[item.component_code] = item.remarks
        if item.amount is None:
            continue
        # A wage type the structure does not have, left at nothing, is not paid.
        if item.amount == 0 and item.component_code not in in_structure:
            continue
        if not fits_structure(structure, item.amount):
            raise _unprocessable(
                f"{item.component_code}: {item.amount} is finer than this structure's "
                "rounding. Round the amount."
            )
        manual[item.component_code] = item.amount * 12 if item.basis == "monthly" else item.amount
        bases[item.component_code] = item.basis
    return await resolve_manual(session, user, structure, ctc, manual, remarks, bases)


def materialise(salary: EmployeeSalary, resolved: ResolvedSalary, tenant_id: uuid.UUID) -> None:
    """Write the resolved amounts onto a salary: the payroll engine's input."""
    for component in resolved.breakdown.components:
        salary.components.append(
            EmployeeSalaryComponent(
                tenant_id=tenant_id,
                component_code=component.code,
                component_name=component.name,
                component_type=component.component_type,
                annual_amount=component.annual_amount,
                monthly_amount=component.monthly_amount,
                taxable=component.taxable,
                part_of_ctc=component.part_of_ctc,
                pro_rate_on_lop=component.pro_rate_on_lop,
                counts_toward_pf=component.counts_toward_pf,
                counts_toward_esi=component.counts_toward_esi,
                display_order=component.display_order,
                is_manual=component.is_manual,
                remarks=resolved.remarks.get(component.code),
                pay_basis=resolved.bases.get(component.code, "monthly"),
            )
        )


def type_totals(
    rows: list[tuple[ComponentType, Decimal, Decimal]],
) -> list[SalaryTypeTotalRead]:
    """Monthly and annual totals per kind of component, from (type, monthly, annual)."""
    sums: dict[ComponentType, tuple[Decimal, Decimal]] = {}
    for kind, monthly, annual in rows:
        current = sums.get(kind, (Decimal(0), Decimal(0)))
        sums[kind] = (current[0] + Decimal(str(monthly)), current[1] + Decimal(str(annual)))
    return [
        SalaryTypeTotalRead(
            component_type=kind, monthly=_money(sums[kind][0]), annual=_money(sums[kind][1])
        )
        for kind in TYPE_ORDER
        if kind in sums
    ]


async def wage_group_map(session: AsyncSession, user: User) -> dict[str, WageGroup]:
    """Every tenant wage type's group, by code."""
    rows = await session.execute(
        tenant_scoped(
            select(
                SalaryComponent.code, SalaryComponent.component_type, SalaryComponent.wage_group
            ),
            SalaryComponent,
            user,
        )
    )
    return {code: wage_group_of(kind, group) for code, kind, group in rows.all()}


def group_of(
    groups: dict[str, WageGroup], code: str, component_type: ComponentType | str
) -> WageGroup:
    """A line's group: its wage type's, or its kind's usual one if the type is gone."""
    return groups.get(code) or DEFAULT_WAGE_GROUP[ComponentType(component_type)]


def group_totals(rows: list[tuple[WageGroup, Decimal, Decimal]]) -> list[SalaryGroupTotalRead]:
    """Monthly and annual totals per wage group, from (group, monthly, annual)."""
    sums: dict[WageGroup, tuple[Decimal, Decimal]] = {}
    for group, monthly, annual in rows:
        current = sums.get(group, (Decimal(0), Decimal(0)))
        sums[group] = (current[0] + Decimal(str(monthly)), current[1] + Decimal(str(annual)))
    return [
        SalaryGroupTotalRead(
            wage_group=group, monthly=_money(sums[group][0]), annual=_money(sums[group][1])
        )
        for group in WageGroup
        if group in sums
    ]


def manual_summary(resolved: ResolvedSalary) -> dict[str, str]:
    """Typed amounts, for an audit entry: code to annual amount."""
    return {
        component.code: str(_money(component.annual_amount))
        for component in resolved.breakdown.components
        if component.is_manual
    }


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

    resolved = await resolve_amounts(session, user, structure, payload)
    ctc = resolved.ctc_annual

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
        ctc_annual=ctc,
        effective_from=payload.effective_from,
        revision_reason=payload.revision_reason,
        approved_by_user_id=user.id,
    )
    materialise(salary, resolved, user.tenant_id)

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
            "new_ctc": str(_money(ctc)),
            "structure": structure.name,
            "effective_from": payload.effective_from.isoformat(),
            "reason": payload.revision_reason,
            "typed_amounts": manual_summary(resolved),
        },
        summary=(
            f"Salary revised for {employee.employee_code}: {_money(previous_ctc)} to {_money(ctc)}"
            if previous_ctc is not None
            else f"Salary assigned to {employee.employee_code}: {_money(ctc)}"
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


def salary_record(salary: EmployeeSalary) -> SalaryRecordRead:
    """One row of a salary series, with its structure's name."""
    return SalaryRecordRead(
        id=salary.id,
        employee_id=salary.employee_id,
        structure_id=salary.structure_id,
        structure_name=salary.structure.name if salary.structure else None,
        ctc_annual=Decimal(str(salary.ctc_annual)),
        currency=salary.currency,
        effective_from=salary.effective_from,
        effective_to=salary.effective_to,
        revision_reason=salary.revision_reason,
        components=[
            EmployeeSalaryComponentRead.model_validate(component)
            for component in sorted(salary.components, key=lambda c: c.display_order)
        ],
    )


async def salary_overview(
    session: AsyncSession,
    user: User,
    *,
    search: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[SalaryOverviewRead], int]:
    """Employees and the salary each is on today — the Salaries screen's list."""
    stmt = tenant_scoped(select(Employee).where(Employee.deleted_at.is_(None)), Employee, user)
    count_stmt = tenant_scoped(
        select(func.count(Employee.id)).where(Employee.deleted_at.is_(None)), Employee, user
    )
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        condition = or_(
            Employee.first_name.ilike(pattern),
            # A full name ("Meera Nair") spans both columns.
            func.concat_ws(" ", Employee.first_name, Employee.last_name).ilike(pattern),
            Employee.last_name.ilike(pattern),
            Employee.employee_code.ilike(pattern),
        )
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    total = int(await session.scalar(count_stmt) or 0)
    employees = (
        await session.scalars(
            stmt.order_by(Employee.employee_code).offset((page - 1) * page_size).limit(page_size)
        )
    ).all()

    current: dict[uuid.UUID, EmployeeSalary] = {}
    if employees:
        salaries = (
            await session.scalars(
                tenant_scoped(
                    select(EmployeeSalary).where(
                        EmployeeSalary.employee_id.in_([e.id for e in employees]),
                        EmployeeSalary.effective_to.is_(None),
                    ),
                    EmployeeSalary,
                    user,
                )
            )
        ).all()
        for salary in salaries:
            known = current.get(salary.employee_id)
            if known is None or salary.effective_from > known.effective_from:
                current[salary.employee_id] = salary

    return [
        SalaryOverviewRead(
            employee_id=employee.id,
            employee_code=employee.employee_code,
            employee_name=employee.full_name,
            designation=employee.designation,
            status=employee.status.value,
            work_email=employee.work_email,
            photo_url=employee.photo_url,
            current_salary=(
                salary_record(current[employee.id]) if employee.id in current else None
            ),
        )
        for employee in employees
    ], total


async def structure_detail(
    session: AsyncSession, user: User, structure_id: uuid.UUID
) -> SalaryStructureDetailRead:
    structure = await get_structure(session, user, structure_id)
    return SalaryStructureDetailRead(
        id=structure.id,
        name=structure.name,
        description=structure.description,
        effective_from=structure.effective_from,
        effective_to=structure.effective_to,
        applicable_grades=list(structure.applicable_grades or []),
        applicable_locations=list(structure.applicable_locations or []),
        rounding_mode=structure.rounding_mode.value,
        rounding_precision=structure.rounding_precision,
        active=structure.active,
        components=[
            StructureComponentDetailRead(
                component_code=item.component.code,
                component_name=item.component.name,
                component_type=item.component.component_type,
                calculation_type=item.component.calculation_type,
                value=(
                    Decimal(str(item.component.value)) if item.component.value is not None else None
                ),
                base_component_code=item.component.base_component_code,
                formula=item.component.formula,
                override_value=(
                    Decimal(str(item.override_value)) if item.override_value is not None else None
                ),
                override_formula=item.override_formula,
                override_base_component_code=item.override_base_component_code,
                counts_toward_pf=item.component.counts_toward_pf,
                counts_toward_esi=item.component.counts_toward_esi,
                part_of_ctc=item.component.part_of_ctc,
                display_order=item.display_order,
            )
            for item in sorted(structure.components, key=lambda c: c.display_order)
        ],
    )


async def paid_months_since(
    session: AsyncSession, user: User, employee_id: uuid.UUID, since: date
) -> list[str]:
    """Months from `since` that an approved run already paid this employee, as "2026-08".

    A revision dated into them changes nothing about what those runs paid: the
    difference is an arrear, raised separately.
    """
    rows = (
        await session.execute(
            tenant_scoped(
                select(PayrollRun.period_year, PayrollRun.period_month)
                .join(Payslip, Payslip.run_id == PayrollRun.id)
                .where(
                    Payslip.employee_id == employee_id,
                    Payslip.is_supplementary.is_(False),
                    PayrollRun.status.in_([PayrollRunStatus.APPROVED, PayrollRunStatus.PAID]),
                    PayrollRun.effective_date >= since,
                )
                .distinct()
                .order_by(PayrollRun.period_year, PayrollRun.period_month),
                PayrollRun,
                user,
            )
        )
    ).all()
    return [f"{year}-{month:02d}" for year, month in rows]


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
