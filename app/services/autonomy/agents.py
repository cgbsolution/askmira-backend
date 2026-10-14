"""The agents that create proposals. Each one is code that reads the record and
proposes; none of them applies anything on its own authority.

**The probation agent** (`lifecycle.raise_confirmation`). A tenant whose
probation policy treats silence as consent has decided that a review nobody
answered ends in confirmation. Something has to carry that out, and it should
not be a silent field edit: the agent drafts the confirmation event — with the
review's own dates — and proposes submitting it. Under the default autonomy a
person approves the proposal; the event then goes through the normal lifecycle
approval like any other, and the letter is issued when it takes effect. A
proposal that is rejected leaves the draft where it is, and the agent does not
propose it again.

The agent makes no judgement. Its reasoning is the facts — how overdue, how
many reminders went unanswered, which policy applies — and its citations are
the review and the employee, so an approver can open both.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.cycle_config import ProbationReview
from app.models.lifecycle import LifecycleEvent, LifecycleEventType, LifecycleStatus
from app.models.user import User
from app.services.autonomy import service as autonomy
from app.services.autonomy.catalogue import AutonomyLevel
from app.services.lifecycle import service as lifecycle

__all__ = ["probation_agent"]

ACTION_KEY = "lifecycle.raise_confirmation"

#: A confirmation in any of these states means the review is already being
#: answered, by a person or by an earlier run of this agent.
_IN_HAND = (
    LifecycleStatus.DRAFT,
    LifecycleStatus.PENDING_APPROVAL,
    LifecycleStatus.APPROVED,
    LifecycleStatus.EFFECTED,
)


async def _confirmation_in_hand(
    session: AsyncSession, user: User, employee_id: uuid.UUID, review: ProbationReview
) -> bool:
    found = await session.scalar(
        tenant_scoped(
            select(LifecycleEvent.id).where(
                LifecycleEvent.employee_id == employee_id,
                LifecycleEvent.event_type == LifecycleEventType.CONFIRMATION,
                LifecycleEvent.status.in_(_IN_HAND),
                LifecycleEvent.created_at >= review.created_at,
            ),
            LifecycleEvent,
            user,
        )
    )
    return found is not None


async def probation_agent(
    session: AsyncSession, user: User, *, today: date | None = None
) -> dict[str, Any]:
    """Propose the confirmations this tenant's silence policy calls for."""
    on = today or date.today()
    try:
        drift = await lifecycle.resolve_probation_drift(session, user, on=on)
    except HTTPException as exc:
        # No probation policy chosen: there is no silence rule to carry out,
        # and the agent does not pick one.
        return {"as_of": on.isoformat(), "skipped": str(exc.detail)}
    confirmations = [c for c in drift["cases"] if c.get("auto_outcome") == "confirm"]

    configured, _threshold, _source = await autonomy.effective_autonomy(
        session, user, action_key=ACTION_KEY
    )
    if configured is AutonomyLevel.SUGGEST_ONLY:
        # The tenant set this action to suggestions only: nothing is drafted or
        # queued, and the drift report already shows the cases.
        return {
            "as_of": on.isoformat(),
            "policy": drift["policy"],
            "proposed": 0,
            "already_in_hand": 0,
            "suggest_only": len(confirmations),
            "refused": [],
        }

    proposed = 0
    already = 0
    refused: list[str] = []
    for case in confirmations:
        review = await session.scalar(
            tenant_scoped(
                select(ProbationReview).where(ProbationReview.id == uuid.UUID(case["review_id"])),
                ProbationReview,
                user,
            )
        )
        if review is None or review.outcome is not None:
            continue
        employee_id = review.employee_id
        if await _confirmation_in_hand(session, user, employee_id, review):
            already += 1
            continue

        reason = (
            f"Probation review was due on {review.review_due_date.isoformat()} and is "
            f"{case['days_overdue']} day(s) past it with no decision after "
            f"{case['reminders_sent']} reminder(s). This organisation's probation policy "
            "confirms when a review passes in silence."
        )
        try:
            # Validation refuses before anything is written, so a refusal here
            # leaves nothing behind.
            event = await lifecycle.raise_event(
                session,
                user,
                employee_id=employee_id,
                event_type=LifecycleEventType.CONFIRMATION,
                effective_from=review.probation_end_date,
                new_values={"probation_end_date": review.probation_end_date.isoformat()},
                reason=reason,
                today=on,
                status_on_raise=LifecycleStatus.DRAFT,
            )
        except HTTPException as exc:
            refused.append(str(exc.detail))
            continue
        await autonomy.propose(
            session,
            user,
            action_key=ACTION_KEY,
            subject_type="lifecycle_event",
            subject_id=event.id,
            proposed_change={"status": LifecycleStatus.PENDING_APPROVAL.value},
            previous_state={"status": LifecycleStatus.DRAFT.value},
            reasoning=reason,
            citations=(
                f"probation_reviews:{review.id}",
                f"employees:{employee_id}",
                f"lifecycle_events:{event.id}",
            ),
            confidence=1.0,
        )
        proposed += 1

    return {
        "as_of": on.isoformat(),
        "policy": drift["policy"],
        "proposed": proposed,
        "already_in_hand": already,
        "suggest_only": 0,
        "refused": refused,
    }
