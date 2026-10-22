"""Statutory rule sets: new versions, and the Compliance SME's sign-off.

═══════════════════════════════════════════════════════════════════════════════
THIS MODULE NEVER WRITES A RATE, AND NEVER EDITS ONE
═══════════════════════════════════════════════════════════════════════════════

A rule set is supplied by the Compliance SME. This code checks that a new
version has the *keys* the engines read, in the shape they read them — never
what the values are — and it never changes `rules` on an existing row. A rate change is a new
effective-dated version; approving it closes the previous version's
`effective_to`, which is the only thing about an older row that ever moves.

Approval is an attestation that the figures match the cited source. Whoever
entered a version cannot approve it.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.core.step_up import Confirm
from app.models.audit import AuditAction
from app.models.employee import Gender
from app.models.payroll import PayrollRuleSet
from app.models.user import User
from app.schemas.payroll_run import RuleSetCreate, RuleSetRead
from app.services import audit

#: Levied by a state; the jurisdiction is the state code.
STATE_RULE_TYPES = frozenset({"professional_tax", "labour_welfare_fund"})
#: Levied nationally; the jurisdiction is the country.
NATIONAL_RULE_TYPES = frozenset({"pf", "esi", "tds", "tds_non_salary", "wage_deductions"})
NATIONAL_JURISDICTION = "IN"

#: The keys each engine refuses to run without. Names only — no values.
REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    # The ECR splits the employer's share into EPF and EPS, each with its own
    # ceiling, and reports EDLI wages: refused at entry, not at filing time.
    "pf": (
        "wage_ceiling",
        "employee_rate",
        "employer_rate",
        "eps_wage_ceiling",
        "edli_wage_ceiling",
        "eps_rate",
    ),
    "esi": ("wage_threshold", "employee_rate", "employer_rate"),
    "professional_tax": ("slabs",),
    "tds": ("regimes", "cess_percent"),
    #: TDS on payments to contractors and professionals (194C, 194J).
    "tds_non_salary": ("sections", "no_pan_rate"),
    #: The legal ceiling on everything deducted from a wage period, as a
    #: percentage of the wages (Payment of Wages Act s.7(3); Code on Wages s.18).
    "wage_deductions": ("max_deduction_percent",),
    "bonus": ("eligibility_wage_ceiling", "calculation_wage_ceiling"),
}

#: Keys read as a number, and those of them read as a percentage. A value the
#: engine cannot read as one is refused here rather than failing mid-run.
_NUMBER_KEYS: dict[str, tuple[str, ...]] = {
    "pf": REQUIRED_KEYS["pf"],
    "esi": REQUIRED_KEYS["esi"],
    "tds": ("cess_percent",),
    "tds_non_salary": ("no_pan_rate",),
    "wage_deductions": ("max_deduction_percent",),
    "bonus": (
        "eligibility_wage_ceiling",
        "calculation_wage_ceiling",
        "minimum_percentage",
        "maximum_percentage",
        "minimum_service_days",
    ),
}
_PERCENT_KEYS = frozenset(
    {
        "employee_rate",
        "employer_rate",
        "eps_rate",
        "cess_percent",
        "no_pan_rate",
        "max_deduction_percent",
        "minimum_percentage",
        "maximum_percentage",
    }
)
#: Rounding the engines know, by the key that chooses it.
_ROUNDING_CHOICES: dict[str, tuple[str, frozenset[str]]] = {
    "pf": ("contribution_rounding", frozenset({"nearest_rupee", "next_rupee", "paise"})),
    "esi": ("contribution_rounding", frozenset({"nearest_rupee", "next_rupee", "paise"})),
    "tds": ("tds_rounding", frozenset({"nearest_rupee", "paise"})),
}
_SLAB_KEYS = frozenset({"up_to", "amount", "month_amounts", "label"})
_MONTHS = frozenset(str(month) for month in range(1, 13))
_GENDERS = frozenset(gender.value for gender in Gender)


def _number(value: object) -> Decimal | None:
    """A JSON value read as the engines read it (`Decimal(str(v))`), or None."""
    if value is None or isinstance(value, bool | dict | list):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _amount(value: object) -> bool:
    number = _number(value)
    return number is not None and number >= 0


def _bound_problems(items: list[Any], where: str, item: str) -> list[str]:
    """`up_to` ascending, and open (null) on the last one only."""
    problems: list[str] = []
    previous: Decimal | None = None
    for number, entry in enumerate(items, start=1):
        label = f"{where}, {item} {number}"
        last = number == len(items)
        if "up_to" not in entry:
            problems.append(
                f"{label} has no up_to (the highest monthly wage it covers; null on the last)."
            )
            continue
        upper = entry["up_to"]
        if upper is None:
            if not last:
                problems.append(
                    f"{label} has no upper bound, so the ones after it could never apply."
                )
            continue
        bound = _number(upper)
        if bound is None:
            problems.append(f"{label}: up_to must be a number, or null on the last.")
            continue
        if previous is not None and bound <= previous:
            problems.append(f"{label}: up_to {upper} must be above the one before it.")
        previous = bound
        if last:
            problems.append(
                f"{label} is the last and needs up_to null: a wage above {upper} would match none."
            )
    return problems


def _slab_problems(slabs: object, where: str) -> list[str]:
    """Professional Tax slabs as `engine._lookup_slab` reads them."""
    if not isinstance(slabs, list) or not slabs:
        return [f"{where} must be a list of slabs, lowest first."]
    if not all(isinstance(slab, dict) for slab in slabs):
        return [f'{where}: each slab is an object like {{"up_to": 15000, "amount": 150}}.']
    problems = _bound_problems(slabs, where, "slab")
    for number, slab in enumerate(slabs, start=1):
        label = f"{where}, slab {number}"
        unknown = sorted(set(slab) - _SLAB_KEYS)
        if unknown:
            problems.append(
                f"{label} has {', '.join(unknown)}, which payroll does not read. A slab "
                "carries up_to and amount, and optionally month_amounts and label."
            )
        if not _amount(slab.get("amount")):
            problems.append(f"{label} needs an amount: a number, 0 or more.")
        month_amounts = slab.get("month_amounts")
        if month_amounts is not None and (
            not isinstance(month_amounts, dict)
            or any(month not in _MONTHS or not _amount(v) for month, v in month_amounts.items())
        ):
            problems.append(
                f'{label}: month_amounts maps a month ("1" to "12") to what that month '
                'charges, like {"2": 300}.'
            )
    return problems


def _lwf_problems(rules: dict[str, Any]) -> list[str]:
    """Labour Welfare Fund as `statutory.compute_lwf` reads it."""
    if not isinstance(rules.get("applicable"), bool):
        return ["applicable must be true or false: whether this state levies LWF."]
    if not rules["applicable"]:
        return []
    problems: list[str] = []
    months = rules.get("deduction_months")
    if (
        not isinstance(months, list)
        or not months
        or any(isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= 12 for m in months)
    ):
        problems.append(
            "deduction_months lists the months LWF is deducted in, as numbers 1 to 12, "
            "like [6, 12]."
        )
    shapes = ("employee_amount", "employer_amount", "employee_percent", "employer_percent")
    bands = rules.get("wage_bands")
    if bands is None:
        sources: list[tuple[str, dict[str, Any]]] = [("The rule set", rules)]
    elif not isinstance(bands, list) or not bands or not all(isinstance(b, dict) for b in bands):
        return [*problems, "wage_bands must be a list of bands, lowest first."]
    else:
        problems += _bound_problems(bands, "wage_bands", "band")
        sources = [(f"wage_bands, band {n}", band) for n, band in enumerate(bands, start=1)]
    for label, source in sources:
        given = [key for key in shapes if key in source]
        if not given:
            problems.append(
                f"{label} needs employee_amount and employer_amount (or the _percent pair)."
            )
        problems += [
            f"{label}: {key} must be a number, 0 or more."
            for key in given
            if not _amount(source[key])
        ]
    return problems


def rule_problems(rule_type: str, rules: dict[str, Any]) -> list[str]:
    """What stops the engines reading these rules as meant. Empty when nothing does.

    Shape only: that a rate is a number from 0 to 100, that slabs climb and end
    open, that a month is 1 to 12. Never whether a value is the right one —
    that is the Compliance SME's attestation.
    """
    problems: list[str] = []
    for key in _NUMBER_KEYS.get(rule_type, ()):
        if key not in rules:
            continue
        number = _number(rules[key])
        if number is None or number < 0:
            problems.append(f"{key} must be a number, 0 or more.")
        elif key in _PERCENT_KEYS and number > 100:
            problems.append(f"{key} is a percentage and cannot be above 100.")
    if rule_type in _ROUNDING_CHOICES:
        key, choices = _ROUNDING_CHOICES[rule_type]
        if key in rules and rules[key] not in choices:
            problems.append(f"{key} must be one of {', '.join(sorted(choices))}.")
    if rule_type == "professional_tax":
        problems += _slab_problems(rules.get("slabs"), "slabs")
        by_gender = rules.get("slabs_by_gender")
        if by_gender is not None:
            if not isinstance(by_gender, dict):
                problems.append(
                    'slabs_by_gender maps a gender to its own slabs, like {"female": [...]}.'
                )
            else:
                for gender, slabs in by_gender.items():
                    if gender not in _GENDERS:
                        problems.append(
                            f"slabs_by_gender: '{gender}' is not one of "
                            f"{', '.join(sorted(_GENDERS))}."
                        )
                    problems += _slab_problems(slabs, f"slabs_by_gender.{gender}")
    if rule_type == "labour_welfare_fund":
        problems += _lwf_problems(rules)
    return problems


def refuse_unreadable(rule_type: str, rules: dict[str, Any]) -> None:
    """422 unless the engines can read `rules`: the required keys, then their shape."""
    missing = [key for key in REQUIRED_KEYS.get(rule_type, ()) if key not in rules]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"A {rule_type} rule set needs {', '.join(missing)}. The payroll "
                "engine refuses to compute without them."
            ),
        )
    problems = rule_problems(rule_type, rules)
    if problems:
        shown = " ".join(problems[:6])
        more = f" (and {len(problems) - 6} more)" if len(problems) > 6 else ""
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Payroll cannot read this {rule_type} rule set as entered: {shown}{more}",
        )


async def _names(
    session: AsyncSession, user: User, ids: set[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
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
    session: AsyncSession, user: User, rows: list[PayrollRuleSet]
) -> list[RuleSetRead]:
    names = await _names(
        session,
        user,
        {r.approved_by_user_id for r in rows} | {r.created_by_user_id for r in rows},
    )
    return [
        RuleSetRead(
            id=row.id,
            rule_type=row.rule_type,
            jurisdiction=row.jurisdiction,
            version=row.version,
            effective_from=row.effective_from,
            effective_to=row.effective_to,
            rules=row.rules,
            source_reference=row.source_reference,
            notes=row.notes,
            is_approved=row.is_approved,
            approved_at=row.approved_at,
            approved_by_user_id=row.approved_by_user_id,
            approved_by_name=names.get(row.approved_by_user_id),  # type: ignore[arg-type]
            created_by_user_id=row.created_by_user_id,
            created_by_name=names.get(row.created_by_user_id),  # type: ignore[arg-type]
            created_at=row.created_at,
            caller_may_approve=(not row.is_approved) and row.created_by_user_id != user.id,
        )
        for row in rows
    ]


async def list_rule_sets(
    session: AsyncSession,
    user: User,
    *,
    rule_type: str | None = None,
    jurisdiction: str | None = None,
    as_of: date | None = None,
) -> list[RuleSetRead]:
    """Every version. `as_of` narrows to what a run effective that day resolves."""
    stmt = tenant_scoped(select(PayrollRuleSet), PayrollRuleSet, user)
    if rule_type:
        stmt = stmt.where(PayrollRuleSet.rule_type == rule_type)
    if jurisdiction:
        stmt = stmt.where(PayrollRuleSet.jurisdiction == jurisdiction)
    if as_of:
        stmt = stmt.where(
            PayrollRuleSet.effective_from <= as_of,
            or_(PayrollRuleSet.effective_to.is_(None), PayrollRuleSet.effective_to >= as_of),
        )
    rows = (
        await session.scalars(
            stmt.order_by(
                PayrollRuleSet.rule_type,
                PayrollRuleSet.jurisdiction,
                PayrollRuleSet.effective_from.desc(),
            )
        )
    ).all()
    return await _reads(session, user, list(rows))


async def create_rule_set(
    session: AsyncSession,
    user: User,
    payload: RuleSetCreate,
    request: Request | None = None,
) -> RuleSetRead:
    """Insert a new, unapproved version. Nothing computes against it until approved."""
    if payload.rule_type in STATE_RULE_TYPES and payload.jurisdiction == NATIONAL_JURISDICTION:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{payload.rule_type} is a state levy; give the state code, not the country.",
        )
    if payload.rule_type in NATIONAL_RULE_TYPES and payload.jurisdiction != NATIONAL_JURISDICTION:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{payload.rule_type} is national; its jurisdiction is IN.",
        )
    refuse_unreadable(payload.rule_type, payload.rules)

    clash = await session.scalar(
        tenant_scoped(
            select(PayrollRuleSet.id).where(
                PayrollRuleSet.rule_type == payload.rule_type,
                PayrollRuleSet.jurisdiction == payload.jurisdiction,
                PayrollRuleSet.effective_from == payload.effective_from,
            ),
            PayrollRuleSet,
            user,
        )
    )
    if clash is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A {payload.rule_type} version for {payload.jurisdiction} effective "
                f"{payload.effective_from} already exists. A correction is a new version with "
                "its own effective date; an existing one is never edited."
            ),
        )

    latest_version = await session.scalar(
        tenant_scoped(
            select(func.max(PayrollRuleSet.version)).where(
                PayrollRuleSet.rule_type == payload.rule_type,
                PayrollRuleSet.jurisdiction == payload.jurisdiction,
            ),
            PayrollRuleSet,
            user,
        )
    )

    row = PayrollRuleSet(
        tenant_id=user.tenant_id,
        rule_type=payload.rule_type,
        jurisdiction=payload.jurisdiction,
        version=int(latest_version or 0) + 1,
        effective_from=payload.effective_from,
        rules=payload.rules,
        source_reference=payload.source_reference.strip(),
        notes=payload.notes,
        created_by_user_id=user.id,
    )
    session.add(row)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="payroll_rule_set",
        entity_id=row.id,
        actor=user,
        changes={
            "step": "create",
            "rule_type": row.rule_type,
            "jurisdiction": row.jurisdiction,
            "version": row.version,
            "effective_from": row.effective_from.isoformat(),
            "source_reference": row.source_reference,
            "rules": row.rules,
        },
        summary=(
            f"Entered {row.rule_type} rules for {row.jurisdiction}, version {row.version}, "
            f"effective {row.effective_from} (awaiting approval)"
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return (await _reads(session, user, [row]))[0]


async def approve_rule_set(
    session: AsyncSession,
    user: User,
    rule_set_id: uuid.UUID,
    *,
    attest: bool,
    confirm: Confirm | None = None,
    request: Request | None = None,
) -> RuleSetRead:
    """The SME sign-off. Records who and when, and supersedes the previous version."""
    if not attest:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Approval is an attestation that these figures match the cited source. "
                "Confirm it to approve."
            ),
        )

    row = await session.scalar(
        tenant_scoped(
            select(PayrollRuleSet).where(PayrollRuleSet.id == rule_set_id), PayrollRuleSet, user
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule set not found")
    if row.is_approved:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This version was already approved on {row.approved_at:%d %b %Y}.",
        )
    if row.created_by_user_id is not None and row.created_by_user_id == user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You entered this version, so somebody else has to approve it. The person "
                "who types the figures in does not attest to them."
            ),
        )
    if confirm is not None:
        await confirm()

    siblings = (
        await session.scalars(
            tenant_scoped(
                select(PayrollRuleSet).where(
                    PayrollRuleSet.rule_type == row.rule_type,
                    PayrollRuleSet.jurisdiction == row.jurisdiction,
                    PayrollRuleSet.id != row.id,
                ),
                PayrollRuleSet,
                user,
            )
        )
    ).all()

    superseded: list[str] = []
    for older in siblings:
        if older.effective_from < row.effective_from and (
            older.effective_to is None or older.effective_to >= row.effective_from
        ):
            older.effective_to = row.effective_from - timedelta(days=1)
            superseded.append(str(older.id))

    later_starts = [s.effective_from for s in siblings if s.effective_from > row.effective_from]
    if later_starts:
        end = min(later_starts) - timedelta(days=1)
        if row.effective_to is None or row.effective_to > end:
            row.effective_to = end

    row.approved_by_user_id = user.id
    row.approved_at = datetime.now(UTC)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="payroll_rule_set",
        entity_id=row.id,
        actor=user,
        changes={
            "step": "approve",
            "attested": True,
            "rule_type": row.rule_type,
            "jurisdiction": row.jurisdiction,
            "version": row.version,
            "effective_from": row.effective_from.isoformat(),
            "source_reference": row.source_reference,
            "superseded": superseded,
        },
        summary=(
            f"Approved {row.rule_type} rules for {row.jurisdiction}, version {row.version}, "
            f"effective {row.effective_from}"
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return (await _reads(session, user, [row]))[0]
