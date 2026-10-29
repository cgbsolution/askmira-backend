"""The gate every AI call passes, and the record every AI call leaves.

Three questions before a model sees anything:

1. **Can this server call a model at all?** No API key, no AI — and the screen
   says so rather than showing a spinner that never resolves.
2. **Has this tenant turned it on?** Off by default. Sending employee data to a
   provider outside India is a decision the customer makes, in Settings, with
   their name on the audit row — not one this product makes for them.
3. **Has this person agreed?** Where the data is theirs, their DPDP consent for
   the purpose has to permit the capability. A blank is not a yes.

And two records after, in the same transaction as each other:

- a `CrossBorderTransfer` naming the sub-processor, purpose and categories —
  refused outright if the sub-processor may not receive one of them;
- an `AgentInvocation` with the model, the outcome and the token count, which is
  what the AI Trust Center publishes from.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.models.audit import AuditAction
from app.models.grounding import AgentInvocation, InvocationOutcome
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.ai import client
from app.services.dpdp import service as dpdp_service

__all__ = [
    "FEATURES",
    "SUB_PROCESSOR",
    "AiSettings",
    "get_settings",
    "http_error",
    "record_call",
    "record_failure",
    "require",
    "require_consent",
    "set_settings",
    "status_for",
]

#: Every model call goes to this sub-processor, as registered in
#: `services/dpdp/transfers.py`. Its permitted categories are what bound what a
#: prompt may carry.
SUB_PROCESSOR = "anthropic"

#: Each feature an admin can switch off on its own, and what it does.
FEATURES: dict[str, str] = {
    "companion": (
        "Ask Mira: a chat that answers from policy, the people directory, leave and the "
        "asker's own record, as the asker."
    ),
    "assistant": "Employees ask policy questions and get cited answers.",
    "recognition_drafts": "Help writing a shoutout from a few notes.",
    "survey_topics": "Themes in survey comments, never quotes.",
    "analytics_questions": "Ask a reporting question in plain words.",
    "hiring_drafts": (
        "Draft a job description, and summarise the résumé of a candidate who ticked "
        "the optional AI box when applying."
    ),
}


class AiSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    companion: bool = True
    assistant: bool = True
    recognition_drafts: bool = True
    survey_topics: bool = True
    analytics_questions: bool = True
    hiring_drafts: bool = True


async def get_settings(session: AsyncSession, user: User) -> AiSettings:
    tenant = await session.get(Tenant, user.tenant_id)
    raw = ((tenant.settings or {}).get("ai") or {}) if tenant else {}
    return AiSettings.model_validate(raw) if raw else AiSettings()


async def set_settings(
    session: AsyncSession, user: User, payload: AiSettings, *, request: Request | None
) -> AiSettings:
    if not permissions.is_admin(user):
        raise permissions.forbidden("Only an admin turns AI features on or off.")
    tenant = await session.get(Tenant, user.tenant_id)
    before = await get_settings(session, user)
    settings = dict(tenant.settings or {})
    settings["ai"] = payload.model_dump(mode="json")
    tenant.settings = settings
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="ai_settings",
        entity_id=tenant.id,
        actor=user,
        changes={
            "before": before.model_dump(mode="json"),
            "after": payload.model_dump(mode="json"),
        },
        summary=f"AI features {'on' if payload.enabled else 'off'}",
        request=request,
    )
    await session.commit()
    return payload


async def status_for(session: AsyncSession, user: User) -> dict[str, Any]:
    """What the screens need to decide whether to show an AI affordance."""
    current = await get_settings(session, user)
    principal = user.employee_id or user.id
    consented = await dpdp_service.capability_permitted(
        session, user, principal_id=principal, capability="ai_companion_queries"
    )
    return {
        "configured": client.configured(),
        "enabled": current.enabled,
        "features": {key: current.enabled and getattr(current, key) for key in FEATURES},
        "feature_descriptions": FEATURES,
        "settings": current.model_dump(mode="json"),
        "model": client.model_name() if client.configured() else None,
        "sub_processor": SUB_PROCESSOR,
        "my_consent": consented,
    }


async def require(session: AsyncSession, user: User, feature: str) -> None:
    """Refuse unless the server can call a model and the tenant has this on."""
    if feature not in FEATURES:
        raise ValueError(f"Unknown AI feature {feature!r}.")
    if not client.configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No AI provider is configured on this server, so AI features are off.",
        )
    current = await get_settings(session, user)
    if not current.enabled or not getattr(current, feature):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This AI feature is turned off for your organisation. An admin can "
                "turn it on in Settings → AI."
            ),
        )


async def require_consent(session: AsyncSession, user: User, capability: str) -> None:
    """Refuse unless the caller's own consent permits `capability`."""
    principal = user.employee_id or user.id
    if not await dpdp_service.capability_permitted(
        session, user, principal_id=principal, capability=capability
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You have not agreed to the AI assistant purpose, so nothing about "
                "you is sent to the AI provider. You can turn it on under Privacy."
            ),
        )


def http_error(exc: client.AiUnavailableError | client.AiDeclinedError) -> HTTPException:
    if isinstance(exc, client.AiDeclinedError):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


def _fingerprint(system: str) -> str:
    return hashlib.sha256(system.encode("utf-8")).hexdigest()[:16]


async def record_call(
    session: AsyncSession,
    user: User,
    *,
    agent_key: str,
    purpose_key: str,
    categories: tuple[str, ...],
    principal_count: int,
    description: str,
    system: str,
    question: str | None,
    completion: client.Completion | None,
    outcome: InvocationOutcome,
    rendered: str | None,
    raw_output: str | None = None,
    confidence: float | None = None,
    confidence_band: str | None = None,
    citations: list[str] | None = None,
    scope_kind: str = "self",
    started: datetime | None = None,
) -> AgentInvocation:
    """The transfer and the log, flushed into the caller's transaction."""
    await dpdp_service.record_transfer(
        session,
        user,
        sub_processor_key=SUB_PROCESSOR,
        purpose_key=purpose_key,
        category_keys=categories,
        principal_count=principal_count,
        description=description,
        commit=False,
    )
    completed = datetime.now(UTC)
    row = AgentInvocation(
        tenant_id=user.tenant_id,
        agent_key=agent_key,
        prompt_fingerprint=_fingerprint(system),
        model=completion.model if completion else client.model_name(),
        requested_by_user_id=user.id,
        scope_kind=scope_kind,
        scope_employee_count=principal_count if scope_kind != "self" else 1,
        question=question,
        retrieved_record_ids=[],
        refused_sources=[],
        outcome=outcome,
        raw_output=raw_output
        if raw_output is not None
        else (completion.text if completion else None),
        rendered_output=rendered,
        citations=list(citations or []),
        citation_failures=[],
        citations_verified=bool(citations),
        refusal_reasons=[],
        confidence=confidence,
        confidence_band=confidence_band,
        latency_ms=((completed - started).total_seconds() * 1000) if started else 0,
        tokens=completion.tokens if completion else 0,
        sub_processor_key=SUB_PROCESSOR,
        completed_at=completed,
    )
    session.add(row)
    await session.flush()
    return row


async def record_failure(
    session: AsyncSession,
    user: User,
    exc: client.AiUnavailableError | client.AiDeclinedError,
    *,
    agent_key: str,
    purpose_key: str,
    categories: tuple[str, ...],
    principal_count: int,
    description: str,
    system: str,
    question: str | None,
    scope_kind: str = "self",
    started: datetime | None = None,
) -> HTTPException:
    """Log a call that reached for the provider and got nothing usable back.

    The prompt may have left the building even when no answer came back, so
    the transfer is recorded either way. Committed here, because the caller is
    about to raise and a rollback would erase the record of what was sent.
    """
    await record_call(
        session,
        user,
        agent_key=agent_key,
        purpose_key=purpose_key,
        categories=categories,
        principal_count=principal_count,
        description=description,
        system=system,
        question=question,
        completion=None,
        outcome=(
            InvocationOutcome.REFUSED
            if isinstance(exc, client.AiDeclinedError)
            else InvocationOutcome.WITHHELD
        ),
        rendered=str(exc),
        raw_output=None,
        scope_kind=scope_kind,
        started=started,
    )
    await session.commit()
    return http_error(exc)
