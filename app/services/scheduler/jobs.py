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

from dataclasses import asdict, is_dataclass
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
        role=UserRole.ADMIN,
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


def _as_dict(result: Any) -> Any:
    """A pydantic model or a dataclass outcome, as the dict `_tally` reads."""
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if is_dataclass(result) and not isinstance(result, type):
        return asdict(result)
    return result


def _last_day_of_previous_month(today: date) -> date:
    return today.replace(day=1) - timedelta(days=1)


# ── Time ────────────────────────────────────────────────────────────────────


async def _attendance_recompute(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.attendance import service as attendance

    return await attendance.nightly_recompute(session, ctx.actor, as_of=ctx.today)


async def _leave_accruals(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.leave import service as leave

    result = await leave.run_accruals(session, ctx.actor, year=ctx.today.year)
    return {"year": result.year, "balances_updated": result.balances_updated}


async def _leave_year_end(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.leave import ledger

    return _tally(await ledger.year_end_job(session, ctx.actor, today=ctx.today))


async def _overtime_sync(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.attendance import overtime

    return _tally(await overtime.nightly_sync(session, ctx.actor, as_of=ctx.today))


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


async def _probation_agent(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.autonomy import agents

    return _tally(await agents.probation_agent(session, ctx.actor, today=ctx.today))


async def _timesheet_agent(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.autonomy import agents

    return _tally(await agents.timesheet_agent(session, ctx.actor, today=ctx.today))


async def _schedule_agent(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.autonomy import agents

    return _tally(await agents.schedule_agent(session, ctx.actor, today=ctx.today))


async def _asset_return_reminders(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.assets import service as assets

    return _tally(await assets.send_return_due_reminders(session, ctx.actor, on=ctx.today))


async def _activate_joiners(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.onboarding import service as onboarding

    return _tally(await onboarding.activate_due_joiners(session, ctx.actor, on=ctx.today))


async def _onboarding_overdue(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.onboarding import service as onboarding

    return _tally(await onboarding.send_overdue_reminders(session, ctx.actor, on=ctx.today))


# ── Performance ─────────────────────────────────────────────────────────────


async def _feedback_reminders(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.performance import feedback

    return _tally(await feedback.send_due_reminders(session, ctx.actor, on=ctx.today))


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


# ── Helpdesk ────────────────────────────────────────────────────────────────


async def _helpdesk_auto_close(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.helpdesk import service as helpdesk

    return _tally(await helpdesk.auto_close_resolved(session, ctx.actor, now=ctx.now))


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


# ── Reminders and sweeps that used to wait for somebody to press a button ───


async def _interview_reminders(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.interviews import service as interviews

    return _tally(
        await interviews.send_due_interview_reminders(session, ctx.actor, window_minutes=30)
    )


async def _performance_sweep(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.appraisal import operations

    return _tally(await operations.run_performance_sweep(session, ctx.actor, as_of=ctx.today))


async def _tax_window_reminders(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.tax import reminders

    return _tally(
        _as_dict(await reminders.send_window_reminders(session, ctx.tenant_id, today=ctx.today))
    )


async def _reimbursement_reminders(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.loans import service as loans

    raised = await loans.send_unclaimed_entitlement_reminders(
        session, ctx.tenant_id, today=ctx.today
    )
    return {"notifications_raised": raised}


async def _announcement_delivery(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.culture import service as culture

    return _tally(await culture.deliver_due_announcements(session, ctx.tenant_id, now=ctx.now))


async def _resurvey_scheduling(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.culture import surveys_portal

    return _tally(
        await surveys_portal.schedule_due_resurveys(session, ctx.tenant_id, today=ctx.today)
    )


async def _document_expiry_notices(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.documents import vault

    return await vault.send_expiry_notices(session, ctx.tenant_id, today=ctx.today)


async def _milestones(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.culture import milestones

    assert ctx.actor is not None
    return await milestones.celebrate_due(session, ctx.actor, today=ctx.today)


async def _learning_reminders(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services import learning

    assert ctx.actor is not None
    return await learning.send_overdue_reminders(session, ctx.actor, today=ctx.today)


async def _webhooks(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services import integrations

    assert ctx.tenant_id is not None
    pumped = await integrations.pump_events(session, ctx.tenant_id, now=ctx.now)
    sent = await integrations.deliver_due(session, ctx.tenant_id, now=ctx.now)
    return {**pumped, **sent}


async def _statutory_alerts(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services import statutory_watch

    assert ctx.actor is not None
    return await statutory_watch.send_alerts(session, ctx.actor, today=ctx.today)


async def _attendance_reminders(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.attendance import reminders

    assert ctx.actor is not None
    return await reminders.send_attendance_reminders(
        session, ctx.actor, today=ctx.today, now=ctx.now
    )


async def _timesheet_reminders(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services import timesheets

    assert ctx.actor is not None
    return await timesheets.send_timesheet_reminders(
        session, ctx.actor, today=ctx.today, now=ctx.now
    )


async def _approval_escalation(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.approvals import rules

    assert ctx.actor is not None
    return await rules.escalate_due(session, ctx.actor, now=ctx.now)


async def _candidate_messages(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.hiring import candidate_comms

    assert ctx.actor is not None
    return await candidate_comms.deliver_due(session, ctx.actor, now=ctx.now)


async def _signal_digest(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.reporting import analytics

    assert ctx.actor is not None
    return await analytics.send_signal_digest(session, ctx.actor, today=ctx.today)


async def _reward_expiry_warnings(session: AsyncSession, ctx: JobContext) -> dict[str, Any]:
    from app.services.rewards import portal

    return _tally(
        _as_dict(await portal.send_expiry_warnings(session, ctx.tenant_id, as_of=ctx.today))
    )


JOBS: tuple[Job, ...] = (
    Job(
        key="attendance.nightly_recompute",
        name="Attendance recompute",
        description=(
            "Recomputes the last three days from punches, leave, holidays and approved "
            "corrections, and backfills any day since last month that was never computed."
        ),
        cadence=Cadence.daily(hour=1),
        handler=_attendance_recompute,
        source="app.services.attendance.service.nightly_recompute",
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
        key="leave.year_end",
        name="Leave year-end and comp-off expiry",
        description=(
            "On 1 January carries each balance forward up to its policy limit and lapses the "
            "rest; every day, lapses comp-off credits past their expiry that were not used."
        ),
        cadence=Cadence.daily(hour=0),
        handler=_leave_year_end,
        source="app.services.leave.ledger.year_end_job",
        module="leave",
    ),
    Job(
        key="attendance.overtime_sync",
        name="Overtime from attendance",
        description=(
            "Turns the last seven days' overtime minutes into overtime entries under each "
            "location's policy, for managers to approve. Runs after the attendance recompute."
        ),
        cadence=Cadence.daily(hour=2),
        handler=_overtime_sync,
        source="app.services.attendance.overtime.nightly_sync",
        module="leave",
    ),
    Job(
        key="hiring.interview_reminders",
        name="Interview reminders",
        description="Reminds the panel and the candidate about interviews starting soon.",
        cadence=Cadence.every(minutes=15),
        handler=_interview_reminders,
        source="app.services.interviews.service.send_due_interview_reminders",
        module="hiring",
    ),
    Job(
        key="performance.sweep",
        name="Performance sweep",
        description=(
            "Advances appraisal stages whose window closed, escalates overdue steps, reminds "
            "owners of steps due soon, and schedules probation reviews."
        ),
        cadence=Cadence.daily(hour=6),
        handler=_performance_sweep,
        source="app.services.appraisal.operations.run_performance_sweep",
    ),
    Job(
        key="tax.window_reminders",
        name="Tax declaration reminders",
        description="Reminds employees that a declaration or proof window is opening or closing.",
        cadence=Cadence.daily(hour=7),
        handler=_tax_window_reminders,
        source="app.services.tax.reminders.send_window_reminders",
        module="payroll",
    ),
    Job(
        key="payroll.reimbursement_reminders",
        name="Unclaimed reimbursement reminders",
        description=(
            "Warns employees whose reimbursement entitlement will become taxable at year end "
            "if no bills are submitted."
        ),
        cadence=Cadence.monthly(day=1, hour=8),
        handler=_reimbursement_reminders,
        source="app.services.loans.service.send_unclaimed_entitlement_reminders",
        module="payroll",
    ),
    Job(
        key="culture.announcement_delivery",
        name="Announcement delivery",
        description="Delivers announcements whose publish time has come to their audience.",
        cadence=Cadence.every(minutes=15),
        handler=_announcement_delivery,
        source="app.services.culture.service.deliver_due_announcements",
        module="culture",
    ),
    Job(
        key="culture.resurvey_scheduling",
        name="Re-survey scheduling",
        description="Opens the next wave of every recurring survey that is due.",
        cadence=Cadence.daily(hour=5),
        handler=_resurvey_scheduling,
        source="app.services.culture.surveys_portal.schedule_due_resurveys",
        module="culture",
    ),
    Job(
        key="documents.expiry_notices",
        name="Document expiry notices",
        description=(
            "Tells employees when a document of theirs expires within 30 days or has expired, "
            "and sends HR one summary — each document at most twice."
        ),
        cadence=Cadence.daily(hour=8),
        handler=_document_expiry_notices,
        source="app.services.documents.vault.send_expiry_notices",
        module="core_hr",
    ),
    Job(
        key="culture.milestones",
        name="Work anniversaries",
        description=(
            "Marks the work anniversaries the company has chosen, on the feed for people who "
            "share theirs and privately for everyone else, with points where the rule says so."
        ),
        cadence=Cadence.daily(hour=9),
        handler=_milestones,
        source="app.services.culture.milestones.celebrate_due",
        module="culture",
    ),
    Job(
        key="attendance.reminders",
        name="Attendance reminders",
        description=(
            "Reminds each approver of attendance corrections waiting more than two days, and "
            "HR when last month's attendance is still unlocked after the 3rd."
        ),
        cadence=Cadence.daily(hour=10),
        handler=_attendance_reminders,
        source="app.services.attendance.reminders.send_attendance_reminders",
        module="leave",
    ),
    Job(
        key="timesheets.reminders",
        name="Timesheet reminders",
        description=(
            "Reminds people of finished weeks they saved but did not submit, and approvers "
            "of timesheets waiting on them more than two days."
        ),
        cadence=Cadence.daily(hour=10),
        handler=_timesheet_reminders,
        source="app.services.timesheets.send_timesheet_reminders",
        module="leave",
    ),
    Job(
        key="integrations.webhooks",
        name="Webhook deliveries",
        description=(
            "Turns committed changes into webhook deliveries and sends what is due, retrying "
            "failures with growing gaps."
        ),
        cadence=Cadence.every(minutes=1),
        handler=_webhooks,
        source="app.services.integrations.deliver_due",
    ),
    Job(
        key="payroll.statutory_alerts",
        name="Statutory change alerts",
        description=(
            "Tells admins once about a published change in PF, ESI, PT, LWF or tax that takes "
            "effect within 30 days and no rule set covers yet."
        ),
        cadence=Cadence.daily(hour=8),
        handler=_statutory_alerts,
        source="app.services.statutory_watch.send_alerts",
        module="payroll",
    ),
    Job(
        key="learning.overdue_reminders",
        name="Overdue course reminders",
        description="Reminds people whose assigned course is overdue, at most once a week.",
        cadence=Cadence.daily(hour=10),
        handler=_learning_reminders,
        source="app.services.learning.send_overdue_reminders",
    ),
    Job(
        key="approvals.escalation",
        name="Approval escalation",
        description=(
            "Moves a leave request or expense claim up a level when its current approver has "
            "had it longer than their approval rule allows: to their manager, else HR."
        ),
        cadence=Cadence.every(60),
        handler=_approval_escalation,
        source="app.services.approvals.rules.escalate_due",
        module="core_hr",
    ),
    Job(
        key="hiring.candidate_messages",
        name="Candidate emails",
        description=(
            "Sends queued emails to candidates — application received, interviews, offers, "
            "decisions — through the tenant's mail relay, retrying a failed send."
        ),
        cadence=Cadence.every(5),
        handler=_candidate_messages,
        source="app.services.hiring.candidate_comms.deliver_due",
        module="hiring",
    ),
    Job(
        key="analytics.signal_digest",
        name="People signals digest",
        description=(
            "On Mondays, tells HR and each line manager who in their scope meets a burnout or "
            "absence rule, with the rule and evidence a click away. Never the pay signal."
        ),
        cadence=Cadence.daily(hour=9),
        handler=_signal_digest,
        source="app.services.reporting.analytics.send_signal_digest",
        module="core_hr",
    ),
    Job(
        key="rewards.expiry_warnings",
        name="Points expiry warnings",
        description="Warns each person whose reward points are about to expire.",
        cadence=Cadence.daily(hour=9),
        handler=_reward_expiry_warnings,
        source="app.services.rewards.portal.send_expiry_warnings",
        module="rewards",
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
        key="autonomy.probation_agent",
        name="Probation agent",
        description=(
            "Under an auto-confirm probation policy, drafts the confirmation for a review "
            "that passed in silence and proposes it for approval."
        ),
        cadence=Cadence.daily(hour=10),
        handler=_probation_agent,
        source="app.services.autonomy.agents.probation_agent",
    ),
    Job(
        key="autonomy.timesheet_agent",
        name="Timesheet agent",
        description=(
            "Checks submitted timesheets against attendance and proposes approving the ones "
            "whose hours are supported, for their approver to decide."
        ),
        cadence=Cadence.daily(hour=11),
        handler=_timesheet_agent,
        source="app.services.autonomy.agents.timesheet_agent",
    ),
    Job(
        key="autonomy.schedule_agent",
        name="Schedule agent",
        description=(
            "Finds shifts short of their minimum headcount over the coming week and proposes "
            "qualified, available people to cover them, for a person to approve."
        ),
        cadence=Cadence.daily(hour=12),
        handler=_schedule_agent,
        source="app.services.autonomy.agents.schedule_agent",
    ),
    Job(
        key="assets.return_reminders",
        name="Asset return reminders",
        description="Reminds holders of assets due back within three days, or overdue, once.",
        cadence=Cadence.daily(hour=9),
        handler=_asset_return_reminders,
        source="app.services.assets.service.send_return_due_reminders",
    ),
    Job(
        key="onboarding.activate_joiners",
        name="Activate new joiners",
        description="Moves pre-joiners whose joining date has arrived to active, audited.",
        cadence=Cadence.daily(hour=0),
        handler=_activate_joiners,
        source="app.services.onboarding.service.activate_due_joiners",
    ),
    Job(
        key="onboarding.overdue_reminders",
        name="Onboarding overdue reminders",
        description="Tells each assignee, once, about onboarding tasks that went overdue.",
        cadence=Cadence.daily(hour=9),
        handler=_onboarding_overdue,
        source="app.services.onboarding.service.send_overdue_reminders",
    ),
    Job(
        key="performance.feedback_reminders",
        name="360 feedback reminders",
        description=(
            "Reminds colleagues whose requested feedback is due within two days or overdue, "
            "at most every two days each, and stops two weeks after the due date."
        ),
        cadence=Cadence.daily(hour=9),
        handler=_feedback_reminders,
        source="app.services.performance.feedback.send_due_reminders",
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
        key="helpdesk.auto_close_resolved",
        name="Close resolved helpdesk tickets",
        description="Closes tickets resolved more than 7 days ago that nobody reopened or rated.",
        cadence=Cadence.daily(hour=2),
        handler=_helpdesk_auto_close,
        source="app.services.helpdesk.service.auto_close_resolved",
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
