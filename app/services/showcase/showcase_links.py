"""Showcase links: made by the platform's super admin, opened by anyone who scans one.

**One QR per workspace, for good.** A workspace has one link at most, and its
code is made once: delete switches the link off and keeps the code, and
making the workspace's link again brings the same code back, so a printed QR
never has to be printed again. A link never moves to another workspace.

**The super admin's side.** A link names an event, the workspace whose Ask
Miraa it opens, the admin of that workspace it answers as, the window it works
in, an optional stand PIN and a daily message cap. It can be switched off at
any moment, edited, or deleted. Every change is audited in the same
transaction.

**A visitor's side.** No sign-in and no portal session: the page can only
chat. Opening the link (with the PIN, when it has one) starts a chat of the
visitor's own (`ShowcaseRun`), held by a random key their page keeps. Each
message goes to Ask Miraa's own service as the chosen admin
(`companion.service.send`): the same tools, the same tenant scoping, the same
audit trail, and an offer is carried out only when the visitor presses
Confirm. A visitor never names a conversation; the run does, so nobody reads
another visitor's chat or the admin's own. Every request checks the link
afresh, so switching it off or letting it end stops open pages at their next
message.

**Not tenant data.** Neither table has a tenant column: links belong to the
platform, runs to a link. This module selects only the showcase tables, and
reads a workspace and its admin by primary key; everything inside the
workspace is read by Ask Miraa's service. Fenced by
`test_the_showcase_service_reads_only_its_own_tables`.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import segno
from fastapi import HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import AuditAction
from app.models.showcase import ShowcaseLink, ShowcaseRun
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.showcase import ShowcaseLinkCreate, ShowcaseLinkUpdate, ShowcaseSayIn
from app.services import audit
from app.services.companion import service as companion
from app.services.hiring.public_guard import Limit, throttle
from app.services.identity import directory

__all__ = [
    "create_link",
    "decide",
    "delete_link",
    "describe",
    "list_links",
    "qr_code",
    "read_run",
    "say",
    "start_run",
    "update_link",
    "workspace_of",
]

#: Chats a single connection may start, wrong PINs it may type, and requests it
#: may make, per ten minutes. Generous: an exhibition hall shares one or two addresses.
START = Limit("showcase_start", 60, 600)
PIN_TRIES = Limit("showcase_pin", 20, 600)
STEP = Limit("showcase_step", 600, 600)
#: Messages one visitor may send in one chat.
MAX_TURNS = 60
#: A chat untouched this long is forgotten when the next one starts (the
#: conversation itself stays in the admin's Ask Miraa history).
KEEP_RUNS_FOR = timedelta(days=3)
IST = ZoneInfo("Asia/Kolkata")

_FIELDS = ("label", "daily_messages", "is_active", "starts_at", "ends_at")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _web_base() -> str:
    base = settings.WEB_APP_URL or (settings.CORS_ORIGINS[0] if settings.CORS_ORIGINS else "")
    return base.rstrip("/")


def _url(code: str) -> str:
    return f"{_web_base()}/showcase/{code}"


def _now() -> datetime:
    return datetime.now(UTC)


def _today() -> date:
    return _now().astimezone(IST).date()


def _status(link: ShowcaseLink, now: datetime | None = None) -> str:
    now = now or _now()
    if not link.is_active or link.deleted_at is not None:
        return "off"
    if now < link.starts_at:
        return "scheduled"
    if now >= link.ends_at:
        return "ended"
    return "live"


def _sent_today(link: ShowcaseLink) -> int:
    return link.messages_today if link.messages_day == _today() else 0


def _refuse_non_platform(user: User) -> None:
    if user.role is not UserRole.SUPER_ADMIN:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only the platform's super admin manages showcase links."
        )


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail)


async def _workspace(session: AsyncSession, workspace_id: uuid.UUID) -> Tenant | None:
    tenant = await session.get(Tenant, workspace_id)
    if tenant is None or getattr(tenant, "deleted_at", None) is not None:
        return None
    return tenant


def _answers_for(user: User | None, workspace_id: uuid.UUID) -> bool:
    """Whether this login may be the one a link's chat answers as."""
    return (
        user is not None
        and user.tenant_id == workspace_id
        and user.role is UserRole.ADMIN
        and user.is_active
    )


async def _check_target(
    session: AsyncSession, workspace_id: uuid.UUID, acting_user_id: uuid.UUID
) -> None:
    if await _workspace(session, workspace_id) is None:
        raise _unprocessable("That workspace does not exist.")
    if not _answers_for(await session.get(User, acting_user_id), workspace_id):
        raise _unprocessable("Pick an active admin of that workspace for the chat to answer as.")


async def _read(session: AsyncSession, link: ShowcaseLink) -> dict[str, Any]:
    tenant = await session.get(Tenant, link.workspace_id)
    acting = await session.get(User, link.acting_user_id)
    code = link.code_encrypted  # EncryptedText: plaintext here, ciphertext at rest
    return {
        "id": link.id,
        "label": link.label,
        "url": _url(code),
        "code": code,
        "status": _status(link),
        "workspace_id": link.workspace_id,
        "company": tenant.name if tenant else "—",
        "acting_user_id": link.acting_user_id,
        "acting_name": acting.full_name if acting else "—",
        "acting_email": acting.email if acting else "—",
        "pin": link.pin_encrypted,
        "daily_messages": link.daily_messages,
        "messages_today": _sent_today(link),
        "is_active": link.is_active,
        "starts_at": link.starts_at,
        "ends_at": link.ends_at,
        "visits": link.visits,
        "last_visit_at": link.last_visit_at,
        "created_at": link.created_at,
        "updated_at": link.updated_at,
    }


async def _link_for_platform(session: AsyncSession, link_id: uuid.UUID) -> ShowcaseLink:
    link = await session.get(ShowcaseLink, link_id)
    if link is None or link.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such showcase link.")
    return link


def _audit_values(link: ShowcaseLink) -> dict[str, Any]:
    return {
        "label": link.label,
        "workspace_id": str(link.workspace_id),
        "acting_user_id": str(link.acting_user_id),
        # Whether there is one, never the PIN itself.
        "pin": "set" if link.pin_encrypted else "none",
        "daily_messages": link.daily_messages,
        "is_active": link.is_active,
        "starts_at": link.starts_at.isoformat(),
        "ends_at": link.ends_at.isoformat(),
    }


# ── The super admin ──────────────────────────────────────────────────────────


async def list_links(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    _refuse_non_platform(user)
    links = (
        await session.scalars(
            select(ShowcaseLink)
            .where(ShowcaseLink.deleted_at.is_(None))
            .order_by(ShowcaseLink.created_at.desc())
        )
    ).all()
    return [await _read(session, link) for link in links]


async def create_link(
    session: AsyncSession, user: User, data: ShowcaseLinkCreate, *, request: Request | None
) -> dict[str, Any]:
    _refuse_non_platform(user)
    await _check_target(session, data.workspace_id, data.acting_user_id)
    if data.ends_at <= _now():
        raise _unprocessable("The link must end in the future.")
    link = await session.scalar(
        select(ShowcaseLink).where(ShowcaseLink.workspace_id == data.workspace_id)
    )
    if link is not None and link.deleted_at is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This workspace already has its showcase QR. Edit that link instead: "
            "its code never changes.",
        )
    restored = link is not None
    if link is None:
        # Made once for the workspace, never again: this is the printed QR.
        code = secrets.token_urlsafe(9)
        link = ShowcaseLink(
            id=uuid.uuid4(),
            code_hash=_hash(code),
            code_encrypted=code,
            workspace_id=data.workspace_id,
            visits=0,
        )
        session.add(link)
    # Deleted before: the same code again, with the new details.
    link.deleted_at = None
    link.acting_user_id = data.acting_user_id
    link.pin_encrypted = data.pin
    link.created_by_user_id = user.id
    link.messages_today = 0
    link.messages_day = None
    for field in _FIELDS:
        setattr(link, field, getattr(data, field))
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="showcase_link",
        entity_id=link.id,
        actor=user,
        actor_type="platform",
        changes=_audit_values(link),
        summary=(
            f"Showcase link made again with its printed code: {link.label}"
            if restored
            else f"Showcase link created: {link.label}"
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(link)
    return await _read(session, link)


async def update_link(
    session: AsyncSession,
    user: User,
    link_id: uuid.UUID,
    data: ShowcaseLinkUpdate,
    *,
    request: Request | None,
) -> dict[str, Any]:
    _refuse_non_platform(user)
    link = await _link_for_platform(session, link_id)
    before = _audit_values(link)
    given = data.model_dump(exclude_unset=True)
    for field in _FIELDS:
        if field in given and given[field] is not None:
            setattr(link, field, given[field])
    if data.acting_user_id is not None:
        link.acting_user_id = data.acting_user_id
    if data.clear_pin:
        link.pin_encrypted = None
    elif data.pin is not None:
        link.pin_encrypted = data.pin
    # The rules hold on the result, whichever fields changed.
    await _check_target(session, link.workspace_id, link.acting_user_id)
    if link.starts_at.tzinfo is None or link.ends_at.tzinfo is None:
        raise _unprocessable("Give times with their time zone.")
    if link.ends_at <= link.starts_at:
        raise _unprocessable("The link must end after it starts.")
    if (link.ends_at - link.starts_at).days > 366:
        raise _unprocessable("A showcase link can run for a year at most.")
    after = _audit_values(link)
    changed = {
        key: {"from": before[key], "to": after[key]} for key in after if before[key] != after[key]
    }
    pin_changed = data.pin is not None and before["pin"] == "set" and not data.clear_pin
    if pin_changed:
        changed.setdefault("pin", {"from": "set", "to": "changed"})
    if not changed:
        return await _read(session, link)
    switched = "is_active" in changed and len(changed) == 1
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="showcase_link",
        entity_id=link.id,
        actor=user,
        actor_type="platform",
        changes=changed,
        summary=(
            f"Showcase link switched {'on' if link.is_active else 'off'}: {link.label}"
            if switched
            else f"Showcase link changed: {link.label}"
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(link)
    return await _read(session, link)


async def delete_link(
    session: AsyncSession, user: User, link_id: uuid.UUID, *, request: Request | None
) -> None:
    _refuse_non_platform(user)
    link = await _link_for_platform(session, link_id)
    values = _audit_values(link)
    label = link.label
    # Open chats end; the link and its code stay, switched off, so the printed QR
    # says so and works again once the workspace's link is made again.
    await session.execute(delete(ShowcaseRun).where(ShowcaseRun.link_id == link.id))
    link.deleted_at = _now()
    link.is_active = False
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="showcase_link",
        entity_id=link_id,
        actor=user,
        actor_type="platform",
        changes=values,
        summary=f"Showcase link deleted (its QR code is kept, switched off): {label}",
        request=request,
    )
    await session.commit()


async def qr_code(
    session: AsyncSession, user: User, link_id: uuid.UUID, kind: str
) -> tuple[bytes, str, str]:
    """The link's QR code to print: SVG (any size) or a large PNG. Ink on white, for any scanner."""
    _refuse_non_platform(user)
    link = await _link_for_platform(session, link_id)
    code = segno.make(_url(link.code_encrypted), error="q", micro=False)
    buffer = io.BytesIO()
    if kind == "png":
        code.save(buffer, kind="png", scale=24, border=4, dark="#1a1110", light="#ffffff")
        media = "image/png"
    else:
        code.save(
            buffer, kind="svg", scale=10, border=4, dark="#1a1110", light="#ffffff", xmldecl=False
        )
        media = "image/svg+xml"
    safe = "".join(ch if ch.isalnum() else "-" for ch in link.label).strip("-")[:60] or "showcase"
    return buffer.getvalue(), media, f"{safe}-qr.{kind}"


# ── A visitor ────────────────────────────────────────────────────────────────

_GONE = {
    "off": "This showcase link has been switched off.",
    "ended": "This showcase link has ended.",
    "scheduled": "This showcase link isn't open yet.",
    "unavailable": "This showcase can't answer right now. Ask the team at the stand.",
}


def _gone(state: str) -> HTTPException:
    # The message for the page; the state for a page that wants to say more.
    return HTTPException(status.HTTP_410_GONE, _GONE[state], headers={"X-Showcase-Status": state})


async def _link_by_code(session: AsyncSession, code: str) -> ShowcaseLink:
    if not 6 <= len(code) <= 64:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such showcase.")
    link = await session.scalar(select(ShowcaseLink).where(ShowcaseLink.code_hash == _hash(code)))
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such showcase.")
    return link


def _require_live(link: ShowcaseLink) -> None:
    state = _status(link)
    if state != "live":
        raise _gone(state)


async def _brand(session: AsyncSession, link: ShowcaseLink) -> tuple[str, str | None]:
    tenant = await _workspace(session, link.workspace_id)
    if tenant is None:
        return "AskMiraa", None
    return tenant.name, directory.logo_url(tenant)


async def _acting(session: AsyncSession, link: ShowcaseLink, request: Request) -> User:
    """The admin the chat answers as, checked afresh: an admin switched off, demoted or
    moved, or a workspace suspended, stops the link at once."""
    tenant = await _workspace(session, link.workspace_id)
    user = await session.get(User, link.acting_user_id)
    if tenant is None or not tenant.is_active or not _answers_for(user, link.workspace_id):
        raise _gone("unavailable")
    assert user is not None
    # What the sign-in path sets on every request (app.core.deps.get_current_user).
    from app.core.access_control import load_request_access
    from app.core.deps import has_direct_reports

    user.has_reports = await has_direct_reports(session, user)
    await load_request_access(request, session, user)
    return user


async def workspace_of(session: AsyncSession, code: str) -> uuid.UUID:
    """The workspace a live link opens: whose upload limit an attached file is read under."""
    link = await _link_by_code(session, code)
    _require_live(link)
    return link.workspace_id


async def describe(session: AsyncSession, code: str, request: Request) -> dict[str, Any]:
    """What the page shows before a chat starts: whose, whether it works now, and a PIN."""
    throttle(request, STEP)
    link = await _link_by_code(session, code)
    state = _status(link)
    company, logo = await _brand(session, link)
    out: dict[str, Any] = {"status": state, "company": company, "logo_url": logo}
    if state == "scheduled":
        out["starts_at"] = link.starts_at.isoformat()
    if state != "live":
        out["message"] = _GONE[state]
        return out
    out.update({"ends_at": link.ends_at.isoformat(), "needs_pin": bool(link.pin_encrypted)})
    return out


async def _chat(
    session: AsyncSession, link: ShowcaseLink, run: ShowcaseRun, user: User
) -> dict[str, Any]:
    """The chat screen's status: Ask Miraa's own, with the link's cap in place of the admin's."""
    company, logo = await _brand(session, link)
    chat = await companion.status_for(session, user)
    today_left = max(0, link.daily_messages - _sent_today(link))
    turns_left = max(0, MAX_TURNS - run.turns)
    chat.update(
        {
            "daily_limit": min(MAX_TURNS, link.daily_messages),
            "sent_today": min(MAX_TURNS, link.daily_messages) - min(turns_left, today_left),
            "can_configure": False,
        }
    )
    return {
        "run_id": str(run.id),
        "company": company,
        "logo_url": logo,
        "ends_at": link.ends_at.isoformat(),
        "acting_name": user.full_name,
        "status": chat,
    }


async def start_run(
    session: AsyncSession, code: str, pin: str | None, request: Request
) -> dict[str, Any]:
    """A new chat for this visitor, and the key their page keeps to carry on with it."""
    throttle(request, START)
    link = await _link_by_code(session, code)
    _require_live(link)
    if link.pin_encrypted:
        if not pin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Type the PIN from the stand.")
        if not hmac.compare_digest(link.pin_encrypted.encode(), pin.strip().encode()):
            throttle(request, PIN_TRIES)
            raise HTTPException(status.HTTP_403_FORBIDDEN, "That PIN is not right.")
    user = await _acting(session, link, request)
    now = _now()
    # Chats nobody has touched for days: forget the key (the conversation stays).
    await session.execute(
        delete(ShowcaseRun).where(
            ShowcaseRun.link_id == link.id, ShowcaseRun.updated_at < now - KEEP_RUNS_FOR
        )
    )
    key = secrets.token_urlsafe(24)
    run = ShowcaseRun(id=uuid.uuid4(), link_id=link.id, key_hash=_hash(key), turns=0)
    session.add(run)
    link.visits = (link.visits or 0) + 1
    link.last_visit_at = now
    await session.commit()
    return {"key": key, **await _chat(session, link, run, user)}


async def _run_for(
    session: AsyncSession, code: str, run_id: uuid.UUID, key: str | None, *, lock: bool = False
) -> tuple[ShowcaseLink, ShowcaseRun]:
    link = await _link_by_code(session, code)
    _require_live(link)
    run = await session.get(ShowcaseRun, run_id, with_for_update=lock)
    if (
        run is None
        or run.link_id != link.id
        or not key
        or not hmac.compare_digest(run.key_hash, _hash(key))
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This chat has gone. Start a new one.")
    return link, run


async def read_run(
    session: AsyncSession, code: str, run_id: uuid.UUID, key: str | None, request: Request
) -> dict[str, Any]:
    """The visitor's chat so far, to carry on with after a reload."""
    throttle(request, STEP)
    link, run = await _run_for(session, code, run_id, key)
    user = await _acting(session, link, request)
    messages: list[dict[str, Any]] = []
    if run.conversation_id is not None:
        try:
            detail = await companion.get_conversation(session, user, run.conversation_id)
            messages = detail["messages"]
        except HTTPException as exc:
            if exc.status_code != status.HTTP_404_NOT_FOUND:
                raise
            # The admin deleted it from their Ask Miraa: start the chat afresh.
            run.conversation_id = None
            await session.commit()
    return {**await _chat(session, link, run, user), "messages": messages}


def _ref(run: ShowcaseRun, client_ref: str | None) -> str:
    """The page's message id, tied to this run: Ask Miraa returns an earlier answer for
    an id it has seen, and that must never be another visitor's."""
    return f"sc{run.id.hex[:12]}-{client_ref or secrets.token_hex(8)}"[:64]


async def say(
    session: AsyncSession,
    code: str,
    run_id: uuid.UUID,
    key: str | None,
    body: ShowcaseSayIn,
    request: Request,
    *,
    files: list[tuple[str, bytes]] | None = None,
) -> dict[str, Any]:
    """One message to Ask Miraa, as the link's admin, in this visitor's conversation."""
    throttle(request, STEP)
    link, run = await _run_for(session, code, run_id, key, lock=True)
    if run.turns >= MAX_TURNS:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"This chat has had its {MAX_TURNS} messages. Start a new chat to carry on.",
        )
    if _sent_today(link) >= link.daily_messages:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "This showcase has answered all its messages for today. Ask the team at the stand.",
        )
    user = await _acting(session, link, request)
    payload = companion.MessageIn(
        text=body.text,
        conversation_id=run.conversation_id,
        pick=companion.PickIn(**body.pick.model_dump()) if body.pick else None,
        client_ref=_ref(run, body.client_ref),
    )
    conversation_before = run.conversation_id
    turn = await companion.send(session, user, payload, request=request, files=files, own_cap=True)
    # Ask Miraa's service has committed: count the message, under a fresh lock.
    run = await session.get(ShowcaseRun, run_id, with_for_update=True, populate_existing=True)
    link = await session.get(ShowcaseLink, link.id, with_for_update=True, populate_existing=True)
    assert run is not None and link is not None
    conversation = uuid.UUID(str(turn["conversation"]["id"]))
    if conversation_before is not None and conversation != conversation_before:
        # Never happens with a run-bound message id; refuse rather than show it.
        raise HTTPException(status.HTTP_409_CONFLICT, "That message belongs to another chat.")
    if not turn.get("repeat"):
        run.conversation_id = conversation
        run.turns += 1
        today = _today()
        if link.messages_day != today:
            link.messages_day = today
            link.messages_today = 0
        link.messages_today += 1
    await session.commit()
    return {**turn, "status": (await _chat(session, link, run, user))["status"]}


async def decide(
    session: AsyncSession,
    code: str,
    run_id: uuid.UUID,
    key: str | None,
    action_id: uuid.UUID,
    confirm: bool,
    request: Request,
) -> dict[str, Any]:
    """Confirm or Cancel on an offer Ask Miraa made in this visitor's chat, and only there."""
    throttle(request, STEP)
    link, run = await _run_for(session, code, run_id, key)
    user = await _acting(session, link, request)
    offered = False
    if run.conversation_id is not None:
        detail = await companion.get_conversation(session, user, run.conversation_id)
        offered = any(
            card.get("type") == "proposal" and str(card.get("action_id")) == str(action_id)
            for message in detail["messages"]
            for card in message.get("cards") or []
        )
    if not offered:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That offer is not in this chat.")
    if confirm:
        return await companion.confirm_offer(session, user, action_id, request=request)
    return await companion.cancel_offer(session, user, action_id, request=request)
