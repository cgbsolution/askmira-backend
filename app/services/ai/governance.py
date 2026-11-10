"""The gate every AI call passes, and the record every AI call leaves.

Three questions before a model sees anything:

1. **Can this server call a model at all?** The workspace picks the provider —
   Anthropic, OpenAI, Gemini or an Ollama server (providers.py) — and may save
   its own key. No key for the chosen provider, no AI — and the screen says so
   rather than showing a spinner that never resolves.
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
import re
import time as clock
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto, permissions
from app.models.audit import AuditAction
from app.models.grounding import AgentInvocation, InvocationOutcome
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.ai import client, providers
from app.services.dpdp import service as dpdp_service

__all__ = [
    "FEATURES",
    "SUB_PROCESSOR",
    "AiSettings",
    "activate",
    "get_settings",
    "http_error",
    "record_call",
    "record_failure",
    "remove_key",
    "require",
    "require_consent",
    "route_for",
    "save_key",
    "set_settings",
    "status_for",
    "test_connection",
]

#: The default sub-processor, as registered in `services/dpdp/transfers.py`. A
#: call goes to the workspace's chosen provider's sub-processor instead
#: (`client.sub_processor()`); its permitted categories bound what a prompt may carry.
SUB_PROCESSOR = "anthropic"

_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")

#: Each feature an admin can switch off on its own, and what it does.
FEATURES: dict[str, str] = {
    "companion": (
        "Ask Miraa: a chat that answers from policy, the people directory, leave and the "
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
    #: The provider that answers ("" = the server's default, AI_PROVIDER).
    provider: str = ""
    #: The model ("" = the provider's default on this server).
    model: str = Field(default="", max_length=100)
    #: Ollama only: the workspace's own server, public https. "" = the server's.
    base_url: str = Field(default="", max_length=300)
    companion: bool = True
    assistant: bool = True
    recognition_drafts: bool = True
    survey_topics: bool = True
    analytics_questions: bool = True
    hiring_drafts: bool = True

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        value = value.strip()
        if value and value not in providers.PROVIDERS:
            raise ValueError(f"Choose one of: {', '.join(providers.PROVIDERS)}.")
        return value

    @field_validator("model")
    @classmethod
    def _model_name(cls, value: str) -> str:
        value = value.strip()
        if value and not _MODEL_NAME.match(value):
            raise ValueError(
                "A model name has letters, digits and . _ : / - only, like gpt-5-mini or "
                "qwen2.5:7b-instruct."
            )
        return value


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
    if payload.base_url.strip():
        if (payload.provider or "") != "ollama":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A server address is only for Ollama. Leave it empty for this provider.",
            )
        try:
            payload = payload.model_copy(
                update={"base_url": await providers.check_public_url(payload.base_url)}
            )
        except providers.UnsafeAddressError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
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
        summary=(
            f"AI features {'on' if payload.enabled else 'off'}; provider "
            f"{payload.provider or 'server default'}"
            + (f", model {payload.model}" if payload.model else "")
        ),
        request=request,
    )
    await session.commit()
    return payload


# ── Provider, model and key ─────────────────────────────────────────────────


def _saved_keys(tenant: Tenant | None) -> dict[str, str]:
    raw = ((tenant.settings or {}).get("ai_keys") or {}) if tenant else {}
    return {k: v for k, v in raw.items() if k in providers.PROVIDERS and isinstance(v, str) and v}


def _workspace_key(tenant: Tenant | None, provider_key: str) -> str | None:
    stored = _saved_keys(tenant).get(provider_key)
    if not stored or tenant is None:
        return None
    try:
        return crypto.decrypt_secret(stored, tenant.id) or None
    except Exception:
        return None


async def route_for(
    session: AsyncSession, user: User, current: AiSettings | None = None
) -> providers.Route:
    """Who answers for this workspace: its provider, model, key and address."""
    current = current or await get_settings(session, user)
    tenant = await session.get(Tenant, user.tenant_id)
    spec = providers.provider(current.provider or providers.server_route().provider)
    own_key = _workspace_key(tenant, spec.key)
    base_url = spec.server_base_url()
    key_source = "workspace" if own_key else "server"
    if spec.key == "ollama" and current.base_url:
        base_url, key_source = current.base_url, "workspace"
    return providers.Route(
        provider=spec.key,
        model=current.model or spec.default_model(),
        api_key=own_key or spec.server_key(),
        base_url=base_url,
        key_source=key_source,
    )


async def activate(
    session: AsyncSession, user: User, current: AiSettings | None = None
) -> providers.Route:
    """Resolve the workspace's route and answer with it for the rest of the request."""
    route = await route_for(session, user, current)
    client.set_route(route)
    return route


def _hint(secret: str | None) -> str:
    return f"…{secret[-4:]}" if secret and len(secret) >= 8 else ("set" if secret else "")


async def _providers_view(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    tenant = await session.get(Tenant, user.tenant_id)
    out = []
    for spec in providers.PROVIDERS.values():
        own = _workspace_key(tenant, spec.key)
        server_key = spec.server_key()
        server_url = spec.server_base_url()
        if spec.protocol == "anthropic":
            server_ready = bool(server_key)
        elif spec.needs_key:
            server_ready = bool(server_key and server_url)
        else:
            server_ready = bool(server_url)
        out.append(
            {
                "key": spec.key,
                "label": spec.label,
                "needs_key": spec.needs_key,
                "suggested_models": list(spec.suggested_models),
                "default_model": spec.default_model(),
                "key_help": spec.key_help,
                "server_ready": server_ready,
                "workspace_key": _hint(own),
                "server_address": bool(spec.key == "ollama" and server_url),
                "sub_processor": spec.sub_processor,
            }
        )
    return out


async def save_key(
    session: AsyncSession,
    user: User,
    provider_key: str,
    api_key: str,
    *,
    request: Request | None,
) -> dict[str, Any]:
    """Keep a workspace's own key for a provider, encrypted. It is never shown again."""
    if not permissions.is_admin(user):
        raise permissions.forbidden("Only an admin adds an AI provider's key.")
    try:
        spec = providers.provider(provider_key)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    api_key = api_key.strip()
    if len(api_key) < 8 or len(api_key) > 400 or any(c.isspace() for c in api_key):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="That does not look like an API key. Paste the whole key, without spaces.",
        )
    if not crypto.can_encrypt():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="This server cannot store secrets yet (no encryption key is set).",
        )
    tenant = await session.get(Tenant, user.tenant_id)
    settings = dict(tenant.settings or {})
    keys = dict(settings.get("ai_keys") or {})
    replaced = spec.key in keys
    keys[spec.key] = crypto.encrypt_secret(api_key, tenant.id)
    settings["ai_keys"] = keys
    tenant.settings = settings
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="ai_settings",
        entity_id=tenant.id,
        actor=user,
        # The key itself never goes in the audit log: only that one was set.
        changes={"provider": spec.key, "key": "replaced" if replaced else "added"},
        summary=f"{spec.label} key {'replaced' if replaced else 'added'} for this workspace",
        request=request,
    )
    await session.commit()
    return {"provider": spec.key, "workspace_key": _hint(api_key)}


async def remove_key(
    session: AsyncSession, user: User, provider_key: str, *, request: Request | None
) -> None:
    if not permissions.is_admin(user):
        raise permissions.forbidden("Only an admin removes an AI provider's key.")
    try:
        spec = providers.provider(provider_key)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    tenant = await session.get(Tenant, user.tenant_id)
    settings = dict(tenant.settings or {})
    keys = dict(settings.get("ai_keys") or {})
    if spec.key not in keys:
        return
    keys.pop(spec.key)
    settings["ai_keys"] = keys
    tenant.settings = settings
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="ai_settings",
        entity_id=tenant.id,
        actor=user,
        changes={"provider": spec.key, "key": "removed"},
        summary=f"{spec.label} key removed from this workspace",
        request=request,
    )
    await session.commit()


_PING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


class _Ping(BaseModel):
    ok: bool


async def test_connection(session: AsyncSession, user: User) -> dict[str, Any]:
    """Ask the workspace's provider one fixed question. No personal data is sent."""
    if not permissions.is_admin(user):
        raise permissions.forbidden("Only an admin tests the AI connection.")
    route = await activate(session, user)
    started = clock.monotonic()
    base = {"provider": route.provider, "label": route.spec.label, "model": route.model}
    if not client.configured():
        return {
            **base,
            "ok": False,
            "message": f"{route.spec.label} has no key (or server address) here yet.",
        }
    try:
        answer, completion = await client.structured(
            system="You check that a connection works. Answer with ok set to true.",
            user="Is this connection working?",
            output=_Ping,
            schema=_PING_SCHEMA,
            max_tokens=2000,
            effort="low",
        )
    except (client.AiUnavailableError, client.AiDeclinedError) as exc:
        return {**base, "ok": False, "message": str(exc)}
    seconds = round(clock.monotonic() - started, 1)
    return {
        **base,
        "ok": bool(answer.ok),
        "model": completion.model or route.model,
        "seconds": seconds,
        "message": f"{route.spec.label} answered in {seconds} seconds.",
    }


async def status_for(session: AsyncSession, user: User) -> dict[str, Any]:
    """What the screens need to decide whether to show an AI affordance."""
    current = await get_settings(session, user)
    route = await activate(session, user, current)
    principal = user.employee_id or user.id
    consented = await dpdp_service.capability_permitted(
        session, user, principal_id=principal, capability="ai_companion_queries"
    )
    configured = client.configured()
    out: dict[str, Any] = {
        "configured": configured,
        "enabled": current.enabled,
        "features": {key: current.enabled and getattr(current, key) for key in FEATURES},
        "feature_descriptions": FEATURES,
        "settings": current.model_dump(mode="json"),
        "provider": route.provider,
        "provider_label": route.spec.label,
        "model": route.model if configured else None,
        "key_source": route.key_source if configured else None,
        "sub_processor": route.sub_processor,
        "my_consent": consented,
    }
    if permissions.is_admin(user):
        out["providers"] = await _providers_view(session, user)
    return out


async def require(session: AsyncSession, user: User, feature: str) -> None:
    """Refuse unless the server can call a model and the tenant has this on."""
    if feature not in FEATURES:
        raise ValueError(f"Unknown AI feature {feature!r}.")
    current = await get_settings(session, user)
    route = await activate(session, user, current)
    if not client.configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"{route.spec.label} is not set up for this workspace, so AI features are off. "
                "An admin can add a key in Settings → AI."
            ),
        )
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
        sub_processor_key=client.sub_processor(),
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
        sub_processor_key=client.sub_processor(),
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
