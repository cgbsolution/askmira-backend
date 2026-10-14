"""Releasing an increment cycle: the new salary, the letter and the arrears.

An approved increment is a decision; releasing the cycle is what makes it true
everywhere it matters, in one transaction:

1. **The salary.** A new effective-dated salary on the person's current
   structure at the approved CTC, from the cycle's effective date. The old one
   closes the day before. Nothing is edited in place.
2. **The letter.** The standard increment letter, merged against the new salary
   (as of the effective date), with the previous CTC and the percentage.
3. **The arrears.** Where the effective date is in the past and payroll has
   already paid those months at the old salary, one arrears batch per legal
   entity, for payroll to approve and pay in the next run. Where nothing was
   paid at the old rate yet, the revision is recorded with no arrears.

Each recommendation records the salary and letter it produced, so a release
that is retried after a failure picks up where it stopped instead of applying an
increment twice.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.arrears import RevisionType, SalaryRevision
from app.models.documents import LetterTemplate
from app.models.employee import Employee
from app.models.payroll import PayrollRun, PayrollRunStatus
from app.models.performance_flow import (
    IncrementCycle,
    IncrementRecommendation,
    RecommendationStatusEnum,
)
from app.models.user import User
from app.schemas.payroll import EmployeeSalaryCreate

__all__ = ["INCREMENT_TEMPLATE_CODE", "apply_approved_increments"]

INCREMENT_TEMPLATE_CODE = "INCREMENT_STD"


def _money_text(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"


async def _template(session: AsyncSession, user: User) -> LetterTemplate:
    """The tenant's increment letter, installing the standard one if missing."""
    from app.services.documents import service as letters

    query = tenant_scoped(
        select(LetterTemplate).where(
            LetterTemplate.code == INCREMENT_TEMPLATE_CODE, LetterTemplate.active.is_(True)
        ).order_by(LetterTemplate.version.desc()),
        LetterTemplate,
        user,
    )
    template = await session.scalar(query)
    if template is None:
        await letters.seed_templates(session, user)
        template = await session.scalar(query)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No increment letter template is available. Add one under Documents.",
        )
    return template


async def _paid_at_old_rate(
    session: AsyncSession, user: User, legal_entity_id: uuid.UUID, effective_from: date
) -> bool:
    """Whether an approved run already paid a month on or after the effective date."""
    found = await session.scalar(
        tenant_scoped(
            select(PayrollRun.id).where(
                PayrollRun.legal_entity_id == legal_entity_id,
                PayrollRun.status.in_([PayrollRunStatus.APPROVED, PayrollRunStatus.PAID]),
                PayrollRun.effective_date >= effective_from,
            ),
            PayrollRun,
            user,
        ).limit(1)
    )
    return found is not None


async def apply_approved_increments(
    session: AsyncSession,
    user: User,
    cycle: IncrementCycle,
    *,
    request: Request | None = None,
) -> dict:
    """Apply every approved, not-yet-applied recommendation of a cycle. No commit."""
    from app.services.arrears import service as arrears_service
    from app.services.documents import service as letters
    from app.services.payroll import service as salary_service

    rows = (
        await session.scalars(
            tenant_scoped(
                select(IncrementRecommendation).where(
                    IncrementRecommendation.cycle_id == cycle.id,
                    IncrementRecommendation.status == RecommendationStatusEnum.APPROVED,
                    IncrementRecommendation.applied_at.is_(None),
                    # A promotion is applied by the lifecycle cascade (record,
                    # salary, position, letter together) - never twice.
                    IncrementRecommendation.promotion_to_grade.is_(None),
                ),
                IncrementRecommendation,
                user,
            )
        )
    ).all()
    if not rows:
        return {"applied": 0, "letters": 0, "arrear_batches": 0, "unchanged": 0}

    template = await _template(session, user)
    revisions: dict[uuid.UUID, list[dict]] = defaultdict(list)
    letters_issued = unchanged = 0

    for rec in rows:
        employee = await session.scalar(
            tenant_scoped(select(Employee).where(Employee.id == rec.employee_id), Employee, user)
        )
        if employee is None or employee.legal_entity_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="An approved increment is for somebody with no legal entity on record.",
            )
        current = await salary_service.active_salary_on(
            session, user, rec.employee_id, cycle.effective_from
        )
        if current is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"{employee.employee_code} has no salary in force on "
                    f"{cycle.effective_from:%d %b %Y}, so the increment has nothing to revise."
                ),
            )
        previous_ctc = Decimal(str(current.ctc_annual))
        new_ctc = Decimal(str(rec.recommended_ctc))
        if new_ctc == previous_ctc:
            rec.applied_at = datetime.now(UTC)
            unchanged += 1
            continue

        salary = await salary_service.assign_salary(
            session,
            user,
            rec.employee_id,
            EmployeeSalaryCreate(
                structure_id=current.structure_id,
                ctc_annual=new_ctc,
                effective_from=cycle.effective_from,
                revision_reason=f"Increment cycle '{cycle.name}'",
            ),
            request=request,
            commit=False,
        )
        percentage = (
            ((new_ctc - previous_ctc) / previous_ctc * Decimal(100)).quantize(Decimal("0.01"))
            if previous_ctc
            else Decimal(0)
        )
        letter = await letters.issue_letter(
            session,
            user,
            employee_id=rec.employee_id,
            template_id=template.id,
            manual_values={
                "previous_ctc": _money_text(previous_ctc),
                "increase_percentage": str(percentage),
            },
            as_of=cycle.effective_from,
            # Rendered on first download from the hashed content, so a release
            # that rolls back leaves no orphaned file behind.
            generate_pdf=False,
            request=request,
            commit=False,
        )
        rec.salary_id = salary.id
        rec.letter_id = letter.id
        rec.applied_at = datetime.now(UTC)
        letters_issued += 1
        revisions[employee.legal_entity_id].append(
            {
                "employee_id": str(rec.employee_id),
                "new_salary_id": str(salary.id),
                "previous_salary_id": str(current.id),
            }
        )

    batches = 0
    for legal_entity_id, items in revisions.items():
        if await _paid_at_old_rate(session, user, legal_entity_id, cycle.effective_from):
            await arrears_service.revise_salary(
                session,
                user,
                legal_entity_id=legal_entity_id,
                revisions=items,
                effective_from=cycle.effective_from,
                revision_type=RevisionType.INCREMENT,
                reason=f"Increment cycle '{cycle.name}'",
                request=request,
            )
            batches += 1
        else:
            for item in items:
                session.add(
                    SalaryRevision(
                        tenant_id=user.tenant_id,
                        employee_id=uuid.UUID(item["employee_id"]),
                        previous_salary_id=uuid.UUID(item["previous_salary_id"]),
                        new_salary_id=uuid.UUID(item["new_salary_id"]),
                        effective_from=cycle.effective_from,
                        revision_type=RevisionType.INCREMENT,
                        reason=f"Increment cycle '{cycle.name}'",
                        processed_at=datetime.now(UTC),
                    )
                )
    await session.flush()
    return {
        "applied": letters_issued,
        "letters": letters_issued,
        "arrear_batches": batches,
        "unchanged": unchanged,
    }
