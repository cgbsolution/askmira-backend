"""Resumes and pre-joining documents: checked, stored under generated keys, read back.

The same rules as the document vault, through the same modules:

- **The type comes from the bytes.** `storage.sniff` reads the file's header; the
  browser's `Content-Type` and the filename are ignored. A resume must be a PDF;
  a pre-joining document may be a PDF, PNG or JPEG.
- **The key is generated.** A candidate's filename never reaches the filesystem.
- **Served as an attachment**, by the router, never inline.

Text is pulled out of a PDF resume with `pypdf` so a recruiter can read it
without opening the file. That is extraction, not parsing: nobody should read
"skills" or "years of experience" into it, and the stored shape says so.
"""

from __future__ import annotations

import io
import re
import uuid
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import uploads
from app.services.storage import StorageError, get_storage
from app.services.storage.sniff import SniffedType

__all__ = [
    "accept_document",
    "accept_resume",
    "attachment_name",
    "read_stored",
]

#: Pages read for text. A resume is a few pages; a 400-page PDF is not one.
_MAX_PAGES = 12
_MAX_TEXT = 20_000


def _extract_text(data: bytes) -> dict[str, Any]:
    """Plain text from a PDF, or an honest note that there was none."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = len(reader.pages)
        chunks: list[str] = []
        for page in reader.pages[:_MAX_PAGES]:
            chunks.append(page.extract_text() or "")
            if sum(len(c) for c in chunks) > _MAX_TEXT:
                break
        text = re.sub(r"[ \t]+", " ", "\n".join(chunks)).strip()[:_MAX_TEXT]
    except Exception:
        return {
            "kind": "text_extraction",
            "status": "failed",
            "note": "The PDF was stored, but no text could be read from it.",
        }
    return {
        "kind": "text_extraction",
        "status": "extracted" if text else "no_text",
        "pages": pages,
        "text": text,
        "note": (
            "Plain text read from the PDF. It is not structured parsing: skills, roles "
            "and tenure have not been identified."
            if text
            else "The PDF has no readable text layer — it is probably a scanned image."
        ),
    }


async def accept_resume(
    session: AsyncSession, data: bytes, tenant_id: uuid.UUID
) -> tuple[str, dict[str, Any]]:
    """Validate, compress and store a resume. Returns the storage key and extracted text.

    The size limit is the hiring workspace's upload limit.
    """
    accepted = await uploads.accept_for_tenant(
        session,
        tenant_id,
        data,
        types=("application/pdf",),
        wrong_type="A resume has to be a PDF. The type is read from the file itself.",
        reject_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )
    kind = accepted.kind

    storage = get_storage()
    key = storage.new_key(tenant_id, "resumes", kind.extension)
    try:
        await storage.put(key, accepted.data)
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The resume could not be stored just now. Nothing was saved; try again.",
        ) from exc
    return key, _extract_text(data)


async def accept_document(
    session: AsyncSession, data: bytes, tenant_id: uuid.UUID
) -> tuple[str, SniffedType]:
    """Validate, compress and store a pre-joining document (or a check report)."""
    accepted = await uploads.accept_for_tenant(
        session, tenant_id, data, reject_status=status.HTTP_422_UNPROCESSABLE_ENTITY
    )
    kind = accepted.kind

    storage = get_storage()
    key = storage.new_key(tenant_id, "candidate-docs", kind.extension)
    try:
        await storage.put(key, accepted.data)
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The document could not be stored just now. Nothing was saved; try again.",
        ) from exc
    return key, kind


async def read_stored(key: str | None) -> bytes:
    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No file is stored.")
    try:
        data = await get_storage().get(key)
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No file is stored."
        ) from exc
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The file record exists but the file is no longer in storage.",
        )
    return data


def attachment_name(stem: str, extension: str) -> str:
    """A download name built from our own values, never a client filename."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-")[:80] or "file"
    return f"{safe}{extension}"
