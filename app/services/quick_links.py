"""The home page's quick links: the workspace's own, set by an admin.

A link is a label and either a path inside AskMira ("/time") or an https
address (the intranet, the travel desk). Anything else — javascript:, data:,
plain http — is refused, because every employee clicks these.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.models.audit import AuditAction
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit

__all__ = ["DEFAULT_LINKS", "QuickLinkIn", "quick_links", "set_quick_links"]

MAX_LINKS = 12

#: What a workspace sees until an admin chooses its own.
DEFAULT_LINKS: list[dict[str, str]] = [
    {"label": "Apply for leave", "url": "/time"},
    {"label": "My payslips", "url": "/pay"},
    {"label": "Company policies", "url": "/docs"},
    {"label": "Ask HR", "url": "/helpdesk"},
]


class QuickLinkIn(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    url: str = Field(min_length=1, max_length=500)


def _clean(link: QuickLinkIn) -> dict[str, str]:
    url = link.url.strip()
    internal = url.startswith("/") and not url.startswith("//")
    if not (internal or url.lower().startswith("https://")):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"'{link.label}' must go to a page here (starting /) or an https:// address.",
        )
    return {"label": link.label.strip(), "url": url}


async def quick_links(session: AsyncSession, user: User) -> dict[str, Any]:
    tenant = await session.get(Tenant, user.tenant_id)
    stored = ((tenant.settings or {}) if tenant else {}).get("quick_links")
    return {
        "links": stored if isinstance(stored, list) else DEFAULT_LINKS,
        "is_default": not isinstance(stored, list),
    }


async def set_quick_links(
    session: AsyncSession,
    user: User,
    links: list[QuickLinkIn] | None,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """Replace the workspace's links; None goes back to the defaults."""
    if not permissions.is_admin(user):
        raise permissions.forbidden("Only an admin changes the home page's quick links.")
    if links is not None and len(links) > MAX_LINKS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"At most {MAX_LINKS} quick links.",
        )
    tenant = await session.get(Tenant, user.tenant_id)
    settings = dict(tenant.settings or {})
    if links is None:
        settings.pop("quick_links", None)
    else:
        settings["quick_links"] = [_clean(link) for link in links]
    tenant.settings = settings
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="quick_links",
        entity_id=user.tenant_id,
        actor=user,
        changes={"links": settings.get("quick_links")},
        summary="Home page quick links " + ("reset" if links is None else "changed"),
        request=request,
    )
    await session.commit()
    return await quick_links(session, user)
