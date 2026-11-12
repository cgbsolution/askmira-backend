"""Birthdays and work anniversaries: who sees them, the card, and the wish.

Every morning the scheduler wishes everybody whose birthday or work
anniversary falls today:

- **a card** — a picture drawn from the template HR keeps for that occasion
  (Culture → Celebrations), with the person's photo, name and, for an
  anniversary, the years completed;
- **an email** to the person carrying the card, in the workspace's branded
  layout, with wording HR can edit (Settings → Notifications → emails);
- **a post on the company feed** with the card, and an in-app notice.

Who sees the post. The workspace decides the default (Culture → Celebrations →
Settings): out of the box everybody's birthday and anniversary is shared with
the whole workspace. Each person can still change their own on the feed (My
celebrations) — a birthday to their department only, or neither — and a choice
they made always beats the default. Somebody who keeps theirs private is still
wished: by email and a post only they see.

Photos. The card emailed to the person carries their own photo. The card on
the shared feed carries it only if they let colleagues see their photo (My
privacy → photo in the directory); otherwise their initials, so a feed post
never shows a face its owner kept to HR.
"""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import dataclasses
import logging
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from fastapi import HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.culture import (
    CelebrationTemplate,
    MilestoneRule,
    Shoutout,
    ShoutoutVisibility,
)
from app.models.culture_depth import EventPrivacyPreference
from app.models.employee import Employee, EmployeeProfile, EmploymentStatus
from app.models.notifications import Channel, DigestMode
from app.models.org import Department, LegalEntity
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.culture import comms as comms_engine
from app.services.culture.cards import (
    PLACEHOLDERS,
    CardDesign,
    CardPerson,
    fill,
    ordinal,
    presets_for,
    render_card,
)
from app.services.culture.cards import preset as find_preset
from app.services.notifications import dispatch
from app.services.notifications.adapters import (
    InlineImage,
    OutboundMessage,
    PermanentFailure,
    TransientFailure,
)
from app.services.notifications.dispatch import notify, register_event
from app.services.storage import StorageError, get_storage

__all__ = [
    "CelebrationSettings",
    "create_template",
    "delete_template",
    "design_for",
    "effective_privacy",
    "get_settings",
    "list_templates",
    "preview",
    "run_daily",
    "save_settings",
    "set_in_use",
    "update_template",
]

logger = logging.getLogger(__name__)

Kind = Literal["birthday", "work_anniversary"]
KINDS: tuple[Kind, ...] = ("birthday", "work_anniversary")
_SETTINGS_KEY = "celebrations"
CARD_CID = "celebration-card"

_DELIVERY = {
    Channel.IN_APP: DigestMode.IMMEDIATE,
    Channel.PUSH: DigestMode.IMMEDIATE,
}
# The email is sent here, with the card in it, to the person's work email —
# they may have no login yet. In-app and push go through the notification
# engine like everything else. The wording of all three is editable.
register_event(
    "culture.birthday",
    "Happy birthday, {first_name}!",
    "{message}",
    preferences=_DELIVERY,
)
register_event(
    "culture.work_anniversary",
    "Happy {years_ordinal} work anniversary, {first_name}!",
    "{message}{points_line}",
    preferences=_DELIVERY,
)


# ═══════════════════════════════════════════════════════════════════════════
# Workspace settings
# ═══════════════════════════════════════════════════════════════════════════


class CelebrationSettings(BaseModel):
    """What the workspace does for birthdays and anniversaries."""

    birthdays: bool = True
    work_anniversaries: bool = True
    #: People who never chose are shared with the workspace. Their own choice wins.
    share_by_default: bool = True
    default_birthday_scope: Literal["team", "company"] = "company"
    send_email: bool = True
    post_to_feed: bool = True


async def get_settings(session: AsyncSession, tenant_id: uuid.UUID) -> CelebrationSettings:
    tenant = await session.get(Tenant, tenant_id)
    raw = ((tenant.settings or {}).get(_SETTINGS_KEY) or {}) if tenant else {}
    try:
        return CelebrationSettings.model_validate(raw)
    except ValueError:
        return CelebrationSettings()


async def save_settings(
    session: AsyncSession,
    user: User,
    payload: CelebrationSettings,
    *,
    request: Request | None = None,
) -> CelebrationSettings:
    permissions.require_hr(user, detail="Celebration settings are HR's.")
    tenant = await session.get(Tenant, user.tenant_id)
    before = await get_settings(session, user.tenant_id)
    stored = dict(tenant.settings or {})
    stored[_SETTINGS_KEY] = payload.model_dump(mode="json")
    tenant.settings = stored
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="celebration_settings",
        entity_id=tenant.id,
        changes={
            "before": before.model_dump(mode="json"),
            "after": payload.model_dump(mode="json"),
        },
        summary="Birthday and work anniversary settings changed",
        request=request,
    )
    return payload


def _default_privacy(settings: CelebrationSettings) -> comms_engine.EventPrivacy | None:
    if not settings.share_by_default:
        return None
    return comms_engine.EventPrivacy(
        employee_id="",
        share_birthday=True,
        share_work_anniversary=True,
        birthday_scope=settings.default_birthday_scope,
    )


async def effective_privacy(
    session: AsyncSession,
    user: User,
    employee_ids: list[uuid.UUID],
    settings: CelebrationSettings | None = None,
) -> dict[str, comms_engine.EventPrivacy]:
    """Each person's sharing: their own choice, else the workspace default.

    Somebody absent from the result shares nothing (default off, no choice).
    """
    settings = settings or await get_settings(session, user.tenant_id)
    chosen = {
        str(p.employee_id): comms_engine.EventPrivacy(
            employee_id=str(p.employee_id),
            share_birthday=p.share_birthday,
            share_work_anniversary=p.share_work_anniversary,
            birthday_scope=p.birthday_scope,
        )
        for p in (
            await session.scalars(
                tenant_scoped(
                    select(EventPrivacyPreference).where(
                        EventPrivacyPreference.employee_id.in_(employee_ids)
                    ),
                    EventPrivacyPreference,
                    user,
                )
            )
        ).all()
    }
    default = _default_privacy(settings)
    if default is not None:
        for employee_id in employee_ids:
            key = str(employee_id)
            if key not in chosen:
                chosen[key] = comms_engine.EventPrivacy(
                    employee_id=key,
                    share_birthday=True,
                    share_work_anniversary=True,
                    birthday_scope=default.birthday_scope,
                )
    return chosen


# ═══════════════════════════════════════════════════════════════════════════
# Templates
# ═══════════════════════════════════════════════════════════════════════════


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such card template.")


def _kind(value: str) -> Kind:
    if value not in KINDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A card is for a birthday or a work anniversary.",
        )
    return value  # type: ignore[return-value]


def _read(row: CelebrationTemplate) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "name": row.name,
        "design": CardDesign.model_validate(row.design or {}).model_dump(mode="json"),
        "in_use": row.is_default,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _templates(session: AsyncSession, user: User) -> list[CelebrationTemplate]:
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(CelebrationTemplate).order_by(
                        CelebrationTemplate.kind, CelebrationTemplate.created_at
                    ),
                    CelebrationTemplate,
                    user,
                )
            )
        ).all()
    )


async def _template(
    session: AsyncSession, user: User, template_id: uuid.UUID
) -> CelebrationTemplate:
    row = await session.scalar(
        tenant_scoped(
            select(CelebrationTemplate).where(CelebrationTemplate.id == template_id),
            CelebrationTemplate,
            user,
        )
    )
    if row is None:
        raise _not_found()
    return row


async def list_templates(session: AsyncSession, user: User) -> dict[str, Any]:
    permissions.require_hr(user, detail="Card templates are HR's.")
    rows = await _templates(session, user)
    return {
        "templates": [_read(r) for r in rows],
        "presets": [
            {
                "key": p.key,
                "kind": p.kind,
                "name": p.name,
                "design": p.design.model_dump(mode="json"),
            }
            for kind in KINDS
            for p in presets_for(kind)
        ],
        "placeholders": [{"token": k, "meaning": v} for k, v in PLACEHOLDERS.items()],
    }


async def _audit_template(
    session: AsyncSession,
    user: User,
    row: CelebrationTemplate,
    action: AuditAction,
    summary: str,
    request: Request | None,
    changes: dict[str, Any] | None = None,
) -> None:
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=action,
        entity_type="celebration_template",
        entity_id=row.id,
        changes=changes,
        summary=summary,
        request=request,
    )


async def create_template(
    session: AsyncSession,
    user: User,
    *,
    kind: str,
    name: str,
    design: CardDesign | None = None,
    preset_key: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """A new template, from a preset or a design. The first of its kind is put in use."""
    permissions.require_hr(user, detail="Card templates are HR's.")
    kind = _kind(kind)
    if design is None:
        chosen = find_preset(preset_key) if preset_key else None
        if preset_key and (chosen is None or chosen.kind != kind):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="That starting design is not one for this occasion.",
            )
        design = chosen.design if chosen else presets_for(kind)[0].design
    name = (name or "").strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Give the template a name."
        )
    has_default = await session.scalar(
        tenant_scoped(
            select(CelebrationTemplate.id).where(
                CelebrationTemplate.kind == kind, CelebrationTemplate.is_default.is_(True)
            ),
            CelebrationTemplate,
            user,
        )
    )
    row = CelebrationTemplate(
        tenant_id=user.tenant_id,
        kind=kind,
        name=name[:120],
        design=design.model_dump(mode="json"),
        is_default=has_default is None,
    )
    session.add(row)
    await session.flush()
    await _audit_template(
        session,
        user,
        row,
        AuditAction.CREATE,
        f"Card template '{row.name}' added for {kind.replace('_', ' ')}s",
        request,
        {"design": row.design, "in_use": row.is_default},
    )
    await session.refresh(row)
    return _read(row)


async def update_template(
    session: AsyncSession,
    user: User,
    template_id: uuid.UUID,
    *,
    name: str | None = None,
    design: CardDesign | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    permissions.require_hr(user, detail="Card templates are HR's.")
    row = await _template(session, user, template_id)
    before = {"name": row.name, "design": row.design}
    if name is not None:
        if not name.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Give the template a name."
            )
        row.name = name.strip()[:120]
    if design is not None:
        row.design = design.model_dump(mode="json")
    await session.flush()
    await _audit_template(
        session,
        user,
        row,
        AuditAction.UPDATE,
        f"Card template '{row.name}' changed",
        request,
        {"before": before, "after": {"name": row.name, "design": row.design}},
    )
    await session.refresh(row)
    return _read(row)


async def set_in_use(
    session: AsyncSession, user: User, template_id: uuid.UUID, *, request: Request | None = None
) -> dict[str, Any]:
    """Use this template for its occasion from tomorrow morning's wishes on."""
    permissions.require_hr(user, detail="Card templates are HR's.")
    row = await _template(session, user, template_id)
    others = await session.scalars(
        tenant_scoped(
            select(CelebrationTemplate).where(
                CelebrationTemplate.kind == row.kind,
                CelebrationTemplate.is_default.is_(True),
                CelebrationTemplate.id != row.id,
            ),
            CelebrationTemplate,
            user,
        )
    )
    for other in others.all():
        other.is_default = False
    # The partial unique index allows one in use per occasion: clear first.
    await session.flush()
    row.is_default = True
    await session.flush()
    await _audit_template(
        session,
        user,
        row,
        AuditAction.UPDATE,
        f"Card template '{row.name}' now used for {row.kind.replace('_', ' ')}s",
        request,
    )
    await session.refresh(row)
    return _read(row)


async def delete_template(
    session: AsyncSession, user: User, template_id: uuid.UUID, *, request: Request | None = None
) -> None:
    """Remove a template. If it was in use, the newest other one takes over."""
    permissions.require_hr(user, detail="Card templates are HR's.")
    row = await _template(session, user, template_id)
    was_in_use = row.is_default
    await _audit_template(
        session,
        user,
        row,
        AuditAction.DELETE,
        f"Card template '{row.name}' removed",
        request,
        {"design": row.design, "in_use": was_in_use},
    )
    await session.delete(row)
    await session.flush()
    if was_in_use:
        successor = await session.scalar(
            tenant_scoped(
                select(CelebrationTemplate)
                .where(CelebrationTemplate.kind == row.kind)
                .order_by(CelebrationTemplate.updated_at.desc())
                .limit(1),
                CelebrationTemplate,
                user,
            )
        )
        if successor is not None:
            successor.is_default = True
            await session.flush()


async def design_for(session: AsyncSession, user: User, kind: str) -> CardDesign:
    """The design in use for an occasion: HR's template, else the built-in one."""
    row = await session.scalar(
        tenant_scoped(
            select(CelebrationTemplate).where(
                CelebrationTemplate.kind == kind, CelebrationTemplate.is_default.is_(True)
            ),
            CelebrationTemplate,
            user,
        )
    )
    if row is not None:
        try:
            return CardDesign.model_validate(row.design or {})
        except ValueError:
            logger.warning("Card template %s has an unreadable design; using the built-in", row.id)
    return presets_for(kind)[0].design


# ═══════════════════════════════════════════════════════════════════════════
# Drawing a card for a person
# ═══════════════════════════════════════════════════════════════════════════


async def _stored(key: str | None) -> bytes | None:
    if not key:
        return None
    with contextlib.suppress(StorageError):
        return await get_storage().get(key)
    return None


@dataclass(slots=True)
class _Context:
    """What a card needs that is the same for everybody in the workspace."""

    company: str
    tenant_logo: str | None
    entity_logos: dict[uuid.UUID, tuple[str, str | None]]
    departments: dict[uuid.UUID, str]


async def _context(session: AsyncSession, user: User) -> _Context:
    tenant = await session.get(Tenant, user.tenant_id)
    entities = {
        row.id: (row.name, row.logo_key)
        for row in (
            await session.scalars(tenant_scoped(select(LegalEntity), LegalEntity, user))
        ).all()
    }
    departments = {
        row.id: row.name
        for row in (
            await session.scalars(tenant_scoped(select(Department), Department, user))
        ).all()
    }
    return _Context(
        company=tenant.name if tenant else "",
        tenant_logo=tenant.logo_key if tenant else None,
        entity_logos=entities,
        departments=departments,
    )


async def _photo_key(session: AsyncSession, user: User, employee_id: uuid.UUID) -> str | None:
    return await session.scalar(
        tenant_scoped(
            select(EmployeeProfile.photo_storage_key).where(
                EmployeeProfile.employee_id == employee_id
            ),
            EmployeeProfile,
            user,
        )
    )


def _first_name(employee: Employee) -> str:
    """What a greeting calls them. An import that put the whole name in the
    first-name field ("Roja Bhaskar Poojari", no last name) gives its first word."""
    first = (employee.first_name or "").strip()
    if first and not (employee.last_name or "").strip() and " " in first:
        return first.split()[0]
    return first or employee.full_name


async def _person(
    session: AsyncSession,
    user: User,
    ctx: _Context,
    employee: Employee,
    *,
    years: int | None,
    with_photo: bool,
) -> CardPerson:
    """The person as a card shows them. The company is their own legal entity
    when they have one, with its logo; else the workspace's."""
    entity = ctx.entity_logos.get(employee.legal_entity_id) if employee.legal_entity_id else None
    # Their company's logo; the workspace's when it has none or it cannot be read.
    logo = await _stored(entity[1] if entity else None) or await _stored(ctx.tenant_logo)
    photo = await _stored(await _photo_key(session, user, employee.id)) if with_photo else None
    return CardPerson(
        first_name=_first_name(employee),
        full_name=employee.full_name,
        company=(entity[0] if entity else None) or ctx.company,
        years=years,
        designation=employee.designation,
        department=ctx.departments.get(employee.department_id) if employee.department_id else None,
        photo=photo,
        logo=logo,
    )


async def _draw(design: CardDesign, person: CardPerson) -> bytes:
    return await asyncio.to_thread(render_card, design, person)


async def preview(
    session: AsyncSession,
    user: User,
    *,
    kind: str,
    design: CardDesign,
    employee_id: uuid.UUID | None = None,
    years: int | None = None,
) -> bytes:
    """The card as it would go out — for a chosen person, else the viewer, else a sample."""
    permissions.require_hr(user, detail="Card templates are HR's.")
    kind = _kind(kind)
    ctx = await _context(session, user)
    target_id = employee_id or user.employee_id
    employee = None
    if target_id is not None:
        employee = await session.scalar(
            tenant_scoped(select(Employee).where(Employee.id == target_id), Employee, user)
        )
        if employee is None and employee_id is not None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such employee.")
    if years is None and kind == "work_anniversary":
        joined = employee.date_of_joining if employee else None
        years = max(1, date.today().year - joined.year) if joined else 3
    if employee is None:
        person = CardPerson(
            first_name="Aanya",
            full_name="Aanya Sharma",
            company=ctx.company,
            years=years,
            designation="Senior Analyst",
            department="Finance",
            logo=await _stored(ctx.tenant_logo),
        )
    else:
        person = await _person(session, user, ctx, employee, years=years, with_photo=True)
    return await _draw(design, person)


async def send_test(
    session: AsyncSession, user: User, *, kind: str, design: CardDesign
) -> dict[str, Any]:
    """The wish email with this card, sent to the HR person asking — to see it in an inbox."""
    card = await preview(session, user, kind=kind, design=design)
    me = None
    if user.employee_id is not None:
        me = await session.scalar(
            tenant_scoped(select(Employee).where(Employee.id == user.employee_id), Employee, user)
        )
    ctx = await _context(session, user)
    first = (_first_name(me) if me else None) or (user.full_name or "there").split()[0]
    years = None
    if kind == "work_anniversary":
        joined = me.date_of_joining if me else None
        years = max(1, date.today().year - joined.year) if joined else 3
    person = CardPerson(
        first_name=first,
        full_name=me.full_name if me else (user.full_name or first),
        company=ctx.company,
        years=years,
    )
    event_key = f"culture.{kind}"
    subject_template, body_template = (
        await dispatch.email_wording(session, user.tenant_id, event_key)
        or dispatch.DEFAULT_TEMPLATES[event_key]
    )
    payload = {
        "message": fill(design.message, design, person),
        "points_line": "",
        "first_name": person.first_name,
        "name": person.full_name,
        "years": str(years or ""),
        "years_ordinal": ordinal(years) if years else "",
        "company": person.company,
    }
    sent, failed = await _send_emails(
        session,
        user.tenant_id,
        [
            _Email(
                name=user.full_name or person.full_name,
                address=user.email,
                subject="[Test] " + dispatch.render(subject_template, payload),
                body=dispatch.render(body_template, payload),
                card=card,
                event_key=event_key,
            )
        ],
    )
    relay = await dispatch.email_relay(session, user.tenant_id)
    if failed or not sent:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "The test email could not be sent. Check Settings → Notifications → mail server."
            ),
        )
    return {"sent_to": user.email, "relay": relay}


async def card_image(session: AsyncSession, user: User, shoutout_id: uuid.UUID) -> bytes:
    """The card on a feed post, for somebody who may see the post."""
    from app.services.culture.feed import visibility_clause

    clause = await visibility_clause(session, user)
    key = await session.scalar(
        tenant_scoped(
            select(Shoutout.card_key).where(Shoutout.id == shoutout_id, clause), Shoutout, user
        )
    )
    data = await _stored(key)
    if not data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No card")
    return data


# ═══════════════════════════════════════════════════════════════════════════
# The morning job
# ═══════════════════════════════════════════════════════════════════════════


def _falls_today(original: date, today: date) -> bool:
    if (original.month, original.day) == (today.month, today.day):
        return True
    # Born or joined on 29 February: marked on the 28th in a common year.
    return (
        original.month == 2
        and original.day == 29
        and not calendar.isleap(today.year)
        and (today.month, today.day) == (2, 28)
    )


@dataclass(slots=True)
class _Wish:
    employee: Employee
    kind: Kind
    years: int | None


@dataclass(slots=True)
class _Email:
    name: str
    address: str
    subject: str
    body: str
    card: bytes
    event_key: str


async def _due(
    session: AsyncSession, actor: User, today: date, settings: CelebrationSettings
) -> list[_Wish]:
    conditions = []
    if settings.birthdays:
        conditions.append(Employee.date_of_birth.is_not(None))
    if settings.work_anniversaries:
        conditions.append(Employee.date_of_joining.is_not(None))
    if not conditions:
        return []
    people = (
        await session.scalars(
            tenant_scoped(
                select(Employee).where(
                    Employee.status == EmploymentStatus.ACTIVE,
                    Employee.deleted_at.is_(None),
                    or_(*conditions),
                ),
                Employee,
                actor,
            )
        )
    ).all()
    wishes: list[_Wish] = []
    for person in people:
        if (
            settings.birthdays
            and person.date_of_birth
            and _falls_today(person.date_of_birth, today)
        ):
            wishes.append(_Wish(person, "birthday", None))
        joined = person.date_of_joining
        if (
            settings.work_anniversaries
            and joined
            and _falls_today(joined, today)
            and today.year - joined.year >= 1
        ):
            wishes.append(_Wish(person, "work_anniversary", today.year - joined.year))
    return wishes


async def _already_wished(
    session: AsyncSession, actor: User, wishes: list[_Wish], today: date
) -> set[tuple[uuid.UUID, str]]:
    ids = [w.employee.id for w in wishes]
    if not ids:
        return set()
    rows = (
        await session.execute(
            tenant_scoped(
                select(
                    Shoutout.to_employee_id,
                    Shoutout.celebration,
                    Shoutout.celebration_year,
                    Shoutout.milestone_years,
                    Shoutout.created_at,
                ).where(
                    Shoutout.to_employee_id.in_(ids),
                    or_(Shoutout.celebration.is_not(None), Shoutout.milestone_years.is_not(None)),
                ),
                Shoutout,
                actor,
            )
        )
    ).all()
    done: set[tuple[uuid.UUID, str]] = set()
    years_for = {w.employee.id: w.years for w in wishes if w.kind == "work_anniversary"}
    for to, kind, year, milestone, _created in rows:
        if kind and year == today.year:
            done.add((to, kind))
        # A milestone post from before cards existed marks that anniversary too.
        if milestone is not None and years_for.get(to) == milestone:
            done.add((to, "work_anniversary"))
    return done


async def _send_email(adapter, message: OutboundMessage) -> str | None:
    try:
        await adapter.send(message)
        return None
    except (PermanentFailure, TransientFailure) as exc:
        return str(exc)
    except Exception as exc:
        return str(exc)


async def run_daily(session: AsyncSession, actor: User, *, today: date) -> dict[str, int]:
    """Scheduler-callable: today's birthdays and work anniversaries, wished once."""
    settings = await get_settings(session, actor.tenant_id)
    wishes = await _due(session, actor, today, settings)
    tally = {
        "birthdays": 0,
        "anniversaries": 0,
        "posted": 0,
        "emailed": 0,
        "rewarded": 0,
        "failed": 0,
    }
    if not wishes:
        return tally

    done = await _already_wished(session, actor, wishes, today)
    wishes = [w for w in wishes if (w.employee.id, w.kind) not in done]
    if not wishes:
        return tally

    ids = list({w.employee.id for w in wishes})
    privacy = await effective_privacy(session, actor, ids, settings)
    from app.services.dpdp import service as dpdp_service

    photo_ok = await dpdp_service.principals_permitting(
        session, actor, principal_ids=ids, capability="photo_in_directory"
    )
    logins = dict(
        (
            await session.execute(
                tenant_scoped(
                    select(User.employee_id, User.id).where(
                        User.employee_id.in_(ids), User.is_active.is_(True)
                    ),
                    User,
                    actor,
                )
            )
        ).all()
    )
    login_emails = dict(
        (
            await session.execute(
                tenant_scoped(
                    select(User.employee_id, User.email).where(
                        User.employee_id.in_(ids), User.is_active.is_(True)
                    ),
                    User,
                    actor,
                )
            )
        ).all()
    )
    rules = {
        r.years: r
        for r in (
            await session.scalars(
                tenant_scoped(
                    select(MilestoneRule).where(MilestoneRule.active.is_(True)),
                    MilestoneRule,
                    actor,
                )
            )
        ).all()
    }
    tenant = await session.get(Tenant, actor.tenant_id)
    rewards_on = bool(tenant and (tenant.enabled_modules or {}).get("rewards"))
    ctx = await _context(session, actor)
    designs = {kind: await design_for(session, actor, kind) for kind in KINDS}
    wording = {}
    for kind in KINDS:
        event_key = f"culture.{kind}"
        wording[kind] = (
            await dispatch.email_wording(session, actor.tenant_id, event_key)
            or dispatch.DEFAULT_TEMPLATES[event_key]
        )

    emails: list[_Email] = []
    storage = get_storage()
    for wish in wishes:
        employee, kind, years = wish.employee, wish.kind, wish.years
        design = designs[kind]
        setting = privacy.get(str(employee.id))
        shared = bool(
            setting
            and (setting.share_birthday if kind == "birthday" else setting.share_work_anniversary)
        )
        if not (shared and settings.post_to_feed):
            visibility = ShoutoutVisibility.PRIVATE
        elif kind == "birthday" and setting and setting.birthday_scope == "team":
            visibility = ShoutoutVisibility.DEPARTMENT
        else:
            visibility = ShoutoutVisibility.COMPANY

        own = await _person(
            session, actor, ctx, employee, years=years, with_photo=design.show_photo
        )
        own_card = await _draw(design, own)
        feed_card = own_card
        if (
            visibility is not ShoutoutVisibility.PRIVATE
            and own.photo is not None
            and employee.id not in photo_ok
        ):
            feed_card = await _draw(design, dataclasses.replace(own, photo=None))

        rule = rules.get(years) if kind == "work_anniversary" and years else None
        message = fill(design.message, design, own)
        if rule is not None and rule.message.strip():
            message = (
                rule.message.replace("{name}", own.first_name)
                .replace("{years}", str(years))
                .replace("{company}", own.company)
            )

        key = storage.new_key(actor.tenant_id, "celebrations", ".jpg")
        try:
            await storage.put(key, feed_card)
        except StorageError:
            logger.warning("Card for %s could not be stored; posting without it", employee.id)
            key = None

        post = Shoutout(
            tenant_id=actor.tenant_id,
            from_employee_id=None,
            to_employee_id=employee.id,
            message=message,
            visibility=visibility,
            milestone_years=years if kind == "work_anniversary" else None,
            celebration=kind,
            celebration_year=today.year,
            card_key=key,
        )
        try:
            async with session.begin_nested():
                session.add(post)
                await session.flush()
        except IntegrityError:
            # Another run wished them a moment ago.
            if key:
                with contextlib.suppress(StorageError):
                    await storage.delete(key)
            continue
        tally["posted"] += 1
        tally["birthdays" if kind == "birthday" else "anniversaries"] += 1

        points_line = ""
        if rule is not None and rule.points and rewards_on:
            from app.services.rewards import service as rewards

            try:
                async with session.begin_nested():
                    await rewards.award_points(
                        session,
                        actor,
                        employee_id=employee.id,
                        points=rule.points,
                        awarded_on=today,
                        source_type="milestone",
                        source_id=post.id,
                    )
                post.points_awarded = rule.points
                tally["rewarded"] += 1
                points_line = f" {rule.points} points have been added to your balance."
            except HTTPException:
                points_line = ""

        label = "birthday" if kind == "birthday" else f"{years}-year work anniversary"
        await audit.record(
            session,
            tenant_id=actor.tenant_id,
            actor=actor,
            actor_type="system",
            action=AuditAction.CREATE,
            entity_type="shoutout",
            entity_id=post.id,
            summary=f"{label[0].upper()}{label[1:]} wished ({visibility.value})"
            + (f" with {post.points_awarded} points" if post.points_awarded else ""),
        )

        payload = {
            "message": message,
            "points_line": points_line,
            "first_name": own.first_name,
            "name": own.full_name,
            "years": str(years or ""),
            "years_ordinal": ordinal(years) if years else "",
            "company": own.company,
        }
        login = logins.get(employee.id)
        if login is not None:
            await notify(
                session,
                tenant_id=actor.tenant_id,
                event_key=f"culture.{kind}",
                recipient_user_ids=[login],
                payload=payload,
                commit=False,
            )
        address = login_emails.get(employee.id) or employee.work_email or employee.personal_email
        if settings.send_email and address:
            subject_template, body_template = wording[kind]
            emails.append(
                _Email(
                    name=employee.full_name,
                    address=address,
                    subject=dispatch.render(subject_template, payload),
                    body=dispatch.render(body_template, payload),
                    card=own_card,
                    event_key=f"culture.{kind}",
                )
            )

    await session.commit()

    if emails:
        tally["emailed"], tally["failed"] = await _send_emails(session, actor.tenant_id, emails)
    return tally


async def _send_emails(
    session: AsyncSession, tenant_id: uuid.UUID, emails: list[_Email]
) -> tuple[int, int]:
    """After the posts are committed: a failed email never un-wishes anybody."""
    from app.services.notifications import email_brand

    if await dispatch.email_relay(session, tenant_id) == "registered":
        adapter = dispatch.get_adapter(Channel.EMAIL)
    else:
        try:
            adapter, _relay = await dispatch.email_adapter_for(session, tenant_id)
        except Exception:
            logger.warning("No mail relay for celebration emails in %s", tenant_id, exc_info=True)
            return 0, len(emails)
    sent = failed = 0
    for email in emails:
        rendered = await email_brand.compose(
            session,
            tenant_id,
            subject=email.subject,
            body=email.body,
            hero=InlineImage(cid=CARD_CID, data=email.card, mime_type="image/jpeg"),
        )
        message = OutboundMessage(
            recipient_name=email.name,
            recipient_email=email.address,
            recipient_phone=None,
            subject=email.subject,
            event_key=email.event_key,
            **email_brand.as_message_fields(rendered),
        )
        error = await _send_email(adapter, message)
        if error is None:
            sent += 1
        else:
            failed += 1
            logger.warning("Celebration email not sent: %s", error)
    return sent, failed
