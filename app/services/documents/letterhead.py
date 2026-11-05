"""The workspace's letterhead: what every letter is printed on.

Chosen once by an admin under Documents → Letterhead and used by every template
that asks for it. Kept in `tenant.settings["letterhead"]`; the logo and the
signatory's signature are files in storage, referenced by key.

An issued letter keeps a copy of the letterhead it was printed on
(`IssuedLetter.letterhead`), so changing the colours next year does not change
how last year's letters re-render. That is also why an old logo or signature
file is never deleted when a new one is uploaded: a letter still points at it.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any, Literal

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.org import LegalEntity
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit, uploads
from app.services.documents.letterhead_pdf import LetterheadArt
from app.services.storage import StorageError, get_storage

__all__ = [
    "SAMPLE_LETTER",
    "LetterheadSettings",
    "art_for",
    "company_values",
    "current_snapshot",
    "get_settings",
    "image_bytes",
    "remove_image",
    "save_image",
    "save_settings",
    "signature_html",
    "snapshot",
]

_SETTINGS_KEY = "letterhead"
_IMAGE_TYPES = {"image/png", "image/jpeg"}
_HEX = r"^#[0-9a-fA-F]{6}$"

ImageKind = Literal["logo", "signature"]

#: What the settings screen previews the letterhead with. Plainly a sample.
SAMPLE_LETTER = """
<h2 style="text-align:center">Sample Letter</h2>
<p><strong>Date : {date}</strong></p>
<p>Employee Name<br/>Location</p>
<p><strong>Subject: How your letters will look</strong></p>
<p>Dear Employee,</p>
<p style="text-align:justify">This page shows your letterhead: the colours, logo and contact
details chosen in Documents &rarr; Letterhead. Every letter template that uses the letterhead is
printed on it, with each person's own details filled in when the letter is issued.</p>
<p style="text-align:justify">The text of each letter is edited under Documents &rarr; Templates.
Nothing on this page is a real letter.</p>
<p><strong>Sincerely,</strong></p>
{signature}
<p><strong>{title}<br/>{company}</strong></p>
"""


class LetterheadSettings(BaseModel):
    """What an admin chooses. Blank contact lines are simply not printed."""

    design: Literal["wave", "classic", "none"] = "wave"
    primary_colour: str = Field(default="#1B5A92", pattern=_HEX)
    secondary_colour: str = Field(default="#4DAAF6", pattern=_HEX)
    accent_colour: str = Field(default="#3A9E96", pattern=_HEX)
    #: Totals in the salary annexure (gross, deductions, take-home).
    highlight_colour: str = Field(default="#E00000", pattern=_HEX)
    company_name: str = Field(default="", max_length=200)
    address: str = Field(default="", max_length=400)
    phone: str = Field(default="", max_length=60)
    email: str = Field(default="", max_length=120)
    website: str = Field(default="", max_length=120)
    signatory_name: str = Field(default="", max_length=120)
    signatory_title: str = Field(default="Human Resource", max_length=120)
    show_logo: bool = True
    #: The reference number and "Page 1 of 2" above the footer.
    show_reference: bool = True

    _strip = field_validator(
        "company_name",
        "address",
        "phone",
        "email",
        "website",
        "signatory_name",
        "signatory_title",
        mode="before",
    )(lambda value: value.strip() if isinstance(value, str) else value)


class StoredLetterhead(LetterheadSettings):
    """The settings plus the image keys only uploads set."""

    logo_key: str | None = None
    signature_key: str | None = None


async def _tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    return tenant


async def _stored(session: AsyncSession, tenant: Tenant, user: User | None) -> StoredLetterhead:
    raw = (tenant.settings or {}).get(_SETTINGS_KEY)
    if raw:
        try:
            return StoredLetterhead.model_validate(raw)
        except ValueError:
            # A hand-edited or older shape must not stop letters going out.
            pass
    # Never saved: start from what the workspace already says about itself.
    address = ""
    if user is not None:
        entity = await session.scalar(
            tenant_scoped(
                select(LegalEntity)
                .where(LegalEntity.is_active.is_(True))
                .order_by(LegalEntity.created_at),
                LegalEntity,
                user,
            ).limit(1)
        )
        address = (entity.registered_address or "") if entity else ""
    return StoredLetterhead(company_name=tenant.name, address=address)


async def get_settings(session: AsyncSession, user: User) -> StoredLetterhead:
    return await _stored(session, await _tenant(session, user.tenant_id), user)


def _changes(before: StoredLetterhead, after: StoredLetterhead) -> list[str]:
    old, new = before.model_dump(mode="json"), after.model_dump(mode="json")
    return sorted(key for key, value in new.items() if old.get(key) != value)


async def _write(
    session: AsyncSession,
    user: User,
    tenant: Tenant,
    before: StoredLetterhead,
    after: StoredLetterhead,
    *,
    summary: str,
    request: Request | None,
) -> StoredLetterhead:
    stored = dict(tenant.settings or {})
    stored[_SETTINGS_KEY] = after.model_dump(mode="json")
    tenant.settings = stored
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="letterhead",
        entity_id=tenant.id,
        actor=user,
        changes={"before": before.model_dump(mode="json"), "after": after.model_dump(mode="json")},
        summary=summary,
        request=request,
    )
    await session.commit()
    return after


async def save_settings(
    session: AsyncSession, user: User, payload: LetterheadSettings, *, request: Request | None
) -> StoredLetterhead:
    tenant = await _tenant(session, user.tenant_id)
    before = await _stored(session, tenant, user)
    after = StoredLetterhead(
        **payload.model_dump(), logo_key=before.logo_key, signature_key=before.signature_key
    )
    changed = _changes(before, after)
    return await _write(
        session,
        user,
        tenant,
        before,
        after,
        summary=f"Letterhead changed: {', '.join(changed) or 'nothing'}",
        request=request,
    )


async def save_image(
    session: AsyncSession, user: User, kind: ImageKind, data: bytes, *, request: Request | None
) -> StoredLetterhead:
    """Store a letterhead logo or a signature (PNG or JPEG, within the workspace's
    upload limit), scaled to a size that prints crisply."""
    accepted = await uploads.accept_for_tenant(
        session,
        user.tenant_id,
        data,
        profile=uploads.LOGO,
        types=_IMAGE_TYPES,
        wrong_type=f"The {kind} has to be a PNG or JPEG image.",
        reject_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )
    sniffed, data = accepted.kind, accepted.data
    storage = get_storage()
    key = storage.new_key(user.tenant_id, "letterhead", sniffed.extension)
    try:
        await storage.put(key, data)
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"The {kind} could not be stored. Nothing was changed. {exc}",
        ) from exc

    tenant = await _tenant(session, user.tenant_id)
    before = await _stored(session, tenant, user)
    after = before.model_copy(update={f"{kind}_key": key})
    # The old file stays: letters already issued were printed with it.
    return await _write(
        session,
        user,
        tenant,
        before,
        after,
        summary=f"Letterhead {kind} uploaded",
        request=request,
    )


async def remove_image(
    session: AsyncSession, user: User, kind: ImageKind, *, request: Request | None
) -> StoredLetterhead:
    tenant = await _tenant(session, user.tenant_id)
    before = await _stored(session, tenant, user)
    if getattr(before, f"{kind}_key") is None:
        return before
    after = before.model_copy(update={f"{kind}_key": None})
    return await _write(
        session, user, tenant, before, after, summary=f"Letterhead {kind} removed", request=request
    )


async def _read(key: str | None) -> bytes | None:
    if not key:
        return None
    try:
        return await get_storage().get(key) or None
    except StorageError:
        return None


async def image_bytes(session: AsyncSession, user: User, kind: ImageKind) -> tuple[bytes, str]:
    """The stored logo or signature, for the settings screen."""
    chosen = await get_settings(session, user)
    key = chosen.logo_key if kind == "logo" else chosen.signature_key
    if kind == "logo" and not key:
        tenant = await _tenant(session, user.tenant_id)
        key = tenant.logo_key
    data = await _read(key)
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No {kind}.")
    return data, ("image/png" if data.startswith(b"\x89PNG") else "image/jpeg")


# ── What letters use ────────────────────────────────────────────────────────


def snapshot(chosen: StoredLetterhead, tenant: Tenant) -> dict[str, Any]:
    """The letterhead as an issued letter keeps it: settings plus the logo key used."""
    values = chosen.model_dump(mode="json")
    values["company_name"] = chosen.company_name or tenant.name
    values["logo_key"] = (chosen.logo_key or tenant.logo_key) if chosen.show_logo else None
    return values


async def current_snapshot(session: AsyncSession, user: User) -> dict[str, Any]:
    tenant = await _tenant(session, user.tenant_id)
    return snapshot(await _stored(session, tenant, user), tenant)


async def art_for(values: dict[str, Any]) -> LetterheadArt:
    """The drawing inputs for a letterhead snapshot."""
    return LetterheadArt(
        design=str(values.get("design") or "wave"),
        primary_colour=str(values.get("primary_colour") or "#1B5A92"),
        secondary_colour=str(values.get("secondary_colour") or "#4DAAF6"),
        accent_colour=str(values.get("accent_colour") or "#3A9E96"),
        company_name=str(values.get("company_name") or ""),
        address=str(values.get("address") or ""),
        phone=str(values.get("phone") or ""),
        email=str(values.get("email") or ""),
        website=str(values.get("website") or ""),
        logo=await _read(values.get("logo_key")),
    )


def company_values(values: dict[str, Any]) -> dict[str, Any]:
    """`company.*` merge fields, from a letterhead snapshot."""
    return {
        "name": values.get("company_name") or "",
        "address": values.get("address") or "",
        "phone": values.get("phone") or "",
        "email": values.get("email") or "",
        "website": values.get("website") or "",
        "signatory_name": values.get("signatory_name") or "",
        "signatory_title": values.get("signatory_title") or "",
    }


async def signature_html(values: dict[str, Any]) -> str:
    """The signatory's signature as an embedded image, or room to sign by hand.

    Embedded rather than linked, so the letter's content (and its hash) carries
    the exact signature it went out with.
    """
    data = await _read(values.get("signature_key"))
    if not data:
        return '<div style="height:34pt"></div>'
    mime = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
    encoded = base64.b64encode(data).decode("ascii")
    return (
        f'<img src="data:{mime};base64,{encoded}" alt="Signature" style="height:36pt; margin:0" />'
    )
