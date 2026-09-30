"""The helpdesk's pure rules: numbering, SLA clocks and who may move a ticket where.

No session, no clock of its own — `now` is always passed in — so every rule
here is testable without a database and cannot drift between the API and the
scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.models.helpdesk import HelpdeskRequestKind, HelpdeskTicketStatus

__all__ = [
    "ACTIVE_STATUSES",
    "AUTO_CLOSE_AFTER",
    "REOPEN_WINDOW",
    "STANDARD_CATEGORIES",
    "TERMINAL_STATUSES",
    "StandardCategory",
    "agent_may_move",
    "can_reopen",
    "compute_due_at",
    "due_soon_threshold",
    "format_ticket_number",
    "sla_state",
]

TICKET_PREFIX = "HD"

#: Being worked, or waiting to be. The SLA clock is running on these.
ACTIVE_STATUSES: frozenset[HelpdeskTicketStatus] = frozenset(
    {
        HelpdeskTicketStatus.OPEN,
        HelpdeskTicketStatus.IN_PROGRESS,
        HelpdeskTicketStatus.WAITING_ON_EMPLOYEE,
    }
)

#: Nothing more happens to these.
TERMINAL_STATUSES: frozenset[HelpdeskTicketStatus] = frozenset(
    {HelpdeskTicketStatus.CLOSED, HelpdeskTicketStatus.CANCELLED}
)

#: How long after resolution the employee may reopen, and after which the
#: scheduler closes a resolved ticket nobody has rated.
REOPEN_WINDOW = timedelta(days=7)
AUTO_CLOSE_AFTER = timedelta(days=7)

#: A ticket is "due soon" inside this much of its deadline, or inside a quarter
#: of its SLA if that is shorter — a four-hour SLA is not "due soon" from birth.
DUE_SOON_CAP = timedelta(hours=4)

_AGENT_MOVES: dict[HelpdeskTicketStatus, frozenset[HelpdeskTicketStatus]] = {
    HelpdeskTicketStatus.OPEN: frozenset(
        {
            HelpdeskTicketStatus.IN_PROGRESS,
            HelpdeskTicketStatus.WAITING_ON_EMPLOYEE,
            HelpdeskTicketStatus.RESOLVED,
            HelpdeskTicketStatus.CLOSED,
        }
    ),
    HelpdeskTicketStatus.IN_PROGRESS: frozenset(
        {
            HelpdeskTicketStatus.OPEN,
            HelpdeskTicketStatus.WAITING_ON_EMPLOYEE,
            HelpdeskTicketStatus.RESOLVED,
            HelpdeskTicketStatus.CLOSED,
        }
    ),
    HelpdeskTicketStatus.WAITING_ON_EMPLOYEE: frozenset(
        {
            HelpdeskTicketStatus.OPEN,
            HelpdeskTicketStatus.IN_PROGRESS,
            HelpdeskTicketStatus.RESOLVED,
            HelpdeskTicketStatus.CLOSED,
        }
    ),
    # An agent may take a resolution back, or close it early.
    HelpdeskTicketStatus.RESOLVED: frozenset(
        {HelpdeskTicketStatus.IN_PROGRESS, HelpdeskTicketStatus.CLOSED}
    ),
    HelpdeskTicketStatus.CLOSED: frozenset(),
    HelpdeskTicketStatus.CANCELLED: frozenset(),
}


def format_ticket_number(seq: int) -> str:
    """`HD-00042`. Five digits, growing past them rather than wrapping."""
    if seq < 1:
        raise ValueError("A ticket sequence starts at 1.")
    return f"{TICKET_PREFIX}-{seq:05d}"


def compute_due_at(raised_at: datetime, sla_hours: int) -> datetime:
    """When the resolution is due.

    Calendar hours, not business hours: a ticket raised at 6 pm on Friday with
    a 24-hour SLA is due at 6 pm on Saturday. Holidays and working hours are
    deliberately not consulted — the queue shows the deadline it computed, and
    a team that works weekdays sets its SLAs with that in mind.
    """
    if raised_at.tzinfo is None:
        raise ValueError("raised_at must be timezone-aware.")
    if sla_hours < 1:
        raise ValueError("An SLA is at least one hour.")
    return raised_at + timedelta(hours=sla_hours)


def due_soon_threshold(sla_hours: int) -> timedelta:
    return min(DUE_SOON_CAP, timedelta(hours=sla_hours) / 4)


def sla_state(
    *,
    status: HelpdeskTicketStatus,
    due_at: datetime,
    sla_hours: int,
    resolved_at: datetime | None,
    now: datetime,
) -> str:
    """Where a ticket stands against its deadline.

    `overdue` / `due_soon` / `on_track` while it is being worked; `met` or
    `breached` once resolved; `none` for a ticket cancelled before resolution.
    """
    if resolved_at is not None:
        return "met" if resolved_at <= due_at else "breached"
    if status not in ACTIVE_STATUSES:
        return "none"
    if now > due_at:
        return "overdue"
    if due_at - now <= due_soon_threshold(sla_hours):
        return "due_soon"
    return "on_track"


def agent_may_move(current: HelpdeskTicketStatus, target: HelpdeskTicketStatus) -> bool:
    return target in _AGENT_MOVES.get(current, frozenset())


def can_reopen(
    *,
    status: HelpdeskTicketStatus,
    resolved_at: datetime | None,
    rated_at: datetime | None,
    now: datetime,
) -> bool:
    """Within seven days of resolution, and not after the employee rated it —
    a rating is the employee accepting the resolution."""
    if status not in {HelpdeskTicketStatus.RESOLVED, HelpdeskTicketStatus.CLOSED}:
        return False
    if resolved_at is None or rated_at is not None:
        return False
    return now - resolved_at <= REOPEN_WINDOW


@dataclass(frozen=True, slots=True)
class StandardCategory:
    name: str
    description: str
    request_kind: HelpdeskRequestKind
    sla_hours: int
    is_confidential: bool = False


#: The set an Indian HR team usually starts with. Added only when an admin asks
#: for it; SLAs are a starting point the admin edits, not a recommendation.
STANDARD_CATEGORIES: tuple[StandardCategory, ...] = (
    StandardCategory(
        "Payroll & salary query",
        "Salary credited short, a deduction you do not recognise, PF, TDS or Form 16.",
        HelpdeskRequestKind.PAYROLL,
        48,
    ),
    StandardCategory(
        "Leave & attendance",
        "Leave balance corrections, missed punches and attendance regularisation.",
        HelpdeskRequestKind.GENERAL,
        24,
    ),
    StandardCategory(
        "Letters & documents",
        "Employment, address-proof, salary-certificate and experience letters.",
        HelpdeskRequestKind.LETTER,
        72,
    ),
    StandardCategory(
        "ID card",
        "A new, lost or damaged ID or access card.",
        HelpdeskRequestKind.ID_CARD,
        72,
    ),
    StandardCategory(
        "IT & access",
        "Laptop, email, VPN and application access.",
        HelpdeskRequestKind.IT,
        24,
    ),
    StandardCategory(
        "Reimbursement",
        "A reimbursement not yet paid, or a bill that was not accepted.",
        HelpdeskRequestKind.PAYROLL,
        72,
    ),
    StandardCategory(
        "Policy question",
        "How a company policy applies to you.",
        HelpdeskRequestKind.POLICY,
        48,
    ),
    StandardCategory(
        "Grievance (confidential)",
        "Raise a concern in confidence. Seen only by you, the category owner and HR "
        "admins. A sexual-harassment complaint also has a statutory route: the Internal "
        "Committee under the POSH Act.",
        HelpdeskRequestKind.GRIEVANCE,
        72,
        is_confidential=True,
    ),
)
