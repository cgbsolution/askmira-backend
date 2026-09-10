"""Builders for salary structures, usable without a database.

The resolution engine takes ORM instances but never touches a session, so these
construct unsaved objects and the whole engine test suite runs in-process.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.models.payroll import (
    CalculationType,
    ComponentType,
    EmployeeSalary,
    EmployeeSalaryComponent,
    PayrollRuleSet,
    RoundingMode,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
)


def component(
    code: str,
    *,
    name: str | None = None,
    component_type: ComponentType = ComponentType.EARNING,
    calculation_type: CalculationType = CalculationType.FIXED,
    value: str | None = None,
    base: str | None = None,
    formula: str | None = None,
    part_of_ctc: bool = True,
    pro_rate_on_lop: bool = True,
    counts_toward_pf: bool = False,
    counts_toward_esi: bool = False,
    taxable: bool = True,
    display_order: int = 0,
) -> SalaryComponent:
    return SalaryComponent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        code=code,
        name=name or code.title(),
        component_type=component_type,
        calculation_type=calculation_type,
        value=Decimal(value) if value is not None else None,
        base_component_code=base,
        formula=formula,
        taxable=taxable,
        part_of_ctc=part_of_ctc,
        pro_rate_on_lop=pro_rate_on_lop,
        counts_toward_pf=counts_toward_pf,
        counts_toward_esi=counts_toward_esi,
        display_order=display_order,
        is_system=False,
        active=True,
    )


def structure(
    *components: SalaryComponent,
    name: str = "Test structure",
    rounding_mode: RoundingMode = RoundingMode.HALF_UP,
    rounding_precision: int = 0,
    overrides: dict[str, dict] | None = None,
) -> SalaryStructure:
    from datetime import date

    overrides = overrides or {}
    built = SalaryStructure(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name=name,
        effective_from=date(2026, 4, 1),
        applicable_grades=[],
        applicable_locations=[],
        rounding_mode=rounding_mode,
        rounding_precision=rounding_precision,
        active=True,
    )
    for index, item in enumerate(components):
        override = overrides.get(item.code, {})
        built.components.append(
            SalaryStructureComponent(
                id=uuid.uuid4(),
                tenant_id=built.tenant_id,
                structure_id=built.id,
                component_id=item.id,
                component=item,
                override_value=override.get("value"),
                override_formula=override.get("formula"),
                override_base_component_code=override.get("base"),
                display_order=item.display_order or index,
            )
        )
    return built


def employee_salary_from(breakdown, ctc: Decimal) -> EmployeeSalary:
    """Materialise a breakdown the way `assign_salary` does, without a session."""
    tenant_id = uuid.uuid4()
    salary = EmployeeSalary(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        employee_id=uuid.uuid4(),
        structure_id=uuid.uuid4(),
        ctc_annual=ctc,
        currency="INR",
        effective_from=__import__("datetime").date(2026, 4, 1),
    )
    for resolved in breakdown.components:
        salary.components.append(
            EmployeeSalaryComponent(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
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
    return salary


def rule_set(
    rule_type: str, rules: dict, *, jurisdiction: str = "IN", approved: bool = True
) -> PayrollRuleSet:
    """A rule set as the Compliance SME would supply it.

    The values here are illustrative test fixtures, not an authority on Indian
    statutory rates — production values come from the SME.
    """
    from datetime import UTC, date, datetime

    return PayrollRuleSet(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        rule_type=rule_type,
        jurisdiction=jurisdiction,
        version=1,
        effective_from=date(2026, 4, 1),
        rules=rules,
        approved_at=datetime.now(UTC) if approved else None,
    )


@pytest.fixture
def pf_rules() -> PayrollRuleSet:
    return rule_set(
        "pf",
        {
            "wage_ceiling": 15000,
            "employee_rate": 12,
            "employer_rate": 12,
            "apply_wage_ceiling": True,
        },
    )


@pytest.fixture
def esi_rules() -> PayrollRuleSet:
    return rule_set(
        "esi",
        {"wage_threshold": 21000, "employee_rate": "0.75", "employer_rate": "3.25"},
    )


@pytest.fixture
def pt_rules() -> PayrollRuleSet:
    return rule_set(
        "professional_tax",
        {
            "slabs": [
                {"up_to": 15000, "amount": 0},
                {"up_to": 20000, "amount": 150},
                {"up_to": None, "amount": 200},
            ]
        },
        jurisdiction="KA",
    )
