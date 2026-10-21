"""Approve or reject from the approval email.

An approval email (a missed punch, a leave request, a timesheet...) carries
Approve and Reject buttons. Each is a link with a signed token naming one
approver and one request. The link never decides anything by itself: mail
scanners open links, so a GET only shows the request and a comment box, and
the decision is a POST from that page.

At the moment of the decision, the token's approver must still be active, the
link must be newer than their last forced logout or password change, the
request must still be waiting on them — read from the same queue their
Approvals inbox reads — and the queue's own `decide` runs as them, with its
own checks and its own audit row, marked "via email link".

Kept out of the email entirely when the workspace switched it off (Settings →
Notifications → Email design), or when its policy requires two-step sign-in of
the approver: a link must not be a way round a second factor.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import jwt
from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import has_direct_reports, tenant_filter
from app.core.security import create_email_action_token, decode_token
from app.models.tenant import Tenant
from app.models.user import User
from app.services.hiring.public_guard import Limit, throttle
from app.services.notifications import email_brand
from app.services.notifications.email_layout import EmailAction

__all__ = [
    "APPROVAL_EVENTS",
    "EMAIL_QUEUES",
    "ApprovalRef",
    "buttons_for",
    "decide",
    "describe",
    "sample_buttons",
]

logger = logging.getLogger("askmira.approvals")

#: The emails that ask someone to decide, and so carry Approve / Reject.
APPROVAL_EVENTS: frozenset[str] = frozenset(
    {
        "leave.approval.requested",
        "leave.compoff.requested",
        "attendance.regularisation.requested",
        "attendance.shift_swap.to_approve",
        "timesheets.submitted",
        "expenses.claim.awaiting_manager",
        "expenses.travel.awaiting_manager",
        "assets.request.submitted",
    }
)


def sample_buttons() -> tuple[EmailAction, ...]:
    """The buttons as an approver sees them, for a preview. They lead nowhere."""
    base = (email_brand.portal_url() or "https://askmira.example").rstrip("/")
    return (
        EmailAction(label="Approve", url=f"{base}/act/preview?do=approve"),
        EmailAction(label="Reject", url=f"{base}/act/preview?do=reject", kind="secondary"),
    )


READ_LIMIT = Limit("email_action_read", 60, 600)
DECIDE_LIMIT = Limit("email_action_decide", 20, 600)

#: (session, approver, item id, approve, note, request) -> the queue's own decide.
Decider = Callable[
    [AsyncSession, User, uuid.UUID, bool, str | None, Request | None], Awaitable[Any]
]


@dataclass(frozen=True, slots=True)
class ApprovalRef:
    """Which request an approval email is about, for its Approve / Reject links."""

    queue: str
    item_id: uuid.UUID

    def as_payload(self) -> dict[str, str]:
        return {"queue": self.queue, "item_id": str(self.item_id)}


async def _leave(session, user, item_id, approve, note, request):
    from app.services.leave import service

    await service.decide(session, user, item_id, approve=approve, note=note, request=request)


async def _comp_off(session, user, item_id, approve, note, request):
    from app.services.leave import comp_off

    await comp_off.decide(session, user, item_id, approve=approve, note=note, request=request)


async def _regularisation(session, user, item_id, approve, note, request):
    from app.services.attendance import service

    await service.decide_regularisation(
        session, user, item_id, approve=approve, note=note, request=request
    )


async def _timesheet(session, user, item_id, approve, note, request):
    from app.services import timesheets

    await timesheets.decide(session, user, item_id, approve=approve, note=note, request=request)


async def _shift_swap(session, user, item_id, approve, note, request):
    from app.services.attendance import shift_swaps

    await shift_swaps.decide_swap(
        session, user, item_id, approve=approve, note=note, request=request
    )


async def _overtime(session, user, item_id, approve, note, request):
    from app.services.attendance import overtime

    await overtime.decide(session, user, item_id, approve_it=approve, note=note, request=request)


async def _expense_claim(session, user, item_id, approve, note, request):
    from app.services.expenses import service

    await service.decide_claim_as_manager(
        session, user, claim_id=item_id, approve=approve, note=note, request=request
    )


async def _travel(session, user, item_id, approve, note, request):
    from app.services.expenses import service

    await service.decide_travel_request_as_manager(
        session, user, travel_request_id=item_id, approve=approve, note=note, request=request
    )


async def _asset(session, user, item_id, approve, note, request):
    from app.services.assets import service

    await service.decide_request(
        session, user, item_id, approve=approve, note=note, request=request
    )


#: The queues an approver may decide from email, by their Approvals inbox key.
#: Money-moving and statutory decisions (payroll, loans, settlements) are left
#: out on purpose: they ask for the password again, which an email cannot.
EMAIL_QUEUES: dict[str, Decider] = {
    "leave": _leave,
    "leave_comp_off": _comp_off,
    "attendance_regularisation": _regularisation,
    "timesheet": _timesheet,
    "shift_swap": _shift_swap,
    "overtime": _overtime,
    "expense_claim_manager": _expense_claim,
    "travel_request_manager": _travel,
    "asset_request": _asset,
}

#: The portal's own decide endpoint area per queue. A sub-admin's rights are
#: read from the request path, and the email link's path is in no area, so the
#: rights are checked as if the decision came through the portal.
_DECIDE_PATHS: dict[str, str] = {
    "leave": "/api/v1/leave/requests/decide",
    "leave_comp_off": "/api/v1/leave/comp-off/decide",
    "attendance_regularisation": "/api/v1/attendance/regularisations/decide",
    "timesheet": "/api/v1/timesheets/decide",
    "shift_swap": "/api/v1/attendance/shift-swaps/decide",
    "overtime": "/api/v1/attendance/overtime/decide",
    "expense_claim_manager": "/api/v1/expenses/claims/decide",
    "travel_request_manager": "/api/v1/expenses/travel/decide",
    "asset_request": "/api/v1/assets/requests/decide",
}


def _gone(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_410_GONE, detail=detail)


async def _two_step_applies(session: AsyncSession, user: User) -> bool:
    from app.models.opsec import TenantSessionPolicy
    from app.services.opsec.enforcement import two_step_applies

    row = await session.scalar(
        select(TenantSessionPolicy).where(tenant_filter(TenantSessionPolicy, user.tenant_id))
    )
    return two_step_applies(row, user)


async def buttons_for(
    session: AsyncSession, user: User, approval: dict[str, Any] | None
) -> tuple[EmailAction, ...]:
    """Approve and Reject buttons for this approver's copy of an approval email.

    Empty when the request cannot be decided from email: an unsupported queue,
    the workspace turned it off, or the approver must use two-step sign-in.
    """
    if not approval:
        return ()
    queue = str(approval.get("queue") or "")
    item = str(approval.get("item_id") or "")
    if queue not in EMAIL_QUEUES or not item:
        return ()
    chosen = await email_brand.get_settings(session, user.tenant_id)
    if not chosen.approve_from_email or await _two_step_applies(session, user):
        return ()
    base = email_brand.portal_url()
    if not base:
        return ()
    token = create_email_action_token(
        user_id=str(user.id), tenant_id=str(user.tenant_id), queue=queue, item_id=item
    )
    link = f"{base.rstrip('/')}/act/{token}"
    return (
        EmailAction(label="Approve", url=f"{link}?do=approve"),
        EmailAction(label="Reject", url=f"{link}?do=reject", kind="secondary"),
    )


async def _resolve(
    session: AsyncSession, token: str, request: Request
) -> tuple[User, str, uuid.UUID, Tenant]:
    """The approver, queue and request a link names, or 410 with the reason."""
    try:
        claims = decode_token(token, expected_type="email_action")
    except jwt.ExpiredSignatureError as exc:
        raise _gone(
            "This link has expired. Open AskMira to decide; the request is in your Approvals."
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise _gone("This link is not valid. Open AskMira to decide.") from exc

    queue = str(claims.get("q") or "")
    try:
        user_id = uuid.UUID(str(claims.get("sub")))
        item_id = uuid.UUID(str(claims.get("i")))
        tenant_id = uuid.UUID(str(claims.get("tid")))
    except ValueError as exc:
        raise _gone("This link is not valid. Open AskMira to decide.") from exc
    if queue not in EMAIL_QUEUES:
        raise _gone("This kind of request is decided in AskMira. Open your Approvals.")

    user = await session.get(User, user_id)
    tenant = await session.get(Tenant, tenant_id)
    if user is None or not user.is_active or user.tenant_id != tenant_id:
        raise _gone("This link is no longer valid for your login.")
    if tenant is None or not tenant.is_active:
        raise _gone("This workspace is not active.")
    issued = claims.get("iat")
    if user.tokens_valid_from and issued and int(issued) < int(user.tokens_valid_from.timestamp()):
        raise _gone("You were signed out everywhere since this email was sent. Open AskMira.")
    chosen = await email_brand.get_settings(session, tenant_id)
    if not chosen.approve_from_email:
        raise _gone("Your company decides requests in AskMira, not from email. Open Approvals.")
    if await _two_step_applies(session, user):
        raise _gone("Your company requires two-step sign-in, so decide this in AskMira.")

    # The rest of the context a signed-in request carries.
    from app.core.access_control import load_request_access
    from app.models.opsec import TenantSessionPolicy
    from app.services.opsec.enforcement import _enforce_allowlist

    policy = await session.scalar(
        select(TenantSessionPolicy).where(tenant_filter(TenantSessionPolicy, tenant_id))
    )
    if policy is not None:
        await _enforce_allowlist(request, session, user, policy)
    user.has_reports = await has_direct_reports(session, user)
    as_portal = Request({**request.scope, "path": _DECIDE_PATHS[queue], "method": "POST"})
    await load_request_access(as_portal, session, user)
    return user, queue, item_id, tenant


async def _waiting_item(
    session: AsyncSession, user: User, queue: str, item_id: uuid.UUID
) -> tuple[Any, dict[str, Any] | None]:
    """The inbox entry for this request, if it is still waiting on this approver."""
    from app.services.approvals.service import SOURCES

    source = next((s for s in SOURCES if s.key == queue), None)
    if source is None:
        return None, None
    tenant = await session.get(Tenant, user.tenant_id)
    modules = (tenant.enabled_modules or {}) if tenant else {}
    if source.module and not modules.get(source.module):
        return source, None
    async with session.begin_nested():
        found = await source.fetch(session, user)
    entry = next((row for row in found if str(row.get("id")) == str(item_id)), None)
    return source, entry


async def describe(session: AsyncSession, token: str, request: Request) -> dict[str, Any]:
    """What the link is about, for the page it opens. Changes nothing."""
    throttle(request, READ_LIMIT)
    user, queue, item_id, tenant = await _resolve(session, token, request)
    source, entry = await _waiting_item(session, user, queue, item_id)
    from app.services.identity import directory

    base = email_brand.portal_url().rstrip("/")
    link = (entry or {}).get("link") or (source.link if source else "/approvals")
    submitted = (entry or {}).get("submitted_at")
    return {
        "state": "waiting" if entry else "not_waiting",
        "company_name": tenant.name,
        "logo_url": directory.logo_url(tenant),
        "approver_name": user.full_name,
        "queue_label": source.label if source else "Request",
        "title": (entry or {}).get("title"),
        "subject_name": (entry or {}).get("subject_name"),
        "detail": (entry or {}).get("detail"),
        "submitted_at": submitted.isoformat() if isinstance(submitted, datetime) else submitted,
        "open_url": f"{base}{link}" if base else link,
    }


async def decide(
    session: AsyncSession,
    token: str,
    request: Request,
    *,
    approve: bool,
    comment: str | None,
) -> dict[str, Any]:
    """Decide the request the link names, as its approver."""
    throttle(request, DECIDE_LIMIT)
    user, queue, item_id, _tenant = await _resolve(session, token, request)
    _source, entry = await _waiting_item(session, user, queue, item_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This request is no longer waiting on you: it was decided, withdrawn or "
                "passed to someone else."
            ),
        )
    note = (comment or "").strip() or None
    user.decided_via = "email_link"
    await EMAIL_QUEUES[queue](session, user, item_id, approve, note, request)
    # Every queue's decide commits; this makes sure nothing is left pending if one does not.
    await session.commit()
    logger.info(
        "Email-link decision: tenant=%s user=%s queue=%s item=%s approve=%s",
        user.tenant_id,
        user.id,
        queue,
        item_id,
        approve,
    )
    return {
        "state": "decided",
        "decision": "approved" if approve else "rejected",
        "title": entry.get("title"),
        "subject_name": entry.get("subject_name"),
        "at": datetime.now(UTC).isoformat(),
    }
