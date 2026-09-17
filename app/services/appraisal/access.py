"""Who may see an appraisal, and who may act on the step it is waiting at.

═══════════════════════════════════════════════════════════════════════════════
THE RULES, IN ONE PLACE
═══════════════════════════════════════════════════════════════════════════════

Seeing an appraisal:

- the person it is about;
- anybody above them in the reporting line;
- whoever a line manager delegated their review steps to, for that cycle;
- whoever an overdue step was escalated to;
- HR.

Acting on the current step:

- **Self review** — only the person it is about. HR cannot write somebody's
  own account of their year for them; a self-review written by somebody else is
  not a self-review.
- **Manager and reviewer steps** — never the person it is about, HR included.
  Otherwise their reporting line, a delegate of it, the person the step was
  escalated to, or HR stepping in (recorded as an escalation). The reviewer
  step is a second view, so whoever wrote the manager review cannot also sign
  it off.
- **HR and normalisation steps** — HR, and never on their own appraisal.

These are enforced here, in the service layer, so no route can skip them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.appraisal import (
    Appraisal,
    AppraisalStage,
    AppraisalStageEntry,
    StageDelegation,
    StageRole,
)
from app.models.user import User, UserRole

__all__ = [
    "Actor",
    "can_view",
    "delegated_subject_filter",
    "require_view",
    "stage_actor",
]

LINE_ROLES = (StageRole.MANAGER, StageRole.REVIEWER)
HR_STEP_ROLES = (StageRole.HR, StageRole.NORMALISATION)


@dataclass(frozen=True, slots=True)
class Actor:
    """How the caller comes to be acting on this step."""

    #: The line owner who delegated to the caller, where it was a delegation.
    delegated_from_user_id: uuid.UUID | None
    #: True when the caller acts past the step's own owner.
    is_escalated: bool
    reason: str


async def _delegators(
    session: AsyncSession, user: User, cycle_id: uuid.UUID, on: date | None = None
) -> list[User]:
    """The people who have delegated their review steps to the caller, today."""
    today = on or date.today()
    from_ids = (
        await session.scalars(
            tenant_scoped(
                select(StageDelegation.from_user_id).where(
                    StageDelegation.cycle_id == cycle_id,
                    StageDelegation.to_user_id == user.id,
                    StageDelegation.active.is_(True),
                    StageDelegation.effective_from <= today,
                    StageDelegation.effective_to >= today,
                ),
                StageDelegation,
                user,
            )
        )
    ).all()
    if not from_ids:
        return []
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(User).where(User.id.in_(from_ids), User.is_active.is_(True)),
                    User,
                    user,
                )
            )
        ).all()
    )


async def _line_owner(session: AsyncSession, owner: User, subject_id: uuid.UUID) -> bool:
    """Whether `owner` could act on a line step for this subject themselves."""
    if owner.employee_id is not None and owner.employee_id == subject_id:
        return False
    if permissions.is_hr(owner):
        return True
    return owner.role is UserRole.MANAGER and await permissions.manages(session, owner, subject_id)


async def can_view(session: AsyncSession, user: User, appraisal: Appraisal) -> bool:
    if permissions.is_hr(user) or appraisal.employee_id == user.employee_id:
        return True
    if appraisal.escalated_to_user_id is not None and appraisal.escalated_to_user_id == user.id:
        return True
    if user.role is UserRole.MANAGER and await permissions.manages(
        session, user, appraisal.employee_id
    ):
        return True
    for delegator in await _delegators(session, user, appraisal.cycle_id):
        if await _line_owner(session, delegator, appraisal.employee_id):
            return True
    return False


async def require_view(session: AsyncSession, user: User, appraisal: Appraisal) -> None:
    if not await can_view(session, user, appraisal):
        raise permissions.forbidden("This review belongs to somebody outside your reporting line.")


async def delegated_subject_filter(
    session: AsyncSession, user: User, cycle_id: uuid.UUID
) -> list[User]:
    """The line owners who delegated to the caller in this cycle (for team lists)."""
    return await _delegators(session, user, cycle_id)


async def stage_actor(
    session: AsyncSession,
    user: User,
    appraisal: Appraisal,
    stage: AppraisalStage,
    stages: list[AppraisalStage],
) -> Actor:
    """Decide whether the caller may act on this step, and how. Raises 403."""
    subject = appraisal.employee_id

    if stage.role is StageRole.SELF:
        permissions.require_self(
            user,
            subject,
            detail=(
                "The self-review is the employee's own account of their year. Nobody else "
                "can write it for them, HR included."
            ),
        )
        return Actor(None, False, "The employee's own self-review.")

    if user.employee_id is not None and user.employee_id == subject:
        raise permissions.forbidden(
            "You cannot rate or sign off your own review. This step needs somebody in "
            "your reporting line or HR."
        )

    if stage.role in HR_STEP_ROLES:
        permissions.require_hr(user, detail=f"'{stage.label}' is a step for HR.")
        return Actor(None, False, "HR step.")

    if stage.role is StageRole.REVIEWER:
        manager_stage_ids = {s.id for s in stages if s.role is StageRole.MANAGER}
        wrote_manager_review = await session.scalar(
            tenant_scoped(
                select(AppraisalStageEntry.id).where(
                    AppraisalStageEntry.appraisal_id == appraisal.id,
                    AppraisalStageEntry.stage_id.in_(manager_stage_ids or {uuid.uuid4()}),
                    AppraisalStageEntry.actor_user_id == user.id,
                    AppraisalStageEntry.submitted_at.isnot(None),
                    AppraisalStageEntry.returned_to_stage_id.is_(None),
                ),
                AppraisalStageEntry,
                user,
            ).limit(1)
        )
        if wrote_manager_review is not None:
            raise permissions.forbidden(
                "The reviewer step is a second view. Whoever wrote the manager review "
                "cannot also sign it off."
            )

    if user.role is UserRole.MANAGER and await permissions.manages(session, user, subject):
        return Actor(None, False, "In the employee's reporting line.")

    if appraisal.escalated_to_user_id is not None and appraisal.escalated_to_user_id == user.id:
        return Actor(None, True, "The step was escalated to you after going overdue.")

    for delegator in await _delegators(session, user, appraisal.cycle_id):
        if await _line_owner(session, delegator, subject):
            return Actor(
                delegator.id,
                False,
                "Acting under a delegation; the original owner is recorded on the entry.",
            )

    if permissions.is_hr(user):
        return Actor(None, True, "HR stepping in for the reporting line.")

    raise permissions.forbidden(
        f"'{stage.label}' belongs to the employee's reporting line. Only they, a "
        "delegate of theirs, or HR can act on it."
    )
