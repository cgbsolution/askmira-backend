"""Work anniversaries, marked by the company without anybody having to remember.

Once a day, for each anniversary the tenant marks (1, 3, 5, 10 years — its
choice), everybody whose joining date falls on today gets a post from the
company and, where the rule says so and rewards are on, points.

Two rules carried over from the celebrations calendar:

- **Consent.** A work anniversary on the company feed is a disclosure, and it
  is the person's to make. Somebody who has not chosen to share theirs gets the
  post privately — they are still thanked; nobody else is told.
- **Once.** A milestone is marked once per person, so a re-run, a missed day
  caught up, or a changed rule never posts it twice.
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import extract, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.culture import MilestoneRule, Shoutout, ShoutoutVisibility
from app.models.culture_depth import EventPrivacyPreference
from app.models.employee import Employee, EmploymentStatus
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.notifications.dispatch import notify, register_event

__all__ = ["DEFAULT_MESSAGE", "celebrate_due", "list_rules", "save_rules"]

DEFAULT_MESSAGE = (
    "Happy {years}-year work anniversary, {name}! Thank you from all of us at {company}."
)

register_event(
    "culture.milestone",
    "Happy work anniversary",
    "{message}{points_line}",
)


def _render(template: str, *, name: str, years: int, company: str) -> str:
    return (
        template.replace("{name}", name)
        .replace("{years}", str(years))
        .replace("{company}", company)
    )


def _read(rule: MilestoneRule) -> dict[str, Any]:
    return {
        "id": str(rule.id),
        "years": rule.years,
        "points": rule.points,
        "message": rule.message,
        "active": rule.active,
    }


async def list_rules(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    permissions.require_hr(user, detail="Milestone rules are HR's.")
    rows = (
        await session.scalars(
            tenant_scoped(select(MilestoneRule).order_by(MilestoneRule.years), MilestoneRule, user)
        )
    ).all()
    return [_read(r) for r in rows]


async def save_rules(
    session: AsyncSession,
    user: User,
    rules: list[dict[str, Any]],
    *,
    request: Request | None = None,
) -> list[dict[str, Any]]:
    """Replace the set of marked anniversaries."""
    permissions.require_hr(user, detail="Milestone rules are HR's.")
    years = [int(r["years"]) for r in rules]
    if len(set(years)) != len(years):
        raise HTTPException(status_code=422, detail="Each anniversary appears once.")
    if any(y < 1 or y > 60 for y in years):
        raise HTTPException(status_code=422, detail="Anniversaries are 1 to 60 years.")
    if any((r.get("points") or 0) < 0 for r in rules):
        raise HTTPException(status_code=422, detail="Points cannot be negative.")
    existing = {
        r.years: r
        for r in (
            await session.scalars(tenant_scoped(select(MilestoneRule), MilestoneRule, user))
        ).all()
    }
    before = [_read(r) for r in existing.values()]
    for rule in rules:
        row = existing.pop(int(rule["years"]), None)
        if row is None:
            row = MilestoneRule(tenant_id=user.tenant_id, years=int(rule["years"]), message="")
            session.add(row)
        row.points = int(rule["points"]) if rule.get("points") else None
        row.message = (rule.get("message") or "").strip() or DEFAULT_MESSAGE
        row.active = bool(rule.get("active", True))
    for row in existing.values():
        await session.delete(row)
    await session.flush()
    after = await list_rules(session, user)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="milestone_rules",
        entity_id=None,
        summary="Work anniversary milestones changed",
        changes={"from": before, "to": after},
        request=request,
    )
    return after


def _anniversary_today(joined: date, today: date) -> bool:
    if (joined.month, joined.day) == (today.month, today.day):
        return True
    # Joined on 29 February: marked on the 28th in a year without one.
    return (
        joined.month == 2
        and joined.day == 29
        and not calendar.isleap(today.year)
        and (today.month, today.day) == (2, 28)
    )


async def celebrate_due(session: AsyncSession, actor: User, *, today: date) -> dict[str, int]:
    """Scheduler-callable: today's anniversaries, posted and (optionally) rewarded."""
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
    if not rules:
        return {"posted": 0, "rewarded": 0}
    tenant = await session.get(Tenant, actor.tenant_id)
    company = tenant.name if tenant else "the company"
    rewards_on = bool(tenant and (tenant.enabled_modules or {}).get("rewards"))

    month_matches = [today.month] + ([2] if today.month == 2 else [])
    candidates = (
        await session.scalars(
            tenant_scoped(
                select(Employee).where(
                    Employee.status == EmploymentStatus.ACTIVE,
                    Employee.deleted_at.is_(None),
                    Employee.date_of_joining.is_not(None),
                    extract("month", Employee.date_of_joining).in_(month_matches),
                ),
                Employee,
                actor,
            )
        )
    ).all()
    due = [
        (person, today.year - person.date_of_joining.year)
        for person in candidates
        if _anniversary_today(person.date_of_joining, today)
        and (today.year - person.date_of_joining.year) in rules
    ]
    if not due:
        return {"posted": 0, "rewarded": 0}

    ids = [p.id for p, _ in due]
    already = {
        (to, years)
        for to, years in (
            await session.execute(
                tenant_scoped(
                    select(Shoutout.to_employee_id, Shoutout.milestone_years).where(
                        Shoutout.to_employee_id.in_(ids), Shoutout.milestone_years.is_not(None)
                    ),
                    Shoutout,
                    actor,
                )
            )
        ).all()
    }
    sharing = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(EventPrivacyPreference.employee_id).where(
                        EventPrivacyPreference.employee_id.in_(ids),
                        EventPrivacyPreference.share_work_anniversary.is_(True),
                    ),
                    EventPrivacyPreference,
                    actor,
                )
            )
        ).all()
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

    posted = rewarded = 0
    for person, years in due:
        if (person.id, years) in already:
            continue
        rule = rules[years]
        message = _render(
            rule.message, name=person.first_name or person.full_name, years=years, company=company
        )
        post = Shoutout(
            tenant_id=actor.tenant_id,
            from_employee_id=None,
            to_employee_id=person.id,
            message=message,
            visibility=(
                ShoutoutVisibility.COMPANY if person.id in sharing else ShoutoutVisibility.PRIVATE
            ),
            milestone_years=years,
        )
        session.add(post)
        await session.flush()
        posted += 1
        points_line = ""
        if rule.points and rewards_on:
            from app.services.rewards import service as rewards

            try:
                async with session.begin_nested():
                    await rewards.award_points(
                        session,
                        actor,
                        employee_id=person.id,
                        points=rule.points,
                        awarded_on=today,
                        source_type="milestone",
                        source_id=post.id,
                    )
                post.points_awarded = rule.points
                rewarded += 1
                points_line = f" {rule.points} points have been added to your balance."
            except HTTPException:
                # No valuation or no budget: the post still goes; the points do not.
                points_line = ""
        await audit.record(
            session,
            tenant_id=actor.tenant_id,
            actor=actor,
            actor_type="system",
            action=AuditAction.CREATE,
            entity_type="shoutout",
            entity_id=post.id,
            summary=f"{years}-year work anniversary marked"
            + (f" with {post.points_awarded} points" if post.points_awarded else ""),
        )
        login = logins.get(person.id)
        if login is not None:
            await notify(
                session,
                tenant_id=actor.tenant_id,
                event_key="culture.milestone",
                recipient_user_ids=[login],
                payload={"message": message, "points_line": points_line},
                commit=False,
            )
    await session.commit()
    return {"posted": posted, "rewarded": rewarded}
