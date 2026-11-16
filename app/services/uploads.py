"""One way in for every file a person uploads.

Three things happen to an uploaded file, in this order, wherever it is uploaded
(a document, a photo, a logo, a receipt, a resume, an import sheet):

1. **The workspace's size limit.** An admin sets it under Settings -> Company ->
   Workspace ("Largest file anyone can upload", 1 to 25 MB, 10 MB until
   changed). It is checked against the file as it arrived.
2. **The type check**, from the file's own bytes (`storage.sniff`).
3. **Compression** (`storage.compress`), in a worker thread so one large scan
   does not stall every other request. What is stored, counted, hashed and
   served afterwards is the compressed file.

A ZIP of many documents is one upload of many files: each file inside is held
to the limit, and the ZIP as a whole to `settings.MAX_ARCHIVE_BYTES`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant
from app.services.storage.compress import DOCUMENT, LOGO, PHOTO, Profile, compress
from app.services.storage.sniff import (
    FileRejectedError,
    FileTooLargeError,
    SniffedType,
    size_label,
    validate_upload,
)

__all__ = [
    "CEILING_BYTES",
    "DEFAULT_MB",
    "DOCUMENT",
    "LOGO",
    "MAX_MB",
    "MIN_MB",
    "PHOTO",
    "READ_CEILING",
    "AcceptedFile",
    "accept",
    "accept_for_tenant",
    "check_size",
    "limit_bytes",
    "limit_mb",
    "read_file",
    "rejected",
    "shrink",
]

#: Until an admin chooses, and for anything that cannot find its workspace.
DEFAULT_MB = 10
#: The range an admin can choose from.
MIN_MB = 1
MAX_MB = 25
_MB = 1024 * 1024
#: The highest limit an admin can set, in bytes. A parser that reads an import
#: sheet holds the file to this; the workspace's own limit is applied as the
#: file is read (`read_file`).
CEILING_BYTES = MAX_MB * _MB
#: The most a router reads of one file before the workspace's own limit is
#: checked: one byte past the highest limit an admin can set.
READ_CEILING = CEILING_BYTES + 1
#: The key in `Tenant.settings`.
SETTING = "max_upload_mb"


def limit_mb(settings: dict[str, Any] | None) -> int:
    """The workspace's limit in MB, from its settings; the default when unset or odd."""
    value = (settings or {}).get(SETTING)
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_MB
    return min(max(value, MIN_MB), MAX_MB)


async def limit_bytes(session: AsyncSession, tenant_id: uuid.UUID | None) -> int:
    """The workspace's limit in bytes."""
    tenant = await session.get(Tenant, tenant_id) if tenant_id else None
    return limit_mb(tenant.settings if tenant else None) * _MB


@dataclass(frozen=True, slots=True)
class AcceptedFile:
    """A file that passed the checks, compressed and ready to store."""

    #: The bytes to store: compressed when that made the file smaller.
    data: bytes
    kind: SniffedType
    #: The size as uploaded, before compression.
    original_size: int

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def sizes(self) -> dict[str, int]:
        """For an audit row: what arrived and what was kept."""
        return {"size_bytes": self.size, "uploaded_size_bytes": self.original_size}


def check_size(data: bytes, max_bytes: int) -> None:
    """Refuse a file over the limit, naming both sizes. For files that are not
    stored (import sheets), which have no type check here."""
    if len(data) > max_bytes:
        raise FileTooLargeError(
            f"The file is {size_label(len(data), up=True)}. "
            f"The upload limit is {size_label(max_bytes)}."
        )


async def read_file(file: UploadFile, session: AsyncSession, tenant_id: uuid.UUID) -> bytes:
    """Read an uploaded file no further than the workspace's limit; 413 past it.

    For a router: it stops reading one byte past the limit, and names the
    file's real size, which the multipart parser already knows.
    """
    limit = await limit_bytes(session, tenant_id)
    data = await file.read(limit + 1)
    if len(data) > limit:
        size = max(file.size or 0, len(data))
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"The file is {size_label(size, up=True)}. "
                f"The upload limit is {size_label(limit)}."
            ),
        )
    return data


async def shrink(data: bytes, mime_type: str, profile: Profile = DOCUMENT) -> bytes:
    """`compress`, off the event loop."""
    return await asyncio.to_thread(compress, data, mime_type, profile)


async def accept(
    data: bytes,
    *,
    max_bytes: int,
    profile: Profile = DOCUMENT,
    types: Iterable[str] | None = None,
    wrong_type: str | None = None,
) -> AcceptedFile:
    """Check a file against a limit and the accepted types, then compress it.

    `types` narrows the accepted MIME types (a photo must be an image);
    `wrong_type` is the reason given when it is not one of them. Raises
    `FileTooLargeError` or `FileRejectedError`.
    """
    kind = validate_upload(data, max_bytes=max_bytes)
    if types is not None and kind.mime_type not in set(types):
        raise FileRejectedError(wrong_type or "This type of file is not accepted here.")
    return AcceptedFile(
        data=await shrink(data, kind.mime_type, profile), kind=kind, original_size=len(data)
    )


async def accept_for_tenant(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    data: bytes,
    *,
    profile: Profile = DOCUMENT,
    types: Iterable[str] | None = None,
    wrong_type: str | None = None,
    reject_status: int = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
) -> AcceptedFile:
    """`accept` against the workspace's own limit, refusing with an HTTP error.

    Too large answers 413; any other refusal `reject_status`, so each screen
    keeps the status it has always answered.
    """
    try:
        return await accept(
            data,
            max_bytes=await limit_bytes(session, tenant_id),
            profile=profile,
            types=types,
            wrong_type=wrong_type,
        )
    except FileRejectedError as exc:
        raise rejected(exc, reject_status=reject_status) from exc


def rejected(
    exc: FileRejectedError, *, reject_status: int = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
) -> HTTPException:
    """The HTTP answer for a refused file: 413 when it is too large."""
    code = (
        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        if isinstance(exc, FileTooLargeError)
        else reject_status
    )
    return HTTPException(status_code=code, detail=str(exc))
