"""What runs, and when. Pure: no session, no clock of its own.

A job is a name, a cadence and a handler that calls an existing service
function. Nothing here decides *what* a nightly recompute does — the attendance
module already knows that. This module only decides that it happens, once, on
one instance, and that the outcome is written down.

Cadences are evaluated in India time. "Nightly at 01:00" means 01:00 IST for a
product whose tenants are Indian companies; a UTC-anchored nightly job lands in
the middle of a Bengaluru working morning.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.user import User

__all__ = [
    "SCHEDULE_ZONE",
    "Cadence",
    "CadenceKind",
    "Job",
    "JobContext",
    "JobScope",
]

SCHEDULE_ZONE = ZoneInfo("Asia/Kolkata")


class CadenceKind(str, enum.Enum):
    INTERVAL = "interval"
    DAILY = "daily"
    MONTHLY = "monthly"


class JobScope(str, enum.Enum):
    #: Runs once per active tenant, as that tenant's system actor.
    TENANT = "tenant"
    #: Runs once for the whole platform and belongs to no tenant.
    PLATFORM = "platform"


@dataclass(frozen=True, slots=True)
class Cadence:
    kind: CadenceKind
    minutes: int = 0
    hour: int = 0
    day: int = 1

    @classmethod
    def every(cls, minutes: int) -> Cadence:
        if minutes < 1:
            raise ValueError("An interval job needs at least a one-minute interval.")
        return cls(kind=CadenceKind.INTERVAL, minutes=minutes)

    @classmethod
    def daily(cls, hour: int) -> Cadence:
        if not 0 <= hour <= 23:
            raise ValueError("A daily job's hour must be 0-23.")
        return cls(kind=CadenceKind.DAILY, hour=hour)

    @classmethod
    def monthly(cls, day: int, hour: int) -> Cadence:
        if not 1 <= day <= 28:
            # Capped at 28 so every month has the day. A job set for the 31st
            # that silently skips February is a job that silently skips.
            raise ValueError("A monthly job's day must be 1-28.")
        return cls(kind=CadenceKind.MONTHLY, day=day, hour=hour)

    def latest_slot(self, now: datetime) -> datetime | None:
        """The most recent moment this cadence was due, at or before `now`.

        None for an interval cadence, which is due by elapsed time instead.
        """
        local = now.astimezone(SCHEDULE_ZONE)
        if self.kind is CadenceKind.DAILY:
            slot = local.replace(hour=self.hour, minute=0, second=0, microsecond=0)
            if slot > local:
                slot -= timedelta(days=1)
            return slot.astimezone(UTC)
        if self.kind is CadenceKind.MONTHLY:
            slot = local.replace(day=self.day, hour=self.hour, minute=0, second=0, microsecond=0)
            if slot > local:
                year, month = (
                    (slot.year, slot.month - 1)
                    if slot.month > 1
                    else (
                        slot.year - 1,
                        12,
                    )
                )
                slot = slot.replace(year=year, month=month)
            return slot.astimezone(UTC)
        return None

    def is_due(self, *, last_started: datetime | None, now: datetime) -> bool:
        """Whether a run should start now, given when the last one started."""
        if self.kind is CadenceKind.INTERVAL:
            return last_started is None or now - last_started >= timedelta(minutes=self.minutes)
        slot = self.latest_slot(now)
        assert slot is not None
        return last_started is None or last_started < slot

    def describe(self) -> str:
        if self.kind is CadenceKind.INTERVAL:
            if self.minutes % 60 == 0:
                hours = self.minutes // 60
                return f"Every {hours} hour{'s' if hours != 1 else ''}"
            return f"Every {self.minutes} minutes"
        if self.kind is CadenceKind.DAILY:
            return f"Daily at {self.hour:02d}:00 IST"
        return f"Monthly on day {self.day} at {self.hour:02d}:00 IST"


@dataclass(frozen=True, slots=True)
class JobContext:
    """What a handler is given.

    `actor` is the tenant's system actor — never a real person's login — and is
    None for a platform job.
    """

    tenant_id: uuid.UUID | None
    actor: User | None
    now: datetime

    @property
    def today(self) -> date:
        return self.now.astimezone(SCHEDULE_ZONE).date()


Handler = Callable[["AsyncSession", JobContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class Job:
    key: str
    name: str
    description: str
    cadence: Cadence
    handler: Handler
    #: The service function the job calls, for the admin screen. The claim
    #: "the scheduler runs the attendance recompute" is checkable only if the
    #: screen names what it actually calls.
    source: str
    scope: JobScope = JobScope.TENANT
    #: Workspace module that must be switched on. None = always applies.
    module: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "cadence": self.cadence.describe(),
            "source": self.source,
            "scope": self.scope.value,
            "module": self.module,
        }
