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
    STATUTORY_OVERRIDE_CODES,
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
    ResolvedComponent,
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


def rupees(amount: Decimal) -> str:
    """₹6,20,000.04: grouped the Indian way, paise only when there are any."""
    whole, _, paise = f"{abs(amount):.2f}".partition(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    text = ",".join([*groups, tail]) if groups else tail
    sign = "-" if amount < 0 else ""
    return f"{sign}₹{text}" + ("" if paise == "00" else f".{paise}")


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
    return structure, await resolve_amounts(session, user, structure, payload, strict=False)


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
    #: MONTHLY GROSS as typed: (amount, basis), or None for the earnings' sum.
    gross: tuple[Decimal, str] | None = None
    #: Rules a preview found broken; a save refuses them instead.
    warnings: list[str] = field(default_factory=list)

    @property
    def earnings_monthly(self) -> Decimal:
        return _money(
            sum(
                (
                    c.monthly_amount
                    for c in self.breakdown.components
                    if c.component_type is ComponentType.EARNING
                ),
                Decimal(0),
            )
        )

    @property
    def earnings_annual(self) -> Decimal:
        return _money(
            sum(
                (
                    c.annual_amount
                    for c in self.breakdown.components
                    if c.component_type is ComponentType.EARNING
                ),
                Decimal(0),
            )
        )

    def gross_figures(self) -> tuple[Decimal, Decimal]:
        """(monthly, annual) MONTHLY GROSS: as typed, else the earnings' sum."""
        if self.gross is not None:
            annual, monthly = to_period_amounts(*self.gross)
            return monthly, annual
        return self.earnings_monthly, self.earnings_annual


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


#: How a figure in another Pay Basis becomes a month's and a year's: 52 weeks a
#: year, 26 working days a month, 8 working hours a day.
WEEKS_A_YEAR = Decimal(52)
DAYS_A_MONTH = Decimal(26)
HOURS_A_DAY = Decimal(8)


def to_period_amounts(amount: Decimal, basis: str) -> tuple[Decimal, Decimal]:
    """(annual, monthly) for an amount in a Pay Basis, to the paisa."""
    if basis == "monthly":
        annual, monthly = amount * 12, amount
    elif basis == "annual":
        annual, monthly = amount, amount / 12
    elif basis == "weekly":
        annual = amount * WEEKS_A_YEAR
        monthly = annual / 12
    elif basis == "daily":
        monthly = amount * DAYS_A_MONTH
        annual = monthly * 12
    elif basis == "hourly":
        monthly = amount * HOURS_A_DAY * DAYS_A_MONTH
        annual = monthly * 12
    else:  # pragma: no cover - the schema allows no other value
        raise _unprocessable(f"'{basis}' is not a pay basis")
    return _money(annual), _money(monthly)


async def resolve_typed(
    session: AsyncSession,
    user: User,
    ctc_annual: Decimal,
    payload: SalaryAmountsInput,
    *,
    strict: bool = True,
    components: dict[str, SalaryComponent] | None = None,
) -> ResolvedSalary:
    """A manual entry: every amount exactly as typed, nothing worked out.

    `strict=False` (the live preview) reports a rule broken as a warning and
    still works every figure out, so the screen keeps showing them; saving is
    always strict.

    A wage type left empty is not paid. Zero is not paid either, except on a
    statutory MANUAL row, where 0 means "deduct nothing" rather than "work it
    out". The CTC is kept as typed; the wage types need not add up to it (the
    CTC also holds employer PF, ESI and admin charges, which payroll adds).
    """
    items = [item for item in payload.components if item.amount is not None]
    codes = {item.component_code for item in items}
    found: dict[str, SalaryComponent] = {}
    if codes and components is not None:
        # The workspace's wage types, loaded once by a caller resolving many rows.
        found = {code: components[code] for code in codes if code in components}
    elif codes:
        found = {
            component.code: component
            for component in (
                await session.scalars(
                    tenant_scoped(
                        select(SalaryComponent).where(SalaryComponent.code.in_(codes)),
                        SalaryComponent,
                        user,
                    )
                )
            ).all()
        }
    missing = sorted(codes - set(found))
    if missing:
        raise _unprocessable(f"Unknown wage type(s): {', '.join(missing)}")
    inactive = sorted(found[code].name for code in codes if not found[code].active)
    if inactive:
        raise _unprocessable(f"{', '.join(inactive)} is switched off in payroll setup")

    components: list[ResolvedComponent] = []
    remarks: dict[str, str] = {}
    bases: dict[str, str] = {}
    for item in items:
        assert item.amount is not None
        if item.amount == 0 and item.component_code not in STATUTORY_OVERRIDE_CODES:
            continue
        component = found[item.component_code]
        annual, monthly = to_period_amounts(item.amount, item.basis)
        components.append(
            ResolvedComponent(
                code=component.code,
                name=component.name,
                component_type=component.component_type,
                annual_amount=annual,
                monthly_amount=monthly,
                taxable=component.taxable,
                part_of_ctc=component.part_of_ctc,
                pro_rate_on_lop=component.pro_rate_on_lop,
                counts_toward_pf=component.counts_toward_pf,
                counts_toward_esi=component.counts_toward_esi,
                display_order=component.display_order,
                is_manual=True,
            )
        )
        bases[component.code] = item.basis
        if item.remarks:
            remarks[component.code] = item.remarks
    components.sort(key=lambda c: (c.display_order, c.code))
    ctc = _money(ctc_annual)
    in_ctc = _money(sum((c.annual_amount for c in components if c.part_of_ctc), Decimal(0)))
    warnings: list[str] = []
    if in_ctc > ctc:
        message = (
            f"The amounts that are part of the CTC add up to {rupees(in_ctc)} a year, more "
            f"than CTC TOTAL ({rupees(ctc)} a year). Lower an amount or raise CTC TOTAL."
        )
        if strict:
            raise _unprocessable(message)
        warnings.append(message)
    gross = (
        (payload.gross_amount, payload.gross_basis) if payload.gross_amount is not None else None
    )
    return ResolvedSalary(
        ctc_annual=ctc,
        breakdown=SalaryBreakdown(ctc_annual=ctc, components=components),
        remarks=remarks,
        bases=bases,
        gross=gross,
        warnings=warnings,
    )


async def resolve_amounts(
    session: AsyncSession,
    user: User,
    structure: SalaryStructure,
    payload: SalaryAmountsInput,
    *,
    strict: bool = True,
    components: dict[str, SalaryComponent] | None = None,
) -> ResolvedSalary:
    """Resolve what the Salary master sent: a CTC, typed amounts and remarks.

    `components`, the workspace's wage types by code, saves a query per call
    for a caller resolving hundreds of rows (the bulk upload).

    Figures in any Pay Basis become monthly and yearly ones here, on the
    server, so a browser never multiplies money.
    """
    manual_entry = payload.entry == "manual"
    negative = sorted(
        item.component_code
        for item in payload.components
        if item.amount is not None and item.amount < 0
    )
    flagged: list[str] = []
    if negative:
        message = f"{', '.join(negative)}: an amount cannot be negative."
        if strict:
            raise _unprocessable(message)
        flagged.append(message)
    if payload.ctc_annual is not None:
        ctc = payload.ctc_annual
    else:
        monthly_ctc = payload.ctc_monthly
        basis = "monthly" if monthly_ctc is not None else payload.ctc_basis
        amount = monthly_ctc if monthly_ctc is not None else payload.ctc_amount
        assert amount is not None  # the schema requires one way of giving the CTC
        ctc, _ = to_period_amounts(amount, basis)
        if not manual_entry:
            ctc = round_to_structure(structure, ctc)
    if manual_entry:
        resolved = await resolve_typed(
            session, user, ctc, payload, strict=strict, components=components
        )
        resolved.warnings[:0] = flagged
        return resolved

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
        manual[item.component_code], _ = to_period_amounts(item.amount, item.basis)
        bases[item.component_code] = item.basis
    resolved = await resolve_manual(session, user, structure, ctc, manual, remarks, bases)
    resolved.warnings[:0] = flagged
    return resolved


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
    strict: bool = True,
) -> EmployeeSalary:
    """Assign or revise an employee's salary, effective-dated.

    Materialises the resolved component amounts onto `EmployeeSalaryComponent`
    rows. Those rows are the payroll engine's input and are never rewritten, so
    a later edit to the structure leaves this assignment untouched.

    `strict=False` only for a record of what somebody who has left was paid,
    kept as the old system had it (the Salary master bulk upload): the rules
    that guard future pay are then not applied.
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

    resolved = await resolve_amounts(session, user, structure, payload, strict=strict)

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

    salary = _new_salary(user, employee_id, structure, payload, resolved)
    session.add(salary)
    await session.flush()

    await _salary_audit(
        session, user, employee, salary, structure, payload, resolved, previous_ctc,
        request=request,
    )
    # `commit=False` for a caller that owns the transaction — a lifecycle
    # cascade runs inside a savepoint, and committing here would close it out
    # from under the caller, taking the atomicity guarantee with it.
    if commit:
        await session.commit()
        await session.refresh(salary)
    return salary


def _new_salary(
    user: User,
    employee_id: uuid.UUID,
    structure: SalaryStructure,
    payload: EmployeeSalaryCreate,
    resolved: ResolvedSalary,
) -> EmployeeSalary:
    """A salary revision with its resolved amounts on it, not yet added."""
    salary = EmployeeSalary(
        id=uuid.uuid4(),
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        structure_id=structure.id,
        ctc_annual=resolved.ctc_annual,
        effective_from=payload.effective_from,
        revision_reason=payload.revision_reason,
        approved_by_user_id=user.id,
        gross_amount=resolved.gross[0] if resolved.gross else None,
        gross_basis=resolved.gross[1] if resolved.gross else None,
    )
    materialise(salary, resolved, user.tenant_id)
    return salary


async def _salary_audit(
    session: AsyncSession,
    user: User,
    employee: Employee,
    salary: EmployeeSalary,
    structure: SalaryStructure,
    payload: EmployeeSalaryCreate,
    resolved: ResolvedSalary,
    previous_ctc: Decimal | None,
    *,
    request: Request | None,
    flush: bool = True,
) -> None:
    ctc = resolved.ctc_annual
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE if previous_ctc is not None else AuditAction.CREATE,
        entity_type="employee_salary",
        entity_id=salary.id,
        actor=user,
        changes={
            "employee_id": str(employee.id),
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
        flush=flush,
    )


async def assign_salaries(
    session: AsyncSession,
    user: User,
    items: list[tuple[Employee, EmployeeSalaryCreate, bool]],
    *,
    request: Request | None = None,
) -> list[EmployeeSalary]:
    """`assign_salary` for many people at once — the Salary master bulk upload.

    The same checks, revisions and audit entries, row for row, in the caller's
    transaction (no commit). What a row needs is loaded once for all of them
    (the structures, the wage types, everybody's revision in force) and the
    rows are written in one flush: hundreds of salaries cost a handful of
    round trips to the database instead of ten each. `items` are (employee,
    payload, strict); a later row for the same person follows the earlier one.
    """
    if not items:
        return []
    components = {
        c.code: c
        for c in (
            await session.scalars(tenant_scoped(select(SalaryComponent), SalaryComponent, user))
        ).all()
    }
    structures: dict[uuid.UUID, SalaryStructure] = {}
    for structure_id in {payload.structure_id for _, payload, _ in items}:
        structures[structure_id] = await get_structure(session, user, structure_id)
    current: dict[uuid.UUID, EmployeeSalary] = {}
    for row in (
        await session.scalars(
            tenant_scoped(
                select(EmployeeSalary)
                .where(
                    EmployeeSalary.employee_id.in_({employee.id for employee, _, _ in items}),
                    EmployeeSalary.effective_to.is_(None),
                )
                .order_by(EmployeeSalary.effective_from),
                EmployeeSalary,
                user,
            )
        )
    ).all():
        current[row.employee_id] = row  # the latest open one wins, as in assign_salary

    saved: list[EmployeeSalary] = []
    with session.no_autoflush:
        for employee, payload, strict in items:
            structure = structures[payload.structure_id]
            if payload.effective_from < structure.effective_from:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Salary is effective {payload.effective_from} but the structure "
                        f"'{structure.name}' only starts on {structure.effective_from}"
                    ),
                )
            resolved = await resolve_amounts(
                session, user, structure, payload, strict=strict, components=components
            )
            before = current.get(employee.id)
            previous_ctc: Decimal | None = None
            if before is not None:
                if before.effective_from >= payload.effective_from:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"{employee.employee_code} already has a salary effective "
                            f"{before.effective_from}. A revision must start after it — "
                            "backdated corrections are an arrears run, not an edit."
                        ),
                    )
                previous_ctc = Decimal(str(before.ctc_annual))
                before.effective_to = payload.effective_from - timedelta(days=1)
            salary = _new_salary(user, employee.id, structure, payload, resolved)
            session.add(salary)
            await _salary_audit(
                session, user, employee, salary, structure, payload, resolved, previous_ctc,
                request=request, flush=False,
            )
            current[employee.id] = salary
            saved.append(salary)
    await session.flush()
    return saved


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
