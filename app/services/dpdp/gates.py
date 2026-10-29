"""The questions other modules ask the consent log before they act. DB-facing.

The consent log (`ConsentRecord`) is the one place a person's choice for an
optional purpose lives. A module that offers something consent-based — survey
invitations, a photo shown to colleagues, an AI tool reading the directory —
asks here rather than keeping a flag of its own, so the switch on My privacy is
the switch that stops it (DPDP s.6(4), s.6(6)).

Two kinds of question:

- **Permitted** (`dpdp.service.principals_permitting`): a current, affirmative
  yes. Used where the processing itself needs consent: a photo shown to
  colleagues, someone's record sent to an AI tool. A blank is a no.
- **Declined** (`principals_declining` here): the person has said no. Used
  where the action is *asking* — a survey invitation is how the question is put,
  and answering it is the yes. Once someone says no, nobody asks again.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.dpdp import ConsentRecord
from app.models.user import User

__all__ = [
    "has_declined",
    "hidden_photo_ids",
    "principals_declining",
    "strip_hidden_photos",
]


async def _latest(
    session: AsyncSession, user: User, *, principal_ids: list[uuid.UUID], purpose_key: str
) -> dict[uuid.UUID, bool]:
    """Each person's latest decision for one purpose, where they have made one."""
    if not principal_ids:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(
                select(ConsentRecord.principal_id, ConsentRecord.granted).where(
                    ConsentRecord.principal_id.in_(principal_ids),
                    ConsentRecord.purpose_key == purpose_key,
                ),
                ConsentRecord,
                user,
            ).order_by(ConsentRecord.decided_at)
        )
    ).all()
    latest: dict[uuid.UUID, bool] = {}
    for principal_id, granted in rows:
        latest[principal_id] = granted
    return latest


async def principals_declining(
    session: AsyncSession, user: User, *, principal_ids: Iterable[uuid.UUID], purpose_key: str
) -> set[uuid.UUID]:
    """Who has said no to this purpose — declined, or withdrew."""
    latest = await _latest(
        session, user, principal_ids=list(dict.fromkeys(principal_ids)), purpose_key=purpose_key
    )
    return {pid for pid, granted in latest.items() if not granted}


async def has_declined(
    session: AsyncSession, user: User, *, principal_id: uuid.UUID, purpose_key: str
) -> bool:
    return bool(
        await principals_declining(
            session, user, principal_ids=[principal_id], purpose_key=purpose_key
        )
    )


async def hidden_photo_ids(
    session: AsyncSession, user: User, employee_ids: Iterable[uuid.UUID]
) -> set[uuid.UUID]:
    """The people among these whose photo this viewer may not see.

    The person always sees their own, and HR sees every photo on the record it
    administers. Everyone else — colleagues, managers — sees a photo only when
    its owner agreed to `directory_visibility` on My privacy.
    """
    from app.services.dpdp import service as dpdp_service

    if permissions.is_hr(user):
        return set()
    others = {eid for eid in employee_ids if eid is not None and eid != user.employee_id}
    if not others:
        return set()
    permitted = await dpdp_service.principals_permitting(
        session, user, principal_ids=list(others), capability="photo_in_directory"
    )
    return others - permitted


async def strip_hidden_photos(
    session: AsyncSession,
    user: User,
    items: list[Any],
    *,
    id_field: str = "id",
    photo_field: str = "photo_url",
) -> list[Any]:
    """Blank `photo_field` on every item whose owner has not agreed to show it.

    Works on Pydantic models and plain dicts alike, so a screen shows initials
    instead of asking for an image the server would refuse.
    """

    def _get(item: Any, name: str) -> Any:
        return item.get(name) if isinstance(item, dict) else getattr(item, name, None)

    ids: set[uuid.UUID] = set()
    for item in items:
        if not _get(item, photo_field):
            continue
        raw = _get(item, id_field)
        if raw is None:
            continue
        ids.add(raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw)))
    hidden = await hidden_photo_ids(session, user, ids)
    if not hidden:
        return items
    for item in items:
        raw = _get(item, id_field)
        if raw is None:
            continue
        if (raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))) not in hidden:
            continue
        if isinstance(item, dict):
            item[photo_field] = None
        else:
            setattr(item, photo_field, None)
    return items
