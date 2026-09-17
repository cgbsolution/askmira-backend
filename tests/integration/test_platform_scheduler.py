"""The scheduler: one leader, per-tenant runs, failures recorded, modules respected."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import SchedulerJobRun, SchedulerRunStatus, UserRole
from app.services.scheduler import service as scheduler
from app.services.scheduler.registry import Cadence, Job
from app.services.scheduler.runner import SchedulerRunner
from tests.integration import platform_support
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, make_person

pytestmark = requires_db

client = platform_support.client

LOCK = 0x5445535431


async def test_only_one_runner_holds_the_lock(engine):
    first = SchedulerRunner(engine=engine, jobs=(), lock_key=LOCK)
    second = SchedulerRunner(engine=engine, jobs=(), lock_key=LOCK)
    try:
        assert await first.try_acquire_leadership() is True
        assert await second.try_acquire_leadership() is False
        # The leader keeps it on the next tick.
        assert await first.try_acquire_leadership() is True
        await first.release_leadership()
        assert await second.try_acquire_leadership() is True
        assert await first.try_acquire_leadership() is False
    finally:
        await first.release_leadership()
        await second.release_leadership()


async def _ok(session, ctx):
    return {"tenant": str(ctx.tenant_id), "names": ["never", "stored"]}


async def _boom(session, ctx):
    raise RuntimeError("the attendance device feed is down")


async def test_a_tick_runs_each_tenant_once_and_records_failures(
    engine, session: AsyncSession, tenant, other_tenant
):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    jobs = (
        Job("test.ok", "OK", "", Cadence.daily(hour=0), _ok, source="tests"),
        Job("test.boom", "Boom", "", Cadence.daily(hour=0), _boom, source="tests"),
        Job(
            "test.rewards", "Needs rewards", "", Cadence.every(minutes=5), _ok,
            source="tests", module="rewards",
        ),
    )
    moment = datetime.now(UTC)
    outcomes = await scheduler.run_due_jobs(factory, jobs=jobs, now=moment)
    by_job: dict[str, list[str]] = {}
    for outcome in outcomes:
        by_job.setdefault(outcome["job"], []).append(outcome["status"])
    assert sorted(by_job["test.ok"]) == ["succeeded", "succeeded"]
    assert by_job["test.boom"] == ["failed", "failed"]
    assert by_job["test.rewards"] == ["skipped", "skipped"]

    failed = await session.scalar(
        select(SchedulerJobRun).where(SchedulerJobRun.job_key == "test.boom")
    )
    assert failed.status is SchedulerRunStatus.FAILED
    assert "feed is down" in failed.error
    ok = await session.scalar(select(SchedulerJobRun).where(SchedulerJobRun.job_key == "test.ok"))
    assert ok.detail["names"] == ["never", "stored"]

    # Nothing is due again on the same day.
    assert await scheduler.run_due_jobs(factory, jobs=jobs[:2], now=moment) == []


async def test_the_jobs_screen_is_admin_only(client, session, tenant):
    admin = await make_person(session, tenant, role=UserRole.HR_ADMIN, name="Asha Admin")
    employee = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Emp One")
    listing = await client.get("/api/v1/admin/scheduler/jobs", headers=bearer(admin))
    assert listing.status_code == 200
    body = listing.json()
    keys = {job["key"] for job in body["jobs"]}
    assert {"attendance.nightly_recompute", "retention.sweep"} <= keys
    assert body["not_yet_registered"]
    assert body["runner"]["enabled"] is False, "the scheduler never runs under the test suite"
    denied = await client.get("/api/v1/admin/scheduler/jobs", headers=bearer(employee))
    assert denied.status_code == 403


async def test_a_manual_run_is_recorded_for_the_callers_workspace_only(client, session, tenant):
    owner = await make_person(session, tenant, role=UserRole.TENANT_ADMIN, name="Owner One")
    hr_admin = await make_person(session, tenant, role=UserRole.HR_ADMIN, name="Asha Admin")
    path = "/api/v1/admin/scheduler/jobs/autonomy.expire_proposals/run"
    assert (await client.post(path, headers=bearer(hr_admin))).status_code == 403
    response = await client.post(path, headers=bearer(owner))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "succeeded"
    run = await session.scalar(
        select(SchedulerJobRun).where(SchedulerJobRun.job_key == "autonomy.expire_proposals")
    )
    assert run.tenant_id == tenant.id and run.trigger == "manual"
