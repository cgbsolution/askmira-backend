"""Plumbing shared by the payroll-operations modules.

Filings, tax, disbursement, arrears, loans and exits each need the same four
things, and each getting them slightly differently is how one of them ends up
serving a PAN in plain text:

1. **Files at rest.** Uploaded evidence (tax proofs, Form 16, bills) and
   generated statutory files go through the storage vault in
   `app.services.storage` — keys the server generates, content types read from
   the bytes — and are **encrypted** before they are written, because every one
   of them carries a PAN, a UAN or a bank account.
2. **Attachment headers.** A generated file leaves as a download, never as
   something a browser renders inline.
3. **Notification templates** for the events these modules raise, so
   `notify()` has something to render rather than silently suppressing.
4. **Letter templates** for the documents these modules issue through the
   letter engine (Form 12BB, Form 10E, the settlement statement).

And a `system_actor` for the functions a scheduler calls with no signed-in user.
"""

from __future__ import annotations

import base64
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.deps import tenant_filter, tenant_scoped
from app.models.documents import IssuedLetter, LetterTemplate
from app.models.notifications import Channel, NotificationTemplate
from app.models.user import User, UserRole
from app.services.storage import (
    FileRejected,
    FileStorage,
    StorageError,
    get_storage,
    validate_upload,
)

__all__ = [
    "MAX_UPLOAD_BYTES",
    "StoredFile",
    "active_user_ids",
    "attachment_headers",
    "ensure_event_templates",
    "ensure_letter_template",
    "letter_template_code",
    "load_file",
    "store_generated",
    "store_upload",
    "system_actor",
]

#: Evidence uploads: a scanned rent receipt or a Form 16 is well under this.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
#: Longest stored document title (the `document_name` columns).
MAX_TITLE_LENGTH = 255

_MIME_BY_EXTENSION = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".txt": "text/plain",
    ".csv": "text/csv",
}


# ═══════════════════════════════════════════════════════════════════════════
# Files
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class StoredFile:
    key: str
    mime_type: str
    extension: str
    size: int


async def _put(tenant_id: uuid.UUID, key: str, data: bytes) -> None:
    sealed = encrypt_secret(base64.b64encode(data).decode("ascii"), tenant_id)
    try:
        await get_storage().put(key, sealed.encode("ascii"))
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


async def store_upload(tenant_id: uuid.UUID, namespace: str, data: bytes) -> StoredFile:
    """Validate and store an uploaded document. Refuses with the vault's reason.

    The client's filename and content type are never consulted: the type comes
    from the file's first bytes, and the key is generated here.
    """
    try:
        kind = validate_upload(data, max_bytes=MAX_UPLOAD_BYTES)
    except FileRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    key = FileStorage.new_key(tenant_id, namespace, kind.extension)
    await _put(tenant_id, key, data)
    return StoredFile(key=key, mime_type=kind.mime_type, extension=kind.extension, size=len(data))


async def store_generated(
    tenant_id: uuid.UUID, namespace: str, content: bytes, extension: str
) -> str:
    """Store a file this system produced (a return, a bank file). Returns its key."""
    key = FileStorage.new_key(tenant_id, namespace, extension)
    await _put(tenant_id, key, content)
    return key


async def load_file(tenant_id: uuid.UUID, key: str | None) -> tuple[bytes, str] | None:
    """The bytes at a key and their media type, or None.

    A key belonging to another tenant is treated as absent, whatever the
    caller's query did — the vault's own check is on the path, not the tenant.
    """
    if not key or not key.startswith(f"{tenant_id}/"):
        return None
    try:
        raw = await get_storage().get(key)
    except StorageError:
        return None
    if raw is None:
        return None
    plain = decrypt_secret(raw.decode("ascii"), tenant_id)
    if plain is None:
        return None
    extension = key[key.rfind(".") :] if "." in key else ""
    return base64.b64decode(plain), _MIME_BY_EXTENSION.get(extension, "application/octet-stream")


_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def attachment_headers(filename: str) -> dict[str, str]:
    """Headers for a download: attachment, never cached, never sniffed."""
    safe = _UNSAFE_FILENAME.sub("_", filename).strip("._") or "download"
    return {
        "Content-Disposition": f'attachment; filename="{safe}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }


# ═══════════════════════════════════════════════════════════════════════════
# Notifications
# ═══════════════════════════════════════════════════════════════════════════


async def ensure_event_templates(
    session: AsyncSession, tenant_id: uuid.UUID, templates: dict[str, tuple[str, str]]
) -> None:
    """Install email and in-app templates for these events where missing.

    `notify()` suppresses an event it has no template for. The events below are
    raised by payroll operations, so they are installed by payroll operations —
    a tenant admin who later edits the wording keeps their edit, because an
    existing template is never overwritten.
    """
    for event_key, (subject, body) in templates.items():
        for channel in (Channel.EMAIL, Channel.IN_APP):
            existing = await session.scalar(
                select(NotificationTemplate.id).where(
                    tenant_filter(NotificationTemplate, tenant_id),
                    NotificationTemplate.event_key == event_key,
                    NotificationTemplate.channel == channel,
                )
            )
            if existing is None:
                session.add(
                    NotificationTemplate(
                        tenant_id=tenant_id,
                        event_key=event_key,
                        channel=channel,
                        subject=subject,
                        body=body,
                    )
                )
    await session.flush()


async def active_user_ids(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    roles: set[UserRole] | None = None,
    employee_ids: set[uuid.UUID] | None = None,
) -> list[uuid.UUID]:
    """Active logins in a tenant, by role or by employee record."""
    stmt = select(User.id).where(tenant_filter(User, tenant_id), User.is_active.is_(True))
    if roles is not None:
        stmt = stmt.where(User.role.in_(roles))
    if employee_ids is not None:
        if not employee_ids:
            return []
        stmt = stmt.where(User.employee_id.in_(employee_ids))
    return list((await session.scalars(stmt)).all())


def system_actor(tenant_id: uuid.UUID) -> User:
    """A stand-in caller for a scheduled job.

    Never added to a session. It exists so a job can go through the same
    tenant-scoped service functions a request does, and its audit rows carry no
    user id because no person took the action.
    """
    return User(
        tenant_id=tenant_id,
        email="scheduler@askmira.invalid",
        password_hash="!",
        full_name="Scheduled job",
        role=UserRole.SUB_ADMIN,
        is_active=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Letters
# ═══════════════════════════════════════════════════════════════════════════


async def ensure_letter_template(
    session: AsyncSession,
    user: User,
    code: str,
    factory: Callable[[uuid.UUID], LetterTemplate],
) -> LetterTemplate:
    """The tenant's active template for a code, created from `factory` if absent.

    A tenant that has edited the wording through the letter engine keeps its
    newest active version; only a tenant with none gets the prescribed one.
    """
    template = await session.scalar(
        tenant_scoped(
            select(LetterTemplate)
            .where(LetterTemplate.code == code, LetterTemplate.active.is_(True))
            .order_by(LetterTemplate.version.desc()),
            LetterTemplate,
            user,
        )
    )
    if template is None:
        template = factory(user.tenant_id)
        session.add(template)
        await session.flush()
    return template


async def letter_template_code(
    session: AsyncSession, user: User, letter: IssuedLetter
) -> str | None:
    """The template code an issued letter came from."""
    return await session.scalar(
        tenant_scoped(
            select(LetterTemplate.code).where(LetterTemplate.id == letter.template_id),
            LetterTemplate,
            user,
        )
    )
