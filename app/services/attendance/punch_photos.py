"""A photo taken at the moment of a self-service punch.

A punch from a phone says where somebody was; the photo says who pressed the
button — the "proxy punch" a colleague makes for a friend. A workspace chooses
`off`, `optional` or `required` (`AttendanceSettings.photo_mode`). The photo
is kept privately in storage and shown only to the person, their reporting
line and HR, beside the punch it belongs to.

What this does not do is decide whether the face is real or matches anybody:
liveness and face matching are a vendor's model, not something to approximate
here. A reviewer looks at the photo; nothing scores it.
"""

from __future__ import annotations

import base64
import binascii
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.attendance import AttendancePunch, PunchSource
from app.models.tenant import Tenant
from app.models.user import User
from app.services.storage import get_storage
from app.services.storage.sniff import FileRejectedError, validate_upload

__all__ = [
    "MAX_PHOTO_BYTES",
    "photo_for",
    "require_photo_if_configured",
    "store_photo",
    "store_photo_for",
]

#: A phone camera frame, compressed by the browser before it is sent.
MAX_PHOTO_BYTES = 600 * 1024


async def require_photo_if_configured(
    session: AsyncSession, user: User, *, source: PunchSource, has_photo: bool
) -> None:
    """Refuse a self-service punch without a photo when the workspace requires one."""
    if has_photo or source not in (PunchSource.WEB, PunchSource.MOBILE_GEO):
        return
    tenant = await session.get(Tenant, user.tenant_id)
    mode = (((tenant.settings or {}).get("attendance") or {}) if tenant else {}).get(
        "photo_mode", "off"
    )
    if mode == "required":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Your company takes a photo with every punch. Allow camera access for this "
                "site, then punch again."
            ),
        )


async def store_photo(user: User, encoded: str) -> str:
    """Decode a base64 image (a data URL is fine), check it is one, and store it."""
    return await store_photo_for(user.tenant_id, encoded)


async def store_photo_for(tenant_id: uuid.UUID, encoded: str) -> str:
    """As `store_photo`, for a kiosk punch, where nobody is signed in."""
    payload = encoded.split(",", 1)[1] if encoded.startswith("data:") else encoded
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="The photo could not be read.") from exc
    try:
        kind = validate_upload(data, max_bytes=MAX_PHOTO_BYTES)
    except FileRejectedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not kind.mime_type.startswith("image/"):
        raise HTTPException(status_code=422, detail="The punch photo must be a JPEG or PNG.")
    key = f"{tenant_id}/punch-photos/{uuid.uuid4()}{kind.extension}"
    await get_storage().put(key, data)
    return key


async def photo_for(
    session: AsyncSession, user: User, punch_id: uuid.UUID
) -> tuple[bytes, str]:
    """The photo, for the person, their reporting line or HR."""
    punch = await session.scalar(
        tenant_scoped(
            select(AttendancePunch).where(AttendancePunch.id == punch_id), AttendancePunch, user
        )
    )
    if punch is None or not punch.photo_key:
        raise HTTPException(status_code=404, detail="No photo with this punch.")
    if user.employee_id != punch.employee_id:
        await permissions.require_manager_or_hr(
            session, user, punch.employee_id,
            detail="A punch photo is for the person, their manager and HR.",
        )
    data = await get_storage().get(punch.photo_key)
    if data is None:
        raise HTTPException(status_code=404, detail="The photo is missing from storage.")
    media = "image/png" if punch.photo_key.endswith(".png") else "image/jpeg"
    return data, media
