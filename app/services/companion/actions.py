"""What Ask Mira can do for a person, and the rule that it never does it unasked.

Every action is two steps:

1. **Propose.** The tool checks everything the module would check — the leave
   balance, holidays and overlaps (leave's own preview), whether a request is
   still waiting on this approver (the approvals inbox) — and saves a
   `CompanionAction` with what would be done. The person sees a card with the
   figures and a Confirm button. Nothing has changed yet.
2. **Confirm.** The person presses Confirm, or says yes in a later message (the
   model then calls `confirm_action`). The module's own service runs as them —
   `leave.apply`, `attendance.raise_regularisation`, the approval queue's
   decide, `helpdesk.create_ticket` — with its own checks, approval chain,
   notifications and audit row, marked "via ask_mira". The action row is marked
   done in the same transaction as that change.

A typed yes is accepted only for an action proposed in an earlier turn of the
same conversation, and only when the person's latest message reads as a yes.
The model cannot propose and confirm in one breath, and a line in some record
it read cannot confirm for anybody.

Decisions that move money or file with the government (payroll, loans,
settlements) are not here: they ask for the password again in the portal, as
they do from an approval email.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import access_control, permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.companion import CompanionAction, CompanionActionStatus
from app.models.employee import Employee
from app.models.leave import LeaveRequest, LeaveRequestStatus
from app.models.user import User
from app.services import audit

__all__ = [
    "KINDS",
    "ToolContext",
    "cancel",
    "card_for",
    "confirm",
    "is_affirmative",
    "propose_decision",
    "propose_hr_ticket",
    "propose_leave",
    "propose_leave_cancel",
    "propose_regularisation",
    "refresh_cards",
]

#: How long an offer stands. Balances and queues move; an old offer is asked again.
OFFER_LIFETIME = timedelta(hours=2)
#: The most requests one "approve these" may carry.
MAX_DECISIONS = 20

KINDS = ("leave_request", "leave_cancel", "regularisation", "approval_decision", "hr_ticket")

#: Where each kind's own endpoint lives, so a sub-admin's rights are read as the
#: portal would read them for the same change.
_PATHS = {
    "leave_request": "/api/v1/leave/requests",
    "leave_cancel": "/api/v1/leave/requests/cancel",
    "regularisation": "/api/v1/attendance/regularisations",
    "hr_ticket": "/api/v1/helpdesk/tickets",
}
_HOME_PATH = "/api/v1/companion/messages"


@dataclass(slots=True)
class ToolContext:
    """What a tool needs to know about the turn it runs in."""

    conversation_id: uuid.UUID
    #: When this turn began: an action proposed after it cannot be confirmed in it.
    turn_started: datetime
    #: The person's words this turn, for the typed-yes check.
    latest_text: str
    request: Request | None = None


# ── Cards ────────────────────────────────────────────────────────────────────


def _notice(text: str, tone: str = "info") -> dict[str, Any]:
    return {"type": "notice", "tone": tone, "text": text}


def _day(value: date) -> str:
    return f"{value:%a %d %b %Y}"


def _span(start: date, end: date) -> str:
    return _day(start) if start == end else f"{_day(start)} to {_day(end)}"


def _num(value: Any) -> str:
    text = f"{value}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def card_for(action: CompanionAction) -> dict[str, Any]:
    """The card a proposal shows, with its state now."""
    now = datetime.now(UTC)
    expired = action.status is CompanionActionStatus.PROPOSED and action.expires_at <= now
    return {
        "type": "proposal",
        "action_id": str(action.id),
        "kind": action.kind,
        "status": "expired" if expired else action.status.value,
        "expires_at": action.expires_at.isoformat(),
        **action.summary,
        "result": action.result,
    }


async def refresh_cards(
    session: AsyncSession, user: User, cards: list[dict[str, Any]], *, owner_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Saved cards with each proposal's current state (confirmed, cancelled, expired)."""
    ids = []
    for card in cards:
        if card.get("type") == "proposal":
            try:
                ids.append(uuid.UUID(str(card.get("action_id"))))
            except ValueError:
                continue
    if not ids:
        return cards
    rows = {
        str(row.id): row
        for row in await session.scalars(
            tenant_scoped(
                select(CompanionAction).where(
                    CompanionAction.id.in_(ids), CompanionAction.user_id == owner_id
                ),
                CompanionAction,
                user,
            )
        )
    }
    return [
        card_for(rows[card["action_id"]])
        if card.get("type") == "proposal" and card.get("action_id") in rows
        else card
        for card in cards
    ]


async def _offer(
    session: AsyncSession,
    user: User,
    ctx: ToolContext,
    *,
    kind: str,
    payload: dict[str, Any],
    summary: dict[str, Any],
) -> CompanionAction:
    now = datetime.now(UTC)
    action = CompanionAction(
        tenant_id=user.tenant_id,
        conversation_id=ctx.conversation_id,
        user_id=user.id,
        kind=kind,
        payload=payload,
        summary=summary,
        status=CompanionActionStatus.PROPOSED,
        # The app's clock, like the turn's start it is compared with, not the database's.
        created_at=now,
        expires_at=now + OFFER_LIFETIME,
    )
    session.add(action)
    await session.flush()
    return action


def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def _parse_time(value: Any) -> time | None:
    raw = str(value or "").strip().lower().replace(".", ":")
    if not raw:
        return None
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", raw)
    if not match:
        raise ValueError(f"'{value}' is not a time. Give it like 09:30 or 6:15 pm.")
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if match.group(3) == "pm" and hour < 12:
        hour += 12
    if match.group(3) == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        raise ValueError(f"'{value}' is not a time of day.")
    return time(hour, minute)


def _own_employee(user: User) -> uuid.UUID:
    if user.employee_id is None:
        raise HTTPException(
            status_code=422,
            detail="Your login is not linked to an employee record, so this cannot be done.",
        )
    return user.employee_id


# ── Leave ───────────────────────────────────────────────────────────────────


def _match_type(balances: list[Any], wanted: str) -> list[Any]:
    """Leave types matching what was typed: the code exactly, else words of the name."""
    key = re.sub(r"[^a-z0-9 ]", " ", wanted.lower()).strip()
    if not key:
        return []
    exact = [b for b in balances if b.leave_type_code.lower() == key.replace(" ", "")]
    if exact:
        return exact
    words = [w for w in key.split() if w not in {"leave", "leaves", "day", "days"}] or key.split()
    return [b for b in balances if all(w in b.leave_type_name.lower() for w in words)]


def _balances_card(balances: list[Any], title: str, note: str) -> dict[str, Any]:
    rows = [
        [b.leave_type_name, b.leave_type_code, _num(b.available), _num(b.pending)]
        for b in balances
        if not b.is_statutory
    ]
    return {
        "type": "record",
        "title": title,
        "subtitle": "",
        "heading": "",
        "figures": [],
        "table": {"columns": ["Leave", "Code", "Available", "Waiting for approval"], "rows": rows},
        "note": note,
        "open": "/time",
        "open_label": "Open Time off",
    }


async def propose_leave(
    session: AsyncSession, user: User, inputs: dict[str, Any], ctx: ToolContext
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Check a leave request the way the Time page does, and offer to send it for approval."""
    from app.schemas.leave import LeaveRequestCreate
    from app.services.leave import service as leave

    start = _parse_day(inputs.get("from"))
    end = _parse_day(inputs.get("to")) or start
    if start is None:
        return [_notice("Which day (or days) is the leave for?")], {}
    if end < start:
        return [_notice("The last day of the leave is before the first.", "warning")], {}
    half = bool(inputs.get("half_day"))
    if half and end != start:
        return [_notice("A half day is one date. Give one day for a half day.", "warning")], {}
    session_choice = str(inputs.get("half_day_session") or "none")
    reason = str(inputs.get("reason") or "").strip()[:2000] or None

    employee_id = _own_employee(user)
    balances = await leave.balances(session, user, employee_id=employee_id, year=start.year)
    usable = [b for b in balances if not b.is_statutory]
    wanted = str(inputs.get("leave_type") or "").strip()
    matches = _match_type(usable, wanted) if wanted else []
    used = {"from": start.isoformat(), "to": end.isoformat(), "leave_type": wanted}
    if len(matches) != 1:
        if not usable:
            return [
                _notice(
                    f"You have no leave types set up for {start.year}. Ask HR to assign a "
                    "leave policy.",
                    "warning",
                )
            ], used
        note = (
            "Which type should it be?"
            if not wanted
            else f"No single leave type matches “{wanted}”. Which one should it be?"
        )
        return [_balances_card(usable, f"Your leave balance for {start.year}", note)], used
    chosen = matches[0]

    payload = LeaveRequestCreate(
        leave_type_id=chosen.leave_type_id,
        start_date=start,
        end_date=end,
        is_half_day=half,
        half_day_session=session_choice if half and session_choice != "none" else None,
        reason=reason,
    )
    preview = await leave.preview(session, user, payload)
    days = _num(preview.total_days)
    figures = [
        {"label": "Leave", "value": chosen.leave_type_name},
        {"label": "Dates", "value": _span(start, end) + (" (half day)" if half else "")},
        {"label": "Days counted", "value": days},
    ]
    if preview.available_before is not None:
        waiting = preview.pending_days or 0
        figures.append(
            {
                "label": "Available now",
                "value": _num(preview.available_before)
                + (f" ({_num(waiting)} waiting for approval)" if waiting else ""),
            }
        )
    if preview.available_after is not None:
        figures.append({"label": "Left after this", "value": _num(preview.available_after)})
    skipped = [f"{h.name} ({h.date:%d %b})" for h in preview.holidays]
    notes = list(preview.warnings)
    if skipped:
        notes.append("Holidays not counted: " + ", ".join(skipped) + ".")
    if preview.weekly_offs:
        notes.append(f"{len(preview.weekly_offs)} weekly off day(s) not counted.")
    if not preview.allowed:
        return [
            {
                "type": "record",
                "title": "This leave cannot be applied",
                "subtitle": "",
                "heading": chosen.leave_type_name,
                "figures": figures,
                "note": " ".join([*preview.problems, *notes]),
                "open": "/time",
                "open_label": "Open Time off",
            }
        ], used
    approvers = ", ".join(preview.approvers) or "HR"
    figures.append({"label": "Goes for approval to", "value": approvers})
    action = await _offer(
        session,
        user,
        ctx,
        kind="leave_request",
        payload=payload.model_dump(mode="json"),
        summary={
            "title": "Leave request",
            "figures": figures,
            "notes": notes,
            "confirm_label": "Send for approval",
            "question": f"Send this request to {approvers} for approval?",
        },
    )
    return [card_for(action)], used


async def propose_leave_cancel(
    session: AsyncSession, user: User, inputs: dict[str, Any], ctx: ToolContext
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Find the person's own pending or approved leave on a day, and offer to withdraw it."""
    day = _parse_day(inputs.get("from"))
    if day is None:
        return [_notice("Which day's leave should be withdrawn?")], {}
    employee_id = _own_employee(user)
    rows = (
        await session.scalars(
            tenant_scoped(
                select(LeaveRequest)
                .where(
                    LeaveRequest.employee_id == employee_id,
                    LeaveRequest.status.in_(
                        (LeaveRequestStatus.PENDING, LeaveRequestStatus.APPROVED)
                    ),
                    LeaveRequest.start_date <= day,
                    LeaveRequest.end_date >= day,
                )
                .order_by(LeaveRequest.start_date),
                LeaveRequest,
                user,
            )
        )
    ).all()
    used = {"from": day.isoformat()}
    if not rows:
        return [_notice(f"You have no pending or approved leave on {_day(day)}.")], used
    leave = rows[0]
    state = "waiting for approval" if leave.status is LeaveRequestStatus.PENDING else "approved"
    action = await _offer(
        session,
        user,
        ctx,
        kind="leave_cancel",
        payload={"request_id": str(leave.id)},
        summary={
            "title": "Withdraw leave",
            "figures": [
                {"label": "Dates", "value": _span(leave.start_date, leave.end_date)},
                {"label": "Days", "value": _num(leave.total_days)},
                {"label": "Now", "value": state.capitalize()},
            ],
            "notes": ["Whoever was asked to approve it is told it was withdrawn."],
            "confirm_label": "Withdraw it",
            "question": "Withdraw this leave?",
        },
    )
    return [card_for(action)], used


# ── Attendance ──────────────────────────────────────────────────────────────

_REG_KINDS = {
    "missed_punch": "Missed punch",
    "status_change": "Correct the day's status",
    "on_duty": "On duty",
    "work_from_home": "Work from home",
}
_REG_STATUSES = {
    "present": "Present",
    "half_day": "Half day",
    "absent": "Absent",
    "lop": "Loss of pay",
}


async def propose_regularisation(
    session: AsyncSession, user: User, inputs: dict[str, Any], ctx: ToolContext
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Offer a missed punch, a status correction, on-duty or work from home for one day."""
    from app.services.attendance import service as attendance

    day = _parse_day(inputs.get("from"))
    kind = str(inputs.get("attendance_kind") or "none")
    if day is None:
        return [_notice("Which day is this for?")], {}
    if kind not in _REG_KINDS:
        return [
            _notice(
                "Is it a missed punch, a correction of the day's status, on duty, or work "
                "from home?"
            )
        ], {}
    reason = str(inputs.get("reason") or "").strip()
    used = {"from": day.isoformat(), "attendance_kind": kind}
    if len(reason) < 3:
        return [_notice("What is the reason? The approver sees it.")], used
    try:
        first_in = _parse_time(inputs.get("in_time"))
        last_out = _parse_time(inputs.get("out_time"))
    except ValueError as exc:
        return [_notice(str(exc), "warning")], used
    today = await attendance.local_today(session, user)
    if kind in ("missed_punch", "status_change") and day > today:
        return [
            _notice(
                "A missed punch or a status correction is for a day that has happened. For a "
                "day ahead, ask for on duty, work from home or leave.",
                "warning",
            )
        ], used
    if kind in ("on_duty", "work_from_home") and day > today + timedelta(
        days=attendance.MAX_ADVANCE_DAYS
    ):
        return [
            _notice(
                f"On duty and work from home can be asked for up to "
                f"{attendance.MAX_ADVANCE_DAYS} days ahead.",
                "warning",
            )
        ], used
    if kind == "missed_punch" and first_in is None and last_out is None:
        return [_notice("What time did you come in, or leave (or both)?")], used
    status_key = str(inputs.get("attendance_status") or "present")
    if status_key not in _REG_STATUSES:
        status_key = "present"
    zone = await attendance.tenant_timezone(session, user)

    def at(moment: time | None) -> str | None:
        return datetime.combine(day, moment, tzinfo=zone).isoformat() if moment else None

    figures = [
        {"label": "Request", "value": _REG_KINDS[kind]},
        {"label": "Day", "value": _day(day)},
    ]
    if first_in:
        figures.append({"label": "In", "value": first_in.strftime("%H:%M")})
    if last_out:
        figures.append({"label": "Out", "value": last_out.strftime("%H:%M")})
    if kind == "status_change":
        figures.append({"label": "Mark as", "value": _REG_STATUSES[status_key]})
    figures.append({"label": "Reason", "value": reason[:200]})
    hr = permissions.is_hr(user)
    action = await _offer(
        session,
        user,
        ctx,
        kind="regularisation",
        payload={
            "attendance_date": day.isoformat(),
            "kind": kind,
            "requested_status": status_key,
            "reason": reason[:1000],
            "requested_first_in": at(first_in),
            "requested_last_out": at(last_out),
        },
        summary={
            "title": "Attendance request",
            "figures": figures,
            "notes": (
                ["As HR, your own correction is applied at once."]
                if hr
                else ["It goes to your approver; the day changes once it is approved."]
            ),
            "confirm_label": "Apply it" if hr else "Send for approval",
            "question": "Apply this correction?" if hr else "Send this for approval?",
        },
    )
    return [card_for(action)], used


# ── Approvals ───────────────────────────────────────────────────────────────


def _refs(raw: Any) -> list[tuple[str, uuid.UUID]]:
    out: list[tuple[str, uuid.UUID]] = []
    for part in re.split(r"[,\s]+", str(raw or "")):
        queue, _, item = part.strip().partition(":")
        try:
            pair = (queue, uuid.UUID(item))
        except ValueError:
            continue
        if pair not in out:
            out.append(pair)
    return out


async def propose_decision(
    session: AsyncSession, user: User, inputs: dict[str, Any], ctx: ToolContext
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Offer to approve or reject requests from the person's approvals, by their refs."""
    from app.services.approvals.email_actions import EMAIL_QUEUES, _waiting_item

    decision = str(inputs.get("decision") or "none")
    if decision not in ("approve", "reject"):
        return [_notice("Should these be approved or rejected?")], {}
    refs = _refs(inputs.get("refs"))
    if not refs:
        return [
            _notice(
                "Which requests? Look them up in your approvals first; each has a ref.",
                "warning",
            )
        ], {}
    note = str(inputs.get("note") or "").strip()[:1000] or None
    used = {"decision": decision, "refs": ",".join(f"{q}:{i}" for q, i in refs)}
    if len(refs) > MAX_DECISIONS:
        return [
            _notice(f"At most {MAX_DECISIONS} requests can be decided at once here.", "warning")
        ], used
    items: list[dict[str, str]] = []
    problems: list[str] = []
    for queue, item_id in refs:
        if queue not in EMAIL_QUEUES:
            problems.append(
                "A payroll, loan, settlement or hiring decision is made on its own page in "
                "AskMira, where it asks for your password."
            )
            continue
        _source, entry = await _waiting_item(session, user, queue, item_id)
        if entry is None:
            problems.append("One request is no longer waiting on you.")
            continue
        items.append(
            {
                "queue": queue,
                "id": str(item_id),
                "title": str(entry.get("title") or ""),
                "who": str(entry.get("subject_name") or ""),
            }
        )
    if not items:
        return [_notice(" ".join(dict.fromkeys(problems)) or "Nothing to decide.", "warning")], used
    verb = "Approve" if decision == "approve" else "Reject"
    action = await _offer(
        session,
        user,
        ctx,
        kind="approval_decision",
        payload={"decision": decision, "note": note, "items": items},
        summary={
            "title": f"{verb} {len(items)} request(s)",
            "figures": [
                {"label": item["who"] or "Request", "value": item["title"]} for item in items
            ],
            "notes": [*dict.fromkeys(problems), *([f"Your note: {note}"] if note else [])],
            "confirm_label": verb,
            "question": f"{verb} {'this request' if len(items) == 1 else 'these requests'}?",
        },
    )
    return [card_for(action)], used


# ── HR helpdesk ─────────────────────────────────────────────────────────────


async def propose_hr_ticket(
    session: AsyncSession, user: User, inputs: dict[str, Any], ctx: ToolContext
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Offer to raise an HR helpdesk ticket in a category the person picks."""
    from app.services.helpdesk import categories as helpdesk_categories

    subject = " ".join(str(inputs.get("subject") or "").split())[:200]
    description = str(inputs.get("description") or "").strip()[:5000]
    wanted = str(inputs.get("category") or "").strip().lower()
    found = await helpdesk_categories.list_categories(session, user)
    if not found:
        return [_notice("The HR helpdesk has no categories set up yet.", "warning")], {}
    matches = [c for c in found if wanted and wanted in c.name.lower()]
    used = {"category": wanted, "subject": subject}
    if len(matches) != 1:
        names = ", ".join(c.name for c in found)
        return [_notice(f"Which category fits best: {names}?")], used
    if len(subject) < 3 or len(description) < 3:
        return [_notice("What should the ticket say? Give a short subject and the details.")], used
    category = matches[0]
    action = await _offer(
        session,
        user,
        ctx,
        kind="hr_ticket",
        payload={
            "category_id": str(category.id),
            "subject": subject,
            "description": description,
        },
        summary={
            "title": "HR helpdesk ticket",
            "figures": [
                {"label": "Category", "value": category.name},
                {"label": "Subject", "value": subject},
            ],
            "notes": [description[:300] + ("…" if len(description) > 300 else "")],
            "confirm_label": "Raise ticket",
            "question": "Raise this ticket?",
        },
    )
    return [card_for(action)], used


# ── Confirming ──────────────────────────────────────────────────────────────

_YES = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|ya|haan|ha|han|ji|haan ji|ok|okay|okk|sure|confirm(?:ed)?|"
    r"go ahead|go for it|do it|please do|please|proceed|submit|send|send it|apply|"
    r"apply it|approve|approve it|approve them|reject|reject it|reject them|raise it|"
    r"correct|that'?s right|right|fine|done|kar do|kardo|bhej do|theek hai|thik hai)"
    r"\b[\s\w,.!'-]*$",
    re.IGNORECASE,
)
_NO = re.compile(r"\b(?:no|not|don'?t|do not|cancel|stop|wait|nahi|mat|change)\b", re.IGNORECASE)


def is_affirmative(text: str) -> bool:
    """Whether a message reads as a plain yes to what was just offered."""
    cleaned = text.strip()
    if not cleaned or len(cleaned) > 80:
        return False
    if _NO.search(cleaned):
        return False
    return bool(_YES.match(cleaned))


async def _locked(
    session: AsyncSession, user: User, action_id: uuid.UUID, *, owner_id: uuid.UUID
) -> CompanionAction:
    row = await session.scalar(
        tenant_scoped(
            select(CompanionAction)
            .where(CompanionAction.id == action_id, CompanionAction.user_id == owner_id)
            .with_for_update(),
            CompanionAction,
            user,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No such offer.")
    return row


async def _undo(session: AsyncSession, user: User, savepoint: Any) -> None:
    """Undo what a failed step did, and only that.

    The step runs in a savepoint, so its rollback leaves the turn's other work
    (and every object the caller holds) as it was. If the service committed
    before failing, the savepoint is gone: the whole transaction is rolled
    back and the caller's login reloaded, since a rollback expires it.
    """
    if savepoint.is_active:
        await savepoint.rollback()
        return
    await session.rollback()
    await session.refresh(user)


def _scope_for(request: Request | None, path: str) -> Request:
    base = dict(request.scope) if request is not None else {"type": "http", "headers": []}
    return Request({**base, "path": path, "method": "POST", "query_string": b""})


async def _as_portal(session: AsyncSession, user: User, request: Request | None, path: str) -> None:
    """Read a sub-admin's rights as the module's own endpoint would."""
    await access_control.load_request_access(_scope_for(request, path), session, user)


async def _run(
    session: AsyncSession, user: User, action: CompanionAction, request: Request | None
) -> dict[str, Any]:
    """Do it, through the module's own service. Returns what came of it."""
    payload = action.payload
    if action.kind == "leave_request":
        from app.schemas.leave import LeaveRequestCreate
        from app.services.leave import service as leave

        await _as_portal(session, user, request, _PATHS[action.kind])
        read = await leave.apply(
            session, user, LeaveRequestCreate.model_validate(payload), request=request
        )
        waiting = [s.approver_name for s in read.approval_chain if s.status == "pending"]
        text = (
            f"Your leave request is with {', '.join(waiting)} for approval."
            if waiting and read.status is LeaveRequestStatus.PENDING
            else f"Your leave request is {read.status.value}."
        )
        return {"text": text, "open": "/time", "reference": str(read.id)}

    if action.kind == "leave_cancel":
        from app.services.leave import service as leave

        await _as_portal(session, user, request, _PATHS[action.kind])
        await leave.cancel(session, user, uuid.UUID(payload["request_id"]), request=request)
        return {"text": "Your leave was withdrawn.", "open": "/time"}

    if action.kind == "regularisation":
        from app.models.attendance import AttendanceStatus, RegularisationKind
        from app.services.attendance import service as attendance

        await _as_portal(session, user, request, _PATHS[action.kind])

        def when(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        row = await attendance.raise_regularisation(
            session,
            user,
            employee_id=_own_employee(user),
            attendance_date=date.fromisoformat(payload["attendance_date"]),
            requested_status=AttendanceStatus(payload["requested_status"]),
            reason=payload["reason"],
            kind=RegularisationKind(payload["kind"]),
            requested_first_in=when(payload.get("requested_first_in")),
            requested_last_out=when(payload.get("requested_last_out")),
            request=request,
        )
        state = getattr(row.status, "value", str(row.status))
        text = (
            "The correction is applied."
            if state == "approved"
            else "Your request is with your approver."
        )
        return {"text": text, "open": "/time", "reference": str(row.id)}

    if action.kind == "hr_ticket":
        from app.services.helpdesk import service as helpdesk

        await _as_portal(session, user, request, _PATHS[action.kind])
        ticket = await helpdesk.create_ticket(
            session,
            user,
            category_id=uuid.UUID(payload["category_id"]),
            subject=payload["subject"],
            description=payload["description"],
            request=request,
        )
        owner = ticket.assignee_name or "HR"
        return {
            "text": f"Ticket {ticket.ticket_number} is raised and with {owner}.",
            "open": "/helpdesk",
            "reference": ticket.ticket_number,
        }

    if action.kind == "approval_decision":
        from app.services.approvals.email_actions import (
            _DECIDE_PATHS,
            EMAIL_QUEUES,
            _waiting_item,
        )

        approve = payload["decision"] == "approve"
        done: list[str] = []
        failed: list[str] = []
        for item in payload["items"]:
            label = f"{item.get('who') or 'Request'}: {item.get('title')}"
            await _as_portal(session, user, request, _DECIDE_PATHS[item["queue"]])
            _source, entry = await _waiting_item(
                session, user, item["queue"], uuid.UUID(item["id"])
            )
            if entry is None:
                failed.append(f"{label} (no longer waiting on you)")
                continue
            savepoint = await session.begin_nested()
            try:
                await EMAIL_QUEUES[item["queue"]](
                    session, user, uuid.UUID(item["id"]), approve, payload.get("note"), request
                )
                await session.commit()
                done.append(label)
            except HTTPException as exc:
                await _undo(session, user, savepoint)
                failed.append(f"{label} ({exc.detail})")
        verb = "approved" if approve else "rejected"
        if not done:
            raise HTTPException(status_code=409, detail="Nothing was decided: " + "; ".join(failed))
        text = f"{len(done)} request(s) {verb}."
        if failed:
            text += " Not decided: " + "; ".join(failed)
        return {"text": text, "open": "/approvals", "done": done, "failed": failed}

    raise HTTPException(status_code=422, detail="This offer cannot be carried out.")


async def confirm(
    session: AsyncSession,
    user: User,
    action_id: uuid.UUID,
    *,
    owner_id: uuid.UUID,
    via: str,
    request: Request | None,
    ctx: ToolContext | None = None,
) -> CompanionAction:
    """Carry out an offer the person agreed to. Refused if it was decided, expired or not theirs.

    With `ctx` (a typed yes, through the model) it must also be an offer from an
    earlier turn of this conversation, and the person's message must read as a yes.
    """
    action = await _locked(session, user, action_id, owner_id=owner_id)
    if ctx is not None:
        if action.conversation_id != ctx.conversation_id:
            raise HTTPException(status_code=404, detail="No such offer in this conversation.")
        if action.created_at is not None and action.created_at >= ctx.turn_started:
            raise HTTPException(
                status_code=409,
                detail="The person has not answered this offer yet. Ask them to confirm it.",
            )
        if not is_affirmative(ctx.latest_text):
            raise HTTPException(
                status_code=409,
                detail=(
                    "The person's message is not a clear yes. Ask them to confirm, or to press "
                    "the button on the offer."
                ),
            )
    if action.status is not CompanionActionStatus.PROPOSED:
        raise HTTPException(
            status_code=409, detail=f"This offer was already {action.status.value}."
        )
    if action.expires_at <= datetime.now(UTC):
        raise HTTPException(
            status_code=409,
            detail="This offer has expired. Ask again so the figures are checked afresh.",
        )

    # Marked done before the service runs: its commit then saves this together
    # with the change and the service's own audit row. On failure it is rolled
    # back and marked failed instead.
    action.status = CompanionActionStatus.DONE
    action.decided_at = datetime.now(UTC)
    action.decided_via = via
    user.decided_via = "ask_mira"
    savepoint = await session.begin_nested()
    try:
        result = await _run(session, user, action, request)
    except HTTPException as exc:
        await _undo(session, user, savepoint)
        action = await _locked(session, user, action_id, owner_id=owner_id)
        detail = exc.detail if isinstance(exc.detail, str) else "It could not be done."
        action.status = CompanionActionStatus.FAILED
        action.decided_at = datetime.now(UTC)
        action.decided_via = via
        action.result = {"text": detail, "error": True}
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="companion_action",
            entity_id=action.id,
            actor=user,
            changes={"kind": action.kind, "status": "failed", "reason": detail},
            summary=f"Ask Mira could not carry out a {action.kind.replace('_', ' ')}",
            request=request,
        )
        await session.commit()
        return action
    finally:
        user.decided_via = None
        await _as_portal(session, user, request, _HOME_PATH)

    # Set again: a queue that failed part-way rolled back what was pending.
    action = await _locked(session, user, action_id, owner_id=owner_id)
    action.status = CompanionActionStatus.DONE
    action.decided_at = action.decided_at or datetime.now(UTC)
    action.decided_via = via
    action.result = result
    await session.commit()
    await session.refresh(action)
    return action


async def cancel(
    session: AsyncSession,
    user: User,
    action_id: uuid.UUID,
    *,
    owner_id: uuid.UUID,
    via: str,
    request: Request | None,
    conversation_id: uuid.UUID | None = None,
) -> CompanionAction:
    """The person said no. The offer stays in the conversation, marked cancelled."""
    action = await _locked(session, user, action_id, owner_id=owner_id)
    if conversation_id is not None and action.conversation_id != conversation_id:
        raise HTTPException(status_code=404, detail="No such offer in this conversation.")
    if action.status is not CompanionActionStatus.PROPOSED:
        raise HTTPException(
            status_code=409, detail=f"This offer was already {action.status.value}."
        )
    action.status = CompanionActionStatus.CANCELLED
    action.decided_at = datetime.now(UTC)
    action.decided_via = via
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="companion_action",
        entity_id=action.id,
        actor=user,
        changes={"kind": action.kind, "status": "cancelled", "via": via},
        summary=f"Ask Mira's offer ({action.kind.replace('_', ' ')}) declined by its owner",
        request=request,
    )
    await session.commit()
    await session.refresh(action)
    return action


async def pending_offers(
    session: AsyncSession, user: User, conversation_id: uuid.UUID, *, owner_id: uuid.UUID
) -> list[CompanionAction]:
    """Offers in this conversation still waiting on the person's say."""
    rows = await session.scalars(
        tenant_scoped(
            select(CompanionAction)
            .where(
                CompanionAction.conversation_id == conversation_id,
                CompanionAction.user_id == owner_id,
                CompanionAction.status == CompanionActionStatus.PROPOSED,
                CompanionAction.expires_at > datetime.now(UTC),
            )
            .order_by(CompanionAction.created_at),
            CompanionAction,
            user,
        )
    )
    return list(rows.all())


async def recent_offers(
    session: AsyncSession,
    user: User,
    conversation_id: uuid.UUID,
    *,
    owner_id: uuid.UUID,
    limit: int = 6,
) -> list[CompanionAction]:
    """The latest offers in this conversation, whatever became of them, oldest first."""
    rows = await session.scalars(
        tenant_scoped(
            select(CompanionAction)
            .where(
                CompanionAction.conversation_id == conversation_id,
                CompanionAction.user_id == owner_id,
            )
            .order_by(CompanionAction.created_at.desc())
            .limit(limit),
            CompanionAction,
            user,
        )
    )
    return list(reversed(rows.all()))


async def employee_name(session: AsyncSession, user: User, employee_id: uuid.UUID) -> str:
    row = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    return row.full_name if row else "—"
