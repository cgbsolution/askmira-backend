"""Read, sign and accept a letter from the link in its email — no sign-in.

The email that asks someone to acknowledge a letter (and the one that tells
them it was issued) carries a button with a signed token. The token names one
letter and the one person it was issued to, nothing else, and lasts 30 days.

The page it opens shows the letter as its PDF prints, page by page, and asks
the person to tick, type their name and upload a picture of their signature.
Accepting records the same acknowledgement the portal does
(`acknowledgement.record_acceptance`) and keeps a signed copy: the letter with
the signature and date printed in. The signature picture is used to print that
copy and then dropped; it is never stored.

GET only shows things (mail scanners open links); acceptance is a POST with
the typed name, which must be the person's own. A withdrawn letter closes the
link. After acceptance the same link downloads the signed copy until it
expires.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from types import SimpleNamespace
from typing import Any

import jwt
from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_filter, tenant_scoped
from app.core.security import create_letter_link_token, decode_token
from app.models.documents import IssuedLetter, LetterStatus, LetterTemplate
from app.models.employee import Employee
from app.models.letter_acknowledgement import LetterAcknowledgement
from app.models.tenant import Tenant
from app.models.user import User
from app.services.documents import service as letters
from app.services.documents import signed_copy
from app.services.documents.pdf import content_hash
from app.services.hiring.public_guard import Limit, throttle
from app.services.notifications import email_brand
from app.services.notifications.email_layout import EmailAction

__all__ = [
    "acknowledge",
    "buttons_for",
    "describe",
    "email_without_login",
    "link_for",
    "own_link",
    "page",
    "payload_for",
    "pdf",
]

logger = logging.getLogger("askmira.letters")

READ_LIMIT = Limit("letter_link_read", 240, 600)
SIGN_LIMIT = Limit("letter_link_sign", 10, 600)
_PAYLOAD_KEY = "_letter_link"


def _gone(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_410_GONE, detail=detail)


# ── The link ───────────────────────────────────────────────────────────────


def link_for(tenant_id: uuid.UUID, letter_id: uuid.UUID, employee_id: uuid.UUID) -> str | None:
    """The page the email's button opens, or None with no portal address set."""
    base = email_brand.portal_url()
    if not base:
        return None
    token = create_letter_link_token(
        letter_id=str(letter_id), tenant_id=str(tenant_id), employee_id=str(employee_id)
    )
    return f"{base.rstrip('/')}/letter/{token}"


def payload_for(letter: IssuedLetter) -> dict[str, Any]:
    """What a notification carries so its email gets the letter's button.

    Only the ids: the token is made when the email is sent, so no working link
    is ever written to the notification row.
    """
    if letter.employee_id is None:
        return {}
    return {_PAYLOAD_KEY: {"letter_id": str(letter.id), "employee_id": str(letter.employee_id)}}


def buttons_for(user: User, payload: dict[str, Any] | None) -> tuple[EmailAction, ...]:
    """The "Read and sign" button for this recipient's copy of a letter email."""
    value = (payload or {}).get(_PAYLOAD_KEY)
    if not value:
        return ()
    try:
        letter_id = uuid.UUID(str(value.get("letter_id")))
        employee_id = uuid.UUID(str(value.get("employee_id")))
    except (TypeError, ValueError, AttributeError):
        return ()
    # Only the person the letter was issued to gets a link that opens it.
    if user.employee_id != employee_id:
        return ()
    url = link_for(user.tenant_id, letter_id, employee_id)
    return (EmailAction(label="Read and sign the letter", url=url),) if url else ()


async def email_without_login(
    session: AsyncSession,
    *,
    letter: IssuedLetter,
    event_key: str,
    payload: dict[str, Any],
) -> int:
    """Email the link to someone with no login: their work and personal address.

    Sent now, outside any transaction, and never fatal: the letter stands
    whether or not a relay is set up. Returns how many emails went.
    """
    from app.services.notifications.adapters import OutboundMessage
    from app.services.notifications.dispatch import email_adapter_for, email_wording, render

    if letter.employee_id is None:
        return 0
    employee = await session.scalar(
        select(Employee).where(
            tenant_filter(Employee, letter.tenant_id), Employee.id == letter.employee_id
        )
    )
    url = link_for(letter.tenant_id, letter.id, letter.employee_id)
    if employee is None or url is None:
        return 0
    addresses = []
    for address in (employee.work_email, employee.personal_email):
        clean = (address or "").strip()
        if clean and clean.lower() not in {a.lower() for a in addresses}:
            addresses.append(clean)
    if not addresses:
        return 0
    try:
        wording = await email_wording(session, letter.tenant_id, event_key)
        if wording is None:
            return 0
        subject, body = render(wording[0], payload), render(wording[1], payload)
        rendered = await email_brand.compose(
            session,
            letter.tenant_id,
            subject=subject,
            body=body,
            audience="letter",
            actions=(EmailAction(label="Read and sign the letter", url=url),),
        )
        adapter, _relay = await email_adapter_for(session, letter.tenant_id)
    except Exception:
        logger.warning("Letter email for %s could not be built", letter.id, exc_info=True)
        return 0
    sent = 0
    for address in addresses:
        try:
            await adapter.send(
                OutboundMessage(
                    recipient_name=employee.full_name,
                    recipient_email=address,
                    recipient_phone=None,
                    subject=subject,
                    event_key=event_key,
                    **email_brand.as_message_fields(rendered),
                )
            )
            sent += 1
        except Exception:
            logger.warning("Letter email for %s did not go", letter.id, exc_info=True)
    return sent


async def own_link(session: AsyncSession, user: User, letter_id: uuid.UUID) -> str:
    """The signing page for a letter issued to the caller (an onboarding form).

    Employees see no letters in the portal; a form a joiner must sign opens
    the same page the letter's email does. Anyone else's letter does not exist.
    """
    letter = await session.scalar(
        tenant_scoped(select(IssuedLetter).where(IssuedLetter.id == letter_id), IssuedLetter, user)
    )
    if letter is None or letter.employee_id is None or letter.employee_id != user.employee_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Letter not found")
    token = create_letter_link_token(
        letter_id=str(letter.id), tenant_id=str(user.tenant_id), employee_id=str(letter.employee_id)
    )
    return f"/letter/{token}"


# ── Resolving a link ───────────────────────────────────────────────────────


async def _resolve(
    session: AsyncSession, token: str
) -> tuple[Tenant, IssuedLetter, Employee, LetterAcknowledgement | None]:
    try:
        claims = decode_token(token, expected_type="letter_link")
    except jwt.ExpiredSignatureError as exc:
        raise _gone(
            "This link has expired. Ask HR to send it again, or open the letter in AskMiraa "
            "under Documents."
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise _gone(
            "This link is not valid. Check you opened the whole link from the email."
        ) from exc
    try:
        tenant_id = uuid.UUID(str(claims.get("tid")))
        letter_id = uuid.UUID(str(claims.get("l")))
        employee_id = uuid.UUID(str(claims.get("e")))
    except ValueError as exc:
        raise _gone("This link is not valid.") from exc

    tenant = await session.get(Tenant, tenant_id)
    if tenant is None or not tenant.is_active:
        raise _gone("This workspace is not active.")
    scope = SimpleNamespace(tenant_id=tenant_id)
    letter = await session.scalar(
        tenant_scoped(select(IssuedLetter).where(IssuedLetter.id == letter_id), IssuedLetter, scope)
    )
    if letter is None or letter.employee_id != employee_id:
        raise _gone("This letter is no longer available.")
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, scope)
    )
    if employee is None:
        raise _gone("This letter is no longer available.")
    row = await session.scalar(
        tenant_scoped(
            select(LetterAcknowledgement).where(LetterAcknowledgement.letter_id == letter.id),
            LetterAcknowledgement,
            scope,
        )
    )
    return tenant, letter, employee, row


def _state(letter: IssuedLetter, row: LetterAcknowledgement | None) -> str:
    if row is not None and row.acknowledged_at is not None:
        return "acknowledged"
    if letter.status is LetterStatus.REVOKED:
        return "withdrawn"
    if letter.status not in letters.ACKNOWLEDGEABLE:
        return "not_issued"
    if content_hash(letter.merged_html) != letter.content_hash:
        return "changed"
    return "waiting"


async def _document(
    session: AsyncSession, letter: IssuedLetter, row: LetterAcknowledgement | None
) -> bytes:
    """What the page shows: the signed copy once accepted, else the letter."""
    if row is not None and row.signed_copy_storage_key:
        data = await letters.stored_pdf(row.signed_copy_storage_key)
        if data is not None:
            return data
    if letter.status is LetterStatus.REVOKED:
        raise _gone("This letter has been withdrawn.")
    if content_hash(letter.merged_html) != letter.content_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This letter does not match the copy recorded when it was issued. Contact HR.",
        )
    return await letters.pdf_bytes(session, letter)


async def describe(session: AsyncSession, token: str, request: Request) -> dict[str, Any]:
    """What the link is about. Changes nothing."""
    throttle(request, READ_LIMIT)
    tenant, letter, employee, row = await _resolve(session, token)
    from app.services.identity import directory

    template = await session.scalar(
        tenant_scoped(
            select(LetterTemplate).where(LetterTemplate.id == letter.template_id),
            LetterTemplate,
            SimpleNamespace(tenant_id=tenant.id),
        )
    )
    state = _state(letter, row)
    pages = 0
    if state in {"waiting", "acknowledged"}:
        with contextlib.suppress(HTTPException):
            pages = signed_copy.page_count(await _document(session, letter, row))
    acknowledged = row is not None and row.acknowledged_at is not None
    return {
        "state": state,
        "company_name": tenant.name,
        "logo_url": directory.logo_url(tenant),
        "letter_name": template.name if template else "Letter",
        "reference_number": letter.reference_number,
        "employee_name": employee.full_name,
        "issued_at": letter.issued_at.isoformat() if letter.issued_at else None,
        "page_count": pages,
        "acknowledged_at": row.acknowledged_at.isoformat() if acknowledged else None,
        "acknowledged_name": row.typed_name if acknowledged else None,
        "has_signed_copy": bool(row and row.signed_copy_storage_key),
    }


async def page(session: AsyncSession, token: str, number: int, request: Request) -> bytes:
    throttle(request, READ_LIMIT)
    _tenant, letter, _employee, row = await _resolve(session, token)
    return signed_copy.page_image(await _document(session, letter, row), number)


async def pdf(session: AsyncSession, token: str, request: Request) -> tuple[str, bytes]:
    throttle(request, READ_LIMIT)
    _tenant, letter, _employee, row = await _resolve(session, token)
    data = await _document(session, letter, row)
    stem = await letters.download_stem(session, letter)
    signed = row is not None and bool(row.signed_copy_storage_key)
    return f"{stem}{'-signed' if signed else ''}.pdf", data


async def acknowledge(
    session: AsyncSession,
    token: str,
    request: Request,
    *,
    typed_name: str,
    accept: bool,
    signature: bytes,
) -> dict[str, Any]:
    """Accept the letter as the person it was issued to, with their signature."""
    from app.services.documents import acknowledgement

    throttle(request, SIGN_LIMIT)
    tenant, letter, employee, row = await _resolve(session, token)
    if row is not None and row.acknowledged_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"You accepted this letter on {row.acknowledged_at:%d %b %Y}.",
        )
    intact = content_hash(letter.merged_html) == letter.content_hash
    acknowledgement.ensure_acceptable(letter, intact)
    if not accept:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tick the confirmation to accept the letter.",
        )
    typed = " ".join(typed_name.split())
    if not typed or not acknowledgement.names_match(typed, employee):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Type your name as it appears on your record: {employee.full_name}.",
        )
    prepared = signed_copy.prepare_signature(signature)
    row = await acknowledgement.record_acceptance(
        session,
        scope=SimpleNamespace(tenant_id=tenant.id),
        letter=letter,
        employee=employee,
        typed_name=typed,
        actor=None,
        channel="email_link",
        signature_png=prepared,
        request=request,
    )
    # The picture is not kept anywhere; only the printed copy is.
    del prepared, signature
    logger.info("Letter %s accepted from its email link", letter.id)
    return {
        "state": "acknowledged",
        "acknowledged_at": row.acknowledged_at.isoformat() if row.acknowledged_at else None,
        "acknowledged_name": row.typed_name,
        "reference_number": letter.reference_number,
    }
