"""The registered jobs. Each one calls a service function that already exists.

A handler is deliberately thin: work out the date the job is about, call the
module's own function as the tenant's system actor, and return a tally. What a
recompute or an accrual *means* stays in the module that owns it.

Service imports are local to each handler. The scheduler is imported from the
application lifespan, and importing every module's service at that point would
make a circular import in any one of them an application that will not start.

The system actor is a `User` that is never added to a session: it carries the
tenant id and a tenant-wide role so `tenant_scoped` and the permission checks
resolve, and it has no id, so every audit row it causes records no human actor
rather than a fabricated one. The scheduler's own run table says which job did it.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole
from app.services.scheduler.registry import Cadence, Job, JobContext, JobScope

__all__ = ["JOBS", "NOT_YET_REGISTERED", "job_by_key", "system_actor"]


def system_actor(tenant_id) -> User:
    """The identity a scheduled job acts as inside one tenant. Never persisted."""
    return User(
        tenant_id=tenant_id,
        email="scheduler@system.askmira.invalid",
        full_name="AskMira scheduler",
        role=UserRole.TENANT_ADMIN,
        is_active=True,
        password_hash="!",
        employee_id=None,
    )


def _tally(result: Any) -> dict[str, Any]:
    """Counts and flags only. A list becomes its length — names never land here."""
    if not isinstance(result, dict):
        return {"result": result if isinstance(result, int | bool) else str(type(result).__name__)}
    out: dict[str, Any] = {}
    for key, value in result.items():
        if isinstance(value, bool | int | float):
            out[key] = value
        elif isinstance(value, list | tuple | set | dict):
            out[key] = len(value)
        elif isinstance(value, str) and len(value) <= 64:
            out[key] = value
    return out


def _last_day_of_previous_month(today: date) -> date:
    return today.replace(day=1) - timedelta(days=1)


# ── Time ────────────────────────────────────────────────────────────────────


async def _attendance_recompute(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.attendance import service as attendance

    day = ctx.today - timedelta(days=1)
    written = await attendance.recompute_range(
        session, ctx.actor, employee_ids=None, start=day, end=day
    )
    return {"date": day.isoformat(), "days_written": written}


async def _leave_accruals(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.leave import service as leave

    result = await leave.run_accruals(session, ctx.actor, year=ctx.today.year)
    return {"year": result.year, "balances_updated": result.balances_updated}


async def _device_silence(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.identity import devices

    silent = await devices.silent_devices(session, ctx.tenant_id)
    return {"silent_devices": len(silent)}


# ── Payroll ─────────────────────────────────────────────────────────────────


async def _gratuity_accrual(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.exit_benefits import service as exit_benefits

    as_of = _last_day_of_previous_month(ctx.today)
    accruals = await exit_benefits.accrue_gratuity(session, ctx.actor, as_of=as_of)
    return {"as_of": as_of.isoformat(), "accruals": len(accruals)}


# ── Notifications ───────────────────────────────────────────────────────────


async def _notifications_due(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.notifications import dispatch

    sent = await dispatch.process_due_notifications(session, ctx.tenant_id)
    return {"sent": sent}


async def _notification_digests(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.notifications import dispatch

    sent = await dispatch.send_digests(session, ctx.tenant_id)
    return {"digests_sent": sent}


# ── People ──────────────────────────────────────────────────────────────────


async def _lifecycle_effect_due(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.lifecycle import service as lifecycle

    return _tally(await lifecycle.effect_due_events(session, ctx.actor, on=ctx.today))


async def _probation_reminders(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.lifecycle import service as lifecycle

    return _tally(await lifecycle.send_probation_reminders(session, ctx.actor, on=ctx.today))


# ── Hiring ──────────────────────────────────────────────────────────────────


async def _offers_lapse(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.hiring import offers

    return _tally(await offers.lapse_due_offers(session, ctx.actor, as_of=ctx.today))


async def _candidate_purge(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.hiring import careers

    return _tally(await careers.purge_expired(session, ctx.actor, as_of=ctx.today))


async def _job_board_refresh(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    """Refresh every posting that is due. One posting's failure is recorded by
    the job-board module against that posting and does not stop the rest."""
    from fastapi import HTTPException

    from app.services.jobboards import distribution

    due_ids = [p.id for p in await distribution.due_refreshes(session, ctx.actor, as_of=ctx.now)]
    refreshed = failed = 0
    for posting_id in due_ids:
        try:
            await distribution.refresh_posting(
                session, ctx.actor, posting_id=posting_id, now=ctx.now
            )
            await session.commit()
            refreshed += 1
        except HTTPException:
            await session.rollback()
            failed += 1
    return {"due": len(due_ids), "refreshed": refreshed, "failed": failed}


# ── Rewards ─────────────────────────────────────────────────────────────────


async def _points_expiry(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.rewards import service as rewards

    return _tally(await rewards.expire_points(session, ctx.actor, as_of=ctx.today))


# ── Reports ─────────────────────────────────────────────────────────────────


async def _report_schedules(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    """Run every schedule that is due, each in its own transaction."""
    from fastapi import HTTPException

    from app.services.reporting import service as reporting

    due_ids = [s.id for s in await reporting.due_schedules(session, ctx.actor, as_of=ctx.now)]
    sent = failed = 0
    for schedule_id in due_ids:
        try:
            await reporting.run_schedule(session, ctx.actor, schedule_id=schedule_id, as_of=ctx.now)
            await session.commit()
            sent += 1
        except HTTPException:
            await session.rollback()
            failed += 1
    return {"due": len(due_ids), "run": sent, "failed": failed}


# ── Governance ──────────────────────────────────────────────────────────────


async def _retention_sweep(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.retention import service as retention

    return _tally(await retention.scheduled_sweep(session, ctx.actor, today=ctx.today))


async def _proposal_expiry(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.autonomy import service as autonomy

    return _tally(await autonomy.expire_stale(session, ctx.actor, now=ctx.now))


# ── Platform ────────────────────────────────────────────────────────────────


async def _identity_purge(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.identity import sso

    assertions = await sso.purge_expired_assertions(session)
    attempts = await sso.purge_expired_login_attempts(session)
    return {"assertions_purged": assertions, "login_attempts_purged": attempts}


async def _prune_scheduler_history(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.scheduler import service as scheduler

    return {"runs_pruned": await scheduler.prune_history(session, now=ctx.now)}


JOBS: tuple[Job, ...] = (
    Job(
        key="attendance.nightly_recompute",
        name="Attendance recompute",
        description="Recomputes yesterday's attendance days from punches, leave and holidays.",
        cadence=Cadence.daily(hour=1),
        handler=_attendance_recompute,
        source="app.services.attendance.service.recompute_range",
        module="leave",
    ),
    Job(
        key="leave.monthly_accruals",
        name="Leave accruals",
        description="Brings every active employee's accrued leave up to date for the year.",
        cadence=Cadence.monthly(day=1, hour=2),
        handler=_leave_accruals,
        source="app.services.leave.service.run_accruals",
        module="leave",
    ),
    Job(
        key="devices.silence_check",
        name="Silent attendance devices",
        description="Marks biometric devices that have not reported within their threshold.",
        cadence=Cadence.every(minutes=60),
        handler=_device_silence,
        source="app.services.identity.devices.silent_devices",
    ),
    Job(
        key="payroll.gratuity_accrual",
        name="Gratuity accrual",
        description="Accrues gratuity for every active employee as of the end of last month.",
        cadence=Cadence.monthly(day=1, hour=3),
        handler=_gratuity_accrual,
        source="app.services.exit_benefits.service.accrue_gratuity",
        module="payroll",
    ),
    Job(
        key="notifications.process_due",
        name="Deferred and retried notifications",
        description="Sends notifications held by quiet hours and retries transient failures.",
        cadence=Cadence.every(minutes=5),
        handler=_notifications_due,
        source="app.services.notifications.dispatch.process_due_notifications",
    ),
    Job(
        key="notifications.daily_digests",
        name="Daily digests",
        description="Sends each person's digest once it is due in the tenant's timezone.",
        cadence=Cadence.every(minutes=60),
        handler=_notification_digests,
        source="app.services.notifications.dispatch.send_digests",
    ),
    Job(
        key="lifecycle.effect_due",
        name="Effect approved lifecycle events",
        description="Applies approved promotions, transfers and confirmations on their date.",
        cadence=Cadence.daily(hour=0),
        handler=_lifecycle_effect_due,
        source="app.services.lifecycle.service.effect_due_events",
    ),
    Job(
        key="lifecycle.probation_reminders",
        name="Probation reminders",
        description="Sends escalating reminders for probation reviews that are due.",
        cadence=Cadence.daily(hour=9),
        handler=_probation_reminders,
        source="app.services.lifecycle.service.send_probation_reminders",
    ),
    Job(
        key="hiring.lapse_offers",
        name="Lapse expired offers",
        description="Expires released offers past their validity date.",
        cadence=Cadence.daily(hour=0),
        handler=_offers_lapse,
        source="app.services.hiring.offers.lapse_due_offers",
        module="hiring",
    ),
    Job(
        key="hiring.candidate_purge",
        name="Candidate data purge",
        description="Purges candidate data past its consented retention window.",
        cadence=Cadence.daily(hour=3),
        handler=_candidate_purge,
        source="app.services.hiring.careers.purge_expired",
        module="hiring",
    ),
    Job(
        key="hiring.job_board_refresh",
        name="Job-board refresh",
        description="Boosts postings whose refresh is due on each connected job board.",
        cadence=Cadence.every(minutes=60),
        handler=_job_board_refresh,
        source="app.services.jobboards.distribution.refresh_posting",
        module="hiring",
    ),
    Job(
        key="rewards.expire_points",
        name="Points expiry",
        description="Writes expiry entries for reward points past the tenant's policy.",
        cadence=Cadence.daily(hour=2),
        handler=_points_expiry,
        source="app.services.rewards.service.expire_points",
        module="rewards",
    ),
    Job(
        key="reports.scheduled_distribution",
        name="Scheduled reports",
        description="Runs due report schedules, compiling each copy as its recipient.",
        cadence=Cadence.every(minutes=15),
        handler=_report_schedules,
        source="app.services.reporting.service.run_schedule",
    ),
    Job(
        key="retention.sweep",
        name="Retention sweep",
        description=(
            "Plans retention actions from the tenant's policies, executes them when "
            "under the approval threshold, and re-applies the erasure suppression list."
        ),
        cadence=Cadence.daily(hour=4),
        handler=_retention_sweep,
        source="app.services.retention.service.scheduled_sweep",
    ),
    Job(
        key="autonomy.expire_proposals",
        name="Expire stale agent proposals",
        description="Marks pending agent proposals past their expiry. Never applies one.",
        cadence=Cadence.every(minutes=60),
        handler=_proposal_expiry,
        source="app.services.autonomy.service.expire_stale",
    ),
    Job(
        key="identity.purge_expired",
        name="Purge expired sign-in state",
        description="Removes expired SAML assertion ids and abandoned SSO login attempts.",
        cadence=Cadence.daily(hour=5),
        handler=_identity_purge,
        source="app.services.identity.sso.purge_expired_assertions",
        scope=JobScope.PLATFORM,
    ),
    Job(
        key="scheduler.prune_history",
        name="Prune job history",
        description="Deletes scheduler run records older than 30 days.",
        cadence=Cadence.daily(hour=5),
        handler=_prune_scheduler_history,
        source="app.services.scheduler.service.prune_history",
        scope=JobScope.PLATFORM,
    ),
)

#: Recurring requirements from the build prompts with no service function to
#: call yet. Listed on the admin screen rather than silently missing.
NOT_YET_REGISTERED: tuple[dict[str, str], ...] = (
    {
        "requirement": "Device pull ingestion (prompt 04 §11)",
        "reason": "No function polls a device or an on-prem agent.",
    },
    {
        "requirement": "Job-board application polling (prompt 20 §3)",
        "reason": "Ingestion is push/manual only; no poll function exists.",
    },
    {
        "requirement": "Interview reminders (prompt 21 §5)",
        "reason": "No reminder sweep function exists in the interview module.",
    },
    {
        "requirement": "Appraisal stage reminders (prompt 15 §6)",
        "reason": "No reminder sweep function exists in the appraisal module.",
    },
    {
        "requirement": "Tax declaration reminders (prompt 07 §6)",
        "reason": "No reminder sweep function exists in the tax module.",
    },
    {
        "requirement": "Culture re-survey scheduling (prompt 32)",
        "reason": "No scheduling function exists in the culture module.",
    },
    {
        "requirement": "BI connector scheduled exports (prompt 37 §1)",
        "reason": "No bucket or Postgres-role transport exists to deliver to.",
    },
    {
        "requirement": "Agent evaluation production sampling (prompt 27 §5)",
        "reason": "No production agent exists to sample.",
    },
)


def job_by_key(key: str) -> Job | None:
    return next((job for job in JOBS if job.key == key), None)
