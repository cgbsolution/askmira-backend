"""The culture feed and clubs (FRD 5.3, FS-CUL-01).

═══════════════════════════════════════════════════════════════════════════════
VISIBILITY IS A WHERE CLAUSE, NOT A FILTER ON THE WAY OUT
═══════════════════════════════════════════════════════════════════════════════

Company shoutouts are everybody's. Department shoutouts are for the recipient's
department, the two people in them and HR. Private shoutouts are for the two
people in them and nobody else - HR included. Every read, reaction, pin and
delete goes through `visibility_clause`, so a shoutout somebody cannot see is a
404 whatever they try to do to it, and the total on a page counts only what the
page could show.

═══════════════════════════════════════════════════════════════════════════════
CLUB MEMBERSHIP IS NEVER SHOWN TO HR AS "WHO JOINED WHAT"
═══════════════════════════════════════════════════════════════════════════════

A club is a safe social space independent of the org structure. Its member list
is readable by its members and by nobody else, HR included; HR sees
participation in aggregate. A private club is invisible to non-members, which
also makes it unjoinable by them - there is no invite flow, because an invite
flow that adds somebody without their acceptance is worse than none.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from fastapi import HTTPException, Request, status
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_filter, tenant_scoped
from app.models.audit import AuditAction
from app.models.culture import (
    Club,
    ClubMember,
    ClubPost,
    Shoutout,
    ShoutoutReaction,
    ShoutoutVisibility,
)
from app.models.culture_depth import (
    AwardCycle,
    AwardProgramme,
    AwardResult,
    CompanyValue,
    TagSourceEnum,
    ValueTag,
)
from app.models.employee import Employee, EmploymentStatus
from app.models.user import User
from app.schemas.culture import (
    ALLOWED_REACTIONS,
    AwardPostRef,
    ClubCreate,
    ClubMemberRead,
    ClubParticipationRead,
    ClubRead,
    CompanyValueRead,
    PersonRef,
    ReactionSummary,
    ShoutoutCreate,
    ShoutoutRead,
    slugify,
)
from app.services import audit
from app.services.notifications import dispatch

__all__ = [
    "club_members",
    "club_participation",
    "count_visible_since",
    "create_club",
    "create_shoutout",
    "delete_shoutout",
    "join_club",
    "leave_club",
    "list_clubs",
    "list_feed",
    "list_values",
    "person_refs",
    "pin_shoutout",
    "set_reaction",
    "visibility_clause",
]

#: Employment states that can still be recognised or join a club.
_CURRENT = (
    EmploymentStatus.ACTIVE,
    EmploymentStatus.ON_NOTICE,
    EmploymentStatus.ON_LEAVE,
)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


# ══════════════════════════════════════════════════════════════════════════════
# People
# ══════════════════════════════════════════════════════════════════════════════


def _ref(employee: Employee) -> PersonRef:
    return PersonRef(
        employee_id=employee.id,
        name=employee.full_name,
        photo_url=employee.photo_url,
        designation=employee.designation,
    )


def _missing_ref(employee_id: uuid.UUID) -> PersonRef:
    return PersonRef(
        employee_id=employee_id, name="Former colleague", photo_url=None, designation=None
    )


async def person_refs(
    session: AsyncSession, user: User, ids: set[uuid.UUID]
) -> dict[uuid.UUID, PersonRef]:
    """Directory-level identity for a set of employees in the caller's tenant."""
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = await session.scalars(
        tenant_scoped(select(Employee).where(Employee.id.in_(wanted)), Employee, user)
    )
    found = {row.id: _ref(row) for row in rows}
    from app.services.dpdp.gates import strip_hidden_photos

    await strip_hidden_photos(session, user, list(found.values()), id_field="employee_id")
    return {i: found.get(i) or _missing_ref(i) for i in wanted}


async def _current_employee(
    session: AsyncSession, user: User, employee_id: uuid.UUID, *, detail: str
) -> Employee:
    row = await session.scalar(
        tenant_scoped(
            select(Employee).where(
                Employee.id == employee_id,
                Employee.deleted_at.is_(None),
                Employee.status.in_(_CURRENT),
            ),
            Employee,
            user,
        )
    )
    if row is None:
        raise _not_found(detail)
    return row


async def _login_for(session: AsyncSession, user: User, employee_id: uuid.UUID) -> uuid.UUID | None:
    return await session.scalar(
        tenant_scoped(
            select(User.id).where(User.employee_id == employee_id, User.is_active.is_(True)),
            User,
            user,
        )
    )


# ══════════════════════════════════════════════════════════════════════════════
# Shoutouts
# ══════════════════════════════════════════════════════════════════════════════


async def visibility_clause(session: AsyncSession, user: User, *, is_hr: bool | None = None):
    """Which shoutouts this person may see, as a SQL condition.

    COMPANY is everybody's. DEPARTMENT is the recipient's department, the two
    people in it, and HR. PRIVATE is the two people in it and nobody else.
    """
    me = user.employee_id
    conditions = [Shoutout.visibility == ShoutoutVisibility.COMPANY]
    if me is not None:
        conditions += [Shoutout.from_employee_id == me, Shoutout.to_employee_id == me]
    if permissions.is_hr(user) if is_hr is None else is_hr:
        conditions.append(Shoutout.visibility == ShoutoutVisibility.DEPARTMENT)
    elif me is not None:
        department = await session.scalar(
            tenant_scoped(select(Employee.department_id).where(Employee.id == me), Employee, user)
        )
        if department is not None:
            colleagues = tenant_scoped(
                select(Employee.id).where(Employee.department_id == department),
                Employee,
                user,
            )
            conditions.append(
                and_(
                    Shoutout.visibility == ShoutoutVisibility.DEPARTMENT,
                    Shoutout.to_employee_id.in_(colleagues),
                )
            )
    return or_(*conditions)


async def _visible_shoutout(session: AsyncSession, user: User, shoutout_id: uuid.UUID) -> Shoutout:
    """The shoutout, if this person may see it. Otherwise 404 - not 403, which
    would confirm that a private shoutout with this id exists."""
    clause = await visibility_clause(session, user)
    row = await session.scalar(
        tenant_scoped(select(Shoutout).where(Shoutout.id == shoutout_id, clause), Shoutout, user)
    )
    if row is None:
        raise _not_found("No such shoutout.")
    return row


async def _reactions(
    session: AsyncSession, user: User, shoutout_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ReactionSummary]]:
    if not shoutout_ids:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(
                select(
                    ShoutoutReaction.shoutout_id,
                    ShoutoutReaction.emoji,
                    ShoutoutReaction.employee_id,
                ).where(ShoutoutReaction.shoutout_id.in_(shoutout_ids)),
                ShoutoutReaction,
                user,
            )
        )
    ).all()
    counts: dict[uuid.UUID, dict[str, list[uuid.UUID]]] = {}
    for shoutout_id, emoji, employee_id in rows:
        counts.setdefault(shoutout_id, {}).setdefault(emoji, []).append(employee_id)

    order = {emoji: index for index, emoji in enumerate(ALLOWED_REACTIONS)}
    out: dict[uuid.UUID, list[ReactionSummary]] = {}
    for shoutout_id, by_emoji in counts.items():
        out[shoutout_id] = [
            ReactionSummary(
                emoji=emoji,
                count=len(people),
                reacted=user.employee_id is not None and user.employee_id in people,
            )
            for emoji, people in sorted(
                by_emoji.items(), key=lambda pair: order.get(pair[0], len(order))
            )
        ]
    return out


async def _award_refs(
    session: AsyncSession, user: User, result_ids: set[uuid.UUID]
) -> dict[uuid.UUID, AwardPostRef]:
    if not result_ids:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(
                select(AwardResult, AwardProgramme.categories)
                .join(AwardCycle, AwardCycle.id == AwardResult.cycle_id)
                .join(AwardProgramme, AwardProgramme.id == AwardCycle.programme_id)
                .where(AwardResult.id.in_(result_ids)),
                AwardResult,
                user,
            )
        )
    ).all()
    out: dict[uuid.UUID, AwardPostRef] = {}
    for result, categories in rows:
        name = next(
            (c.get("name") for c in (categories or []) if c.get("key") == result.category_key),
            None,
        )
        out[result.id] = AwardPostRef(
            result_id=result.id,
            programme_name=result.programme_name,
            category_key=result.category_key,
            category_name=name,
        )
    return out


async def _read_many(session: AsyncSession, user: User, rows: list[Shoutout]) -> list[ShoutoutRead]:
    if not rows:
        return []
    people = await person_refs(
        session,
        user,
        {r.to_employee_id for r in rows} | {r.from_employee_id for r in rows if r.from_employee_id},
    )
    codes = {r.value_tag for r in rows if r.value_tag}
    value_names: dict[str, str] = {}
    if codes:
        value_names = {
            code: name
            for code, name in (
                await session.execute(
                    tenant_scoped(
                        select(CompanyValue.code, CompanyValue.name).where(
                            CompanyValue.code.in_(codes)
                        ),
                        CompanyValue,
                        user,
                    )
                )
            ).all()
        }
    reactions = await _reactions(session, user, [r.id for r in rows])
    awards = await _award_refs(
        session, user, {r.award_result_id for r in rows if r.award_result_id}
    )
    is_hr = permissions.is_hr(user)
    me = user.employee_id

    return [
        ShoutoutRead(
            id=row.id,
            kind=(
                "award" if row.award_result_id else "milestone" if row.milestone_years else "peer"
            ),
            milestone_years=row.milestone_years,
            from_employee_id=row.from_employee_id,
            to_employee_id=row.to_employee_id,
            sender=people.get(row.from_employee_id) if row.from_employee_id else None,
            recipient=people[row.to_employee_id],
            message=row.message,
            value_tag=row.value_tag,
            value_name=value_names.get(row.value_tag) if row.value_tag else None,
            visibility=row.visibility,
            is_pinned=row.is_pinned,
            points_awarded=row.points_awarded,
            created_at=row.created_at,
            reactions=reactions.get(row.id, []),
            award=awards.get(row.award_result_id) if row.award_result_id else None,
            can_delete=is_hr or (me is not None and row.from_employee_id == me),
            can_pin=is_hr,
        )
        for row in rows
    ]


async def list_feed(
    session: AsyncSession, user: User, *, page: int, page_size: int
) -> tuple[list[ShoutoutRead], int]:
    """The feed. Pinned first, then newest; the total counts what is visible."""
    clause = await visibility_clause(session, user, is_hr=permissions.is_hr(user))
    total = await session.scalar(
        tenant_scoped(select(func.count(Shoutout.id)).where(clause), Shoutout, user)
    )
    rows = list(
        (
            await session.scalars(
                tenant_scoped(select(Shoutout).where(clause), Shoutout, user)
                .order_by(Shoutout.is_pinned.desc(), Shoutout.created_at.desc(), Shoutout.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
    )
    return await _read_many(session, user, rows), total or 0


async def count_visible_since(session: AsyncSession, user: User, since: datetime) -> int:
    """How many shoutouts this person could see, posted since a moment."""
    clause = await visibility_clause(session, user)
    count = await session.scalar(
        tenant_scoped(
            select(func.count(Shoutout.id)).where(clause, Shoutout.created_at >= since),
            Shoutout,
            user,
        )
    )
    return count or 0


async def create_shoutout(
    session: AsyncSession, user: User, payload: ShoutoutCreate, *, request: Request | None = None
) -> ShoutoutRead:
    """Post a shoutout from the caller's own employee record.

    The sender is never taken from the request. A value tag must be one of the
    tenant's defined values, and it is written to `value_tags` in the same
    transaction so the analytics and the feed cannot disagree. The recipient is
    notified in that transaction too.
    """
    sender_id = permissions.own_employee_id(user)
    if payload.to_employee_id == sender_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="You cannot give yourself a shoutout",
        )
    sender = await _current_employee(
        session, user, sender_id, detail="Your employee record is not active."
    )
    recipient = await _current_employee(
        session, user, payload.to_employee_id, detail="No such colleague in your workspace."
    )

    if payload.value_tag:
        value = await session.scalar(
            tenant_scoped(
                select(CompanyValue).where(
                    CompanyValue.code == payload.value_tag,
                    CompanyValue.is_active.is_(True),
                ),
                CompanyValue,
                user,
            )
        )
        if value is None:
            raise _bad_request(
                f"{payload.value_tag!r} is not one of your company's values. A tally "
                "over codes nobody defined is a tally of typos."
            )

    shoutout = Shoutout(
        tenant_id=user.tenant_id,
        from_employee_id=sender.id,
        to_employee_id=recipient.id,
        message=payload.message,
        value_tag=payload.value_tag,
        visibility=payload.visibility,
    )
    session.add(shoutout)
    await session.flush()

    if payload.value_tag:
        session.add(
            ValueTag(
                tenant_id=user.tenant_id,
                source=TagSourceEnum.SHOUTOUT,
                source_id=shoutout.id,
                value_code=payload.value_tag,
                subject_employee_id=recipient.id,
                group_key=str(recipient.department_id) if recipient.department_id else None,
                occurred_on=date.today(),
            )
        )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="shoutout",
        entity_id=shoutout.id,
        actor=user,
        summary=(
            f"Posted a {payload.visibility.value} shoutout"
            + (f" for the value {payload.value_tag!r}." if payload.value_tag else ".")
        ),
        request=request,
    )

    recipient_login = await _login_for(session, user, recipient.id)
    if recipient_login is not None:
        await dispatch.notify(
            session,
            tenant_id=user.tenant_id,
            event_key="culture.shoutout.received",
            recipient_user_ids=[recipient_login],
            payload={
                "from_name": sender.full_name,
                "message": payload.message[:280],
                "shoutout_id": str(shoutout.id),
            },
            commit=False,
        )

    await session.commit()
    await session.refresh(shoutout)
    return (await _read_many(session, user, [shoutout]))[0]


async def set_reaction(
    session: AsyncSession,
    user: User,
    shoutout_id: uuid.UUID,
    *,
    emoji: str,
    active: bool,
    request: Request | None = None,
) -> list[ReactionSummary]:
    """Put the caller's reaction on or off. Idempotent.

    Only on a shoutout the caller can see. A second identical call changes
    nothing and writes no audit row; a change writes the reaction and its audit
    row in one transaction.
    """
    if emoji not in ALLOWED_REACTIONS:
        raise _bad_request("That reaction is not one of the allowed set.")
    me = permissions.own_employee_id(user)
    tenant_id = user.tenant_id
    shoutout = await _visible_shoutout(session, user, shoutout_id)

    existing = await session.scalar(
        tenant_scoped(
            select(ShoutoutReaction.id).where(
                ShoutoutReaction.shoutout_id == shoutout.id,
                ShoutoutReaction.employee_id == me,
                ShoutoutReaction.emoji == emoji,
            ),
            ShoutoutReaction,
            user,
        )
    )

    if active and existing is None:
        reaction = ShoutoutReaction(
            tenant_id=tenant_id, shoutout_id=shoutout.id, employee_id=me, emoji=emoji
        )
        try:
            async with session.begin_nested():
                session.add(reaction)
        except IntegrityError:
            # A double click raced itself. The state asked for exists; nothing
            # more to write.
            pass
        else:
            await audit.record(
                session,
                tenant_id=tenant_id,
                action=AuditAction.CREATE,
                entity_type="shoutout_reaction",
                entity_id=reaction.id,
                actor=user,
                summary=f"Reacted {emoji} to a shoutout.",
                request=request,
            )
            await session.commit()
    elif not active and existing is not None:
        await session.execute(
            delete(ShoutoutReaction).where(
                tenant_filter(ShoutoutReaction, tenant_id),
                ShoutoutReaction.id == existing,
            )
        )
        await audit.record(
            session,
            tenant_id=tenant_id,
            action=AuditAction.DELETE,
            entity_type="shoutout_reaction",
            entity_id=existing,
            actor=user,
            summary=f"Removed a {emoji} reaction from a shoutout.",
            request=request,
        )
        await session.commit()

    return (await _reactions(session, user, [shoutout_id])).get(shoutout_id, [])


async def delete_shoutout(
    session: AsyncSession, user: User, shoutout_id: uuid.UUID, *, request: Request | None = None
) -> None:
    """Delete a shoutout: by the person who posted it, or by HR.

    The value tag goes with it - a deleted shoutout still counted in the values
    analytics is recognition nobody can find.
    """
    shoutout = await _visible_shoutout(session, user, shoutout_id)
    is_sender = user.employee_id is not None and shoutout.from_employee_id == user.employee_id
    if not (is_sender or permissions.is_hr(user)):
        raise permissions.forbidden("Only the person who posted a shoutout, or HR, can delete it.")
    tenant_id = user.tenant_id
    target = shoutout.id
    was_award = shoutout.award_result_id is not None

    await session.execute(
        delete(ValueTag).where(
            tenant_filter(ValueTag, tenant_id),
            ValueTag.source == TagSourceEnum.SHOUTOUT,
            ValueTag.source_id == target,
        )
    )
    await session.execute(
        delete(ShoutoutReaction).where(
            tenant_filter(ShoutoutReaction, tenant_id), ShoutoutReaction.shoutout_id == target
        )
    )
    await session.execute(
        delete(Shoutout).where(tenant_filter(Shoutout, tenant_id), Shoutout.id == target)
    )
    await audit.record(
        session,
        tenant_id=tenant_id,
        action=AuditAction.DELETE,
        entity_type="shoutout",
        entity_id=target,
        actor=user,
        summary=(
            "Removed an award announcement from the feed; the award itself stands."
            if was_award
            else ("Deleted their own shoutout." if is_sender else "HR removed a shoutout.")
        ),
        request=request,
    )
    session.expunge(shoutout)
    await session.commit()


async def pin_shoutout(
    session: AsyncSession,
    user: User,
    shoutout_id: uuid.UUID,
    *,
    is_pinned: bool,
    request: Request | None = None,
) -> ShoutoutRead:
    """Pin or unpin. HR only - gated in the router and checked again here."""
    permissions.require_hr(user, detail="Only HR can pin a shoutout.")
    shoutout = await _visible_shoutout(session, user, shoutout_id)
    if shoutout.is_pinned != is_pinned:
        shoutout.is_pinned = is_pinned
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="shoutout",
            entity_id=shoutout.id,
            actor=user,
            summary="Pinned a shoutout." if is_pinned else "Unpinned a shoutout.",
            request=request,
        )
        await session.commit()
        await session.refresh(shoutout)
    return (await _read_many(session, user, [shoutout]))[0]


# ══════════════════════════════════════════════════════════════════════════════
# Clubs
# ══════════════════════════════════════════════════════════════════════════════


async def _my_memberships(session: AsyncSession, user: User) -> dict[uuid.UUID, str]:
    if user.employee_id is None:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(
                select(ClubMember.club_id, ClubMember.role).where(
                    ClubMember.employee_id == user.employee_id
                ),
                ClubMember,
                user,
            )
        )
    ).all()
    return {club_id: role for club_id, role in rows}


async def _member_counts(
    session: AsyncSession, user: User, club_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not club_ids:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(
                select(ClubMember.club_id, func.count(ClubMember.id))
                .where(ClubMember.club_id.in_(club_ids))
                .group_by(ClubMember.club_id),
                ClubMember,
                user,
            )
        )
    ).all()
    return {club_id: count for club_id, count in rows}


def _club_read(club: Club, count: int, role: str | None) -> ClubRead:
    return ClubRead(
        id=club.id,
        name=club.name,
        slug=club.slug,
        description=club.description,
        icon=club.icon,
        is_private=club.is_private,
        member_count=count,
        is_member=role is not None,
        my_role=role,
        created_at=club.created_at,
    )


async def _visible_club(session: AsyncSession, user: User, club_id: uuid.UUID) -> Club:
    """An active club this person may see: public, or private and a member."""
    mine = await _my_memberships(session, user)
    club = await session.scalar(
        tenant_scoped(select(Club).where(Club.id == club_id, Club.is_active.is_(True)), Club, user)
    )
    if club is None or (club.is_private and club.id not in mine):
        raise _not_found("No such club.")
    return club


async def list_clubs(session: AsyncSession, user: User) -> list[ClubRead]:
    """The directory: public clubs, and private ones the caller belongs to."""
    mine = await _my_memberships(session, user)
    visible = Club.is_private.is_(False)
    if mine:
        visible = or_(visible, Club.id.in_(list(mine)))
    clubs = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(Club).where(Club.is_active.is_(True), visible).order_by(Club.name),
                    Club,
                    user,
                )
            )
        ).all()
    )
    counts = await _member_counts(session, user, [c.id for c in clubs])
    return [_club_read(c, counts.get(c.id, 0), mine.get(c.id)) for c in clubs]


async def create_club(
    session: AsyncSession, user: User, payload: ClubCreate, *, request: Request | None = None
) -> ClubRead:
    """Create a club. The caller becomes its organiser and first member."""
    me = permissions.own_employee_id(user)
    base = slugify(payload.name)
    taken = set(
        (
            await session.scalars(
                tenant_scoped(select(Club.slug).where(Club.slug.like(f"{base}%")), Club, user)
            )
        ).all()
    )
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1

    club = Club(
        tenant_id=user.tenant_id,
        name=payload.name,
        slug=slug,
        description=(payload.description or "").strip() or None,
        icon=payload.icon,
        is_private=payload.is_private,
        created_by_employee_id=me,
    )
    session.add(club)
    await session.flush()
    session.add(
        ClubMember(tenant_id=user.tenant_id, club_id=club.id, employee_id=me, role="organiser")
    )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="club",
        entity_id=club.id,
        actor=user,
        summary=(
            f"Created the {'private' if payload.is_private else 'public'} club {payload.name!r}."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(club)
    return _club_read(club, 1, "organiser")


async def join_club(
    session: AsyncSession, user: User, club_id: uuid.UUID, *, request: Request | None = None
) -> ClubRead:
    """Join a public club. Idempotent; a private club is invisible to a
    non-member and so cannot be joined."""
    me = permissions.own_employee_id(user)
    tenant_id = user.tenant_id
    club = await _visible_club(session, user, club_id)
    mine = await _my_memberships(session, user)

    if club.id not in mine:
        member = ClubMember(tenant_id=tenant_id, club_id=club.id, employee_id=me, role="member")
        try:
            async with session.begin_nested():
                session.add(member)
        except IntegrityError:
            pass
        else:
            # The audit row names the membership row, not the club: the trail
            # records that a mutation happened without turning the audit log
            # into a "who joined what" report.
            await audit.record(
                session,
                tenant_id=tenant_id,
                action=AuditAction.CREATE,
                entity_type="club_member",
                entity_id=member.id,
                actor=user,
                summary="Joined a club.",
                request=request,
            )
            await session.commit()
        mine = await _my_memberships(session, user)

    counts = await _member_counts(session, user, [club.id])
    return _club_read(club, counts.get(club.id, 0), mine.get(club.id))


async def leave_club(
    session: AsyncSession, user: User, club_id: uuid.UUID, *, request: Request | None = None
) -> ClubRead | None:
    """Leave a club. When the last member leaves, the club is closed rather
    than left as an empty room nobody can find."""
    me = permissions.own_employee_id(user)
    tenant_id = user.tenant_id
    club = await _visible_club(session, user, club_id)
    membership = await session.scalar(
        tenant_scoped(
            select(ClubMember).where(ClubMember.club_id == club.id, ClubMember.employee_id == me),
            ClubMember,
            user,
        )
    )
    if membership is None:
        counts = await _member_counts(session, user, [club.id])
        return _club_read(club, counts.get(club.id, 0), None)

    membership_id = membership.id
    session.expunge(membership)
    await session.execute(
        delete(ClubMember).where(
            tenant_filter(ClubMember, tenant_id), ClubMember.id == membership_id
        )
    )
    remaining = await session.scalar(
        tenant_scoped(
            select(func.count(ClubMember.id)).where(ClubMember.club_id == club.id),
            ClubMember,
            user,
        )
    )
    if not remaining:
        club.is_active = False
    await audit.record(
        session,
        tenant_id=tenant_id,
        action=AuditAction.DELETE,
        entity_type="club_member",
        entity_id=membership_id,
        actor=user,
        summary="Left a club."
        + (" It had no members left and was closed." if not remaining else ""),
        request=request,
    )
    await session.commit()
    if not remaining:
        return None
    await session.refresh(club)
    return _club_read(club, remaining or 0, None)


async def club_members(
    session: AsyncSession, user: User, club_id: uuid.UUID
) -> list[ClubMemberRead]:
    """A club's members - for its members only, HR included in the refusal."""
    club = await _visible_club(session, user, club_id)
    mine = await _my_memberships(session, user)
    if club.id not in mine:
        raise permissions.forbidden(
            "A club's member list is visible to its members only. Participation "
            "is reported to HR in aggregate, never as who joined what."
        )
    rows = (
        await session.execute(
            tenant_scoped(
                select(ClubMember, Employee)
                .join(Employee, Employee.id == ClubMember.employee_id)
                .where(ClubMember.club_id == club.id),
                ClubMember,
                user,
            )
        )
    ).all()
    members = [
        ClubMemberRead(
            employee_id=employee.id,
            name=employee.full_name,
            photo_url=employee.photo_url,
            designation=employee.designation,
            role=member.role,
            joined_at=member.created_at,
        )
        for member, employee in rows
    ]
    from app.services.dpdp.gates import strip_hidden_photos

    await strip_hidden_photos(session, user, members, id_field="employee_id")
    return sorted(members, key=lambda m: (m.role != "organiser", m.name.lower()))


async def club_participation(session: AsyncSession, user: User) -> ClubParticipationRead:
    """Participation in aggregate, for HR. No club is paired with a person."""
    permissions.require_hr(user, detail="Club participation is reported to HR.")
    clubs = (
        await session.execute(
            tenant_scoped(
                select(Club.is_private, func.count(Club.id))
                .where(Club.is_active.is_(True))
                .group_by(Club.is_private),
                Club,
                user,
            )
        )
    ).all()
    by_privacy = {bool(private): count for private, count in clubs}
    active_club_ids = tenant_scoped(select(Club.id).where(Club.is_active.is_(True)), Club, user)
    memberships = await session.scalar(
        tenant_scoped(
            select(func.count(ClubMember.id)).where(ClubMember.club_id.in_(active_club_ids)),
            ClubMember,
            user,
        )
    )
    distinct = await session.scalar(
        tenant_scoped(
            select(func.count(func.distinct(ClubMember.employee_id))).where(
                ClubMember.club_id.in_(active_club_ids)
            ),
            ClubMember,
            user,
        )
    )
    headcount = await session.scalar(
        tenant_scoped(
            select(func.count(Employee.id)).where(
                Employee.deleted_at.is_(None), Employee.status.in_(_CURRENT)
            ),
            Employee,
            user,
        )
    )
    return ClubParticipationRead(
        active_clubs=sum(by_privacy.values()),
        public_clubs=by_privacy.get(False, 0),
        private_clubs=by_privacy.get(True, 0),
        total_memberships=memberships or 0,
        distinct_members=distinct or 0,
        active_headcount=headcount or 0,
        participation_rate=(round((distinct or 0) / headcount, 3) if headcount else None),
        note=(
            "Counts only. Which person is in which club is visible to that "
            "club's members and to nobody else, HR included."
        ),
    )


async def list_values(
    session: AsyncSession, user: User, *, include_inactive: bool = False
) -> list[CompanyValueRead]:
    """The company's values, in display order. Inactive ones only for HR."""
    stmt = select(CompanyValue).order_by(CompanyValue.display_order, CompanyValue.name)
    if not (include_inactive and permissions.is_hr(user)):
        stmt = stmt.where(CompanyValue.is_active.is_(True))
    rows = (await session.scalars(tenant_scoped(stmt, CompanyValue, user))).all()
    return [CompanyValueRead.model_validate(row) for row in rows]


# ── Club posts ──────────────────────────────────────────────────────────────


def _post_read(post: ClubPost, names: dict[uuid.UUID, str], me: uuid.UUID | None) -> dict:
    return {
        "id": str(post.id),
        "club_id": str(post.club_id),
        "author_employee_id": str(post.author_employee_id) if post.author_employee_id else None,
        "author_name": names.get(post.author_employee_id, "A former member")
        if post.author_employee_id
        else "A former member",
        "body": post.body,
        "created_at": post.created_at.isoformat() if post.created_at else None,
        "mine": post.author_employee_id is not None and post.author_employee_id == me,
    }


async def club_posts(
    session: AsyncSession, user: User, club_id: uuid.UUID, *, limit: int = 100
) -> list[dict]:
    """A public club's posts for everybody; a private club's for its members only."""
    club = await _visible_club(session, user, club_id)
    if club.is_private and club.id not in await _my_memberships(session, user):
        raise permissions.forbidden("A private club's posts are for its members.")
    rows = (
        await session.scalars(
            tenant_scoped(
                select(ClubPost)
                .where(ClubPost.club_id == club.id)
                .order_by(ClubPost.created_at.desc())
                .limit(limit),
                ClubPost,
                user,
            )
        )
    ).all()
    authors = {r.author_employee_id for r in rows if r.author_employee_id}
    names = (
        {
            e.id: e.full_name
            for e in (
                await session.scalars(
                    tenant_scoped(select(Employee).where(Employee.id.in_(authors)), Employee, user)
                )
            ).all()
        }
        if authors
        else {}
    )
    return [_post_read(r, names, user.employee_id) for r in rows]


async def add_club_post(
    session: AsyncSession, user: User, club_id: uuid.UUID, body: str, *, request: Request | None
) -> dict:
    """Members post; somebody who has not joined reads a public club but does not post in it."""
    me = permissions.own_employee_id(user)
    club = await _visible_club(session, user, club_id)
    if club.id not in await _my_memberships(session, user):
        raise permissions.forbidden("Join the club to post in it.")
    text = body.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Write something to post.")
    post = ClubPost(tenant_id=user.tenant_id, club_id=club.id, author_employee_id=me, body=text)
    session.add(post)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="club_post",
        entity_id=post.id,
        actor=user,
        summary="Posted in a club.",
        request=request,
    )
    await session.commit()
    employee = await session.get(Employee, me)
    return _post_read(post, {me: employee.full_name if employee else ""}, me)


async def delete_club_post(
    session: AsyncSession, user: User, post_id: uuid.UUID, *, request: Request | None
) -> None:
    """The author removes their own post; HR removes anything that breaks the rules."""
    post = await session.scalar(
        tenant_scoped(select(ClubPost).where(ClubPost.id == post_id), ClubPost, user)
    )
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    if not (permissions.is_hr(user) or post.author_employee_id == user.employee_id):
        raise permissions.forbidden("Only the author, or HR, removes a post.")
    await session.delete(post)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="club_post",
        entity_id=post.id,
        actor=user,
        summary="Removed a club post.",
        request=request,
    )
    await session.commit()
