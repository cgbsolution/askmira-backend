"""Street addresses for punch locations, looked up once per place and kept.

A punch stores coordinates. People reading attendance want an address
("12 MG Road, Bengaluru"), so each place is looked up the first time a screen
or an export needs it and kept in `punch_addresses`. A place is the
coordinates rounded to four decimals (about 11 metres): every punch at an
office shares one lookup, and a punch row is never changed.

Lookups run under a time budget. A screen asks for the few places on it and
waits a few seconds at most; whatever is not found in time shows no address
and is looked up on the next read. Nothing here ever fails a read.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

import httpx
from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.attendance import PunchAddress
from app.models.user import User
from app.services.attendance import maps

logger = logging.getLogger("askmira.attendance.addresses")

__all__ = ["Place", "addresses_for", "maps_link", "place_of"]

Place = tuple[Decimal, Decimal]

_STEP = Decimal("0.0001")
#: Lookups at the same time; the API allows far more, this is politeness.
_PARALLEL = 8
_CHUNK = 500

maps_link = maps.maps_link


def place_of(latitude: object, longitude: object) -> Place | None:
    """The place a point belongs to, or None without both coordinates."""
    if latitude is None or longitude is None:
        return None
    try:
        lat = Decimal(str(latitude)).quantize(_STEP, rounding=ROUND_HALF_UP)
        lng = Decimal(str(longitude)).quantize(_STEP, rounding=ROUND_HALF_UP)
    except ArithmeticError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


async def _cached(
    session: AsyncSession, user: User, places: list[Place]
) -> dict[Place, str | None]:
    found: dict[Place, str | None] = {}
    for start in range(0, len(places), _CHUNK):
        chunk = places[start : start + _CHUNK]
        rows = await session.execute(
            tenant_scoped(
                select(PunchAddress.lat_key, PunchAddress.lng_key, PunchAddress.address).where(
                    tuple_(PunchAddress.lat_key, PunchAddress.lng_key).in_(chunk)
                ),
                PunchAddress,
                user,
            )
        )
        for lat, lng, address in rows.all():
            found[(Decimal(lat).quantize(_STEP), Decimal(lng).quantize(_STEP))] = address
    return found


async def _look_up(places: list[Place], budget_seconds: float) -> dict[Place, str | None]:
    """Ask the map provider about each place, within the budget. Unanswered ones are left out."""
    answers: dict[Place, str | None] = {}
    gate = asyncio.Semaphore(_PARALLEL)

    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:

        async def one(place: Place) -> None:
            async with gate:
                try:
                    answers[place] = await maps.reverse_geocode(client, place[0], place[1])
                except maps.AddressLookupError:
                    return

        tasks = [asyncio.create_task(one(place)) for place in places]
        _, pending = await asyncio.wait(tasks, timeout=budget_seconds)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    return answers


async def addresses_for(
    session: AsyncSession,
    user: User,
    points: Iterable[tuple[object, object]],
    *,
    budget_seconds: float = 4.0,
    max_lookups: int = 200,
) -> dict[Place, str | None]:
    """The address of each point's place that is known now.

    Places already kept come from the database; up to `max_lookups` new ones
    are looked up within `budget_seconds` and kept. A place missing from the
    result has no address yet (lookups off, out of time, or the provider did
    not answer). The caller commits the session.
    """
    places = sorted({p for lat, lng in points if (p := place_of(lat, lng)) is not None})
    if not places:
        return {}
    known = await _cached(session, user, places)
    missing = [p for p in places if p not in known]
    if not missing or max_lookups <= 0 or not maps.geocoding_enabled():
        return known

    answers = await _look_up(missing[:max_lookups], budget_seconds)
    if answers:
        await session.execute(
            insert(PunchAddress)
            .values(
                [
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": user.tenant_id,
                        "lat_key": lat,
                        "lng_key": lng,
                        "address": address,
                    }
                    for (lat, lng), address in answers.items()
                ]
            )
            .on_conflict_do_nothing(constraint="uq_punch_address_place")
        )
        known.update(answers)
    if len(missing) > len(answers):
        logger.info(
            "Addresses: %d of %d new places looked up; the rest on a later read",
            len(answers),
            len(missing),
        )
    return known
