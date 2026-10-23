"""The welcome email a new login gets, at the work address it signs in with.

What it carries:

- **Where and as whom to sign in** — the company's own sign-in page and the
  email address that is the login.
- **A link to set their own password**, single-use and valid for a week (a
  pre-joiner may be given a login well before they look at it). It is the same
  kind of link a forgotten password gets, so `reset_password` redeems it.
  Never the password itself: every notification's rendered body is stored,
  and a password must not sit in a table. The one-time password the admin was
  shown keeps working until either is used.
- **Their onboarding checklist**, when a plan has started for them. A joiner
  given a login after their plan started missed that plan's welcome, which
  went to nobody; this is where they learn of it.

A workspace that enforces single sign-on takes no passwords, so its welcome
says to sign in with the company account and carries no link.

The person may not have signed in yet, so it goes by email only, and it is
urgent: somebody is usually waiting for it, and quiet hours should not hold
it overnight.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_filter, tenant_scoped
from app.models.identity import IdentityProvider
from app.models.notifications import Channel, DigestMode, Notification, NotificationStatus
from app.models.onboarding import (
    OnboardingOwnerType,
    OnboardingPlan,
    OnboardingPlanStatus,
    OnboardingTask,
    OnboardingTaskStatus,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.services import auth as auth_service
from app.services.notifications.dispatch import email_relay, notify, register_event

__all__ = ["WELCOME_EVENT", "WELCOME_LINK_LIFETIME", "WelcomeEmail", "send_welcome"]

WELCOME_EVENT = "auth.login.created"
WELCOME_LINK_LIFETIME = timedelta(days=7)

register_event(
    WELCOME_EVENT,
    "Your AskMira login for {company}",
    "Hi {name},\n\n"
    "Your AskMira login at {company} has been set up by {inviter}. AskMira is where you "
    "punch in, apply for leave, see your payslips and raise questions with HR.\n\n"
    "Sign in at: {signin_link}\n"
    "Your login: {email}\n\n"
    "{password_step}\n\n"
    "{onboarding}"
    "If you were not expecting this, let {inviter} know.",
    preferences={Channel.EMAIL: DigestMode.IMMEDIATE},
)


@dataclass(slots=True)
class WelcomeEmail:
    """What became of the welcome email, for the admin who created the login."""

    status: Literal["sent", "queued", "failed", "no_relay"]
    detail: str
    sent_to: str
    link_expires_at: datetime | None


async def _sso_only(session: AsyncSession, target: User, tenant: Tenant) -> bool:
    enforced = await session.scalar(
        select(IdentityProvider.id).where(
            tenant_filter(IdentityProvider, target),
            IdentityProvider.enabled.is_(True),
            IdentityProvider.enforce_sso.is_(True),
        )
    )
    if enforced is None:
        return False
    # The break-glass account keeps its password even then.
    break_glass = str((tenant.settings or {}).get("break_glass_email") or "").lower()
    return target.email.lower() != break_glass


async def _onboarding_line(session: AsyncSession, target: User) -> str:
    """One paragraph on their live onboarding plan, or nothing."""
    if target.employee_id is None:
        return ""
    plan = await session.scalar(
        tenant_scoped(
            select(OnboardingPlan)
            .where(
                OnboardingPlan.employee_id == target.employee_id,
                OnboardingPlan.status == OnboardingPlanStatus.IN_PROGRESS,
            )
            .order_by(OnboardingPlan.created_at.desc())
            .limit(1),
            OnboardingPlan,
            target,
        )
    )
    if plan is None:
        return ""
    open_tasks = int(
        await session.scalar(
            tenant_scoped(
                select(func.count(OnboardingTask.id)).where(
                    OnboardingTask.plan_id == plan.id,
                    OnboardingTask.owner_type == OnboardingOwnerType.NEW_JOINER,
                    OnboardingTask.status == OnboardingTaskStatus.PENDING,
                ),
                OnboardingTask,
                target,
            )
        )
        or 0
    )
    joining = f"{plan.joining_date.day} {plan.joining_date:%B %Y}"
    if open_tasks == 0:
        return (
            f"Your onboarding plan for joining on {joining} is ready. After you sign in, open "
            "My onboarding to see it.\n\n"
        )
    return (
        f"Your onboarding checklist is ready: {open_tasks} task(s) for you around your joining "
        f"date of {joining}. After you sign in, open My onboarding to get started.\n\n"
    )


async def send_welcome(
    session: AsyncSession,
    *,
    target: User,
    inviter_name: str,
    request: Request | None = None,
    deliver: bool = True,
) -> WelcomeEmail:
    """Email a new login its sign-in details. Does not commit; the caller does.

    Raised in the same transaction as the login it welcomes, so a login that
    is rolled back leaves no live link behind. `deliver=False` queues it for
    the delivery job, for logins created many at a time.
    """
    tenant = await session.get(Tenant, target.tenant_id)
    company = tenant.name if tenant else "your company"
    base = auth_service.web_app_url()
    signin_link = f"{base}/signin/{tenant.slug}" if tenant else f"{base}/signin"

    expires_at: datetime | None = None
    if tenant is not None and await _sso_only(session, target, tenant):
        password_step = (
            f"{company} signs in with its company account, so there is no AskMira password "
            "to set: choose your company's sign-in on that page."
        )
    else:
        token = await auth_service.issue_password_link(
            session, target, lifetime=WELCOME_LINK_LIFETIME, request=request
        )
        expires_at = datetime.now(UTC) + WELCOME_LINK_LIFETIME
        password_step = (
            "First, choose your password here. The link works once and expires in "
            f"{WELCOME_LINK_LIFETIME.days} days:\n"
            f"{base}/reset-password?token={token}&welcome=1"
        )

    outcome = await notify(
        session,
        tenant_id=target.tenant_id,
        event_key=WELCOME_EVENT,
        recipient_user_ids=[target.id],
        payload={
            "name": target.full_name.split(" ")[0],
            "company": company,
            "inviter": inviter_name,
            "email": target.email,
            "signin_link": signin_link,
            "password_step": password_step,
            "onboarding": await _onboarding_line(session, target),
        },
        commit=False,
        deliver=deliver,
    )

    # The row says what happened; the outcome's counts do not tell a transient
    # failure (queued for a retry) from a permanent one.
    row = await session.scalar(
        tenant_scoped(
            select(Notification)
            .where(
                Notification.recipient_user_id == target.id,
                Notification.event_key == WELCOME_EVENT,
                Notification.channel == Channel.EMAIL,
            )
            .order_by(Notification.created_at.desc(), Notification.attempt_count.desc())
            .limit(1),
            Notification,
            target,
        )
    )
    status: Literal["sent", "queued", "failed", "no_relay"]
    if row is None:
        status = "failed"
        detail = (
            "; ".join(outcome.errors)
            or "Nothing was sent: email for this event is switched off in this workspace."
        )
    elif row.status in (NotificationStatus.SENT, NotificationStatus.DELIVERED):
        if await email_relay(session, target.tenant_id) == "console":
            status = "no_relay"
            detail = (
                "No mail relay is set up, so it was only written to the server log. Set one "
                "up under Settings → Notifications, or share the one-time password yourself."
            )
        else:
            status, detail = "sent", f"Sent to {target.email}."
    elif row.status is NotificationStatus.FAILED:
        status = "failed"
        detail = (
            f"It could not be sent ({row.failure_reason or 'no reason given'}). Share the "
            "one-time password yourself, or resend the welcome once mail is working."
        )
    else:
        status = "queued"
        detail = "Queued; it goes out with the next delivery attempt." + (
            f" Last attempt: {row.failure_reason}" if row.failure_reason else ""
        )
    return WelcomeEmail(
        status=status, detail=detail, sent_to=target.email, link_expires_at=expires_at
    )
