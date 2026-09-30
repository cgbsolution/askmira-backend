"""Supporting documents for leave requests: a medical certificate, a death notice.

Stored through the same storage backend and content sniffing as the employee
vault, under their own namespace. The vault's upload is HR-only; this one is
not, because the person who needs a sick-leave certificate on file is the
employee on sick leave.

A request carries the storage key the upload returned, never a URL. The key is
checked against the tenant and the namespace before it is saved, so nobody can
attach another tenant's file — or a link to anywhere — by typing its name.
"""

from __future__ import annotations

import re
import uuid

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import AuditAction
from app.models.user import User
from app.schemas.leave import LeaveAttachmentRead
from app.services import audit
from app.services.storage import FileRejectedError, StorageError, get_storage, sniff
from app.services.storage.sniff import validate_upload

NAMESPACE = "leave-docs"

_KEY = re.compile(r"^(?P<tenant>[0-9a-f-]{36})/leave-docs/[0-9a-f]{32}\.(pdf|png|jpg)$")


def is_leave_document_key(user: User, key: str) -> bool:
    """A key this tenant's leave upload could have produced."""
    match = _KEY.match(key)
    return match is not None and match.group("tenant") == str(user.tenant_id)


def require_leave_document_key(user: User, key: str | None) -> None:
    if key is not None and not is_leave_document_key(user, key):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Attach the document with the upload button; a typed link is not accepted.",
        )


async def require_stored(key: str) -> None:
    try:
        present = await get_storage().exists(key)
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    if not present:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The attached document is no longer available. Upload it again.",
        )


async def upload(
    session: AsyncSession, user: User, data: bytes, *, request: Request | None = None
) -> LeaveAttachmentRead:
    """Store a PDF, PNG or JPEG and return the key to put on the request."""
    if len(data) > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"The file is larger than the {settings.MAX_UPLOAD_BYTES // (1024 * 1024)} MB "
                "limit."
            ),
        )
    try:
        kind = validate_upload(data, max_bytes=settings.MAX_UPLOAD_BYTES)
    except FileRejectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc

    storage = get_storage()
    key = storage.new_key(user.tenant_id, NAMESPACE, kind.extension)
    try:
        await storage.put(key, data)
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="leave_attachment",
        actor=user,
        changes={"key": key, "mime_type": kind.mime_type, "size_bytes": len(data)},
        summary="Uploaded a supporting document for a leave request",
        request=request,
    )
    await session.commit()
    return LeaveAttachmentRead(document_url=key, mime_type=kind.mime_type, size_bytes=len(data))


async def read(key: str) -> tuple[str, bytes]:
    """The file's sniffed type and bytes."""
    try:
        data = await get_storage().get(key)
    except StorageError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The stored document for this request is missing.",
        )
    kind = sniff(data[:16])
    return (kind.mime_type if kind else "application/octet-stream"), data


def download_name(request_id: uuid.UUID, mime: str) -> str:
    extension = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg"}.get(mime, "")
    return f"leave-document-{str(request_id)[:8]}{extension}"
