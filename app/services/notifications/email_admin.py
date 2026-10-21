"""Settings → Notifications → Email design and Emails: what an admin sees and changes.

Every email AskMira can send is listed — the built-in wording for each event,
and the workspace's own version where an admin wrote one — so nothing goes out
in words the admin cannot see. Editing reuses the versioned templates in
`admin.save_template`; "Use the built-in wording" retires the workspace's
versions rather than deleting them, so the history stays.

Previews and test sends fill each `{placeholder}` with a sample value, so the
admin sees the email the way a person will, not a page of braces.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.notifications import Channel, NotificationTemplate
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.approvals import email_actions
from app.services.notifications import dispatch, email_brand
from app.services.notifications.adapters import OutboundMessage, PermanentFailure, TransientFailure
from app.services.notifications.email_brand import BrandSettings

__all__ = [
    "catalogue",
    "get_branding",
    "preview",
    "reset_template",
    "save_branding",
    "send_test",
]

_OPEN, _CLOSE = chr(0x2039), chr(0x203A)
_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")

#: Readable names for each event family, by the key's first segment.
_GROUPS: dict[str, str] = {
    "analytics": "Analytics",
    "approvals": "Approvals",
    "attendance": "Attendance",
    "auth": "Sign-in and security",
    "culture": "Culture",
    "documents": "Documents and letters",
    "expenses": "Expenses",
    "helpdesk": "Helpdesk",
    "hiring": "Hiring and candidates",
    "learning": "Learning",
    "leave": "Time off",
    "onboarding": "Onboarding",
    "payroll": "Payroll",
    "performance": "Performance",
    "security": "Sign-in and security",
    "statutory": "Compliance",
    "system": "Company announcements",
    "timesheets": "Timesheets",
}

#: What a preview shows in place of each placeholder. Anything not listed is
#: shown as its own name in angle quotes, so it is visibly a stand-in.
_SAMPLES: dict[str, str] = {
    "employee_name": "Meera Nair",
    "name": "Meera",
    "from_name": "Vikram Shah",
    "manager_name": "Vikram Shah",
    "approver_name": "Vikram Shah",
    "candidate_name": "Ayesha Fernandes",
    "leave_type": "Earned Leave",
    "start_date": "5 Nov 2026",
    "end_date": "6 Nov 2026",
    "attendance_date": "16 Oct 2026",
    "date": "21 Oct 2026",
    "due_date": "30 Nov 2026",
    "decision": "approved",
    "period": "October 2026",
    "month": "October 2026",
    "reason": "Forgot to punch out after the late release call.",
    "message": "Thank you for staying late to get the release out.",
    "title": "Office closed on Diwali",
    "category": "Salary certificate",
    "reference_number": "ASK/SAL/2026-27/00003",
    "role": "Backend Engineer, Payments",
    "requisition_title": "Backend Engineer, Payments",
    "company": "your company",
    "link": "https://example.com/link",
    "minutes": "30",
    "count": "3",
    "ticket_number": "HD-00009",
    "subject": "Address-proof letter for a rental agreement",
    "course_title": "POSH awareness 2026",
    "task_title": "Upload your PAN card",
    "joiner_name": "Rohit Sharma",
    "week": "12 Oct 2026",
    "hours": "41",
    "kind_label": "missed punch",
    "first_name": "Meera",
    "subject_name": "Meera Nair",
    "requester_name": "Meera Nair",
    "partner_name": "Arjun Mehta",
    "requester_shift": "General shift",
    "partner_shift": "Support - evening",
    "swap_date": "28 Oct 2026",
    "worked_date": "18 Oct 2026",
    "worked_day": "Sunday",
    "days": "1",
    "amount": "INR 1,850",
    "total": "5",
    "incurred_on": "16 Oct 2026",
    "from_date": "16 Nov 2026",
    "to_date": "17 Nov 2026",
    "purpose": "Payments cut-over review with Tata Capital, Mumbai",
    "category_name": "Headset",
    "asset_name": "Jabra Evolve2 40",
    "asset_tag": "AM-HDS-001",
    "assigned_by": "Karthik Iyer",
    "assigned_on": "21 Oct 2026",
    "decided_by": "Vikram Shah",
    "actor_name": "Karthik Iyer",
    "author_name": "Vikram Shah",
    "reviewer_name": "Arjun Mehta",
    "cancelled_by": "Meera Nair",
    "accepted_by": "Vikram Shah",
    "inviter": "Ananya Rao",
    "comment": "Great work on the release.",
    "note": "Looks good.",
    "needed_by_line": " by 30 Oct 2026",
    "note_line": " Note: take one from the spares.",
    "note_suffix": " Note: approved, thanks for flagging it.",
    "next_line": " It goes to finance next.",
    "remarks_line": " Remarks: the rent receipt for March is missing.",
    "points_line": " You have also been given 500 points.",
    "acknowledge_line": "Please acknowledge it under Assets > My assets.",
    "password_step": "Choose a password when you first sign in.",
    "before_joining": "before you join",
    "signin_link": "https://askmira.example/signin",
    "effective_from": "1 Dec 2026",
    "due_on": "30 Nov 2026",
    "first_due": "Upload your PAN card",
    "first_title": "Upload your PAN card",
    "task_count": "16",
    "joining_date": "2 Nov 2026",
    "last_working_day": "4 Dec 2026",
    "resignation_date": "21 Oct 2026",
    "paid_on": "30 Oct 2026",
    "payment_reference": "HDFC000123",
    "valid_until": "15 Nov 2026",
    "expires": "28 Oct 2026",
    "when": "Thu, 29 Oct 2026, 11:00 am",
    "where": "Google Meet: the link is in the calendar invite.",
    "what": "leave request",
    "from": "Vikram Shah",
    "oldest": "3 days",
    "titles": "PAN card, Passport",
    "section": "bank account",
    "stage": "technical",
    "outcome": "approved",
    "accepted": "INR 1,50,000",
    "proved": "INR 1,60,000",
    "financial_year": "2026-27",
    "responses": "3",
    "goal_title": "Migrate payments to the new ledger",
    "letter_name": "Salary certificate",
    "component": "Internet and broadband",
    "course": "POSH awareness 2026",
    "event": "Diwali celebration",
    "email": "meera@example.com",
    "job_title": "Backend Engineer, Payments",
    "organisation": "your company",
    "agency": "AuthBridge",
    "checks": "employment history, education",
    "onboarding": "Standard onboarding (India)",
}


def _group(event_key: str) -> str:
    head = event_key.split(".", 1)[0]
    return _GROUPS.get(head, head.replace("_", " ").capitalize())


def _placeholders(*texts: str | None) -> list[str]:
    found: list[str] = []
    for text in texts:
        for name in _PLACEHOLDER.findall(text or ""):
            if name not in found:
                found.append(name)
    return found


def _sample_payload(names: list[str], user: User, company: str) -> dict[str, str]:
    # Unknown placeholders show as their own name between single angle quotes.
    values = {name: _SAMPLES.get(name, f"{_OPEN}{name}{_CLOSE}") for name in names}
    for key in ("company", "organisation"):
        if key in values:
            values[key] = company
    if "name" in values and user.full_name:
        values["name"] = user.full_name.split(" ")[0]
    return values


def _audience(event_key: str) -> email_brand.Audience:
    return "candidate" if event_key.startswith("hiring.candidate.") else "employee"


async def catalogue(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Every email the workspace can send: built-in wording and its own versions."""
    rows = (
        await session.scalars(
            tenant_scoped(
                select(NotificationTemplate).where(
                    NotificationTemplate.active.is_(True),
                    NotificationTemplate.channel.in_([Channel.EMAIL, Channel.IN_APP, Channel.PUSH]),
                ),
                NotificationTemplate,
                user,
            )
        )
    ).all()
    custom: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        custom.setdefault(row.event_key, {})[row.channel.value] = {
            "subject": row.subject,
            "body": row.body,
            "version": row.version,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    keys = sorted(set(dispatch.DEFAULT_TEMPLATES) | set(custom))
    out: list[dict[str, Any]] = []
    for key in keys:
        default_subject, default_body = dispatch.DEFAULT_TEMPLATES.get(key, ("", ""))
        own = custom.get(key, {})
        out.append(
            {
                "event_key": key,
                "group": _group(key),
                "audience": _audience(key),
                "urgent": key in dispatch.URGENT_EVENT_KEYS,
                # The approver's copy carries Approve / Reject buttons.
                "approval_buttons": key in email_actions.APPROVAL_EVENTS,
                "default_subject": default_subject,
                "default_body": default_body,
                "placeholders": _placeholders(
                    default_subject,
                    default_body,
                    *(item.get("subject") for item in own.values()),
                    *(item.get("body") for item in own.values()),
                ),
                "custom": own,
            }
        )
    return out


async def get_branding(session: AsyncSession, user: User) -> dict[str, Any]:
    chosen = await email_brand.get_settings(session, user.tenant_id)
    tenant = await session.get(Tenant, user.tenant_id)
    return {
        **chosen.model_dump(mode="json"),
        "company_name": tenant.name if tenant else "",
        # Whether there is a logo to show at all; `show_logo` is whether to.
        "has_logo": bool(tenant and tenant.logo_key),
    }


async def save_branding(
    session: AsyncSession, user: User, payload: BrandSettings, *, request: Request | None
) -> dict[str, Any]:
    await email_brand.save_settings(session, user, payload, request=request)
    return await get_branding(session, user)


async def _render(
    session: AsyncSession,
    user: User,
    *,
    event_key: str | None,
    subject: str,
    body: str,
    branding: BrandSettings | None,
    embed_logo: bool,
):
    brand = await email_brand.brand_for(session, user.tenant_id, branding)
    payload = _sample_payload(
        _placeholders(subject, body), user, brand.company_name or "your company"
    )
    chosen = branding or await email_brand.get_settings(session, user.tenant_id)
    buttons = (
        email_actions.sample_buttons()
        if event_key in email_actions.APPROVAL_EVENTS and chosen.approve_from_email
        else ()
    )
    return await email_brand.compose(
        session,
        user.tenant_id,
        subject=dispatch.render(subject, payload),
        body=dispatch.render(body, payload),
        audience=_audience(event_key or ""),
        actions=buttons,
        override=branding,
        embed_logo=embed_logo,
    )


async def preview(
    session: AsyncSession,
    user: User,
    *,
    event_key: str | None,
    subject: str,
    body: str,
    branding: BrandSettings | None = None,
) -> dict[str, Any]:
    """The email as a person would see it, for the screen. Nothing is saved or sent."""
    rendered = await _render(
        session,
        user,
        event_key=event_key,
        subject=subject,
        body=body,
        branding=branding,
        embed_logo=False,
    )
    return {"subject": rendered.subject, "html": rendered.html, "text": rendered.text}


async def send_test(
    session: AsyncSession,
    user: User,
    *,
    event_key: str | None,
    subject: str,
    body: str,
    branding: BrandSettings | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Send the previewed email to the admin who asked, through the workspace's relay."""
    if not user.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Your login has no email address to send the test to.",
        )
    rendered = await _render(
        session,
        user,
        event_key=event_key,
        subject=subject,
        body=body,
        branding=branding,
        embed_logo=True,
    )
    adapter, relay = await dispatch.email_adapter_for(session, user.tenant_id)
    ok, error = True, None
    try:
        await adapter.send(
            OutboundMessage(
                recipient_name=user.full_name,
                recipient_email=user.email,
                recipient_phone=None,
                subject=f"[Test] {rendered.subject}",
                event_key=event_key or "system.email_test",
                **email_brand.as_message_fields(rendered),
            )
        )
    except (PermanentFailure, TransientFailure) as exc:
        ok, error = False, str(exc)[:1000]
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="email_test",
        entity_id=user.id,
        actor=user,
        changes={"event_key": event_key, "relay": relay, "ok": ok},
        summary=f"Test email {'sent' if ok else 'failed'}: {event_key or 'email design'}",
        request=request,
    )
    await session.commit()
    return {
        "ok": ok,
        "error": error,
        "sent_to": user.email if ok else None,
        # Said plainly: with no relay the "sent" test reached a log, not an inbox.
        "delivered": ok and relay != "console",
        "relay": relay,
        "at": datetime.now(UTC).isoformat(),
    }


async def reset_template(
    session: AsyncSession,
    user: User,
    *,
    event_key: str,
    channel: Channel,
    locale: str = "en-IN",
    request: Request | None = None,
) -> dict[str, Any]:
    """Go back to the built-in wording. The workspace's versions are kept, inactive."""
    rows = (
        await session.scalars(
            tenant_scoped(
                select(NotificationTemplate).where(
                    NotificationTemplate.event_key == event_key,
                    NotificationTemplate.channel == channel,
                    NotificationTemplate.locale == locale,
                    NotificationTemplate.active.is_(True),
                ),
                NotificationTemplate,
                user,
            )
        )
    ).all()
    if not rows:
        return {"event_key": event_key, "channel": channel.value, "retired": 0}
    if event_key not in dispatch.DEFAULT_TEMPLATES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This message has no built-in wording to go back to; without your version "
                "nothing would be sent. Edit it instead."
            ),
        )
    for row in rows:
        row.active = False
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="notification_template",
        entity_id=rows[0].id,
        actor=user,
        changes={"event_key": event_key, "channel": channel.value, "retired": len(rows)},
        summary=f"Template {event_key}/{channel.value} back to the built-in wording",
        request=request,
    )
    await session.commit()
    return {"event_key": event_key, "channel": channel.value, "retired": len(rows)}
