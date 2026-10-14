"""Statutory change notices: published once by the platform, acted on per workspace.

- Only AskMira's super admin publishes or withdraws a notice.
- A workspace sees the notices for the national levies and for the states it
  has locations in, each as:
    * `in_place` — an approved rule-set version of that type and jurisdiction
      takes effect on or after the notice's date;
    * `drafted` — such a version exists but is not approved yet;
    * `action_needed` — nothing yet.
- Drafting from a notice goes through the ordinary rule-set service: a new,
  unapproved version, which a second admin approves. A notice without reviewed
  rules cannot be drafted from; the workspace enters the rules itself.
- A daily job tells each workspace's admins about a notice needing action
  within 30 days of it taking effect, once.
- Coverage lists, for the national levies and each state with locations, which
  rule types have an approved version in force today. Whether a state levies
  PT or LWF at all is for the workspace to judge; this shows what is entered.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_filter, tenant_scoped
from app.models.audit import AuditAction
from app.models.org import Location
from app.models.payroll import PayrollRuleSet
from app.models.statutory_watch import StatutoryNotice, StatutoryNoticeAlert
from app.models.user import User, UserRole
from app.schemas.payroll_run import RuleSetCreate, RuleType
from app.services import audit
from app.services.notifications.dispatch import notify, register_event
from app.services.payroll import rule_sets

__all__ = [
    "NoticeIn",
    "coverage",
    "draft_from_notice",
    "notices_for",
    "publish",
    "send_alerts",
    "withdraw",
]

ALERT_WINDOW = timedelta(days=30)
NATIONAL = "IN"
NATIONAL_TYPES = ("pf", "esi", "tds")
STATE_TYPES = ("professional_tax", "labour_welfare_fund")

register_event(
    "statutory.change_due",
    "Statutory change: {title}",
    "{title} takes effect on {effective_from}. No rule set covers it yet in this workspace. "
    "Open Payroll > Rule sets to enter or draft it.",
)


class NoticeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_type: RuleType
    jurisdiction: str = Field(min_length=2, max_length=8, pattern=r"^[A-Z]{2,8}$")
    effective_from: date
    title: str = Field(min_length=5, max_length=200)
    summary: str = Field(min_length=10, max_length=5000)
    source_reference: str = Field(min_length=3, max_length=512)
    suggested_rules: dict[str, Any] | None = None


def _require_platform(user: User) -> None:
    if user.role is not UserRole.SUPER_ADMIN:
        raise permissions.forbidden("Statutory notices are published by the platform.")


def _read(notice: StatutoryNotice) -> dict[str, Any]:
    return {
        "id": str(notice.id),
        "rule_type": notice.rule_type,
        "jurisdiction": notice.jurisdiction,
        "effective_from": notice.effective_from.isoformat(),
        "title": notice.title,
        "summary": notice.summary,
        "source_reference": notice.source_reference,
        "has_suggested_rules": notice.suggested_rules is not None,
        "withdrawn": notice.withdrawn_at is not None,
    }


async def publish(
    session: AsyncSession, user: User, payload: NoticeIn, *, request: Request | None
) -> dict[str, Any]:
    _require_platform(user)
    row = StatutoryNotice(**payload.model_dump(), published_by_user_id=user.id)
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="statutory_notice",
        entity_id=row.id,
        actor=user,
        changes={
            "rule_type": row.rule_type,
            "jurisdiction": row.jurisdiction,
            "effective_from": row.effective_from.isoformat(),
        },
        summary=f"Published the statutory notice {row.title}",
        request=request,
    )
    await session.commit()
    return _read(row)


async def withdraw(
    session: AsyncSession, user: User, notice_id: uuid.UUID, *, request: Request | None
) -> dict[str, Any]:
    _require_platform(user)
    row = await session.get(StatutoryNotice, notice_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notice not found")
    if row.withdrawn_at is None:
        row.withdrawn_at = datetime.now(UTC)
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="statutory_notice",
            entity_id=row.id,
            actor=user,
            changes={"withdrawn": True},
            summary=f"Withdrew the notice {row.title}",
            request=request,
        )
        await session.commit()
    return _read(row)


async def _jurisdictions(session: AsyncSession, user: User) -> set[str]:
    states = (
        await session.scalars(
            tenant_scoped(
                select(Location.state_code).where(Location.is_active.is_(True)).distinct(),
                Location,
                user,
            )
        )
    ).all()
    return {NATIONAL} | {s for s in states if s}


async def _status(session: AsyncSession, user: User, notice: StatutoryNotice) -> str:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(PayrollRuleSet).where(
                    PayrollRuleSet.rule_type == notice.rule_type,
                    PayrollRuleSet.jurisdiction == notice.jurisdiction,
                    PayrollRuleSet.effective_from >= notice.effective_from,
                ),
                PayrollRuleSet,
                user,
            )
        )
    ).all()
    if any(r.approved_at is not None for r in rows):
        return "in_place"
    return "drafted" if rows else "action_needed"


async def notices_for(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """The notices that touch this workspace, newest effective date first."""
    if not (permissions.is_hr(user) or user.role is UserRole.SUPER_ADMIN):
        raise permissions.forbidden("Statutory notices are for HR and payroll.")
    wanted = await _jurisdictions(session, user)
    stmt = select(StatutoryNotice).order_by(StatutoryNotice.effective_from.desc()).limit(200)
    if user.role is not UserRole.SUPER_ADMIN:
        stmt = stmt.where(
            StatutoryNotice.jurisdiction.in_(wanted), StatutoryNotice.withdrawn_at.is_(None)
        )
    notices = (await session.scalars(stmt)).all()
    return [{**_read(n), "status": await _status(session, user, n)} for n in notices]


async def draft_from_notice(
    session: AsyncSession, user: User, notice_id: uuid.UUID, *, request: Request | None
) -> dict[str, Any]:
    """A new unapproved rule-set version from a notice's reviewed rules."""
    if not permissions.is_admin(user):
        raise permissions.forbidden("Rule sets are entered by an admin.")
    notice = await session.get(StatutoryNotice, notice_id)
    if notice is None or notice.withdrawn_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notice not found")
    if notice.suggested_rules is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This notice has no reviewed rules to draft from. Enter the rule set "
            "yourself from the source it cites.",
        )
    created = await rule_sets.create_rule_set(
        session,
        user,
        RuleSetCreate(
            rule_type=notice.rule_type,
            jurisdiction=notice.jurisdiction,
            effective_from=notice.effective_from,
            rules=notice.suggested_rules,
            source_reference=notice.source_reference,
            notes=f"Drafted from the statutory notice: {notice.title}",
        ),
        request=request,
    )
    return created.model_dump(mode="json")


async def coverage(
    session: AsyncSession, user: User, *, on: date | None = None
) -> list[dict[str, Any]]:
    """Which rule types have an approved version in force today, per jurisdiction."""
    permissions.require_hr(user, detail="Statutory coverage is for HR and payroll.")
    day = on or date.today()
    places = sorted(await _jurisdictions(session, user))
    in_force = (
        await session.scalars(
            tenant_scoped(
                select(PayrollRuleSet).where(
                    PayrollRuleSet.approved_at.is_not(None),
                    PayrollRuleSet.effective_from <= day,
                    or_(PayrollRuleSet.effective_to.is_(None), PayrollRuleSet.effective_to >= day),
                ),
                PayrollRuleSet,
                user,
            )
        )
    ).all()
    have = {(r.rule_type, r.jurisdiction): r for r in in_force}
    out = []
    for place in places:
        types = NATIONAL_TYPES if place == NATIONAL else STATE_TYPES
        out.append(
            {
                "jurisdiction": place,
                "rules": [
                    {
                        "rule_type": t,
                        "in_force": (t, place) in have,
                        "effective_from": have[(t, place)].effective_from.isoformat()
                        if (t, place) in have
                        else None,
                    }
                    for t in types
                ],
            }
        )
    return out


async def send_alerts(session: AsyncSession, actor: User, *, today: date) -> dict[str, Any]:
    """Scheduler: tell this workspace's admins about notices needing action soon, once."""
    wanted = await _jurisdictions(session, actor)
    due = (
        await session.scalars(
            select(StatutoryNotice).where(
                StatutoryNotice.jurisdiction.in_(wanted),
                StatutoryNotice.withdrawn_at.is_(None),
                StatutoryNotice.effective_from <= today + ALERT_WINDOW,
            )
        )
    ).all()
    alerted = set(
        (
            await session.scalars(
                select(StatutoryNoticeAlert.notice_id).where(
                    tenant_filter(StatutoryNoticeAlert, actor.tenant_id)
                )
            )
        ).all()
    )
    admins = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(User.id).where(User.role == UserRole.ADMIN, User.is_active.is_(True)),
                    User,
                    actor,
                )
            )
        ).all()
    )
    sent = 0
    for notice in due:
        if notice.id in alerted or await _status(session, actor, notice) != "action_needed":
            continue
        session.add(StatutoryNoticeAlert(tenant_id=actor.tenant_id, notice_id=notice.id))
        if admins:
            await notify(
                session,
                tenant_id=actor.tenant_id,
                event_key="statutory.change_due",
                recipient_user_ids=admins,
                payload={
                    "title": notice.title,
                    "effective_from": notice.effective_from.strftime("%d %b %Y"),
                },
                commit=False,
            )
        sent += 1
    await session.commit()
    return {"alerted": sent}
