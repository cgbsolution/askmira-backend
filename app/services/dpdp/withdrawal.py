"""What a withdrawal of consent deletes. DB-facing.

DPDP s.8(7)(a): personal data held because of a consent is erased once the
consent is withdrawn, unless a law requires keeping it. `consent.Purpose`
says in words what each withdrawal deletes (`withdrawal_erases`) and keeps
(`withdrawal_keeps`); this module does the deleting, inside the caller's
transaction — the withdrawal row, the deletion and their audit entries commit
together or not at all.

Per purpose:

- **ai_assistance** — the person's Ask Mira conversations and messages and their
  feedback on AI answers go; the AI call log keeps one line per call (when,
  which feature, which model) with the question and answer text removed.
- **culture_analytics** — invitations not yet answered go. Answers already given
  were stored with no link to the person (`culture.submit_response`), so there
  is nothing that could be found and deleted.
- **celebrations** — the sharing flags are switched off; nothing else is held.
- **directory_visibility** — nothing is held only for it: the photo is part of
  the HR record. Colleagues stop seeing it (`gates.hidden_photo_ids`).

Candidate purposes (talent pool, background checks) are withdrawn on the
careers portal, where the candidate is, and handled by the hiring module.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_filter, tenant_scoped
from app.models.agenteval import AgentFeedback
from app.models.companion import CompanionConversation, CompanionMessage
from app.models.culture_depth import EventPrivacyPreference, Survey, SurveyParticipant
from app.models.employee import Employee
from app.models.grounding import AgentInvocation
from app.models.user import User

__all__ = ["erase_on_withdrawal"]


async def _identities(
    session: AsyncSession, user: User, principal_id: uuid.UUID
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    employee_id = await session.scalar(
        tenant_scoped(select(Employee.id).where(Employee.id == principal_id), Employee, user)
    )
    login_id = await session.scalar(
        tenant_scoped(
            select(User.id).where(or_(User.employee_id == principal_id, User.id == principal_id)),
            User,
            user,
        )
    )
    return employee_id, login_id


async def erase_on_withdrawal(
    session: AsyncSession, user: User, *, principal_id: uuid.UUID, purpose_key: str
) -> dict[str, int]:
    """Delete what was held only for this purpose. Counts per kind; no commit."""
    employee_id, login_id = await _identities(session, user, principal_id)
    tenant = user.tenant_id
    out: dict[str, int] = {}

    if purpose_key == "ai_assistance" and login_id is not None:
        messages = await session.execute(
            delete(CompanionMessage).where(
                tenant_filter(CompanionMessage, tenant), CompanionMessage.user_id == login_id
            )
        )
        conversations = await session.execute(
            delete(CompanionConversation).where(
                tenant_filter(CompanionConversation, tenant),
                CompanionConversation.user_id == login_id,
            )
        )
        feedback = await session.execute(
            delete(AgentFeedback).where(
                tenant_filter(AgentFeedback, tenant),
                AgentFeedback.submitted_by_user_id == login_id,
            )
        )
        calls = await session.execute(
            update(AgentInvocation)
            .where(
                tenant_filter(AgentInvocation, tenant),
                AgentInvocation.requested_by_user_id == login_id,
                or_(
                    AgentInvocation.question.is_not(None),
                    AgentInvocation.raw_output.is_not(None),
                    AgentInvocation.rendered_output.is_not(None),
                ),
            )
            .values(question=None, raw_output=None, rendered_output=None)
        )
        out = {
            "ask_mira_conversations": int(conversations.rowcount or 0),
            "ask_mira_messages": int(messages.rowcount or 0),
            "ai_feedback": int(feedback.rowcount or 0),
            "ai_call_texts_removed": int(calls.rowcount or 0),
        }

    elif purpose_key == "culture_analytics" and employee_id is not None:
        open_survey_ids = select(Survey.id).where(
            tenant_filter(Survey, tenant), Survey.closes_on >= date.today()
        )
        invitations = await session.execute(
            delete(SurveyParticipant).where(
                tenant_filter(SurveyParticipant, tenant),
                SurveyParticipant.employee_id == employee_id,
                SurveyParticipant.responded_at.is_(None),
                SurveyParticipant.survey_id.in_(open_survey_ids),
            )
        )
        out = {"survey_invitations": int(invitations.rowcount or 0)}

    elif purpose_key == "celebrations" and employee_id is not None:
        switched = await session.execute(
            update(EventPrivacyPreference)
            .where(
                tenant_filter(EventPrivacyPreference, tenant),
                EventPrivacyPreference.employee_id == employee_id,
            )
            .values(share_birthday=False, share_work_anniversary=False)
        )
        out = {"celebration_settings_switched_off": int(switched.rowcount or 0)}

    await session.flush()
    return out
