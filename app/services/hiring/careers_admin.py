"""Careers site settings, application status, and portal links — for HR and admins.

Saved with an audit row, a clear refusal when another tenant already holds the
slug, and checks on anything that ends up on a public page: links must be
https, colours must be hex, and a published page must carry a privacy notice.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.hiring import Candidate
from app.models.jobboard import Application, ApplicationStatus, CareersPageConfig
from app.models.user import User
from app.services import audit
from app.services.hiring import careers

__all__ = [
    "CONFIG_FIELDS",
    "careers_config",
    "issue_portal_link",
    "save_careers_config",
    "set_application_status",
]

CONFIG_FIELDS: tuple[str, ...] = (
    "slug",
    "is_published",
    "hero_heading",
    "hero_subheading",
    "about_html",
    "logo_url",
    "hero_image_url",
    "primary_colour",
    "accent_colour",
    "culture_enabled",
    "allow_apply_without_account",
    "privacy_notice_html",
    "privacy_notice_version",
    "resume_retention_days",
    "seo_title",
    "seo_description",
)

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


async def careers_config(session: AsyncSession, user: User) -> dict[str, Any]:
    config = await session.scalar(
        tenant_scoped(select(CareersPageConfig), CareersPageConfig, user)
    )
    if config is None:
        return {"exists": False, **{field: None for field in CONFIG_FIELDS}}
    return {"exists": True, **{field: getattr(config, field) for field in CONFIG_FIELDS}}


async def save_careers_config(
    session: AsyncSession, user: User, *, values: dict[str, Any], request: Request | None = None
) -> dict[str, Any]:
    for field in ("logo_url", "hero_image_url"):
        url = values.get(field)
        if url and not str(url).startswith("https://"):
            raise _unprocessable("Images on the public careers page must be https links.")
    for field in ("primary_colour", "accent_colour"):
        colour = values.get(field)
        if colour and not _HEX.match(str(colour)):
            raise _unprocessable("Colours are six-digit hex values, like #14595C.")
    if values.get("is_published") and not (values.get("privacy_notice_html") or "").strip():
        raise _unprocessable(
            "A published careers page needs a privacy notice. Collecting a resume without "
            "telling the candidate what happens to it or for how long is not something "
            "DPDP allows, and it is the notice they consent to."
        )

    taken = await session.scalar(
        select(CareersPageConfig.tenant_id).where(CareersPageConfig.slug == values["slug"])
    )
    if taken is not None and taken != user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That address is already used by another organisation. Choose another.",
        )

    config = await session.scalar(
        tenant_scoped(select(CareersPageConfig), CareersPageConfig, user)
    )
    created = config is None
    if config is None:
        config = CareersPageConfig(tenant_id=user.tenant_id, slug=values["slug"])
        session.add(config)

    changed = sorted(
        field for field in CONFIG_FIELDS
        if field in values and (created or getattr(config, field) != values[field])
    )
    for field in CONFIG_FIELDS:
        if field in values:
            setattr(config, field, values[field])
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE if created else AuditAction.UPDATE,
        entity_type="careers_page_config",
        entity_id=config.id,
        summary=(
            f"Careers site {'created' if created else 'updated'} at /careers/{config.slug}"
            + (" and published" if config.is_published else " (not published)")
        ),
        changes={"fields": changed},
        request=request,
    )
    return {"slug": config.slug, "is_published": config.is_published, "changed": changed}


async def set_application_status(
    session: AsyncSession,
    user: User,
    *,
    application_id: uuid.UUID,
    to_status: ApplicationStatus,
    request: Request | None = None,
) -> dict[str, Any]:
    application = await session.scalar(
        tenant_scoped(
            select(Application).where(Application.id == application_id), Application, user
        )
    )
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    previous = application.status
    application.status = to_status
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="application",
        entity_id=application.id,
        summary=f"Application status set from {previous.value} to {to_status.value}",
        request=request,
    )
    return {"application_id": str(application.id), "status": application.status.value}


async def issue_portal_link(
    session: AsyncSession, user: User, *, candidate_id: uuid.UUID, request: Request | None = None
) -> dict[str, Any]:
    """Mint a candidate portal link — only for a candidate in this tenant.

    The token is returned exactly once; only its hash is stored.
    """
    candidate = await session.scalar(
        tenant_scoped(select(Candidate).where(Candidate.id == candidate_id), Candidate, user)
    )
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    row, token = await careers.issue_portal_token(session, user, candidate_id=candidate.id)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="candidate_portal_token",
        entity_id=row.id,
        summary="Candidate portal link issued",
        request=request,
    )
    return {
        "token": token,
        "expires_at": row.expires_at.isoformat(),
        "note": (
            "This link is shown once and cannot be retrieved again. Send it to the "
            "candidate now; issue a new one if it is lost."
        ),
    }
