"""Declaration and proof window reminders (slice 07 §6).

The March TDS spike is legitimate — an unproven declaration contributes nothing
once the proof window closes — and it is avoidable only in advance. So:

- when a window opens, everyone with an employee record is told;
- on each configured day before it closes (`DeclarationWindow.reminder_days_before`),
  everyone is reminded;
- before the **proof** window closes, anyone whose accepted amount is still
  below what they declared is told exactly how far short they are.

There is no scheduler in this build. `send_window_reminders` is the function a
daily job calls; until one exists, HR runs it from the proofs screen. It is
idempotent per window and per reminder day — `reminders_sent` records what has
gone — so running it twice on the same day sends nothing twice.

No day count is a constant here: the reminder days are the window's own
configuration.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_filter
from app.models.audit import AuditAction
from app.models.tax import DeclarationStatus, DeclarationWindow, TaxDeclaration, WindowType
from app.services import audit
from app.services.filings import support
from app.services.notifications.dispatch import notify

ZERO = Decimal(0)

EVENT_OPENED = "tax.window.opened"
EVENT_CLOSING = "tax.window.closing"
EVENT_UNPROVEN = "tax.proof.unproven"

TEMPLATES: dict[str, tuple[str, str]] = {
    EVENT_OPENED: (
        "The {window} window for {financial_year} is open",
        "You can submit your tax {window}s for {financial_year} in AskMira until {closes_at}.",
    ),
    EVENT_CLOSING: (
        "{days_left} day(s) left: tax {window} window for {financial_year}",
        "The {window} window for {financial_year} closes on {closes_at}. After it "
        "closes, only what has been accepted counts towards your tax.",
    ),
    EVENT_UNPROVEN: (
        "Your tax proofs for {financial_year} are short",
        "You declared {declared} for {financial_year} and {accepted} has been accepted "
        "so far. The proof window closes on {closes_at}. After that only accepted "
        "amounts reduce your tax, which usually raises your March TDS.",
    ),
}


@dataclass(slots=True)
class ReminderOutcome:
    windows_checked: int = 0
    notifications_raised: int = 0
    events: list[str] = field(default_factory=list)


async def _employee_user_ids(
    session: AsyncSession, tenant_id: uuid.UUID
) -> dict[uuid.UUID, uuid.UUID]:
    """Active logins linked to an employee record: employee id → user id."""
    from app.models.user import User

    rows = (
        await session.execute(
            select(User.employee_id, User.id).where(
                tenant_filter(User, tenant_id),
                User.is_active.is_(True),
                User.employee_id.is_not(None),
            )
        )
    ).all()
    return {employee_id: user_id for employee_id, user_id in rows}


async def _shortfalls(
    session: AsyncSession, tenant_id: uuid.UUID, financial_year: str
) -> dict[uuid.UUID, tuple[Decimal, Decimal]]:
    """Employees whose accepted amount is below what they declared."""
    declarations = (
        await session.scalars(
            select(TaxDeclaration).where(
                tenant_filter(TaxDeclaration, tenant_id),
                TaxDeclaration.financial_year == financial_year,
                TaxDeclaration.status == DeclarationStatus.SUBMITTED,
            )
        )
    ).all()
    totals: dict[uuid.UUID, list[Decimal]] = defaultdict(lambda: [ZERO, ZERO])
    for declaration in declarations:
        totals[declaration.employee_id][0] += Decimal(str(declaration.declared_amount))
        totals[declaration.employee_id][1] += Decimal(str(declaration.accepted_amount))
    return {
        employee_id: (declared, accepted)
        for employee_id, (declared, accepted) in totals.items()
        if accepted < declared
    }


async def send_window_reminders(
    session: AsyncSession, tenant_id: uuid.UUID, *, today: date
) -> ReminderOutcome:
    """Raise every window notification due today, once. Commits.

    For the scheduler: call daily per tenant. Safe to call more than once a day.
    """
    await support.ensure_event_templates(session, tenant_id, TEMPLATES)
    outcome = ReminderOutcome()

    windows = (
        await session.scalars(
            select(DeclarationWindow).where(tenant_filter(DeclarationWindow, tenant_id))
        )
    ).all()
    recipients = await _employee_user_ids(session, tenant_id)

    for window in windows:
        if today > window.closes_at:
            continue
        outcome.windows_checked += 1
        sent = list(window.reminders_sent or [])
        marks: list[str] = []
        base = {
            "window": window.window_type.value,
            "financial_year": window.financial_year,
            "closes_at": window.closes_at.isoformat(),
        }

        if window.opens_at <= today and "opened" not in sent:
            result = await notify(
                session,
                tenant_id=tenant_id,
                event_key=EVENT_OPENED,
                recipient_user_ids=list(recipients.values()),
                payload=base,
                commit=False,
            )
            outcome.notifications_raised += result.queued + result.digested + result.deferred
            outcome.events.append(
                f"{EVENT_OPENED} {window.window_type.value} {window.financial_year}"
            )
            marks.append("opened")

        days_left = (window.closes_at - today).days
        marker = f"closing:{days_left}"
        if (
            window.opens_at <= today
            and days_left in (window.reminder_days_before or [])
            and marker not in sent
        ):
            result = await notify(
                session,
                tenant_id=tenant_id,
                event_key=EVENT_CLOSING,
                recipient_user_ids=list(recipients.values()),
                payload={**base, "days_left": days_left},
                commit=False,
            )
            outcome.notifications_raised += result.queued + result.digested + result.deferred
            outcome.events.append(
                f"{EVENT_CLOSING} {window.window_type.value} {window.financial_year} ({days_left})"
            )
            marks.append(marker)

            if window.window_type is WindowType.PROOF:
                for employee_id, (declared, accepted) in (
                    await _shortfalls(session, tenant_id, window.financial_year)
                ).items():
                    user_id = recipients.get(employee_id)
                    if user_id is None:
                        continue
                    result = await notify(
                        session,
                        tenant_id=tenant_id,
                        event_key=EVENT_UNPROVEN,
                        recipient_user_ids=[user_id],
                        payload={**base, "declared": str(declared), "accepted": str(accepted)},
                        commit=False,
                    )
                    outcome.notifications_raised += (
                        result.queued + result.digested + result.deferred
                    )
                    outcome.events.append(f"{EVENT_UNPROVEN} {window.financial_year}")

        if marks:
            # Reassigned rather than appended: an in-place change to a JSONB
            # list is invisible to the unit of work and would never be saved.
            window.reminders_sent = sent + marks

    await audit.record(
        session,
        tenant_id=tenant_id,
        action=AuditAction.CREATE,
        entity_type="declaration_window_reminder",
        actor_type="system",
        changes={
            "as_of": today.isoformat(),
            "windows_checked": outcome.windows_checked,
            "notifications_raised": outcome.notifications_raised,
        },
        summary=(
            f"Tax window reminders for {today.isoformat()}: "
            f"{outcome.notifications_raised} notification(s)"
        ),
    )
    await session.commit()
    return outcome
