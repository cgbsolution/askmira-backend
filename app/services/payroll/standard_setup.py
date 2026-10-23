"""From nothing to a working salary setup in one step: the usual Indian wage types and a structure.

Offered on the Salary master when a workspace has no salary structure yet. It only
adds: a wage type whose code the workspace already has is left exactly as it is,
and the structure is created only when no active structure exists. The rules are
stated on the button that runs this, and all of it can be changed afterwards in
Payroll setup.

The structure follows the split most Indian CTC letters use: Basic is 50% of
CTC, HRA 40% of Basic, gratuity 4.81% of Basic (15/26 of a month's basic per
year of service, provided for monthly), and Special allowance takes whatever is
left. Every other wage type starts at nothing and is typed per person.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.payroll import (
    CalculationType,
    ComponentType,
    RoundingMode,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
    WageGroup,
)
from app.models.user import User
from app.schemas.payroll import StandardSetupRead
from app.services import audit
from app.services.payroll.salary_structure import SalaryStructureError, validate_structure

STRUCTURE_NAME = "Standard CTC"
#: Early enough that any past revision (a joiner from years back) can use it.
STRUCTURE_FROM = date(2000, 1, 1)


@dataclass(frozen=True)
class _Spec:
    code: str
    name: str
    kind: ComponentType
    group: WageGroup
    calculation: CalculationType = CalculationType.FIXED
    base: str | None = None
    value: str | None = "0"
    formula: str | None = None
    taxable: bool = True
    part_of_ctc: bool = True
    pro_rate: bool = True
    pf: bool = False
    esi: bool = False
    order: int = 0
    in_structure: bool = False


E, R, D, C = (
    ComponentType.EARNING,
    ComponentType.REIMBURSEMENT,
    ComponentType.DEDUCTION,
    ComponentType.EMPLOYER_CONTRIBUTION,
)

STANDARD: tuple[_Spec, ...] = (
    _Spec("BASIC", "Basic salary", E, WageGroup.REGULAR_EARNING, CalculationType.PERCENTAGE_OF,
          base="CTC", value="50", pf=True, esi=True, order=10, in_structure=True),
    _Spec("HRA", "House rent allowance", E, WageGroup.REGULAR_EARNING,
          CalculationType.PERCENTAGE_OF, base="BASIC", value="40", esi=True, order=20,
          in_structure=True),
    _Spec("CONVEYANCE", "Conveyance allowance", E, WageGroup.REGULAR_EARNING, esi=True, order=30),
    _Spec("EDUCATION", "Education allowance", E, WageGroup.REGULAR_EARNING, esi=True, order=40),
    _Spec("TELEPHONE", "Telephone allowance", E, WageGroup.REGULAR_EARNING, esi=True, order=50),
    _Spec("UNIFORM", "Uniform allowance", E, WageGroup.REGULAR_EARNING, esi=True, order=60),
    _Spec("SPECIAL", "Special allowance", E, WageGroup.REGULAR_EARNING, CalculationType.BALANCE,
          value=None, esi=True, order=90, in_structure=True),
    _Spec("BONUS", "Bonus", E, WageGroup.VARIABLE_EARNING, pro_rate=False, order=110),
    _Spec("LEAVE_ENCASH", "Leave encashment", E, WageGroup.VARIABLE_EARNING, pro_rate=False,
          order=120),
    _Spec("FUEL", "Fuel reimbursement", R, WageGroup.REIMBURSEMENT, taxable=False,
          part_of_ctc=False, pro_rate=False, order=210),
    _Spec("MEDICAL", "Medical reimbursement", R, WageGroup.REIMBURSEMENT, taxable=False,
          part_of_ctc=False, pro_rate=False, order=220),
    _Spec("INSURANCE", "Insurance", D, WageGroup.NON_STATUTORY_DEDUCTION, part_of_ctc=False,
          pro_rate=False, order=410),
    _Spec("GRATUITY", "Gratuity", C, WageGroup.PROVISION, CalculationType.FORMULA, value=None,
          formula="BASIC * 0.0481", taxable=False, pro_rate=False, order=610, in_structure=True),
    _Spec("BONUS_PROVISION", "Bonus provision", C, WageGroup.PROVISION, taxable=False,
          pro_rate=False, order=620),
)  # fmt: skip


async def install_standard(
    session: AsyncSession, user: User, request: Request | None = None
) -> StandardSetupRead:
    """Add the standard wage types a workspace lacks, and the structure if it has none."""
    existing = {
        component.code: component
        for component in (
            await session.scalars(tenant_scoped(select(SalaryComponent), SalaryComponent, user))
        ).all()
    }
    added: list[SalaryComponent] = []
    for spec in STANDARD:
        if spec.code in existing:
            continue
        component = SalaryComponent(
            tenant_id=user.tenant_id,
            code=spec.code,
            name=spec.name,
            component_type=spec.kind,
            calculation_type=spec.calculation,
            base_component_code=spec.base,
            value=Decimal(spec.value) if spec.value is not None else None,
            formula=spec.formula,
            taxable=spec.taxable,
            part_of_ctc=spec.part_of_ctc,
            pro_rate_on_lop=spec.pro_rate,
            counts_toward_pf=spec.pf,
            counts_toward_esi=spec.esi,
            display_order=spec.order,
            wage_group=spec.group.value,
        )
        session.add(component)
        existing[spec.code] = component
        added.append(component)
    await session.flush()
    for component in added:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.CREATE,
            entity_type="salary_component",
            entity_id=component.id,
            actor=user,
            changes={"code": component.code, "name": component.name, "standard_setup": True},
            summary=f"Created salary component {component.code} (standard setup)",
            request=request,
        )

    structure_name: str | None = None
    skipped: str | None = None
    has_structure = await session.scalar(
        tenant_scoped(
            select(SalaryStructure.id).where(SalaryStructure.active.is_(True)).limit(1),
            SalaryStructure,
            user,
        )
    )
    if has_structure is None:
        structure = SalaryStructure(
            tenant_id=user.tenant_id,
            name=STRUCTURE_NAME,
            description=(
                "Basic 50% of CTC, HRA 40% of Basic, gratuity 4.81% of Basic, "
                "Special allowance the balance."
            ),
            effective_from=STRUCTURE_FROM,
            applicable_grades=[],
            applicable_locations=[],
            rounding_mode=RoundingMode.HALF_UP,
            rounding_precision=0,
            active=True,
        )
        for index, spec in enumerate(s for s in STANDARD if s.in_structure):
            component = existing[spec.code]
            if component.calculation_type is not spec.calculation:
                # The workspace already has this code worked out another way. The
                # structure would not be the split the button promised, so leave
                # it to Payroll setup rather than bend someone's own definition.
                skipped = (
                    f"{component.code} is already set up as "
                    f"{component.calculation_type.value.replace('_', ' ')}, so the standard "
                    "structure was not created. Build one under Payroll setup → Salary structures."
                )
                break
            # The structure states its own rule, whatever the component's default is.
            structure.components.append(
                SalaryStructureComponent(
                    tenant_id=user.tenant_id,
                    component_id=component.id,
                    component=component,
                    override_value=Decimal(spec.value) if spec.base else None,
                    override_base_component_code=spec.base,
                    override_formula=spec.formula,
                    display_order=index,
                )
            )
        if skipped is None:
            try:
                validate_structure(structure)
            except SalaryStructureError as exc:
                skipped = f"The standard structure was not created: {exc}"
        if skipped is not None:
            structure = None
        if structure is not None:
            session.add(structure)
            await session.flush()
            await audit.record(
                session,
                tenant_id=user.tenant_id,
                action=AuditAction.CREATE,
                entity_type="salary_structure",
                entity_id=structure.id,
                actor=user,
                changes={
                    "name": structure.name,
                    "components": [s.code for s in STANDARD if s.in_structure],
                    "standard_setup": True,
                },
                summary=f"Created salary structure {structure.name} (standard setup)",
                request=request,
            )
            structure_name = structure.name

    await session.commit()
    return StandardSetupRead(
        components_added=[component.code for component in added],
        structure_name=structure_name,
        structure_skipped=skipped,
    )
