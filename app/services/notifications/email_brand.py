"""A workspace's email brand, and composing a message in it.

The brand is the company's logo (the one on its sign-in page, Settings →
Company), an accent colour, a sign-off and a footer. An admin sets them under
Settings → Notifications → Email design; they are kept in the tenant's
settings, and every change is audited in the same transaction.

`compose` is what every sending path calls — notifications, digests, candidate
messages, the relay test — so no email leaves AskMira as a bare line of text.
"""

from __future__ import annotations

import contextlib
import uuid
from collections import OrderedDict
from typing import Any, Literal

from fastapi import Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import AuditAction
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.notifications.email_layout import (
    DEFAULT_ACCENT,
    EmailAction,
    EmailBrand,
    RenderedEmail,
    render_email,
)
from app.services.storage import StorageError, get_storage

__all__ = [
    "BrandSettings",
    "as_message_fields",
    "brand_for",
    "compose",
    "get_settings",
    "portal_url",
    "save_settings",
]

_SETTINGS_KEY = "email_branding"


class BrandSettings(BaseModel):
    """What an admin chooses. The logo itself is managed under Settings → Company."""

    accent_colour: str = Field(default=DEFAULT_ACCENT, pattern=r"^#[0-9a-fA-F]{6}$")
    show_logo: bool = True
    sign_off: str = Field(default="", max_length=300)
    footer_text: str = Field(default="", max_length=800)
    # Approval emails carry Approve / Reject buttons the approver can use
    # without signing in (see app.services.approvals.email_actions).
    approve_from_email: bool = True


async def get_settings(session: AsyncSession, tenant_id: uuid.UUID) -> BrandSettings:
    tenant = await session.get(Tenant, tenant_id)
    raw = ((tenant.settings or {}).get(_SETTINGS_KEY) or {}) if tenant else {}
    try:
        return BrandSettings.model_validate(raw)
    except ValueError:
        # A hand-edited or older shape must not stop email going out.
        return BrandSettings()


async def save_settings(
    session: AsyncSession, user: User, payload: BrandSettings, *, request: Request | None
) -> BrandSettings:
    tenant = await session.get(Tenant, user.tenant_id)
    before = await get_settings(session, user.tenant_id)
    stored = dict(tenant.settings or {})
    stored[_SETTINGS_KEY] = payload.model_dump(mode="json")
    tenant.settings = stored
    changed = sorted(
        key
        for key, value in payload.model_dump(mode="json").items()
        if before.model_dump(mode="json").get(key) != value
    )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="email_branding",
        entity_id=tenant.id,
        actor=user,
        changes={
            "before": before.model_dump(mode="json"),
            "after": payload.model_dump(mode="json"),
        },
        summary=f"Email design changed: {', '.join(changed) or 'nothing'}",
        request=request,
    )
    await session.commit()
    return payload


# The logo is read from storage for every email otherwise. Keyed by the storage
# key, which changes on every upload, so a new logo is never served stale.
_LOGOS: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
_LOGO_CACHE_SIZE = 64


async def _logo(key: str) -> tuple[bytes, str] | None:
    cached = _LOGOS.get(key)
    if cached is not None:
        _LOGOS.move_to_end(key)
        return cached
    data = None
    with contextlib.suppress(StorageError):
        data = await get_storage().get(key)
    if not data:
        return None
    found = (data, "image/png" if data.startswith(b"\x89PNG") else "image/jpeg")
    _LOGOS[key] = found
    while len(_LOGOS) > _LOGO_CACHE_SIZE:
        _LOGOS.popitem(last=False)
    return found


async def brand_for(
    session: AsyncSession, tenant_id: uuid.UUID, override: BrandSettings | None = None
) -> EmailBrand:
    """The brand to send in. `override` previews unsaved choices."""
    tenant = await session.get(Tenant, tenant_id)
    chosen = override or await get_settings(session, tenant_id)
    logo = None
    if tenant is not None and tenant.logo_key and chosen.show_logo:
        logo = await _logo(tenant.logo_key)
    return EmailBrand(
        company_name=tenant.name if tenant else "",
        accent_colour=chosen.accent_colour,
        sign_off=chosen.sign_off,
        footer_text=chosen.footer_text,
        logo=logo[0] if logo else None,
        logo_mime=logo[1] if logo else "image/png",
    )


def portal_url() -> str:
    return settings.WEB_APP_URL or (settings.CORS_ORIGINS[0] if settings.CORS_ORIGINS else "")


Audience = Literal["employee", "candidate", "admin"]


def _reason(audience: Audience, company: str) -> str:
    if audience == "candidate":
        return f"You are receiving this because you applied for a role at {company}."
    if audience == "admin":
        return f"You are receiving this because you manage {company}'s AskMira workspace."
    return (
        f"You are receiving this because you work at {company}. Choose what reaches you "
        "by email under My profile → Notifications."
    )


async def compose(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    subject: str,
    body: str,
    audience: Audience = "employee",
    with_button: bool = True,
    actions: tuple[EmailAction, ...] = (),
    override: BrandSettings | None = None,
    embed_logo: bool = True,
) -> RenderedEmail:
    brand = await brand_for(session, tenant_id, override)
    # A candidate has no login: the portal is no use to them.
    button = with_button and audience != "candidate"
    return render_email(
        brand,
        subject=subject,
        body=body,
        action_url=(portal_url() or None) if button else None,
        actions=actions,
        reason=_reason(audience, brand.company_name or "your company"),
        embed_logo=embed_logo,
    )


def as_message_fields(rendered: RenderedEmail) -> dict[str, Any]:
    """The `OutboundMessage` fields an email carries."""
    return {"body": rendered.text, "html": rendered.html, "inline_images": rendered.images}
