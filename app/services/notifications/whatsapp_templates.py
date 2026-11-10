"""Settings → Notifications → WhatsApp: the Meta templates, and which event sends which.

WhatsApp lets a business start a conversation only with a template Meta has
approved, so a WhatsApp message needs three things: the workspace's number
(TenantWhatsAppSettings), the person's opt-in (WhatsAppConsent), and — this
module — the approved template registered here and linked to the event.
Before this existed nothing in the portal created a template, so no WhatsApp
message could ever be sent.

AskMira cannot read Meta's approval state with only a phone number id, so the
admin records it as Meta shows it; "Send a test" proves the template, number and
token work together before anyone relies on it.
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
from app.models.notifications import (
    Channel,
    NotificationTemplate,
    WhatsAppTemplate,
    WhatsAppTemplateStatus,
)
from app.models.user import User
from app.services import audit
from app.services.notifications import dispatch
from app.services.notifications.adapters import (
    OutboundMessage,
    PermanentFailure,
    TransientFailure,
)

__all__ = [
    "CATEGORIES",
    "create_template",
    "delete_template",
    "link_event",
    "overview",
    "send_test",
    "update_template",
]

CATEGORIES = ("UTILITY", "MARKETING", "AUTHENTICATION")
_NAME = re.compile(r"^[a-z0-9_]{1,512}$")
_LOCALE = re.compile(r"^[a-z]{2,3}(_[A-Z]{2})?$")
_PARAM = re.compile(r"^[a-z0-9_.]{1,64}$")


def _bad(detail: str, code: int = status.HTTP_422_UNPROCESSABLE_ENTITY) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


def _read(row: WhatsAppTemplate, events: list[str]) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "meta_name": row.meta_name,
        "locale": row.locale,
        "category": row.category,
        "status": row.status.value,
        "parameter_names": list(row.parameter_names or []),
        "rejection_reason": row.rejection_reason,
        "events": sorted(events),
    }


async def _links(session: AsyncSession, user: User) -> dict[str, uuid.UUID]:
    """event_key → the Meta template its active WhatsApp wording names."""
    rows = (
        await session.scalars(
            tenant_scoped(
                select(NotificationTemplate).where(
                    NotificationTemplate.channel == Channel.WHATSAPP,
                    NotificationTemplate.active.is_(True),
                    NotificationTemplate.whatsapp_template_id.is_not(None),
                ),
                NotificationTemplate,
                user,
            )
        )
    ).all()
    return {row.event_key: row.whatsapp_template_id for row in rows}


async def overview(session: AsyncSession, user: User) -> dict[str, Any]:
    templates = (
        await session.scalars(
            tenant_scoped(
                select(WhatsAppTemplate).order_by(WhatsAppTemplate.meta_name),
                WhatsAppTemplate,
                user,
            )
        )
    ).all()
    links = await _links(session, user)
    by_template: dict[uuid.UUID, list[str]] = {}
    for event, template_id in links.items():
        by_template.setdefault(template_id, []).append(event)
    return {
        "templates": [_read(row, by_template.get(row.id, [])) for row in templates],
        "events": [
            {
                "event_key": key,
                "label": subject,
                "template_id": str(links[key]) if key in links else None,
            }
            for key, (subject, _body) in sorted(dispatch.DEFAULT_TEMPLATES.items())
        ],
        "categories": list(CATEGORIES),
        "connected": await dispatch.whatsapp_adapter_for(session, user.tenant_id) is not None,
    }


def _validate(meta_name: str, locale: str, category: str, parameter_names: list[str]) -> None:
    if not _NAME.match(meta_name):
        raise _bad(
            "Use the template's exact Meta name: lower-case letters, digits and underscores."
        )
    if not _LOCALE.match(locale):
        raise _bad("Use Meta's language code for the template, such as en, en_US or hi.")
    if category not in CATEGORIES:
        raise _bad("Choose the category Meta shows: Utility, Marketing or Authentication.")
    if len(parameter_names) > 20 or any(not _PARAM.match(p) for p in parameter_names):
        raise _bad(
            "Parameters are the event's field names in the order of {{1}}, {{2}}… — "
            "lower-case letters, digits, dots and underscores."
        )


async def _one(session: AsyncSession, user: User, template_id: uuid.UUID) -> WhatsAppTemplate:
    row = await session.scalar(
        tenant_scoped(
            select(WhatsAppTemplate).where(WhatsAppTemplate.id == template_id),
            WhatsAppTemplate,
            user,
        )
    )
    if row is None:
        raise _bad("That template is not registered here.", status.HTTP_404_NOT_FOUND)
    return row


async def create_template(
    session: AsyncSession,
    user: User,
    *,
    meta_name: str,
    locale: str,
    category: str,
    parameter_names: list[str],
    approved: bool,
    request: Request | None = None,
) -> dict[str, Any]:
    meta_name, locale, category = meta_name.strip(), locale.strip(), category.strip().upper()
    parameter_names = [p.strip() for p in parameter_names if p.strip()]
    _validate(meta_name, locale, category, parameter_names)
    existing = await session.scalar(
        tenant_scoped(
            select(WhatsAppTemplate).where(
                WhatsAppTemplate.meta_name == meta_name, WhatsAppTemplate.locale == locale
            ),
            WhatsAppTemplate,
            user,
        )
    )
    if existing is not None:
        raise _bad(f"'{meta_name}' in {locale} is already registered.", status.HTTP_409_CONFLICT)
    row = WhatsAppTemplate(
        tenant_id=user.tenant_id,
        meta_name=meta_name,
        locale=locale,
        category=category,
        parameter_names=parameter_names,
        status=WhatsAppTemplateStatus.APPROVED if approved else WhatsAppTemplateStatus.PENDING,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="whatsapp_template",
        entity_id=row.id,
        changes={"meta_name": meta_name, "locale": locale, "status": row.status.value},
        summary=f"WhatsApp template {meta_name} registered",
        request=request,
    )
    await session.commit()
    return _read(row, [])


async def update_template(
    session: AsyncSession,
    user: User,
    template_id: uuid.UUID,
    *,
    approved: bool | None = None,
    rejection_reason: str | None = None,
    parameter_names: list[str] | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    row = await _one(session, user, template_id)
    if parameter_names is not None:
        cleaned = [p.strip() for p in parameter_names if p.strip()]
        _validate(row.meta_name, row.locale, row.category, cleaned)
        row.parameter_names = cleaned
    if rejection_reason:
        row.status = WhatsAppTemplateStatus.REJECTED
        row.rejection_reason = rejection_reason.strip()[:500]
    elif approved is not None:
        row.status = WhatsAppTemplateStatus.APPROVED if approved else WhatsAppTemplateStatus.PENDING
        row.rejection_reason = None
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="whatsapp_template",
        entity_id=row.id,
        changes={"status": row.status.value, "parameters": list(row.parameter_names or [])},
        summary=f"WhatsApp template {row.meta_name} updated",
        request=request,
    )
    await session.commit()
    links = await _links(session, user)
    return _read(row, [e for e, t in links.items() if t == row.id])


async def delete_template(
    session: AsyncSession, user: User, template_id: uuid.UUID, *, request: Request | None = None
) -> None:
    row = await _one(session, user, template_id)
    linked = [e for e, t in (await _links(session, user)).items() if t == row.id]
    if linked:
        raise _bad(
            f"{row.meta_name} still sends {', '.join(linked)}. Unlink those events first.",
            status.HTTP_409_CONFLICT,
        )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.DELETE,
        entity_type="whatsapp_template",
        entity_id=row.id,
        changes={"meta_name": row.meta_name},
        summary=f"WhatsApp template {row.meta_name} removed",
        request=request,
    )
    await session.delete(row)
    await session.commit()


async def link_event(
    session: AsyncSession,
    user: User,
    event_key: str,
    template_id: uuid.UUID | None,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """Send `event_key` on WhatsApp with this template, or (None) stop sending it there."""
    default = dispatch.DEFAULT_TEMPLATES.get(event_key)
    if default is None:
        raise _bad("That is not an AskMira notification.", status.HTTP_404_NOT_FOUND)
    meta = await _one(session, user, template_id) if template_id else None
    if meta is not None and not meta.is_sendable:
        raise _bad(f"{meta.meta_name} is not approved in Meta yet, so it cannot send anything.")
    current = await session.scalar(
        tenant_scoped(
            select(NotificationTemplate).where(
                NotificationTemplate.event_key == event_key,
                NotificationTemplate.channel == Channel.WHATSAPP,
                NotificationTemplate.active.is_(True),
            ),
            NotificationTemplate,
            user,
        )
    )
    if meta is None:
        if current is not None:
            current.whatsapp_template_id = None
    elif current is not None:
        current.whatsapp_template_id = meta.id
    else:
        subject, body = default
        session.add(
            NotificationTemplate(
                tenant_id=user.tenant_id,
                event_key=event_key,
                channel=Channel.WHATSAPP,
                locale="en-IN",
                subject=subject,
                # Kept for the record: WhatsApp shows the Meta template's own wording.
                body=body,
                version=1,
                active=True,
                whatsapp_template_id=meta.id,
            )
        )
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="whatsapp_event",
        changes={"event_key": event_key, "template": meta.meta_name if meta else None},
        summary=(
            f"WhatsApp now sends {event_key} with {meta.meta_name}"
            if meta
            else f"WhatsApp no longer sends {event_key}"
        ),
        request=request,
    )
    await session.commit()
    return await overview(session, user)


async def send_test(
    session: AsyncSession,
    user: User,
    template_id: uuid.UUID,
    *,
    phone: str,
    values: list[str],
    request: Request | None = None,
) -> dict[str, Any]:
    """Send the template to one number now, with sample values. Meta's own answer comes back."""
    row = await _one(session, user, template_id)
    number = re.sub(r"[\s\-()]", "", phone or "")
    if not re.fullmatch(r"\+?\d{10,15}", number):
        raise _bad("Enter the phone number with its country code, such as +91 98765 43210.")
    number = number.lstrip("+")
    if len(number) == 10:
        number = "91" + number
    adapter = await dispatch.whatsapp_adapter_for(session, user.tenant_id)
    if adapter is None:
        raise _bad(
            "Turn WhatsApp on with the business number's id and token first.",
            status.HTTP_409_CONFLICT,
        )
    params = list(values or [])
    if len(params) != len(row.parameter_names or []):
        raise _bad(
            f"{row.meta_name} takes {len(row.parameter_names or [])} value(s): "
            f"{', '.join(row.parameter_names or []) or 'none'}."
        )
    message = OutboundMessage(
        recipient_name=user.full_name,
        recipient_email=None,
        recipient_phone=number,
        subject=None,
        body="",
        event_key="whatsapp.test",
        whatsapp_template_name=row.meta_name,
        whatsapp_parameters=[str(v) for v in params],
        whatsapp_language=row.locale,
    )
    try:
        result = await adapter.send(message)
    except (PermanentFailure, TransientFailure) as exc:
        raise _bad(f"WhatsApp did not send it: {exc}", status.HTTP_502_BAD_GATEWAY) from exc
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="whatsapp_test",
        entity_id=row.id,
        changes={"meta_name": row.meta_name, "to": f"…{number[-4:]}"},
        summary=f"WhatsApp test of {row.meta_name} sent",
        request=request,
    )
    await session.commit()
    return {"sent": True, "message_id": result.provider_message_id}
