"""In-portal letter acknowledgement: HR asks, the employee reads and accepts.

Separate from the e-signature path in `service.send_for_signature`, which
stays as it was: a letter can be acknowledged in the portal, signed through a
provider, both, or neither.

What an acknowledgement records, and why:

- **The typed name**, which must be the employee's own. A click alone is easy
  to make by accident; typing your name is not.
- **The content hash of the letter at that moment.** If the stored text later
  stops matching it, the letter on file is not the one this person accepted.
  A letter whose text already fails its hash check cannot be acknowledged.
- **When, from which address, with which browser** — on the row and in the
  audit entry written in the same transaction.

Only the employee can acknowledge (`require_self`). HR cannot accept a letter
on somebody's behalf; an acknowledgement recorded by somebody else is not one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.documents import IssuedLetter, LetterStatus
from app.models.employee import Employee
from app.models.letter_acknowledgement import LetterAcknowledgement
from app.models.user import User
from app.schemas.letters import LetterAcknowledgeRequest, LetterRead
from app.services import audit
from app.services.documents import service as letters
from app.services.documents.pdf import content_hash
from app.services.notifications.dispatch import notify

__all__ = ["acknowledge", "names_match", "request_acknowledgement"]


def _normalise(name: str) -> str:
    return " ".join(name.split()).casefold()


def names_match(typed: str, employee: Employee) -> bool:
    """Whether the typed name is this person's, ignoring case and spacing."""
    wanted = {
        _normalise(employee.full_name),
        _normalise(" ".join(p for p in (employee.first_name, employee.last_name) if p)),
    }
    return _normalise(typed) in wanted - {""}


async def _row(
    session: AsyncSession, user: User, letter: IssuedLetter
) -> LetterAcknowledgement | None:
    return await session.scalar(
        tenant_scoped(
            select(LetterAcknowledgement).where(LetterAcknowledgement.letter_id == letter.id),
            LetterAcknowledgement,
            user,
        )
    )


async def _template_name(session: AsyncSession, user: User, letter: IssuedLetter) -> str:
    template = await letters.get_template(session, user, letter.template_id)
    return template.name


async def request_acknowledgement(
    session: AsyncSession, user: User, letter_id: uuid.UUID, *, request: Request | None = None
) -> LetterRead:
    """HR asks the employee to read and acknowledge a letter. Asking again reminds."""
    permissions.require_hr(user, detail="Only HR asks for a letter to be acknowledged.")
    letter, intact = await letters.verify_letter(session, user, letter_id)
    if letter.employee_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Offer letters are accepted through the offer, not here.",
        )
    if letter.status not in letters.ACKNOWLEDGEABLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A {letter.status.value.replace('_', ' ')} letter cannot be acknowledged.",
        )
    if not intact:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This letter does not match its stored content hash; do not send it.",
        )
    row = await _row(session, user, letter)
    if row is not None and row.acknowledged_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This letter was acknowledged on {row.acknowledged_at:%d %b %Y}.",
        )
    now = datetime.now(UTC)
    if row is None:
        row = LetterAcknowledgement(
            tenant_id=user.tenant_id,
            letter_id=letter.id,
            employee_id=letter.employee_id,
            request_count=0,
        )
        session.add(row)
    row.requested_at = now
    row.requested_by_user_id = user.id
    row.request_count += 1
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="issued_letter",
        entity_id=letter.id,
        actor=user,
        changes={"acknowledgement_requested": row.request_count},
        summary=f"Asked for {letter.reference_number} to be acknowledged",
        request=request,
    )
    recipients = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(User.id).where(
                        User.employee_id == letter.employee_id, User.is_active.is_(True)
                    ),
                    User,
                    user,
                )
            )
        ).all()
    )
    await notify(
        session,
        tenant_id=user.tenant_id,
        event_key="documents.letter.acknowledgement_requested",
        recipient_user_ids=recipients,
        payload={
            "actor_name": user.full_name,
            "letter_name": await _template_name(session, user, letter),
            "reference_number": letter.reference_number,
        },
        commit=False,
    )
    await session.commit()
    await session.refresh(letter)
    return (await letters.letter_reads(session, user, [letter]))[0]


async def acknowledge(
    session: AsyncSession,
    user: User,
    letter_id: uuid.UUID,
    payload: LetterAcknowledgeRequest,
    *,
    request: Request | None = None,
) -> LetterRead:
    """The employee accepts a letter issued to them. Once, and only them."""
    letter, intact = await letters.verify_letter(session, user, letter_id)
    if letter.employee_id is None or letter.employee_id != user.employee_id:
        # Not theirs: indistinguishable from a letter that does not exist.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Letter not found")
    permissions.require_self(
        user, letter.employee_id, detail="Only the person a letter was issued to can accept it."
    )
    if not payload.accept:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tick the confirmation to accept the letter.",
        )
    if letter.status is LetterStatus.REVOKED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This letter has been withdrawn."
        )
    if letter.status not in letters.ACKNOWLEDGEABLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This letter has not been issued yet, so it cannot be acknowledged.",
        )
    if not intact:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This letter does not match the copy recorded when it was issued. "
                "Contact HR before accepting it."
            ),
        )
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == letter.employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Letter not found")
    typed = " ".join(payload.typed_name.split())
    if not names_match(typed, employee):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Type your name as it appears on your record: {employee.full_name}.",
        )

    row = await _row(session, user, letter)
    if row is not None and row.acknowledged_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"You acknowledged this letter on {row.acknowledged_at:%d %b %Y}.",
        )
    if row is None:
        row = LetterAcknowledgement(
            tenant_id=user.tenant_id,
            letter_id=letter.id,
            employee_id=letter.employee_id,
            request_count=0,
        )
        session.add(row)
    now = datetime.now(UTC)
    row.acknowledged_at = now
    row.acknowledged_by_user_id = user.id
    row.typed_name = typed
    row.content_hash = content_hash(letter.merged_html)
    row.ip_address = request.client.host if request and request.client else None
    user_agent = request.headers.get("user-agent") if request else None
    row.user_agent = user_agent[:512] if user_agent else None
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE,
        entity_type="issued_letter",
        entity_id=letter.id,
        actor=user,
        changes={
            "acknowledged_at": now.isoformat(),
            "typed_name": typed,
            "content_hash": row.content_hash,
        },
        summary=f"{employee.full_name} acknowledged letter {letter.reference_number}",
        request=request,
    )
    # A generated onboarding form is signed by this acknowledgement.
    from app.services.onboarding import service as onboarding

    await onboarding.form_acknowledged(session, user, letter.id)

    notify_ids = {uid for uid in (letter.issued_by_user_id, row.requested_by_user_id) if uid}
    notify_ids.discard(user.id)
    if notify_ids:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key="documents.letter.acknowledged",
            recipient_user_ids=sorted(notify_ids, key=str),
            payload={
                "employee_name": employee.full_name,
                "letter_name": await _template_name(session, user, letter),
                "reference_number": letter.reference_number,
            },
            commit=False,
        )
    await session.commit()
    await session.refresh(letter)
    return (await letters.letter_reads(session, user, [letter]))[0]
