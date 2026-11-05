"""Payroll → Salary structures: CTC templates HR builds, and splitting a CTC by one.

A template is a `SalaryStructure` whose lines carry a rule each
(`salary_structure_components.rule`) and whose `template` column holds the
employer contributions kept inside the CTC. The arithmetic is in
`ctc_template.py`; this module loads, checks, saves and audits.

A structure of the earlier kind (built before templates, e.g. the "Standard
CTC" the one-click setup made) is shown translated into rules. Saving it turns
it into a template; until then it keeps working exactly as before.

Splitting a CTC never writes a salary. The Salary master fills its boxes with
the figures, HR reviews them, and the save goes through the same path as a
salary typed by hand.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.offer import Offer
from app.models.payroll import (
    CalculationType,
    ComponentType,
    EmployeeSalary,
    RoundingMode,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
    WageGroup,
    wage_group_of,
)
from app.models.user import User
from app.schemas.payroll import (
    SalaryGroupTotalRead,
    SalaryTemplateDraftRead,
    SalaryTemplateInput,
    SalaryTemplateRead,
    SalaryTemplateSummaryRead,
    TemplateCalcLineRead,
    TemplateCalcRead,
    TemplateEmployerRead,
    TemplateLineInput,
    TemplateLineRead,
    TemplateRuleIO,
)
from app.services import audit
from app.services.payroll import ctc_template as ct
from app.services.payroll.salary_structure import (
    ResolvedComponent,
    SalaryBreakdown,
    SalaryStructureError,
)

#: Early enough that a revision from years back can use a new template.
TEMPLATE_FROM = date(2000, 1, 1)
_PAISE = Decimal("0.01")
_MONTHS = Decimal(12)


def _money(value: Decimal) -> Decimal:
    return value.quantize(_PAISE, rounding=ROUND_HALF_UP)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Salary structure not found")


# ── Defaults ────────────────────────────────────────────────────────────────

#: Employer PF kept inside the CTC: 12% PF + 0.5% admin + 0.5% EDLI on PF wages
#: up to the ₹15,000 ceiling — ₹1,950 a month at the ceiling, which is what the
#: CTC letters HR already issues set aside.
DEFAULT_EMPLOYER_PF: dict[str, Any] = {
    "method": "percent",
    "value": "13",
    "of": ct.PF_WAGES,
    "ceiling": "15000",
}
#: Employer ESI kept inside the CTC: 3.25% of ESI wages, only while they are up
#: to ₹21,000 a month, rounded up to the rupee as ESIC collects it.
DEFAULT_EMPLOYER_ESI: dict[str, Any] = {
    "method": "percent",
    "value": "3.25",
    "of": ct.ESI_WAGES,
    "when": {"on": ct.ESI_WAGES, "op": "up_to", "amount": "21000"},
    "rounding": "up",
}
#: The split most Indian CTC letters use, for the wage types the standard setup makes.
DEFAULT_LINES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("GRATUITY", {"method": "percent", "value": "4.81", "of": "BASIC"}),
    ("BASIC", {"method": "percent", "value": "50", "of": ct.CTC}),
    ("HRA", {"method": "percent", "value": "40", "of": "BASIC"}),
    ("SPECIAL", {"method": "balance"}),
)


# ── Loading ─────────────────────────────────────────────────────────────────


async def _components(session: AsyncSession, user: User) -> dict[str, SalaryComponent]:
    rows = await session.scalars(tenant_scoped(select(SalaryComponent), SalaryComponent, user))
    return {component.code: component for component in rows.all()}


async def _structure(session: AsyncSession, user: User, structure_id: uuid.UUID) -> SalaryStructure:
    structure = await session.scalar(
        tenant_scoped(
            select(SalaryStructure).where(SalaryStructure.id == structure_id),
            SalaryStructure,
            user,
        )
    )
    if structure is None:
        raise _not_found()
    return structure


async def _used_by(
    session: AsyncSession, user: User, structure_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """People with a salary revision on each structure."""
    if not structure_ids:
        return {}
    rows = await session.execute(
        tenant_scoped(
            select(
                EmployeeSalary.structure_id,
                func.count(func.distinct(EmployeeSalary.employee_id)),
            )
            .where(EmployeeSalary.structure_id.in_(structure_ids))
            .group_by(EmployeeSalary.structure_id),
            EmployeeSalary,
            user,
        )
    )
    return {structure_id: count for structure_id, count in rows.all()}


# ── Rules from rows, and rows from rules ────────────────────────────────────


def _line(component: SalaryComponent, rule: ct.Rule) -> ct.Line:
    return ct.Line(
        code=component.code,
        name=component.name,
        component_type=ComponentType(component.component_type),
        group=wage_group_of(component.component_type, component.wage_group),
        rule=rule,
        part_of_ctc=component.part_of_ctc,
        counts_toward_pf=component.counts_toward_pf,
        counts_toward_esi=component.counts_toward_esi,
        display_order=component.display_order,
    )


def _employer_rows(raw: Mapping[str, Any] | None) -> tuple[ct.EmployerRow, ...]:
    rows: list[ct.EmployerRow] = []
    stored = (raw or {}).get("employer") or {}
    for key, name in ct.EMPLOYER_ROWS.items():
        rule = stored.get(key)
        if rule:
            rows.append(ct.EmployerRow(key, name, ct.parse_rule(rule, name)))
    return (rows and tuple(rows)) or ()


_PRODUCT = re.compile(
    r"^\s*(?:(?P<code>[A-Z][A-Z0-9_]*)\s*\*\s*(?P<n1>\d+(?:\.\d+)?)"
    r"|(?P<n2>\d+(?:\.\d+)?)\s*\*\s*(?P<code2>[A-Z][A-Z0-9_]*))\s*$"
)


def _legacy_rule(link: SalaryStructureComponent) -> tuple[dict[str, Any] | None, str | None]:
    """A line of a structure of the earlier kind, as a template rule.

    Returns (rule, note). A formula other than "CODE * number" has no rule to
    show; the line is left out and the note says so.
    """
    component = link.component
    calc = component.calculation_type
    value = link.override_value if link.override_value is not None else component.value
    base = link.override_base_component_code or component.base_component_code
    formula = link.override_formula or component.formula
    if calc is CalculationType.BALANCE:
        return {"method": "balance"}, None
    if calc is CalculationType.PERCENTAGE_OF:
        return {"method": "percent", "value": str(Decimal(str(value or 0))), "of": base}, None
    if calc is CalculationType.FIXED:
        # The earlier engine reads a fixed value as a year's amount.
        return {
            "method": "fixed",
            "value": str(_money(Decimal(str(value or 0)))),
            "basis": "annual",
        }, None
    match = _PRODUCT.match(formula or "")
    if match:
        code = match.group("code") or match.group("code2")
        number = Decimal(match.group("n1") or match.group("n2"))
        percent = (number * 100).normalize()
        return {"method": "percent", "value": format(percent, "f"), "of": code}, None
    return None, (
        f"{component.name} is worked out by the formula '{formula}', which has no rule here; "
        "it was left out. Add it back with a rule before saving."
    )


def template_of(
    structure: SalaryStructure, components: Mapping[str, SalaryComponent] | None = None
) -> tuple[ct.Template, list[str]]:
    """The structure as a template, and notes on what could not be translated.

    Raises:
        ct.TemplateError: a stored rule no longer reads (never expected: rules
            are checked before they are saved).
    """
    notes: list[str] = []
    lines: list[ct.Line] = []
    for link in sorted(structure.components, key=lambda link: link.display_order):
        component = (components or {}).get(link.component.code, link.component)
        if structure.template is not None:
            raw = link.rule
            if raw is None:
                notes.append(f"{component.name} has no rule and was left out.")
                continue
        else:
            raw, note = _legacy_rule(link)
            if note:
                notes.append(note)
            if raw is None:
                continue
        lines.append(_line(component, ct.parse_rule(raw, component.name)))
    precision = 0 if structure.rounding_precision == 0 else 2
    return (
        ct.Template(
            lines=tuple(lines),
            employer=_employer_rows(structure.template),
            precision=precision,
        ),
        notes,
    )


def _build(payload: SalaryTemplateInput, components: Mapping[str, SalaryComponent]) -> ct.Template:
    """A template from what the editor sent, checked whole. Raises 422 with the line named."""
    missing = sorted(
        line.component_code for line in payload.lines if line.component_code not in components
    )
    if missing:
        raise _unprocessable(f"Unknown wage type(s): {', '.join(missing)}")
    switched_off = sorted(
        components[line.component_code].name
        for line in payload.lines
        if not components[line.component_code].active
    )
    if switched_off:
        raise _unprocessable(f"{', '.join(switched_off)} is switched off in payroll setup")
    try:
        lines = tuple(
            _line(
                components[item.component_code],
                ct.parse_rule(item.rule.as_stored(), components[item.component_code].name),
            )
            for item in payload.lines
        )
        employer = tuple(
            ct.EmployerRow(
                key, ct.EMPLOYER_ROWS[key], ct.parse_rule(rule.as_stored(), ct.EMPLOYER_ROWS[key])
            )
            for key, rule in (
                (ct.EMPLOYER_PF, payload.employer_pf),
                (ct.EMPLOYER_ESI, payload.employer_esi),
            )
            if rule is not None
        )
        template = ct.Template(lines=lines, employer=employer, precision=payload.rounding_precision)
        ct.validate(template)
    except ct.TemplateError as exc:
        raise _unprocessable(str(exc)) from exc
    return template


def _names(template: ct.Template) -> dict[str, str]:
    return {line.code: line.name for line in template.lines}


def _stored_template(template: ct.Template) -> dict[str, Any]:
    return {
        "version": 1,
        "employer": {
            key: next((ct.rule_json(row.rule) for row in template.employer if row.key == key), None)
            for key in ct.EMPLOYER_ROWS
        },
    }


def _rule_texts(template: ct.Template) -> dict[str, str]:
    """Every rule in words, by code: what the audit trail records."""
    names = _names(template)
    out = {line.code: ct.describe(line.rule, names) for line in template.lines}
    for row in template.employer:
        out[row.key] = ct.describe(row.rule, names)
    return out


# ── Reads ───────────────────────────────────────────────────────────────────


def _read(
    structure: SalaryStructure,
    template: ct.Template,
    notes: list[str],
    components: Mapping[str, SalaryComponent],
    used_by: int,
) -> SalaryTemplateRead:
    names = _names(template)
    employer = {row.key: row for row in template.employer}
    pf, esi = employer.get(ct.EMPLOYER_PF), employer.get(ct.EMPLOYER_ESI)
    return SalaryTemplateRead(
        id=structure.id,
        name=structure.name,
        description=structure.description,
        rounding_precision=template.precision,
        active=structure.active,
        is_template=structure.template is not None,
        lines=[
            TemplateLineRead(
                component_code=line.code,
                component_name=line.name,
                component_type=line.component_type,
                wage_group=line.group,
                part_of_ctc=line.part_of_ctc,
                active=components[line.code].active if line.code in components else False,
                rule=ct.rule_json(line.rule),
                rule_text=ct.describe(line.rule, names),
            )
            for line in template.lines
        ],
        employer_pf=ct.rule_json(pf.rule) if pf else None,
        employer_pf_text=ct.describe(pf.rule, names) if pf else None,
        employer_esi=ct.rule_json(esi.rule) if esi else None,
        employer_esi_text=ct.describe(esi.rule, names) if esi else None,
        notes=notes,
        used_by=used_by,
        updated_at=structure.updated_at,
    )


def _summary(template: ct.Template) -> str:
    names = _names(template)
    parts = [
        f"{line.name} {ct.describe(line.rule, names)}"
        for line in sorted(template.lines, key=lambda line: (line.display_order, line.code))
        if line.part_of_ctc
    ]
    return " · ".join(parts)


async def list_templates(session: AsyncSession, user: User) -> list[SalaryTemplateSummaryRead]:
    structures = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(SalaryStructure).order_by(
                        SalaryStructure.active.desc(), SalaryStructure.name
                    ),
                    SalaryStructure,
                    user,
                )
            )
        ).all()
    )
    components = await _components(session, user)
    used = await _used_by(session, user, [s.id for s in structures])
    out: list[SalaryTemplateSummaryRead] = []
    for structure in structures:
        try:
            template, _ = template_of(structure, components)
            summary = _summary(template)
            line_count = len(template.lines)
            in_ctc = [row.name for row in template.employer]
        except ct.TemplateError as exc:
            summary, line_count, in_ctc = str(exc), len(structure.components), []
        out.append(
            SalaryTemplateSummaryRead(
                id=structure.id,
                name=structure.name,
                description=structure.description,
                active=structure.active,
                is_template=structure.template is not None,
                line_count=line_count,
                summary=summary,
                employer_in_ctc=in_ctc,
                used_by=used.get(structure.id, 0),
                updated_at=structure.updated_at,
            )
        )
    return out


async def get_template(
    session: AsyncSession, user: User, structure_id: uuid.UUID
) -> SalaryTemplateRead:
    structure = await _structure(session, user, structure_id)
    components = await _components(session, user)
    try:
        template, notes = template_of(structure, components)
    except ct.TemplateError as exc:
        raise _unprocessable(str(exc)) from exc
    used = await _used_by(session, user, [structure.id])
    return _read(structure, template, notes, components, used.get(structure.id, 0))


async def new_template_draft(session: AsyncSession, user: User) -> SalaryTemplateDraftRead:
    """What "New structure" starts from: the usual split, on the wage types the workspace has.

    Only a starting point in the editor; nothing is saved until HR saves it.
    """
    components = await _components(session, user)
    present = {code for code, _ in DEFAULT_LINES if code in components and components[code].active}
    lines = [
        TemplateLineInput(component_code=code, rule=TemplateRuleIO.model_validate(rule))
        for code, rule in DEFAULT_LINES
        if code in present
        # A rule of a wage type the workspace lacks would not save.
        and (rule.get("of") in (None, ct.CTC, *ct.AGGREGATES) or rule.get("of") in present)
    ]
    return SalaryTemplateDraftRead(
        name="",
        rounding_precision=0,
        lines=lines,
        employer_pf=TemplateRuleIO.model_validate(DEFAULT_EMPLOYER_PF),
        employer_esi=TemplateRuleIO.model_validate(DEFAULT_EMPLOYER_ESI),
    )


# ── Writes ──────────────────────────────────────────────────────────────────


async def _name_taken(
    session: AsyncSession, user: User, name: str, other_than: uuid.UUID | None = None
) -> bool:
    stmt = select(SalaryStructure.id).where(func.lower(SalaryStructure.name) == name.lower())
    if other_than is not None:
        stmt = stmt.where(SalaryStructure.id != other_than)
    return (await session.scalar(tenant_scoped(stmt, SalaryStructure, user))) is not None


async def create_template(
    session: AsyncSession, user: User, payload: SalaryTemplateInput, request: Request | None = None
) -> SalaryTemplateRead:
    components = await _components(session, user)
    template = _build(payload, components)
    if await _name_taken(session, user, payload.name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A salary structure called '{payload.name}' already exists",
        )
    structure = SalaryStructure(
        tenant_id=user.tenant_id,
        name=payload.name,
        description=payload.description,
        effective_from=TEMPLATE_FROM,
        applicable_grades=[],
        applicable_locations=[],
        rounding_mode=RoundingMode.HALF_UP,
        rounding_precision=payload.rounding_precision,
        active=True,
        template=_stored_template(template),
    )
    for index, line in enumerate(template.lines):
        structure.components.append(
            SalaryStructureComponent(
                tenant_id=user.tenant_id,
                component_id=components[line.code].id,
                component=components[line.code],
                rule=ct.rule_json(line.rule),
                display_order=index,
            )
        )
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
            "template": True,
            "rounding_precision": payload.rounding_precision,
            "rules": _rule_texts(template),
        },
        summary=f"Created salary structure {structure.name}",
        request=request,
    )
    await session.commit()
    await session.refresh(structure)
    return _read(structure, template, [], components, 0)


async def update_template(
    session: AsyncSession,
    user: User,
    structure_id: uuid.UUID,
    payload: SalaryTemplateInput,
    request: Request | None = None,
) -> SalaryTemplateRead:
    """Save the rules. Salaries already saved keep their amounts: they were
    written when they were saved, and a template only fills the boxes."""
    structure = await _structure(session, user, structure_id)
    components = await _components(session, user)
    template = _build(payload, components)
    if await _name_taken(session, user, payload.name, other_than=structure.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A salary structure called '{payload.name}' already exists",
        )
    try:
        before_template, _ = template_of(structure, components)
        before = _rule_texts(before_template)
    except ct.TemplateError:
        before = {}
    was_template = structure.template is not None
    before_name = structure.name

    structure.name = payload.name
    structure.description = payload.description
    structure.rounding_precision = payload.rounding_precision
    structure.rounding_mode = RoundingMode.HALF_UP
    structure.template = _stored_template(template)

    # Update links in place, so the (structure, wage type) unique key never sees
    # a delete and an insert of the same pair in one flush.
    existing = {link.component_id: link for link in structure.components}
    wanted = [components[line.code] for line in template.lines]
    keep_ids = {component.id for component in wanted}
    for link in list(structure.components):
        if link.component_id not in keep_ids:
            structure.components.remove(link)
    for index, (line, component) in enumerate(zip(template.lines, wanted, strict=True)):
        link = existing.get(component.id)
        if link is None:
            link = SalaryStructureComponent(
                tenant_id=user.tenant_id, component_id=component.id, component=component
            )
            structure.components.append(link)
        link.rule = ct.rule_json(line.rule)
        link.display_order = index
        link.override_value = None
        link.override_formula = None
        link.override_base_component_code = None
    await session.flush()

    after = _rule_texts(template)
    changed = {
        code: [before.get(code), after.get(code)]
        for code in sorted(set(before) | set(after))
        if before.get(code) != after.get(code)
    }
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="salary_structure",
        entity_id=structure.id,
        actor=user,
        changes={
            "name": [before_name, structure.name]
            if before_name != structure.name
            else structure.name,
            "rules": changed,
            "rounding_precision": payload.rounding_precision,
            **({"made_template": True} if not was_template else {}),
        },
        summary=f"Updated salary structure {structure.name}",
        request=request,
    )
    await session.commit()
    await session.refresh(structure)
    used = await _used_by(session, user, [structure.id])
    return _read(structure, template, [], components, used.get(structure.id, 0))


async def _another_active(session: AsyncSession, user: User, structure_id: uuid.UUID) -> bool:
    """Whether a structure other than this one is switched on."""
    found = await session.scalar(
        tenant_scoped(
            select(SalaryStructure.id)
            .where(SalaryStructure.active.is_(True), SalaryStructure.id != structure_id)
            .limit(1),
            SalaryStructure,
            user,
        )
    )
    return found is not None


async def set_active(
    session: AsyncSession,
    user: User,
    structure_id: uuid.UUID,
    active: bool,
    request: Request | None = None,
) -> SalaryTemplateRead:
    structure = await _structure(session, user, structure_id)
    if structure.active != active:
        if not active and not await _another_active(session, user, structure.id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"'{structure.name}' is the only salary structure switched on. Add or "
                    "switch on another first: the Salary master needs one."
                ),
            )
        structure.active = active
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="salary_structure",
            entity_id=structure.id,
            actor=user,
            changes={"name": structure.name, "active": active},
            summary=(
                f"{'Switched on' if active else 'Switched off'} salary structure {structure.name}"
            ),
            request=request,
        )
        await session.commit()
        await session.refresh(structure)
    return await get_template(session, user, structure_id)


async def delete_template(
    session: AsyncSession, user: User, structure_id: uuid.UUID, request: Request | None = None
) -> None:
    """Remove a structure nobody's salary or offer uses; otherwise say to switch it off."""
    structure = await _structure(session, user, structure_id)
    used = (await _used_by(session, user, [structure.id])).get(structure.id, 0)
    offers = await session.scalar(
        tenant_scoped(
            select(func.count(Offer.id)).where(Offer.salary_structure_id == structure.id),
            Offer,
            user,
        )
    )
    if used or offers:
        holders = []
        if used:
            holders.append(f"{used} {'person' if used == 1 else 'people'}'s salary")
        if offers:
            holders.append(f"{offers} offer{'s' if offers != 1 else ''}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"'{structure.name}' is used by {' and '.join(holders)}, so it stays on record. "
                "Switch it off instead: it then leaves the Salary master's list."
            ),
        )
    if structure.active and not await _another_active(session, user, structure.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"'{structure.name}' is the only salary structure switched on. Add or switch on "
                "another first: the Salary master needs one."
            ),
        )
    name = structure.name
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="salary_structure",
        entity_id=structure.id,
        actor=user,
        changes={"name": name},
        summary=f"Deleted salary structure {name}",
        request=request,
    )
    await session.delete(structure)
    await session.commit()


# ── Splitting a CTC ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CtcInput:
    """A CTC as typed: an amount in a Pay Basis."""

    amount: Decimal
    basis: str

    def monthly(self) -> Decimal:
        """The month's CTC, exact: a year's is divided, never rounded first."""
        if self.basis == "annual":
            return self.amount / _MONTHS
        if self.basis == "monthly":
            return self.amount
        # Weekly, daily and hourly figures become a month's the Salary master way.
        from app.services.payroll.service import to_period_amounts

        return to_period_amounts(self.amount, self.basis)[1]

    def annual(self) -> Decimal:
        if self.basis == "annual":
            return self.amount
        return _money(self.monthly() * _MONTHS)

    @property
    def fill_basis(self) -> str:
        """The Pay Basis the Salary master's boxes are filled in."""
        return "annual" if self.basis == "annual" else "monthly"


def run(
    template: ct.Template, ctc: CtcInput, pinned: Mapping[str, Decimal] | None = None
) -> ct.Result:
    try:
        return ct.calculate(template, ctc.monthly(), pinned=pinned)
    except ct.TemplateError as exc:
        raise _unprocessable(str(exc)) from exc


def calc_read(
    structure_id: uuid.UUID | None,
    name: str,
    template: ct.Template,
    result: ct.Result,
    ctc: CtcInput,
    notes: list[str],
) -> TemplateCalcRead:
    names = _names(template)
    lines = {line.code: line for line in template.lines}
    employer_rules = {row.key: row for row in template.employer}
    out_lines: list[TemplateCalcLineRead] = []
    groups: dict[WageGroup, tuple[Decimal, Decimal]] = {}
    for item in result.lines:
        line = lines[item.code]
        monthly = _money(item.monthly)
        annual = _money(item.monthly * _MONTHS)
        out_lines.append(
            TemplateCalcLineRead(
                component_code=line.code,
                component_name=line.name,
                component_type=line.component_type,
                wage_group=line.group,
                part_of_ctc=line.part_of_ctc,
                rule_text=ct.describe(line.rule, names),
                applied=item.applied,
                note=item.note,
                monthly=monthly,
                annual=annual,
                amount=annual if ctc.fill_basis == "annual" else monthly,
                basis=ctc.fill_basis,  # type: ignore[arg-type]
            )
        )
        if monthly:
            current = groups.get(line.group, (Decimal(0), Decimal(0)))
            groups[line.group] = (current[0] + monthly, current[1] + annual)
    employer_monthly = sum((item.monthly for item in result.employer), Decimal(0))
    ctc_annual = ctc.annual()
    unallocated_annual = ctc_annual - _money(result.allocated_monthly * _MONTHS)
    all_notes = list(notes)
    if result.unallocated_monthly > 0:
        all_notes.append(
            f"Rounding leaves {ct._rupees(_money(result.unallocated_monthly))} a month "
            f"({ct._rupees(_money(unallocated_annual))} a year) of the CTC unallocated."
        )
    if template.employer:
        kept = " and ".join(row.name.lower() for row in template.employer)
        all_notes.append(
            f"The CTC keeps {ct._rupees(_money(employer_monthly))} a month for {kept}. "
            "These are not wage types: payroll works out what is contributed each month."
        )
    return TemplateCalcRead(
        structure_id=structure_id,
        structure_name=name,
        ctc_basis=ctc.basis,
        ctc_monthly=_money(ctc.monthly()),
        ctc_annual=_money(ctc_annual),
        lines=out_lines,
        employer=[
            TemplateEmployerRead(
                key=item.code,
                name=item.name,
                rule_text=ct.describe(employer_rules[item.code].rule, names),
                applied=item.applied,
                note=item.note,
                monthly=_money(item.monthly),
                annual=_money(item.monthly * _MONTHS),
            )
            for item in result.employer
        ],
        gross_monthly=_money(result.gross_monthly),
        gross_annual=_money(result.gross_monthly * _MONTHS),
        employer_monthly=_money(employer_monthly),
        allocated_monthly=_money(result.allocated_monthly),
        unallocated_monthly=_money(result.unallocated_monthly),
        group_totals=[
            SalaryGroupTotalRead(wage_group=group, monthly=sums[0], annual=sums[1])
            for group in WageGroup
            if (sums := groups.get(group)) is not None
        ],
        notes=all_notes,
    )


def _usable(
    structure: SalaryStructure, components: Mapping[str, SalaryComponent]
) -> tuple[ct.Template, list[str]]:
    """A structure's template, ready to split a CTC, or a 422 saying what to fix."""
    try:
        template, notes = template_of(structure, components)
        ct.validate(template)
    except ct.TemplateError as exc:
        raise _unprocessable(
            f"'{structure.name}' cannot split a CTC yet: {exc} Open it under Payroll → Salary "
            "structures and save it."
        ) from exc
    if notes and structure.template is None:
        raise _unprocessable(
            f"'{structure.name}' cannot split a CTC yet: {notes[0]} Open it under Payroll → "
            "Salary structures and save it."
        )
    switched_off = [
        line.name
        for line in template.lines
        if line.code in components and not components[line.code].active
    ]
    if switched_off:
        raise _unprocessable(
            f"'{structure.name}' uses {', '.join(switched_off)}, which is switched off in payroll "
            "setup. Take it out of the structure first."
        )
    return template, notes


async def calculate_saved(
    session: AsyncSession, user: User, structure_id: uuid.UUID, amount: Decimal, basis: str
) -> TemplateCalcRead:
    """The Salary master's Calculate: a CTC split by a saved structure. Writes nothing."""
    structure = await _structure(session, user, structure_id)
    components = await _components(session, user)
    template, _ = _usable(structure, components)
    ctc = CtcInput(amount, basis)
    result = run(template, ctc)
    return calc_read(structure.id, structure.name, template, result, ctc, [])


async def preview_draft(
    session: AsyncSession, user: User, payload: SalaryTemplateInput, amount: Decimal, basis: str
) -> TemplateCalcRead:
    """The structure editor's example: an unsaved template split for a CTC. Writes nothing."""
    components = await _components(session, user)
    template = _build(payload, components)
    ctc = CtcInput(amount, basis)
    result = run(template, ctc)
    return calc_read(None, payload.name, template, result, ctc, [])


# ── For the rest of payroll ─────────────────────────────────────────────────


def _resolved(
    line: ct.Line, monthly: Decimal, component: SalaryComponent | None, is_manual: bool
) -> ResolvedComponent:
    monthly = _money(monthly)
    return ResolvedComponent(
        code=line.code,
        name=line.name,
        component_type=line.component_type,
        annual_amount=_money(monthly * _MONTHS),
        monthly_amount=monthly,
        taxable=component.taxable if component is not None else True,
        part_of_ctc=line.part_of_ctc,
        pro_rate_on_lop=component.pro_rate_on_lop if component is not None else True,
        counts_toward_pf=line.counts_toward_pf,
        counts_toward_esi=line.counts_toward_esi,
        display_order=line.display_order,
        is_manual=is_manual,
    )


def template_breakdown(
    structure: SalaryStructure,
    ctc_annual: Decimal,
    *,
    manual: Mapping[str, Decimal] | None = None,
    extras: Sequence[SalaryComponent] = (),
    with_employer: bool = False,
) -> SalaryBreakdown:
    """A template structure's split of a yearly CTC, in the shape `resolve_ctc` returns.

    `manual` holds yearly amounts typed for some wage types; they replace the
    line's rule, and `extras` (wage types outside the template, each with a
    typed amount) are added as they are. The employer contributions kept inside
    the CTC are wage lines only `with_employer` (an offer letter's breakup):
    a salary never stores them, since payroll works them out each month.

    Raises:
        SalaryStructureError: the template cannot split this CTC.
    """
    template, _ = template_of(structure)
    by_code = {line.code: line for line in template.lines}
    manual = dict(manual or {})
    extra_codes = {component.code for component in extras}
    pinned = {code: amount / _MONTHS for code, amount in manual.items() if code in by_code}
    unknown = sorted(set(manual) - set(by_code) - extra_codes)
    if unknown:
        raise SalaryStructureError(
            f"{', '.join(unknown)} {'is' if len(unknown) == 1 else 'are'} "
            "not a wage type of this salary"
        )
    try:
        result = ct.calculate(template, ctc_annual / _MONTHS, pinned=pinned)
    except ct.TemplateError as exc:
        raise SalaryStructureError(str(exc)) from exc
    components_by_code = {link.component.code: link.component for link in structure.components}
    resolved = [
        _resolved(
            by_code[item.code], item.monthly, components_by_code.get(item.code), item.code in pinned
        )
        for item in result.lines
        if item.monthly or item.code in pinned
    ]
    for component in extras:
        if component.code in by_code:
            raise SalaryStructureError(f"'{component.name}' is already part of this structure")
        if component.code not in manual:
            raise SalaryStructureError(
                f"'{component.name}' is not in this structure, so it needs an amount"
            )
        line = _line(component, ct.Rule(ct.Method.FIXED, value=Decimal(0)))
        resolved.append(_resolved(line, manual[component.code] / _MONTHS, component, True))
    if with_employer:
        for index, item in enumerate(result.employer):
            if not item.monthly:
                continue
            monthly = _money(item.monthly)
            resolved.append(
                ResolvedComponent(
                    code=item.code,
                    name=item.name,
                    component_type=ComponentType.EMPLOYER_CONTRIBUTION,
                    annual_amount=_money(monthly * _MONTHS),
                    monthly_amount=monthly,
                    taxable=False,
                    part_of_ctc=True,
                    pro_rate_on_lop=False,
                    counts_toward_pf=False,
                    counts_toward_esi=False,
                    display_order=10_000 + index,
                )
            )
    resolved.sort(key=lambda c: (c.display_order, c.code))
    return SalaryBreakdown(ctc_annual=_money(ctc_annual), components=resolved)


def is_template(structure: SalaryStructure) -> bool:
    return structure.template is not None
