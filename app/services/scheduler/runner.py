"""The in-process runner and its leadership lock.

Every API instance starts a runner; only one runs jobs. Leadership is a
session-level Postgres advisory lock held on a dedicated connection for as
long as the instance lives. If that instance dies, its connection closes, the
lock is released by the database, and the next instance to tick takes over —
no heartbeat table, no clock comparisons between machines.

A follower keeps trying on every tick, so a leader that is replaced is replaced
within one tick interval.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import settings
from app.services.scheduler.jobs import JOBS
from app.services.scheduler.registry import Job

__all__ = [
    "LOCK_KEY",
    "SchedulerRunner",
    "current_runner",
    "runner_status",
    "scheduler_enabled",
    "start_scheduler",
    "stop_scheduler",
]

logger = logging.getLogger("askmira.scheduler")

#: "ASKMIRA" as a bigint. Any fixed value works; it only has to be the same on
#: every instance and unlikely to collide with another application's lock.
LOCK_KEY = 0x41534B4D495241

_RUNNER: SchedulerRunner | None = None


def scheduler_enabled() -> bool:
    """On unless switched off — and never inside the test suite by default."""
    if settings.SCHEDULER_ENABLED is not None:
        return bool(settings.SCHEDULER_ENABLED)
    return "pytest" not in sys.modules


class SchedulerRunner:
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        jobs: tuple[Job, ...] = JOBS,
        tick_seconds: int = 60,
        lock_key: int = LOCK_KEY,
    ) -> None:
        self._engine = engine
        self._factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        self._jobs = jobs
        self._tick_seconds = max(5, tick_seconds)
        self._lock_key = lock_key
        self._lock_conn: AsyncConnection | None = None
        self._task: asyncio.Task | None = None
        self.last_tick_at: datetime | None = None
        self.last_error: str | None = None

    @property
    def is_leader(self) -> bool:
        return self._lock_conn is not None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def try_acquire_leadership(self) -> bool:
        """Hold the lock, or take it if nobody does. Never blocks."""
        if self._lock_conn is not None:
            try:
                await self._lock_conn.execute(text("SELECT 1"))
                await self._lock_conn.commit()
                return True
            except Exception:
                logger.warning("Scheduler lock connection was lost; re-electing.")
                await self._close_lock_connection()

        conn = await self._engine.connect()
        try:
            acquired = (
                await conn.execute(
                    text("SELECT pg_try_advisory_lock(CAST(:key AS bigint))"),
                    {"key": self._lock_key},
                )
            ).scalar()
            await conn.commit()
        except Exception:
            await conn.close()
            raise
        if acquired:
            self._lock_conn = conn
            logger.info("This instance now runs scheduled jobs.")
            return True
        await conn.close()
        return False

    async def release_leadership(self) -> None:
        if self._lock_conn is None:
            return
        try:
            await self._lock_conn.execute(
                text("SELECT pg_advisory_unlock(CAST(:key AS bigint))"), {"key": self._lock_key}
            )
            await self._lock_conn.commit()
        except Exception:
            logger.warning("Could not unlock the scheduler lock cleanly; closing its connection.")
        finally:
            await self._close_lock_connection()

    async def _close_lock_connection(self) -> None:
        conn, self._lock_conn = self._lock_conn, None
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close()

    async def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Run whatever is due, if this instance is the leader."""
        from app.services.scheduler.service import run_due_jobs

        self.last_tick_at = datetime.now(UTC)
        if not await self.try_acquire_leadership():
            return []
        return await run_due_jobs(self._factory, jobs=self._jobs, now=now)

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                logger.exception("Scheduler tick failed")
            await asyncio.sleep(self._tick_seconds)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="askmira-scheduler")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.release_leadership()


def current_runner() -> SchedulerRunner | None:
    return _RUNNER


def runner_status() -> dict[str, Any]:
    runner = _RUNNER
    return {
        "enabled": scheduler_enabled(),
        "running_on_this_instance": bool(runner and runner.is_running),
        "this_instance_is_leader": bool(runner and runner.is_leader),
        "last_tick_at": runner.last_tick_at.isoformat() if runner and runner.last_tick_at else None,
        "last_error": runner.last_error if runner else None,
        "tick_seconds": settings.SCHEDULER_TICK_SECONDS,
    }


async def start_scheduler(engine: AsyncEngine) -> SchedulerRunner:
    global _RUNNER
    runner = SchedulerRunner(engine=engine, tick_seconds=settings.SCHEDULER_TICK_SECONDS)
    runner.start()
    _RUNNER = runner
    return runner


async def stop_scheduler(runner: SchedulerRunner) -> None:
    global _RUNNER
    await runner.stop()
    if _RUNNER is runner:
        _RUNNER = None
