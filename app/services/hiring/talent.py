"""Tags and talent pools: finding last quarter's near-misses when a role opens.

Tags are a recruiter's labels on a candidate. Pools are named lists of
candidates kept for later — and keeping someone for later is a purpose of its
own under DPDP, so a pool admits only a candidate whose latest answer on
talent-pool retention is yes. A candidate who withdraws stays visible in the
pool as withdrawn, so nobody mistakes them for a live prospect, and the
membership can be removed in one click.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.hiring import Candidate, Requisition
from app.models.jobboard import CandidateConsent, ConsentPurpose
from app.models.talent_pools import TalentPool, TalentPoolMember
from app.models.user import User
from app.services import audit
from app.services.hiring import visibility

__all__ = [
    "MAX_TAGS",
    "add_members",
    "create_pool",
    "delete_pool",
    "list_pools",
    "normalise_tags",
    "pool_detail",
    "remove_member",
    "retention_consent",
    "set_tags",
    "tag_counts",
    "update_pool",
]

MAX_TAGS = 20
_TAG = re.compile(r"[^a-z0-9+#. -]")


def normalise_tags(tags: list[str]) -> list[str]:
    """Lower case, trimmed, de-duplicated, in the order given; refuses junk."""
    out: list[str] = []
    for raw in tags:
        tag = re.sub(r"\s+", " ", raw.strip().lower())
        if not tag:
            continue
        if len(tag) > 48 or _TAG.search(tag):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"'{raw.strip()[:60]}' is not a usable tag. Use up to 48 letters, digits, "
                    "spaces and + # . - (so 'c++', 'c#' and 'node.js' work)."
                ),
            )
        if tag not in out:
            out.append(tag)
    if len(out) > MAX_TAGS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"A candidate takes up to {MAX_TAGS} tags.",
        )
    return out


async def set_tags(
    session: AsyncSession,
    user: User,
    *,
    candidate_id: uuid.UUID,
    tags: list[str],
    request: Request | None = None,
) -> list[str]:
    candidate, _requisition = await visibility.load_candidate(session, user, candidate_id)
    cleaned = normalise_tags(tags)
    previous = list(candidate.tags or [])
    if cleaned == previous:
        return cleaned
    candidate.tags = cleaned
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="candidate",
        entity_id=candidate.id,
        summary="Candidate tags changed",
        changes={"from": previous, "to": cleaned},
        request=request,
    )
    return cleaned


async def tag_counts(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Every tag in use among the candidates the caller may see, most used first."""
    tag = func.unnest(Candidate.tags).label("tag")
    inner = tenant_scoped(select(Candidate.id, tag), Candidate, user)
    if not permissions.is_hr(user):
        inner = inner.where(Candidate.requisition_id.in_(visibility.team_requisition_ids(user)))
    sub = inner.subquery()
    rows = (
        await session.execute(
            select(sub.c.tag, func.count(sub.c.id))
            .group_by(sub.c.tag)
            .order_by(func.count(sub.c.id).desc(), sub.c.tag)
        )
    ).all()
    return [{"tag": tag_value, "candidates": count} for tag_value, count in rows]


async def retention_consent(
    session: AsyncSession, user: User, candidate_ids: list[uuid.UUID]
) -> dict[uuid.UUID, bool]:
    """Each candidate's latest answer on talent-pool retention; absent means never asked."""
    if not candidate_ids:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(
                select(
                    CandidateConsent.candidate_id,
                    CandidateConsent.granted,
                    CandidateConsent.recorded_at,
                )
                .where(
                    CandidateConsent.candidate_id.in_(candidate_ids),
                    CandidateConsent.purpose == ConsentPurpose.TALENT_POOL_RETENTION,
                )
                .order_by(CandidateConsent.recorded_at),
                CandidateConsent,
                user,
            )
        )
    ).all()
    latest: dict[uuid.UUID, bool] = {}
    for candidate_id, granted, _recorded in rows:
        latest[candidate_id] = bool(granted)
    return latest


# ── Pools ───────────────────────────────────────────────────────────────────


def _require_hr(user: User) -> None:
    permissions.require_hr(user, detail="Talent pools are kept by HR.")


async def _pool(session: AsyncSession, user: User, pool_id: uuid.UUID) -> TalentPool:
    pool = await session.scalar(
        tenant_scoped(select(TalentPool).where(TalentPool.id == pool_id), TalentPool, user)
    )
    if pool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Talent pool not found")
    return pool


async def _name_free(
    session: AsyncSession, user: User, name: str, *, keep: uuid.UUID | None = None
) -> None:
    stmt = select(TalentPool.id).where(func.lower(TalentPool.name) == name.lower())
    if keep is not None:
        stmt = stmt.where(TalentPool.id != keep)
    if await session.scalar(tenant_scoped(stmt, TalentPool, user)) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"There is already a pool called {name}."
        )


async def list_pools(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    _require_hr(user)
    counts = (
        select(TalentPoolMember.pool_id, func.count(TalentPoolMember.id).label("members"))
        .group_by(TalentPoolMember.pool_id)
        .subquery()
    )
    rows = (
        await session.execute(
            tenant_scoped(
                select(TalentPool, func.coalesce(counts.c.members, 0))
                .outerjoin(counts, counts.c.pool_id == TalentPool.id)
                .order_by(TalentPool.name),
                TalentPool,
                user,
            )
        )
    ).all()
    return [
        {
            "id": str(pool.id),
            "name": pool.name,
            "description": pool.description,
            "members": int(members),
            "created_at": pool.created_at.isoformat(),
        }
        for pool, members in rows
    ]


async def create_pool(
    session: AsyncSession,
    user: User,
    *,
    name: str,
    description: str | None,
    request: Request | None = None,
) -> dict[str, Any]:
    _require_hr(user)
    name = name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="A pool needs a name.")
    await _name_free(session, user, name)
    pool = TalentPool(
        tenant_id=user.tenant_id,
        name=name,
        description=(description or "").strip() or None,
        created_by_user_id=user.id,
    )
    session.add(pool)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="talent_pool",
        entity_id=pool.id,
        summary=f"Talent pool '{name}' created",
        request=request,
    )
    return {
        "id": str(pool.id),
        "name": pool.name,
        "description": pool.description,
        "members": 0,
        "created_at": pool.created_at.isoformat(),
    }


async def update_pool(
    session: AsyncSession,
    user: User,
    pool_id: uuid.UUID,
    *,
    name: str | None,
    description: str | None,
    request: Request | None = None,
) -> dict[str, Any]:
    _require_hr(user)
    pool = await _pool(session, user, pool_id)
    before = {"name": pool.name, "description": pool.description}
    if name is not None and name.strip():
        await _name_free(session, user, name.strip(), keep=pool.id)
        pool.name = name.strip()
    if description is not None:
        pool.description = description.strip() or None
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="talent_pool",
        entity_id=pool.id,
        summary=f"Talent pool '{pool.name}' edited",
        changes={"from": before, "to": {"name": pool.name, "description": pool.description}},
        request=request,
    )
    return (await pool_detail(session, user, pool.id))["pool"]


async def delete_pool(
    session: AsyncSession, user: User, pool_id: uuid.UUID, *, request: Request | None = None
) -> None:
    """The list goes; the candidates and their records do not."""
    _require_hr(user)
    pool = await _pool(session, user, pool_id)
    name = pool.name
    await session.delete(pool)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.DELETE,
        entity_type="talent_pool",
        entity_id=pool_id,
        summary=f"Talent pool '{name}' deleted; its candidates are unaffected",
        request=request,
    )


async def pool_detail(session: AsyncSession, user: User, pool_id: uuid.UUID) -> dict[str, Any]:
    _require_hr(user)
    pool = await _pool(session, user, pool_id)
    rows = (
        await session.execute(
            tenant_scoped(
                select(TalentPoolMember, Candidate, Requisition.code, Requisition.title)
                .join(Candidate, Candidate.id == TalentPoolMember.candidate_id)
                .join(Requisition, Requisition.id == Candidate.requisition_id)
                .where(TalentPoolMember.pool_id == pool.id)
                .order_by(TalentPoolMember.created_at.desc()),
                TalentPoolMember,
                user,
            )
        )
    ).all()
    consent = await retention_consent(session, user, [c.id for _, c, _, _ in rows])
    members = [
        {
            "candidate_id": str(candidate.id),
            "full_name": candidate.full_name,
            "email": candidate.email,
            "stage": candidate.stage.value,
            "tags": list(candidate.tags or []),
            "last_requisition": f"{code} · {title}",
            "total_experience_years": (
                str(candidate.total_experience_years)
                if candidate.total_experience_years is not None
                else None
            ),
            "note": member.note,
            "added_at": member.created_at.isoformat(),
            # Withdrawn means do not contact; the row stays so nobody re-adds them by mistake.
            "consent": "granted" if consent.get(candidate.id) else "withdrawn",
        }
        for member, candidate, code, title in rows
    ]
    return {
        "pool": {
            "id": str(pool.id),
            "name": pool.name,
            "description": pool.description,
            "members": len(members),
            "created_at": pool.created_at.isoformat(),
        },
        "members": members,
    }


async def add_members(
    session: AsyncSession,
    user: User,
    pool_id: uuid.UUID,
    *,
    candidate_ids: list[uuid.UUID],
    note: str | None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Add candidates who agreed to be kept; say why each of the others was not."""
    _require_hr(user)
    pool = await _pool(session, user, pool_id)
    ids = list(dict.fromkeys(candidate_ids))
    found = {
        c.id: c
        for c in (
            await session.scalars(
                tenant_scoped(select(Candidate).where(Candidate.id.in_(ids)), Candidate, user)
            )
        ).all()
    }
    already = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(TalentPoolMember.candidate_id).where(
                        TalentPoolMember.pool_id == pool.id,
                        TalentPoolMember.candidate_id.in_(ids),
                    ),
                    TalentPoolMember,
                    user,
                )
            )
        ).all()
    )
    consent = await retention_consent(session, user, list(found))
    added: list[str] = []
    refused: list[dict[str, str]] = []
    for candidate_id in ids:
        candidate = found.get(candidate_id)
        if candidate is None:
            refused.append({"candidate_id": str(candidate_id), "reason": "No such candidate."})
        elif candidate_id in already:
            refused.append({"candidate_id": str(candidate_id), "reason": "Already in this pool."})
        elif not consent.get(candidate_id):
            refused.append(
                {
                    "candidate_id": str(candidate_id),
                    "reason": (
                        f"{candidate.full_name} has not agreed to be kept for future roles "
                        "(talent-pool consent). Ask them first; keeping them without it is "
                        "not allowed under DPDP."
                    ),
                }
            )
        else:
            session.add(
                TalentPoolMember(
                    tenant_id=user.tenant_id,
                    pool_id=pool.id,
                    candidate_id=candidate_id,
                    note=(note or "").strip() or None,
                    added_by_user_id=user.id,
                )
            )
            added.append(str(candidate_id))
    await session.flush()
    if added:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            actor=user,
            action=AuditAction.CREATE,
            entity_type="talent_pool",
            entity_id=pool.id,
            summary=f"{len(added)} candidate(s) added to talent pool '{pool.name}'",
            changes={"candidate_ids": added},
            request=request,
        )
    return {"added": added, "refused": refused}


async def remove_member(
    session: AsyncSession,
    user: User,
    pool_id: uuid.UUID,
    candidate_id: uuid.UUID,
    *,
    request: Request | None = None,
) -> None:
    _require_hr(user)
    pool = await _pool(session, user, pool_id)
    member = await session.scalar(
        tenant_scoped(
            select(TalentPoolMember).where(
                TalentPoolMember.pool_id == pool.id, TalentPoolMember.candidate_id == candidate_id
            ),
            TalentPoolMember,
            user,
        )
    )
    if member is None:
        raise HTTPException(status_code=404, detail="That candidate is not in this pool.")
    await session.delete(member)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.DELETE,
        entity_type="talent_pool",
        entity_id=pool.id,
        summary=f"Candidate removed from talent pool '{pool.name}'",
        changes={"candidate_id": str(candidate_id)},
        request=request,
    )
