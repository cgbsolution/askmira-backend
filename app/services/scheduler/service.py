"""Running jobs and reading their history.

Two callers: the runner (every tick, on the one instance holding the advisory
lock) and an admin pressing "run now" for their own tenant. Both go through
`execute_in_session`, so a manual run is recorded exactly like a scheduled one.

A job's work and its run record are separate commits on purpose. The work
commits (or rolls back) first; the record of what happened is then written in
its own transaction — a failed job must still leave a row saying it failed,
which it could not if the record shared the rolled-back transaction.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.deps import tenant_filter
from app.models.audit import AuditAction
from app.models.scheduler import SchedulerJobRun, SchedulerRunStatus
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.scheduler.jobs import JOBS, NOT_YET_REGISTERED, job_by_key, system_actor
from app.services.scheduler.registry import Job, JobContext, JobScope

__all__ = [
    "HISTORY_DAYS",
    "execute_in_session",
    "list_jobs",
    "prune_history",
    "recent_runs",
    "run_due_jobs",
    "run_job_now",
]

HISTORY_DAYS = 30
_MAX_ERROR = 4000


def _error_text(exc: BaseException) -> str:
    text = str(exc.detail) if isinstance(exc, HTTPException) else f"{type(exc).__name__}: {exc}"
    return text[:_MAX_ERROR]


async def _last_started(
    session: AsyncSession, job_key: str, tenant_id: uuid.UUID | None
) -> datetime | None:
    """When this job last started for this tenant (or for the platform).

    Measured per tenant, so a workspace created this afternoon gets its first
    run on the next tick rather than waiting for everyone else's cadence.
    """
    owner = (
        SchedulerJobRun.tenant_id.is_(None)
        if tenant_id is None
        else tenant_filter(SchedulerJobRun, tenant_id)
    )
    return await session.scalar(
        select(func.max(SchedulerJobRun.started_at)).where(
            SchedulerJobRun.job_key == job_key, owner
        )
    )


async def _active_tenants(session: AsyncSession) -> list[tuple[uuid.UUID, dict]]:
    """Every active tenant. The one query here that must cross tenants: the
    scheduler's job is to visit each of them in turn."""
    rows = await session.execute(
        select(Tenant.id, Tenant.enabled_modules).where(Tenant.is_active.is_(True))
    )
    return [(row[0], row[1] or {}) for row in rows.all()]


async def execute_in_session(
    session: AsyncSession,
    job: Job,
    *,
    tenant_id: uuid.UUID | None,
    enabled_modules: dict | None,
    now: datetime,
    trigger: str = "schedule",
    triggered_by: User | None = None,
    request: Request | None = None,
) -> SchedulerJobRun:
    """Run one job for one tenant (or the platform) and record the outcome."""
    started = datetime.now(UTC)
    detail: dict[str, Any] = {}
    error: str | None = None

    if job.module and not (enabled_modules or {}).get(job.module):
        run_status = SchedulerRunStatus.SKIPPED
        detail = {"reason": f"The {job.module} module is switched off for this workspace."}
    else:
        ctx = JobContext(
            tenant_id=tenant_id,
            actor=system_actor(tenant_id) if tenant_id is not None else None,
            now=now,
        )
        try:
            detail = await job.handler(session, ctx)
            await session.commit()
            run_status = SchedulerRunStatus.SUCCEEDED
        except Exception as exc:
            await session.rollback()
            run_status = SchedulerRunStatus.FAILED
            error = _error_text(exc)
            detail = {}

    run = SchedulerJobRun(
        tenant_id=tenant_id,
        job_key=job.key,
        started_at=started,
        finished_at=datetime.now(UTC),
        status=run_status,
        detail=detail or {},
        error=error,
        trigger=trigger,
        triggered_by_user_id=triggered_by.id if triggered_by is not None else None,
    )
    session.add(run)
    await session.flush()
    if triggered_by is not None and tenant_id is not None:
        await audit.record(
            session,
            tenant_id=tenant_id,
            action=AuditAction.UPDATE,
            entity_type="scheduler_job",
            entity_id=run.id,
            actor=triggered_by,
            summary=f"Ran '{job.name}' now: {run_status.value}.",
            changes={"job_key": job.key, "status": run_status.value},
            request=request,
        )
    await session.commit()
    return run


async def run_due_jobs(
    factory: async_sessionmaker[AsyncSession],
    *,
    jobs: tuple[Job, ...] = JOBS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """One tick: every due job, for every tenant it applies to."""
    moment = now or datetime.now(UTC)
    outcomes: list[dict[str, Any]] = []
    async with factory() as session:
        tenants = await _active_tenants(session)
    for job in jobs:
        targets = [(None, None)] if job.scope is JobScope.PLATFORM else tenants
        for tenant_id, modules in targets:
            async with factory() as session:
                last = await _last_started(session, job.key, tenant_id)
                if not job.cadence.is_due(last_started=last, now=moment):
                    continue
                run = await execute_in_session(
                    session, job, tenant_id=tenant_id, enabled_modules=modules, now=moment
                )
                outcomes.append(
                    {
                        "job": job.key,
                        "tenant_id": str(tenant_id) if tenant_id else None,
                        "status": run.status.value,
                    }
                )
    return outcomes


async def prune_history(session: AsyncSession, *, now: datetime) -> int:
    """Delete run records past the history window. These are operational
    records, not the audit trail — manual runs are audited separately."""
    cutoff = now - timedelta(days=HISTORY_DAYS)
    result = await session.execute(
        delete(SchedulerJobRun).where(SchedulerJobRun.started_at < cutoff)
    )
    return int(result.rowcount or 0)


# ── The admin screen ────────────────────────────────────────────────────────


def _run_owner(job: Job, user: User):
    """A platform job's runs belong to no tenant; a tenant job's to the caller's."""
    if job.scope is JobScope.PLATFORM:
        return SchedulerJobRun.tenant_id.is_(None)
    return tenant_filter(SchedulerJobRun, user.tenant_id)


def _run_dict(row: SchedulerJobRun | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": str(row.id),
        "status": row.status.value,
        "started_at": row.started_at.isoformat(),
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "trigger": row.trigger,
        "detail": row.detail,
        "error": row.error,
    }


async def _latest(
    session: AsyncSession,
    user: User,
    job: Job,
    *,
    only: SchedulerRunStatus | None = None,
) -> SchedulerJobRun | None:
    stmt = select(SchedulerJobRun).where(
        SchedulerJobRun.job_key == job.key, _run_owner(job, user)
    )
    if only is not None:
        stmt = stmt.where(SchedulerJobRun.status == only)
    return await session.scalar(stmt.order_by(SchedulerJobRun.started_at.desc()).limit(1))


async def list_jobs(session: AsyncSession, user: User) -> dict[str, Any]:
    """Every job, what it calls, and how it last went for this workspace."""
    from app.services.scheduler.runner import runner_status

    tenant = await session.get(Tenant, user.tenant_id)
    modules = (tenant.enabled_modules or {}) if tenant else {}
    week_ago = datetime.now(UTC) - timedelta(days=7)

    items = []
    for job in JOBS:
        failures = await session.scalar(
            select(func.count(SchedulerJobRun.id)).where(
                SchedulerJobRun.job_key == job.key,
                tenant_filter(SchedulerJobRun, user.tenant_id),
                SchedulerJobRun.status == SchedulerRunStatus.FAILED,
                SchedulerJobRun.started_at >= week_ago,
            )
        )
        items.append(
            {
                **job.as_dict(),
                "applies": job.module is None or bool(modules.get(job.module)),
                "last_run": _run_dict(await _latest(session, user, job)),
                "last_success": _run_dict(
                    await _latest(session, user, job, only=SchedulerRunStatus.SUCCEEDED)
                ),
                "last_failure": _run_dict(
                    await _latest(session, user, job, only=SchedulerRunStatus.FAILED)
                ),
                "failures_last_7_days": int(failures or 0),
                "can_run_now": job.scope is JobScope.TENANT,
            }
        )
    return {
        "runner": runner_status(),
        "jobs": items,
        "not_yet_registered": list(NOT_YET_REGISTERED),
        "note": (
            "Jobs run inside the API process on whichever instance holds the "
            "database advisory lock, so exactly one instance runs them. History is "
            f"kept for {HISTORY_DAYS} days."
        ),
    }


async def recent_runs(
    session: AsyncSession, user: User, *, job_key: str, limit: int = 20
) -> list[dict[str, Any]]:
    job = job_by_key(job_key)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such job.")
    stmt = select(SchedulerJobRun).where(
        SchedulerJobRun.job_key == job_key, _run_owner(job, user)
    )
    rows = await session.scalars(stmt.order_by(SchedulerJobRun.started_at.desc()).limit(limit))
    return [_run_dict(row) for row in rows.all()]


async def run_job_now(
    session: AsyncSession, user: User, *, job_key: str, request: Request | None = None
) -> dict[str, Any]:
    """Run one tenant job immediately for the caller's own workspace only."""
    job = job_by_key(job_key)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such job.")
    if job.scope is not JobScope.TENANT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A platform job runs for every workspace and cannot be run from one.",
        )
    tenant = await session.get(Tenant, user.tenant_id)
    modules = (tenant.enabled_modules or {}) if tenant else {}
    run = await execute_in_session(
        session,
        job,
        tenant_id=user.tenant_id,
        enabled_modules=modules,
        now=datetime.now(UTC),
        trigger="manual",
        triggered_by=user,
        request=request,
    )
    return _run_dict(run) or {}
