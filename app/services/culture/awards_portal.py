"""Awards as people use them: programmes, cycles, nominations, jury, results.

The adjudication rules live in `awards.py` (pure) and `service.py`. This module
decides who sees what:

- everybody sees open cycles and announced results;
- a nominator sees the nominations they made;
- a juror on a cycle sees its nominations and where their own score stands,
  never another juror's score - that is `jury_view`, filtered on the server;
- HR sees everything, and the standings - except an HR person who also sits on
  the jury, who sees the standings only once their own scores are submitted.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.culture import Shoutout
from app.models.culture_depth import (
    AwardCycle,
    AwardCycleStatusEnum,
    AwardNomination,
    AwardProgramme,
    AwardResult,
    JuryScore,
    NominationStatusEnum,
)
from app.models.user import User
from app.schemas.culture import (
    AwardCycleCreate,
    AwardCycleRead,
    AwardProgrammeCreate,
    AwardProgrammeRead,
    AwardResultRead,
    AwardWinnerCreate,
    NominationCreate,
    NominationRead,
)
from app.services import audit
from app.services.culture import awards as award_engine
from app.services.culture import service as culture
from app.services.culture.feed import person_refs

__all__ = [
    "create_programme",
    "declare_winner",
    "list_cycles",
    "list_nominations",
    "list_programmes",
    "list_results",
    "nominate",
    "open_cycle",
    "set_cycle_status",
    "standings",
]

_S = AwardCycleStatusEnum
#: Where a cycle may go next. Announced and cancelled are terminal.
_TRANSITIONS: dict[AwardCycleStatusEnum, set[AwardCycleStatusEnum]] = {
    _S.DRAFT: {_S.NOMINATIONS_OPEN, _S.CANCELLED},
    _S.NOMINATIONS_OPEN: {_S.JURY_SCORING, _S.CANCELLED},
    _S.JURY_SCORING: {_S.NOMINATIONS_OPEN, _S.DECIDED, _S.CANCELLED},
    _S.DECIDED: {_S.JURY_SCORING, _S.ANNOUNCED},
    _S.ANNOUNCED: set(),
    _S.CANCELLED: set(),
}


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _category_name(categories: list[dict[str, Any]], key: str) -> str | None:
    return next((c.get("name") for c in categories if c.get("key") == key), None)


# ── Programmes ──────────────────────────────────────────────────────────────


def _programme_read(row: AwardProgramme) -> AwardProgrammeRead:
    return AwardProgrammeRead(
        id=row.id,
        name=row.name,
        description=row.description,
        cadence=row.cadence,
        categories=list(row.categories or []),
        criteria=list(row.criteria or []),
        eligibility_rule=row.eligibility_rule,
        is_active=row.is_active,
        created_at=row.created_at,
    )


async def list_programmes(session: AsyncSession, user: User) -> list[AwardProgrammeRead]:
    stmt = select(AwardProgramme).order_by(AwardProgramme.name)
    if not permissions.is_hr(user):
        stmt = stmt.where(AwardProgramme.is_active.is_(True))
    rows = (await session.scalars(tenant_scoped(stmt, AwardProgramme, user))).all()
    return [_programme_read(row) for row in rows]


async def create_programme(
    session: AsyncSession,
    user: User,
    payload: AwardProgrammeCreate,
    *,
    request: Request | None = None,
) -> AwardProgrammeRead:
    permissions.require_hr(user, detail="Award programmes are set up by HR.")
    name = " ".join(payload.name.split())
    clash = await session.scalar(
        tenant_scoped(
            select(AwardProgramme.id).where(func.lower(AwardProgramme.name) == name.lower()),
            AwardProgramme,
            user,
        )
    )
    if clash is not None:
        raise _conflict("An award programme with this name already exists.")

    row = AwardProgramme(
        tenant_id=user.tenant_id,
        name=name,
        description=(payload.description or "").strip() or None,
        cadence=payload.cadence,
        categories=[c.model_dump() for c in payload.categories],
        criteria=[c.model_dump() for c in payload.criteria],
        eligibility_rule=(payload.eligibility_rule or "").strip() or None,
        jury_roles=[],
        is_active=True,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="award_programme",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Award programme {name!r}: {len(payload.categories)} categories, "
            f"{len(payload.criteria)} criteria, {payload.cadence.value}."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return _programme_read(row)


# ── Cycles ──────────────────────────────────────────────────────────────────


def _window_state(cycle: AwardCycle, today: date) -> str:
    state = award_engine.window_state(
        award_engine.NominationWindow(
            opens_on=cycle.nominations_open_on, closes_on=cycle.nominations_close_on
        ),
        today=today,
    )
    return state.name.lower()


async def _cycle_reads(
    session: AsyncSession, user: User, pairs: list[tuple[AwardCycle, AwardProgramme]]
) -> list[AwardCycleRead]:
    ids = [cycle.id for cycle, _ in pairs]
    counts: dict[uuid.UUID, int] = {}
    if ids:
        counts = {
            cycle_id: count
            for cycle_id, count in (
                await session.execute(
                    tenant_scoped(
                        select(AwardNomination.cycle_id, func.count(AwardNomination.id))
                        .where(AwardNomination.cycle_id.in_(ids))
                        .group_by(AwardNomination.cycle_id),
                        AwardNomination,
                        user,
                    )
                )
            ).all()
        }
    today = date.today()
    me = str(user.id)
    return [
        AwardCycleRead(
            id=cycle.id,
            programme_id=programme.id,
            programme_name=programme.name,
            label=cycle.label,
            nominations_open_on=cycle.nominations_open_on,
            nominations_close_on=cycle.nominations_close_on,
            result_announcement_date=cycle.result_announcement_date,
            status=cycle.status,
            window_state=_window_state(cycle, today),
            categories=list(programme.categories or []),
            criteria=list(programme.criteria or []),
            is_juror=me in [str(j) for j in (cycle.juror_user_ids or [])],
            juror_count=len(cycle.juror_user_ids or []),
            nomination_count=counts.get(cycle.id, 0),
        )
        for cycle, programme in pairs
    ]


def _cycles_stmt():
    return (
        select(AwardCycle, AwardProgramme)
        .join(AwardProgramme, AwardProgramme.id == AwardCycle.programme_id)
        .order_by(AwardCycle.nominations_open_on.desc(), AwardCycle.label)
    )


async def list_cycles(session: AsyncSession, user: User) -> list[AwardCycleRead]:
    """Cycles people can take part in; HR also sees drafts and cancelled ones."""
    stmt = _cycles_stmt()
    if not permissions.is_hr(user):
        stmt = stmt.where(AwardCycle.status.not_in([_S.DRAFT, _S.CANCELLED]))
    rows = (await session.execute(tenant_scoped(stmt, AwardCycle, user))).all()
    return await _cycle_reads(session, user, [(c, p) for c, p in rows])


async def _cycle(
    session: AsyncSession, user: User, cycle_id: uuid.UUID
) -> tuple[AwardCycle, AwardProgramme]:
    row = (
        await session.execute(
            tenant_scoped(_cycles_stmt().where(AwardCycle.id == cycle_id), AwardCycle, user)
        )
    ).first()
    if row is None:
        raise _not_found("No such award cycle.")
    cycle, programme = row
    if not permissions.is_hr(user) and cycle.status in (_S.DRAFT, _S.CANCELLED):
        raise _not_found("No such award cycle.")
    return cycle, programme


async def open_cycle(
    session: AsyncSession,
    user: User,
    payload: AwardCycleCreate,
    *,
    request: Request | None = None,
) -> AwardCycleRead:
    """Open a cycle with a jury chosen from the directory.

    Jurors are resolved to logins here. Somebody without a login cannot score,
    and is refused by name rather than silently left off the panel.
    """
    permissions.require_hr(user, detail="Award cycles are opened by HR.")
    wanted = list(dict.fromkeys(payload.juror_employee_ids))
    logins = {
        employee_id: user_id
        for user_id, employee_id in (
            await session.execute(
                tenant_scoped(
                    select(User.id, User.employee_id).where(
                        User.employee_id.in_(wanted), User.is_active.is_(True)
                    ),
                    User,
                    user,
                )
            )
        ).all()
    }
    missing = [e for e in wanted if e not in logins]
    if missing:
        names = await person_refs(session, user, set(missing))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"{', '.join(sorted(names[m].name for m in missing))} cannot sit on the "
                "jury: there is no active login to score with."
            ),
        )
    label = " ".join(payload.label.split())
    clash = await session.scalar(
        tenant_scoped(
            select(AwardCycle.id).where(
                AwardCycle.programme_id == payload.programme_id, AwardCycle.label == label
            ),
            AwardCycle,
            user,
        )
    )
    if clash is not None:
        raise _conflict("This programme already has a cycle with that label.")

    row = await culture.open_cycle(
        session,
        user,
        programme_id=payload.programme_id,
        label=label,
        nominations_open_on=payload.nominations_open_on,
        nominations_close_on=payload.nominations_close_on,
        juror_user_ids=[logins[e] for e in wanted],
        result_announcement_date=payload.result_announcement_date,
        request=request,
    )
    cycle, programme = await _cycle(session, user, row.id)
    return (await _cycle_reads(session, user, [(cycle, programme)]))[0]


async def set_cycle_status(
    session: AsyncSession,
    user: User,
    cycle_id: uuid.UUID,
    new_status: AwardCycleStatusEnum,
    *,
    request: Request | None = None,
) -> AwardCycleRead:
    permissions.require_hr(user, detail="Award cycles are run by HR.")
    cycle, programme = await _cycle(session, user, cycle_id)
    if new_status is not cycle.status:
        if new_status not in _TRANSITIONS.get(cycle.status, set()):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"A cycle that is {cycle.status.value.replace('_', ' ')} cannot move "
                    f"to {new_status.value.replace('_', ' ')}."
                ),
            )
        previous = cycle.status
        cycle.status = new_status
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="award_cycle",
            entity_id=cycle.id,
            actor=user,
            summary=(
                f"Award cycle {cycle.label!r} moved from {previous.value} to {new_status.value}."
            ),
            request=request,
        )
        await session.commit()
    return (await _cycle_reads(session, user, [(cycle, programme)]))[0]


# ── Nominations ─────────────────────────────────────────────────────────────


async def _nomination_reads(
    session: AsyncSession,
    user: User,
    rows: list[AwardNomination],
    *,
    programme: AwardProgramme | None,
    is_juror: bool,
) -> list[NominationRead]:
    if not rows:
        return []
    people = await person_refs(
        session,
        user,
        {r.nominee_employee_id for r in rows} | {r.nominator_employee_id for r in rows},
    )
    mine: dict[uuid.UUID, bool] = {}
    if is_juror:
        mine = {
            nomination_id: submitted
            for nomination_id, submitted in (
                await session.execute(
                    tenant_scoped(
                        select(JuryScore.nomination_id, JuryScore.is_submitted).where(
                            JuryScore.juror_user_id == user.id,
                            JuryScore.nomination_id.in_([r.id for r in rows]),
                        ),
                        JuryScore,
                        user,
                    )
                )
            ).all()
        }
    categories = list(programme.categories or []) if programme else []
    is_hr = permissions.is_hr(user)
    out = []
    for row in rows:
        own = user.employee_id is not None and row.nominator_employee_id == user.employee_id
        my_score = None
        if is_juror:
            my_score = ("submitted" if mine[row.id] else "draft") if row.id in mine else "none"
        out.append(
            NominationRead(
                id=row.id,
                cycle_id=row.cycle_id,
                category_key=row.category_key,
                category_name=_category_name(categories, row.category_key),
                nominee=people[row.nominee_employee_id],
                nominator=people[row.nominator_employee_id] if (is_hr or own) else None,
                citation=row.citation,
                supporting_evidence=list(row.supporting_evidence or []),
                status=row.status,
                submitted_at=row.submitted_at,
                my_score=my_score,
            )
        )
    return out


async def nominate(
    session: AsyncSession,
    user: User,
    payload: NominationCreate,
    *,
    request: Request | None = None,
) -> NominationRead:
    """Nominate a colleague in your own name. The nominator is never taken
    from the request, and the window is checked against today's date on the
    server - never a date the client sends."""
    me = permissions.own_employee_id(user)
    if payload.nominee_employee_id == me:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="You cannot nominate yourself.",
        )
    duplicate = await session.scalar(
        tenant_scoped(
            select(AwardNomination.id).where(
                AwardNomination.cycle_id == payload.cycle_id,
                AwardNomination.nominee_employee_id == payload.nominee_employee_id,
                AwardNomination.nominator_employee_id == me,
                AwardNomination.category_key == payload.category_key,
            ),
            AwardNomination,
            user,
        )
    )
    if duplicate is not None:
        raise _conflict("You have already nominated this person in this category.")

    row = await culture.nominate(
        session,
        user,
        cycle_id=payload.cycle_id,
        category_key=payload.category_key,
        nominee_employee_id=payload.nominee_employee_id,
        nominator_employee_id=me,
        citation=payload.citation.strip(),
        supporting_evidence=[e.model_dump(mode="json") for e in payload.supporting_evidence],
        value_codes=payload.value_codes,
        request=request,
    )
    programme = await session.scalar(
        tenant_scoped(
            select(AwardProgramme).where(AwardProgramme.id == row.programme_id),
            AwardProgramme,
            user,
        )
    )
    return (await _nomination_reads(session, user, [row], programme=programme, is_juror=False))[0]


async def list_nominations(
    session: AsyncSession, user: User, cycle_id: uuid.UUID
) -> list[NominationRead]:
    """All of a cycle's nominations for its jurors and HR; your own otherwise."""
    cycle, programme = await _cycle(session, user, cycle_id)
    is_hr = permissions.is_hr(user)
    is_juror = str(user.id) in [str(j) for j in (cycle.juror_user_ids or [])]
    stmt = (
        select(AwardNomination)
        .where(AwardNomination.cycle_id == cycle.id)
        .order_by(AwardNomination.category_key, AwardNomination.submitted_at)
    )
    if not (is_hr or is_juror):
        if user.employee_id is None:
            return []
        stmt = stmt.where(AwardNomination.nominator_employee_id == user.employee_id)
    rows = list((await session.scalars(tenant_scoped(stmt, AwardNomination, user))).all())
    return await _nomination_reads(session, user, rows, programme=programme, is_juror=is_juror)


# ── Standings and results ───────────────────────────────────────────────────


async def standings(session: AsyncSession, user: User, cycle_id: uuid.UUID) -> dict[str, Any]:
    """Per category, the nominations ranked on submitted jury scores."""
    permissions.require_hr(user, detail="Award standings are for HR.")
    cycle, programme = await _cycle(session, user, cycle_id)
    nominations = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(AwardNomination).where(
                        AwardNomination.cycle_id == cycle.id,
                        AwardNomination.status != NominationStatusEnum.WITHDRAWN,
                    ),
                    AwardNomination,
                    user,
                )
            )
        ).all()
    )
    scores = (
        list(
            (
                await session.scalars(
                    tenant_scoped(
                        select(JuryScore).where(
                            JuryScore.nomination_id.in_([n.id for n in nominations])
                        ),
                        JuryScore,
                        user,
                    )
                )
            ).all()
        )
        if nominations
        else []
    )
    jurors = [str(j) for j in (cycle.juror_user_ids or [])]
    if str(user.id) in jurors:
        submitted_by_me = {
            s.nomination_id for s in scores if s.juror_user_id == user.id and s.is_submitted
        }
        if any(n.id not in submitted_by_me for n in nominations):
            raise permissions.forbidden(
                "You sit on this jury. The standings show the other jurors' scores, so "
                "they open once every one of your own scores is submitted."
            )

    engine_scores: dict[uuid.UUID, list[award_engine.JuryScore]] = {}
    for s in scores:
        engine_scores.setdefault(s.nomination_id, []).append(
            award_engine.JuryScore(
                nomination_id=str(s.nomination_id),
                juror_id=str(s.juror_user_id),
                scores=dict(s.scores or {}),
                comments=s.comments or "",
                is_submitted=s.is_submitted,
            )
        )
    results = [
        award_engine.aggregate_scores(
            award_engine.Nomination(
                nomination_id=str(n.id),
                nominee_id=str(n.nominee_employee_id),
                nominator_id=str(n.nominator_employee_id),
                category_key=n.category_key,
                citation=n.citation,
            ),
            engine_scores.get(n.id, []),
        )
        for n in nominations
    ]
    minimum = min(2, max(1, len(jurors)))
    categories = [
        {
            "category_name": category.get("name"),
            **award_engine.ranked_shortlist(
                results, category_key=category.get("key"), minimum_jurors=minimum
            ),
        }
        for category in (programme.categories or [])
    ]
    juror_names: dict[str, str] = {}
    if jurors:
        juror_names = {
            str(uid): name
            for uid, name in (
                await session.execute(
                    tenant_scoped(
                        select(User.id, User.full_name).where(
                            User.id.in_([uuid.UUID(j) for j in jurors])
                        ),
                        User,
                        user,
                    )
                )
            ).all()
        }
    winners = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(AwardResult.nomination_id).where(AwardResult.cycle_id == cycle.id),
                    AwardResult,
                    user,
                )
            )
        ).all()
    )
    people = await person_refs(session, user, {n.nominee_employee_id for n in nominations})
    return {
        "cycle_id": str(cycle.id),
        "label": cycle.label,
        "programme_name": programme.name,
        "status": cycle.status.value,
        "minimum_jurors": minimum,
        "categories": categories,
        "nominations": {
            str(n.id): {
                "nominee": people[n.nominee_employee_id].model_dump(mode="json"),
                "category_key": n.category_key,
                "citation": n.citation,
                "status": n.status.value,
                "won": n.id in winners,
                "outstanding_jurors": [
                    juror_names.get(j, "A juror")
                    for j in award_engine.outstanding_jurors(
                        jurors=jurors, scores=engine_scores.get(n.id, [])
                    )
                ],
            }
            for n in nominations
        },
        "jurors": [{"user_id": j, "name": juror_names.get(j)} for j in jurors],
        "note": (
            "Only submitted scores count. A tie is left as a tie for the jury to "
            "decide, and a winner can only be declared once a juror has submitted."
        ),
    }


async def _result_reads(
    session: AsyncSession, user: User, rows: list[AwardResult]
) -> list[AwardResultRead]:
    if not rows:
        return []
    people = await person_refs(session, user, {r.employee_id for r in rows})
    programmes = {
        cycle.id: programme
        for cycle, programme in (
            await session.execute(
                tenant_scoped(
                    _cycles_stmt().where(AwardCycle.id.in_({r.cycle_id for r in rows})),
                    AwardCycle,
                    user,
                )
            )
        ).all()
    }
    posts = {
        result_id: shoutout_id
        for result_id, shoutout_id in (
            await session.execute(
                tenant_scoped(
                    select(Shoutout.award_result_id, Shoutout.id).where(
                        Shoutout.award_result_id.in_([r.id for r in rows])
                    ),
                    Shoutout,
                    user,
                )
            )
        ).all()
    }
    return [
        AwardResultRead(
            id=r.id,
            cycle_id=r.cycle_id,
            programme_name=r.programme_name,
            category_key=r.category_key,
            category_name=_category_name(
                list(programmes[r.cycle_id].categories or []) if r.cycle_id in programmes else [],
                r.category_key,
            ),
            employee=people[r.employee_id],
            citation=r.citation,
            awarded_on=r.awarded_on,
            announced_at=r.announced_at,
            shoutout_id=posts.get(r.id),
        )
        for r in rows
    ]


async def declare_winner(
    session: AsyncSession,
    user: User,
    payload: AwardWinnerCreate,
    *,
    request: Request | None = None,
) -> AwardResultRead:
    permissions.require_hr(user, detail="Winners are declared by HR.")
    row = await culture.declare_winner(
        session,
        user,
        nomination_id=payload.nomination_id,
        awarded_on=payload.awarded_on or date.today(),
        request=request,
    )
    return (await _result_reads(session, user, [row]))[0]


async def list_results(
    session: AsyncSession, user: User, *, limit: int = 100
) -> list[AwardResultRead]:
    """Announced results - public recognition, visible to everybody."""
    rows = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(AwardResult)
                    .where(AwardResult.announced_at.is_not(None))
                    .order_by(AwardResult.awarded_on.desc(), AwardResult.created_at.desc())
                    .limit(limit),
                    AwardResult,
                    user,
                )
            )
        ).all()
    )
    return await _result_reads(session, user, rows)
