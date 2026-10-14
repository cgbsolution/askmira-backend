"""Statutory rule sets: new versions, and the Compliance SME's sign-off.

═══════════════════════════════════════════════════════════════════════════════
THIS MODULE NEVER WRITES A RATE, AND NEVER EDITS ONE
═══════════════════════════════════════════════════════════════════════════════

A rule set is supplied by the Compliance SME. This code checks that a new
version has the *keys* the engines read — never what the values are — and it
never changes `rules` on an existing row. A rate change is a new
effective-dated version; approving it closes the previous version's
`effective_to`, which is the only thing about an older row that ever moves.

Approval is an attestation that the figures match the cited source. Whoever
entered a version cannot approve it.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.core.step_up import Confirm
from app.models.audit import AuditAction
from app.models.payroll import PayrollRuleSet
from app.models.user import User
from app.schemas.payroll_run import RuleSetCreate, RuleSetRead
from app.services import audit

#: Levied by a state; the jurisdiction is the state code.
STATE_RULE_TYPES = frozenset({"professional_tax", "labour_welfare_fund"})
#: Levied nationally; the jurisdiction is the country.
NATIONAL_RULE_TYPES = frozenset({"pf", "esi", "tds", "tds_non_salary"})
NATIONAL_JURISDICTION = "IN"

#: The keys each engine refuses to run without. Names only — no values.
REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    # The ECR splits the employer's share into EPF and EPS, each with its own
    # ceiling, and reports EDLI wages: refused at entry, not at filing time.
    "pf": (
        "wage_ceiling", "employee_rate", "employer_rate",
        "eps_wage_ceiling", "edli_wage_ceiling", "eps_rate",
    ),
    "esi": ("wage_threshold", "employee_rate", "employer_rate"),
    "professional_tax": ("slabs",),
    "tds": ("regimes", "cess_percent"),
    #: TDS on payments to contractors and professionals (194C, 194J).
    "tds_non_salary": ("sections", "no_pan_rate"),
    "bonus": ("eligibility_wage_ceiling", "calculation_wage_ceiling"),
}


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
    missing = [key for key in REQUIRED_KEYS.get(payload.rule_type, ()) if key not in payload.rules]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"A {payload.rule_type} rule set needs {', '.join(missing)}. The payroll "
                "engine refuses to compute without them."
            ),
        )

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
