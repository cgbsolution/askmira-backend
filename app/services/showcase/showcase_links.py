"""Showcase links: made by the platform's super admin, opened by anyone who scans one.

**The super admin's side.** A link names an event, the workspace whose brand
the story wears, the order the story runs, and the window it works in. It can
be switched off at any moment, edited, or deleted (its visitors' stories go
with it). Every change is audited in the same transaction.

**A visitor's side.** No sign-in, ever: a printed QR code is public. Opening
the link starts a story of the visitor's own (`ShowcaseRun`), held by a random
key their page keeps. Every request checks the link afresh, so switching it
off or letting it end stops open pages on their next step. A story reaches no
workspace: it reads the brand (name, logo) and nothing else, and it writes
only its own row.

**Not tenant data.** Neither table has a tenant column: links belong to the
platform, runs to a link. Rows are read by primary key or by the hash of the
code; there is no query by tenant to scope. Fenced by
`test_the_showcase_service_reads_only_its_own_tables`.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import io
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import segno
from fastapi import HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import AuditAction
from app.models.showcase import ShowcaseLink, ShowcaseRun
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.showcase import ShowcaseLinkCreate, ShowcaseLinkUpdate
from app.services import audit
from app.services.hiring.public_guard import Limit, throttle
from app.services.identity import directory
from app.services.showcase import story

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
]

#: Stories a single connection may start, and steps it may take, per ten minutes.
#: Generous: an exhibition hall shares one or two addresses.
START = Limit("showcase_start", 60, 600)
STEP = Limit("showcase_step", 600, 600)
#: A story untouched this long is deleted when the next one starts.
KEEP_RUNS_FOR = timedelta(days=3)

_FIELDS = (
    "label",
    "client_name",
    "role_title",
    "headcount",
    "months",
    "order_value",
    "team_budget",
    "laptop_price",
    "payment_days",
    "is_active",
    "starts_at",
    "ends_at",
)


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


def _status(link: ShowcaseLink, now: datetime | None = None) -> str:
    now = now or _now()
    if not link.is_active:
        return "off"
    if now < link.starts_at:
        return "scheduled"
    if now >= link.ends_at:
        return "ended"
    return "live"


def _refuse_non_platform(user: User) -> None:
    if user.role is not UserRole.SUPER_ADMIN:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only the platform's super admin manages showcase links."
        )


async def _company(session: AsyncSession, link: ShowcaseLink) -> tuple[str, str | None]:
    """The brand the story wears: the workspace's name and logo, or AskMiraa's."""
    if link.brand_tenant_id is None:
        return "AskMiraa", None
    tenant = await session.get(Tenant, link.brand_tenant_id)
    if tenant is None:
        return "AskMiraa", None
    return tenant.name, directory.logo_url(tenant)


async def _read(session: AsyncSession, link: ShowcaseLink) -> dict[str, Any]:
    company, _logo = await _company(session, link)
    code = link.code_encrypted  # EncryptedText: plaintext here, ciphertext at rest
    return {
        "id": link.id,
        "label": link.label,
        "url": _url(code),
        "code": code,
        "status": _status(link),
        "brand_tenant_id": link.brand_tenant_id,
        "company": company,
        "client_name": link.client_name,
        "role_title": link.role_title,
        "headcount": link.headcount,
        "months": link.months,
        "order_value": link.order_value,
        "team_budget": link.team_budget,
        "margin_floor": ((link.order_value - link.team_budget) / link.order_value * 100).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        ),
        "laptop_price": link.laptop_price,
        "payment_days": link.payment_days,
        "is_active": link.is_active,
        "starts_at": link.starts_at,
        "ends_at": link.ends_at,
        "visits": link.visits,
        "last_visit_at": link.last_visit_at,
        "created_at": link.created_at,
        "updated_at": link.updated_at,
    }


async def _check_tenant(session: AsyncSession, brand_tenant_id: uuid.UUID | None) -> None:
    if brand_tenant_id is None:
        return
    tenant = await session.get(Tenant, brand_tenant_id)
    if tenant is None or getattr(tenant, "deleted_at", None) is not None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "That workspace does not exist.")


async def _link_for_platform(session: AsyncSession, link_id: uuid.UUID) -> ShowcaseLink:
    link = await session.get(ShowcaseLink, link_id)
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such showcase link.")
    return link


def _audit_values(link: ShowcaseLink) -> dict[str, Any]:
    return {
        "label": link.label,
        "brand_tenant_id": str(link.brand_tenant_id) if link.brand_tenant_id else None,
        "client_name": link.client_name,
        "role_title": link.role_title,
        "headcount": link.headcount,
        "months": link.months,
        # Two places always: 3000000 typed and 3000000.00 read back are the same amount.
        "order_value": f"{Decimal(link.order_value):.2f}",
        "team_budget": f"{Decimal(link.team_budget):.2f}",
        "laptop_price": f"{Decimal(link.laptop_price):.2f}",
        "payment_days": link.payment_days,
        "is_active": link.is_active,
        "starts_at": link.starts_at.isoformat(),
        "ends_at": link.ends_at.isoformat(),
    }


# ── The super admin ──────────────────────────────────────────────────────────


async def list_links(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    _refuse_non_platform(user)
    links = (
        await session.scalars(select(ShowcaseLink).order_by(ShowcaseLink.created_at.desc()))
    ).all()
    return [await _read(session, link) for link in links]


async def create_link(
    session: AsyncSession, user: User, data: ShowcaseLinkCreate, *, request: Request | None
) -> dict[str, Any]:
    _refuse_non_platform(user)
    await _check_tenant(session, data.brand_tenant_id)
    if data.ends_at <= _now():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "The link must end in the future."
        )
    code = secrets.token_urlsafe(9)
    link = ShowcaseLink(
        id=uuid.uuid4(),
        code_hash=_hash(code),
        code_encrypted=code,
        brand_tenant_id=data.brand_tenant_id,
        created_by_user_id=user.id,
        visits=0,
        **{field: getattr(data, field) for field in _FIELDS},
    )
    session.add(link)
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
        summary=f"Showcase link created: {link.label}",
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
    if data.clear_tenant:
        link.brand_tenant_id = None
    elif data.brand_tenant_id is not None:
        await _check_tenant(session, data.brand_tenant_id)
        link.brand_tenant_id = data.brand_tenant_id
    # The scenario's rules hold on the result, whichever fields changed.
    if link.team_budget >= link.order_value:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The team budget must be less than the order value, or there is no margin.",
        )
    if link.starts_at.tzinfo is None or link.ends_at.tzinfo is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Give times with their time zone."
        )
    if link.ends_at <= link.starts_at:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "The link must end after it starts."
        )
    if (link.ends_at - link.starts_at).days > 366:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "A showcase link can run for a year at most."
        )
    after = _audit_values(link)
    changed = {
        key: {"from": before[key], "to": after[key]} for key in after if before[key] != after[key]
    }
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
    await session.execute(delete(ShowcaseRun).where(ShowcaseRun.link_id == link.id))
    await session.delete(link)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="showcase_link",
        entity_id=link_id,
        actor=user,
        actor_type="platform",
        changes=values,
        summary=f"Showcase link deleted: {label}",
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
}


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
        # The message for the page; the state for a page that wants to say more.
        raise HTTPException(
            status.HTTP_410_GONE, _GONE[state], headers={"X-Showcase-Status": state}
        )


async def describe(session: AsyncSession, code: str, request: Request) -> dict[str, Any]:
    """What the page shows before a story starts: whose story, and whether the link works now."""
    throttle(request, STEP)
    link = await _link_by_code(session, code)
    state = _status(link)
    company, logo = await _company(session, link)
    out: dict[str, Any] = {"status": state, "company": company, "logo_url": logo}
    if state == "scheduled":
        out["starts_at"] = link.starts_at.isoformat()
    if state != "live":
        out["message"] = _GONE[state]
        return out
    out.update(
        {
            "ends_at": link.ends_at.isoformat(),
            "client": link.client_name,
            "role": link.role_title,
            "headcount": link.headcount,
            "months": link.months,
            "order_value": f"{link.order_value:.2f}",
        }
    )
    return out


def _scenario(link: ShowcaseLink, company: str) -> story.Scenario:
    return story.Scenario(
        company=company,
        client=link.client_name,
        role=link.role_title,
        headcount=link.headcount,
        months=link.months,
        order_value=link.order_value,
        team_budget=link.team_budget,
        laptop_price=link.laptop_price,
        payment_days=link.payment_days,
    )


async def start_run(session: AsyncSession, code: str, request: Request) -> dict[str, Any]:
    """A new story for this visitor, and the key their page keeps to carry on with it."""
    throttle(request, START)
    link = await _link_by_code(session, code)
    _require_live(link)
    company, logo = await _company(session, link)
    now = _now()
    # Stories nobody has touched for days are of no use to anyone: drop them.
    await session.execute(
        delete(ShowcaseRun).where(
            ShowcaseRun.link_id == link.id, ShowcaseRun.updated_at < now - KEEP_RUNS_FOR
        )
    )
    key = secrets.token_urlsafe(24)
    today = now.astimezone(story.IST).date()
    run = ShowcaseRun(
        id=uuid.uuid4(),
        link_id=link.id,
        key_hash=_hash(key),
        state=story.start(_scenario(link, company), today=today),
        turns=0,
    )
    session.add(run)
    link.visits = (link.visits or 0) + 1
    link.last_visit_at = now
    await session.commit()
    return {"run_id": str(run.id), "key": key, **_present(link, run, company, logo)}


def _present(
    link: ShowcaseLink, run: ShowcaseRun, company: str, logo: str | None
) -> dict[str, Any]:
    return {
        "run_id": str(run.id),
        "company": company,
        "logo_url": logo,
        "ends_at": link.ends_at.isoformat(),
        "turns_left": max(0, story.MAX_TURNS - run.turns),
        **story.view(run.state),
    }


async def _run_for(
    session: AsyncSession, code: str, run_id: uuid.UUID, key: str | None, *, lock: bool
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
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This story has gone. Start a new one.")
    return link, run


async def read_run(
    session: AsyncSession, code: str, run_id: uuid.UUID, key: str | None, request: Request
) -> dict[str, Any]:
    throttle(request, STEP)
    link, run = await _run_for(session, code, run_id, key, lock=False)
    company, logo = await _company(session, link)
    return _present(link, run, company, logo)


async def _step(
    session: AsyncSession,
    code: str,
    run_id: uuid.UUID,
    key: str | None,
    request: Request,
    move: Any,
) -> dict[str, Any]:
    throttle(request, STEP)
    link, run = await _run_for(session, code, run_id, key, lock=True)
    if run.turns >= story.MAX_TURNS:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This story has run its length. Start over for a fresh one."
        )
    state = copy.deepcopy(run.state)
    try:
        move(state)
    except story.StoryError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    run.state = state
    run.turns += 1
    await session.commit()
    company, logo = await _company(session, link)
    return _present(link, run, company, logo)


async def say(
    session: AsyncSession,
    code: str,
    run_id: uuid.UUID,
    key: str | None,
    text: str,
    request: Request,
) -> dict[str, Any]:
    return await _step(session, code, run_id, key, request, lambda state: story.say(state, text))


async def decide(
    session: AsyncSession,
    code: str,
    run_id: uuid.UUID,
    key: str | None,
    action_id: str,
    confirm: bool,
    request: Request,
) -> dict[str, Any]:
    return await _step(
        session,
        code,
        run_id,
        key,
        request,
        lambda state: story.decide(state, action_id, confirm),
    )
