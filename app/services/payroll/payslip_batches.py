"""Payslips for chosen people over the last 1, 3, 6 or 12 months: one download, or an email each.

HR only (admin and sub-admin; the router is gated). A sub-admin narrowed to
some departments or locations can pick only their people: the ids are checked
while the request's HR scope is in force, and only checked ids are stored.

The period is counted back from a month HR names, the latest approved payroll
month unless they pick another — so "last 3 months" in November means the
three months that have payslips, not October, which has none yet.

What goes out:

- **Download**: every payslip of an approved or paid month, HR's copy, as one
  ZIP of PDFs (or the PDF itself when there is only one). Kept for a day.
- **Email**: each person gets one email with their payslips attached, only
  those already released to them (published), password-protected when the
  workspace protects payslips. Nobody is emailed a month HR has not released.

The work runs in the background (`spawn`); a payslip takes a fraction of a
second to render and an email a second or two to send. Each person's outcome
is committed as it happens, so a batch picked up again after a restart
(`sweep`) carries on where it stopped and never emails anybody twice.
"""

from __future__ import annotations

import asyncio
import calendar
import io
import logging
import sys
import uuid
import zipfile
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.notifications import Channel
from app.models.org import Department
from app.models.payroll import PayrollRun, Payslip
from app.models.payslip_batch import PayslipBatch
from app.models.user import User
from app.schemas.payslip_batch import (
    PayslipBatchCreate,
    PayslipBatchRead,
    PayslipBatchResultRead,
    PayslipEmailCreate,
    PayslipPeopleRead,
    PayslipPersonRead,
)
from app.services import audit
from app.services.documents.pdf import PdfRenderError
from app.services.notifications import dispatch, email_brand
from app.services.notifications.adapters import (
    Attachment,
    OutboundMessage,
    PermanentFailure,
    TransientFailure,
)
from app.services.payroll import payslip_pdf
from app.services.payroll import payslips as payslip_service
from app.services.payroll.run import CLOSED_STATUSES
from app.services.storage import get_storage

__all__ = [
    "EMAIL_EVENT",
    "PERIODS",
    "batch_file",
    "create_batch",
    "get_batch",
    "list_batches",
    "people",
    "process",
    "spawn",
    "sweep",
    "window",
    "window_label",
]

logger = logging.getLogger("askmira.payroll.payslip_batches")

PERIODS = (1, 3, 6, 12)
#: More than a whole workforce over a year; a bigger ask is split by HR.
MAX_PAYSLIPS = 15000
FILE_TTL = timedelta(hours=24)
#: A running batch untouched this long was cut off by a restart.
STALE_AFTER = timedelta(minutes=10)
#: A queued batch nobody has started this long after it was asked for.
UNSTARTED_AFTER = timedelta(seconds=30)
#: Seconds between two emails. Microsoft 365 accepts 30 a minute from one
#: mailbox; going faster gets the rest of a batch deferred.
SEND_INTERVAL = 2.1
#: Waits before retrying an email the mail server deferred.
RETRY_WAITS = (5.0, 30.0, 60.0)

DOWNLOAD = "download"
EMAIL = "email"
QUEUED, RUNNING, COMPLETED, FAILED = "queued", "running", "completed", "failed"

EMAIL_EVENT = "payroll.payslips.emailed"
dispatch.register_event(
    EMAIL_EVENT,
    "Your {payslip_word} for {period}",
    "Dear {employee_name},\n\n"
    "Please find attached your {payslip_word} for {period} from {company}.{password_note}\n\n"
    "If anything on it looks wrong, please contact HR.",
)

_TASKS: set[asyncio.Task] = set()


# ── Months ──────────────────────────────────────────────────────────────────


def _month(value: date) -> date:
    return value.replace(day=1)


def _shift(value: date, months: int) -> date:
    index = value.year * 12 + (value.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def window(end: date, months: int) -> tuple[date, date]:
    """The first days of the first and the last month of `months` months ending at `end`."""
    last = _month(end)
    return _shift(last, -(months - 1)), last


def _label(value: date) -> str:
    return f"{calendar.month_name[value.month]} {value.year}"


def window_label(start: date, end: date) -> str:
    """`September 2026`, or `July 2026 to September 2026`."""
    if start == end:
        return _label(start)
    return f"{_label(start)} to {_label(end)}"


def _index(year: Any, month: Any) -> Any:
    return year * 12 + month


def _in_window(start: date, end: date) -> tuple:
    """A closed run whose month falls in the window."""
    period = _index(PayrollRun.period_year, PayrollRun.period_month)
    return (
        PayrollRun.status.in_(CLOSED_STATUSES),
        period >= _index(start.year, start.month),
        period <= _index(end.year, end.month),
    )


def _check_months(months: int) -> None:
    if months not in PERIODS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Pick the last 1, 3, 6 or 12 months.",
        )


async def latest_month(session: AsyncSession, user: User) -> date | None:
    """The latest month with an approved or paid payroll."""
    row = (
        await session.execute(
            tenant_scoped(
                select(PayrollRun.period_year, PayrollRun.period_month)
                .where(PayrollRun.status.in_(CLOSED_STATUSES))
                .order_by(PayrollRun.period_year.desc(), PayrollRun.period_month.desc())
                .limit(1),
                PayrollRun,
                user,
            )
        )
    ).first()
    return date(row[0], row[1], 1) if row else None


async def _counts(
    session: AsyncSession,
    user: User,
    start: date,
    end: date,
    employee_ids: list[uuid.UUID] | None = None,
) -> dict[uuid.UUID, tuple[int, int]]:
    """Per person: payslips in the window, and how many of them are released."""
    stmt = (
        select(
            Payslip.employee_id,
            func.count(Payslip.id),
            func.count(Payslip.id).filter(Payslip.is_published.is_(True)),
        )
        .join(PayrollRun, Payslip.run_id == PayrollRun.id)
        .where(*_in_window(start, end))
        .group_by(Payslip.employee_id)
    )
    if employee_ids is not None:
        stmt = stmt.where(Payslip.employee_id.in_(employee_ids))
    rows = (await session.execute(tenant_scoped(stmt, Payslip, user))).all()
    return {row[0]: (int(row[1]), int(row[2])) for row in rows}


async def _email_ready(session: AsyncSession, user: User) -> bool:
    return await dispatch.email_relay(session, user.tenant_id) != "console"


# ── Who has payslips ────────────────────────────────────────────────────────


async def people(
    session: AsyncSession, user: User, *, months: int, to: date | None = None
) -> PayslipPeopleRead:
    """Everybody HR can see, with how many payslips each has in the period."""
    _check_months(months)
    latest = await latest_month(session, user)
    end = _month(to) if to else latest
    rows = (
        await session.execute(
            tenant_scoped(
                select(Employee, Department.name)
                .outerjoin(Department, Employee.department_id == Department.id)
                .where(Employee.deleted_at.is_(None))
                .order_by(Employee.first_name, Employee.last_name, Employee.employee_code),
                Employee,
                user,
            )
        )
    ).all()
    counts: dict[uuid.UUID, tuple[int, int]] = {}
    start = None
    if end is not None:
        start, end = window(end, months)
        counts = await _counts(session, user, start, end)
    return PayslipPeopleRead(
        latest_month=latest,
        months=months,
        period_start=start,
        period_end=end,
        period_label=window_label(start, end) if start and end else "",
        email_ready=await _email_ready(session, user),
        password_protected=await payslip_service.payslip_protection(session, user),
        password_rule=payslip_service.PASSWORD_RULE,
        people=[
            PayslipPersonRead(
                employee_id=employee.id,
                employee_code=employee.employee_code,
                full_name=employee.full_name,
                department=department,
                designation=employee.designation,
                employment_status=employee.status.value,
                date_of_exit=employee.date_of_exit,
                work_email=employee.work_email or None,
                personal_email=employee.personal_email or None,
                payslips=counts.get(employee.id, (0, 0))[0],
                released=counts.get(employee.id, (0, 0))[1],
            )
            for employee, department in rows
        ],
    )


# ── Asking for a batch ──────────────────────────────────────────────────────


async def create_batch(
    session: AsyncSession,
    user: User,
    payload: PayslipBatchCreate | PayslipEmailCreate,
    *,
    action: str,
    request: Request | None = None,
) -> PayslipBatchRead:
    """Check the ask, write it down, and start it in the background."""
    _check_months(payload.months)
    latest = await latest_month(session, user)
    if latest is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No payroll month has been approved yet, so there are no payslips to send.",
        )
    start, end = window(payload.to or latest, payload.months)
    label = window_label(start, end)

    ids = list(dict.fromkeys(payload.employee_ids))
    found = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(Employee.id).where(Employee.id.in_(ids), Employee.deleted_at.is_(None)),
                    Employee,
                    user,
                )
            )
        ).all()
    )
    if len(found) != len(ids):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"{len(ids) - len(found)} of the people picked are not in this workspace, "
                "or not among the people your HR rights cover."
            ),
        )

    is_email = action == EMAIL
    if is_email and not await _email_ready(session, user):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Email is not set up for this workspace, so nothing would reach anybody. "
                "An admin sets the mail server under Settings, Notifications."
            ),
        )
    counts = await _counts(session, user, start, end, ids)
    total = sum(pair[1] if is_email else pair[0] for pair in counts.values())
    if total == 0:
        detail = (
            f"None of the people picked has a payslip released to them for {label}."
            if is_email
            else f"None of the people picked has a payslip for {label}."
        )
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)
    if total > MAX_PAYSLIPS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"That is {total:,} payslips; one request holds up to {MAX_PAYSLIPS:,}. "
                "Pick fewer people or fewer months."
            ),
        )

    send_to = payload.send_to if isinstance(payload, PayslipEmailCreate) else None
    batch = PayslipBatch(
        tenant_id=user.tenant_id,
        action=action,
        months=payload.months,
        period_start=start,
        period_end=end,
        employee_ids=[str(i) for i in ids],
        send_to=send_to if is_email else None,
        requested_by_user_id=user.id,
        status=QUEUED,
        total_people=len(ids),
        total_payslips=total,
        results=[],
    )
    session.add(batch)
    await session.flush()
    how = "to email to each person" if is_email else "to download"
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="payslip_batch",
        entity_id=batch.id,
        actor=user,
        changes={
            "action": action,
            "months": payload.months,
            "period": label,
            "people": len(ids),
            "payslips": total,
            "send_to": send_to,
        },
        summary=f"Asked for the payslips of {len(ids)} people for {label}, {how}",
        request=request,
    )
    await session.commit()
    await session.refresh(batch)
    spawn(user.tenant_id, batch.id)
    return await _read(session, user, batch, with_results=True)


# ── Reading batches ─────────────────────────────────────────────────────────


def _visible(user: User):
    """Admins see every batch; a sub-admin sees the ones they asked for."""
    if permissions.is_admin(user):
        return ()
    return (PayslipBatch.requested_by_user_id == user.id,)


async def _load(session: AsyncSession, user: User, batch_id: uuid.UUID) -> PayslipBatch:
    batch = await session.scalar(
        tenant_scoped(
            select(PayslipBatch).where(PayslipBatch.id == batch_id, *_visible(user)),
            PayslipBatch,
            user,
        )
    )
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return batch


async def _read(
    session: AsyncSession,
    user: User,
    batch: PayslipBatch,
    *,
    with_results: bool,
    names: dict[uuid.UUID, str] | None = None,
) -> PayslipBatchRead:
    requester = None
    if batch.requested_by_user_id is not None:
        if names is not None:
            requester = names.get(batch.requested_by_user_id)
        else:
            row = await session.scalar(
                tenant_scoped(
                    select(User.full_name).where(User.id == batch.requested_by_user_id), User, user
                )
            )
            requester = row
    now = datetime.now(UTC)
    return PayslipBatchRead(
        id=batch.id,
        action=batch.action,
        months=batch.months,
        period_start=batch.period_start,
        period_end=batch.period_end,
        period_label=window_label(batch.period_start, batch.period_end),
        send_to=batch.send_to,
        status=batch.status,
        total_people=batch.total_people,
        total_payslips=batch.total_payslips,
        processed=batch.processed,
        succeeded=batch.succeeded,
        skipped=batch.skipped,
        failed=batch.failed,
        error=batch.error,
        file_name=batch.file_name,
        file_size=batch.file_size,
        file_available=bool(
            batch.file_key and batch.file_expires_at and batch.file_expires_at > now
        ),
        file_expires_at=batch.file_expires_at,
        requested_by_name=requester,
        created_at=batch.created_at,
        started_at=batch.started_at,
        completed_at=batch.completed_at,
        results=(
            [PayslipBatchResultRead(**row) for row in batch.results or []] if with_results else []
        ),
    )


async def get_batch(session: AsyncSession, user: User, batch_id: uuid.UUID) -> PayslipBatchRead:
    return await _read(session, user, await _load(session, user, batch_id), with_results=True)


async def list_batches(
    session: AsyncSession, user: User, *, limit: int = 20
) -> list[PayslipBatchRead]:
    """The latest requests, newest first, without each person's outcome."""
    rows = (
        await session.scalars(
            tenant_scoped(
                select(PayslipBatch)
                .where(*_visible(user))
                .order_by(PayslipBatch.created_at.desc())
                .limit(limit),
                PayslipBatch,
                user,
            )
        )
    ).all()
    requester_ids = {row.requested_by_user_id for row in rows if row.requested_by_user_id}
    names: dict[uuid.UUID, str] = {}
    if requester_ids:
        names = {
            uid: name
            for uid, name in (
                await session.execute(
                    tenant_scoped(
                        select(User.id, User.full_name).where(User.id.in_(requester_ids)),
                        User,
                        user,
                    )
                )
            ).all()
        }
    return [await _read(session, user, row, with_results=False, names=names) for row in rows]


async def batch_file(
    session: AsyncSession, user: User, batch_id: uuid.UUID, *, request: Request | None = None
) -> tuple[bytes, str, str]:
    """The prepared download. Taking a copy of people's pay is audited."""
    batch = await _load(session, user, batch_id)
    if batch.action != DOWNLOAD:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="This request was sent by email."
        )
    if batch.status != COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="The payslips are still being prepared."
        )
    expired = HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="This file was removed a day after it was made. Ask for the payslips again.",
    )
    if not batch.file_key or not batch.file_expires_at:
        raise expired
    if batch.file_expires_at <= datetime.now(UTC):
        raise expired
    data = await get_storage().get(batch.file_key)
    if data is None:
        raise expired
    label = window_label(batch.period_start, batch.period_end)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.EXPORT,
        entity_type="payslip_batch",
        entity_id=batch.id,
        actor=user,
        changes={"export": "payslips", "people": batch.succeeded, "period": label},
        summary=f"Downloaded the payslips of {batch.succeeded} people for {label}",
        request=request,
    )
    await session.commit()
    name = batch.file_name or "payslips.zip"
    media = "application/pdf" if name.endswith(".pdf") else "application/zip"
    return data, name, media


# ── Doing the work ──────────────────────────────────────────────────────────


def _under_test_suite() -> bool:
    return "pytest" in sys.modules


def spawn(tenant_id: uuid.UUID, batch_id: uuid.UUID) -> None:
    """Start a batch in the background of this process.

    Not inside the test suite: tests call `process` themselves, on their own
    session. A batch whose start is lost (the server stopped) is started by
    the scheduler's `sweep`.
    """
    if _under_test_suite():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_run(tenant_id, batch_id), name=f"payslip-batch-{batch_id}")
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def _run(tenant_id: uuid.UUID, batch_id: uuid.UUID) -> None:
    from app.db.session import get_session_factory

    async with get_session_factory()() as session:
        try:
            await process(session, tenant_id, batch_id)
        except Exception:
            logger.exception("Payslip batch %s stopped", batch_id)


def _system(tenant_id: uuid.UUID) -> User:
    from app.services.scheduler.jobs import system_actor

    return system_actor(tenant_id)


def _address(employee: Employee, send_to: str | None) -> str | None:
    candidates = (
        [employee.personal_email, employee.work_email]
        if send_to == "personal"
        else [employee.work_email]
    )
    for value in candidates:
        text = (value or "").strip()
        if text and "@" in text and not text.startswith("@") and not text.endswith("@"):
            return text
    return None


def _unique(name: str, taken: set[str], run: PayrollRun) -> str:
    """A file name not yet in the ZIP: a second run in a month gets its type added."""
    if name not in taken:
        return name
    stem = name.removesuffix(".pdf")
    candidate = f"{stem}-{run.run_type.value.replace('_', '-')}.pdf"
    counter = 2
    while candidate in taken:
        candidate = f"{stem}-{counter}.pdf"
        counter += 1
    return candidate


async def _claim(session: AsyncSession, tenant_id: uuid.UUID, batch_id: uuid.UUID) -> bool:
    """Take a queued batch for this worker. False when somebody else has it."""
    batch = await session.scalar(
        tenant_scoped(
            select(PayslipBatch)
            .where(PayslipBatch.id == batch_id, PayslipBatch.status == QUEUED)
            .with_for_update(skip_locked=True),
            PayslipBatch,
            _system(tenant_id),
        )
    )
    if batch is None:
        await session.rollback()
        return False
    now = datetime.now(UTC)
    batch.status = RUNNING
    batch.started_at = batch.started_at or now
    batch.heartbeat_at = now
    if batch.action == DOWNLOAD:
        # The file is built in memory: a download cut off by a restart starts over.
        batch.results = []
        batch.processed = batch.succeeded = batch.skipped = batch.failed = 0
    await session.commit()
    return True


async def _fail(session: AsyncSession, batch_id: uuid.UUID, tenant_id: uuid.UUID, why: str) -> None:
    await session.rollback()
    batch = await session.scalar(
        tenant_scoped(
            select(PayslipBatch).where(PayslipBatch.id == batch_id),
            PayslipBatch,
            _system(tenant_id),
        )
    )
    if batch is None:
        return
    batch.status = FAILED
    batch.error = why[:2000]
    batch.completed_at = datetime.now(UTC)
    await session.commit()


async def process(session: AsyncSession, tenant_id: uuid.UUID, batch_id: uuid.UUID) -> None:
    """Work a queued batch through to the end. Never raises for one person's trouble."""
    if not await _claim(session, tenant_id, batch_id):
        return
    try:
        await _work(session, tenant_id, batch_id)
    except Exception as exc:
        logger.exception("Payslip batch %s failed", batch_id)
        await _fail(session, batch_id, tenant_id, f"Stopped by an unexpected error: {exc}")


async def _work(session: AsyncSession, tenant_id: uuid.UUID, batch_id: uuid.UUID) -> None:
    batch = await session.scalar(
        tenant_scoped(
            select(PayslipBatch).where(PayslipBatch.id == batch_id),
            PayslipBatch,
            _system(tenant_id),
        )
    )
    requester = (
        await session.scalar(
            tenant_scoped(
                select(User).where(User.id == batch.requested_by_user_id),
                User,
                _system(tenant_id),
            )
        )
        if batch.requested_by_user_id
        else None
    )
    if requester is None or not requester.is_active or not permissions.is_hr(requester):
        await _fail(
            session,
            batch_id,
            tenant_id,
            "The person who asked for these payslips no longer has payroll access.",
        )
        return
    requester.has_reports = False
    requester_id = requester.id

    is_email = batch.action == EMAIL
    start, end = batch.period_start, batch.period_end
    send_to = batch.send_to
    employee_ids = [uuid.UUID(value) for value in batch.employee_ids]
    done = {row["employee_id"] for row in batch.results or []}

    protect = is_email and await payslip_service.payslip_protection(session, requester)
    adapter = None
    if is_email:
        # A registered adapter (a test's) wins; else the workspace's relay.
        if await dispatch.email_relay(session, tenant_id) == "registered":
            adapter = dispatch.get_adapter(Channel.EMAIL)
        else:
            try:
                adapter, _relay = await dispatch.email_adapter_for(session, tenant_id)
            except Exception as exc:
                await _fail(
                    session, batch_id, tenant_id, f"The mail server could not be used: {exc}"
                )
                return
        wording = await dispatch.email_wording(session, tenant_id, EMAIL_EVENT)
        subject_template, body_template = wording or dispatch.DEFAULT_TEMPLATES[EMAIL_EVENT]

    buffer = io.BytesIO()
    archive = zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) if not is_email else None
    files: list[tuple[str, bytes]] = []
    taken: set[str] = set()
    last_sent = 0.0

    for employee_id in employee_ids:
        if str(employee_id) in done:
            continue
        employee = await session.scalar(
            tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, requester)
        )
        result: dict[str, Any] = {
            "employee_id": str(employee_id),
            "employee_code": employee.employee_code if employee else None,
            "name": employee.full_name if employee else None,
            "status": "skipped",
            "payslips": 0,
            "email": None,
            "detail": None,
        }
        conditions = [Payslip.employee_id == employee_id, *_in_window(start, end)]
        if is_email:
            conditions.append(Payslip.is_published.is_(True))
        rows = (
            (
                await session.execute(
                    tenant_scoped(
                        select(Payslip, PayrollRun)
                        .join(PayrollRun, Payslip.run_id == PayrollRun.id)
                        .where(*conditions)
                        .order_by(
                            PayrollRun.period_year,
                            PayrollRun.period_month,
                            PayrollRun.created_at,
                        ),
                        Payslip,
                        requester,
                    )
                )
            ).all()
            if employee is not None
            else []
        )

        rendered: list[tuple[str, bytes, date]] = []
        problems: list[str] = []
        for payslip, run in rows:
            month = date(run.period_year, run.period_month, 1)
            try:
                document = await payslip_service.payslip_document(
                    session, requester, payslip, run, employee
                )
                data = await asyncio.to_thread(payslip_pdf.render_payslip_pdf, document)
            except (payslip_pdf.PayslipMismatchError, PdfRenderError) as exc:
                problems.append(f"{_label(month)}: {exc}")
                continue
            if protect:
                data = payslip_service.protect_pdf(data, payslip_service.payslip_password(employee))
            name = _unique(payslip_service.payslip_filename(employee, run), taken, run)
            taken.add(name)
            rendered.append((name, data, month))

        if employee is None:
            result["detail"] = "No longer in the workspace."
        elif not rows:
            result["detail"] = (
                f"No payslip released for {window_label(start, end)}."
                if is_email
                else f"No payslip for {window_label(start, end)}."
            )
        elif not rendered:
            result["status"] = "failed"
            result["detail"] = "; ".join(problems)
        elif not is_email:
            for name, data, _month_of in rendered:
                archive.writestr(name, data)
                files.append((name, data))
            result["status"] = "added"
            result["payslips"] = len(rendered)
            if problems:
                result["detail"] = "Left out: " + "; ".join(problems)
        else:
            address = _address(employee, send_to)
            if address is None:
                result["detail"] = (
                    "No personal or work email address on record."
                    if send_to == "personal"
                    else "No work email address on record."
                )
            else:
                result["email"] = address
                period = window_label(rendered[0][2], rendered[-1][2])
                payload = {
                    "employee_name": employee.full_name,
                    "name": (employee.full_name or "").split(" ")[0],
                    "period": period,
                    "company": await _company(session, requester, rows[0][1]),
                    "count": str(len(rendered)),
                    "payslip_word": "payslips" if len(rendered) > 1 else "payslip",
                    "password_note": (
                        f"\n\nThe attached PDF{'s open' if len(rendered) > 1 else ' opens'} "
                        f"with a password. {payslip_service.PASSWORD_RULE}"
                        if protect
                        else ""
                    ),
                }
                subject = dispatch.render(subject_template, payload)
                body = dispatch.render(body_template, payload)
                fields: dict[str, Any] = {"body": body}
                try:
                    fields = email_brand.as_message_fields(
                        await email_brand.compose(
                            session, tenant_id, subject=subject, body=body, audience="employee"
                        )
                    )
                except Exception:
                    logger.warning("Branded layout unavailable for a payslip email", exc_info=True)
                message = OutboundMessage(
                    recipient_name=employee.full_name,
                    recipient_email=address,
                    recipient_phone=None,
                    subject=subject,
                    event_key=EMAIL_EVENT,
                    attachments=tuple(
                        Attachment(filename=name, data=data) for name, data, _m in rendered
                    ),
                    **fields,
                )
                wait = SEND_INTERVAL - (asyncio.get_running_loop().time() - last_sent)
                if last_sent and wait > 0:
                    await asyncio.sleep(wait)
                error = await _send(adapter, message)
                last_sent = asyncio.get_running_loop().time()
                if error is None:
                    result["status"] = "sent"
                    result["payslips"] = len(rendered)
                    if problems:
                        result["detail"] = "Left out: " + "; ".join(problems)
                    await audit.record(
                        session,
                        tenant_id=tenant_id,
                        action=AuditAction.EXPORT,
                        entity_type="payslip",
                        entity_id=rows[0][0].id,
                        actor_user_id=requester_id,
                        changes={
                            "export": "payslip_email",
                            "employee_code": employee.employee_code,
                            "period": period,
                            "payslips": len(rendered),
                            "batch_id": str(batch_id),
                        },
                        summary=(
                            f"Emailed the {period} payslip{'s' if len(rendered) > 1 else ''} "
                            f"of {employee.employee_code} to {address}"
                        ),
                    )
                else:
                    result["status"] = "failed"
                    result["detail"] = error

        batch = await session.scalar(
            tenant_scoped(
                select(PayslipBatch).where(PayslipBatch.id == batch_id),
                PayslipBatch,
                requester,
            )
        )
        batch.results = [*(batch.results or []), result]
        batch.processed += 1
        if result["status"] in ("sent", "added"):
            batch.succeeded += 1
        elif result["status"] == "failed":
            batch.failed += 1
        else:
            batch.skipped += 1
        batch.heartbeat_at = datetime.now(UTC)
        await session.commit()

    batch = await session.scalar(
        tenant_scoped(
            select(PayslipBatch).where(PayslipBatch.id == batch_id), PayslipBatch, requester
        )
    )
    label = window_label(start, end)
    if not is_email:
        archive.close()
        if not files:
            await _fail(session, batch_id, tenant_id, f"No payslip could be prepared for {label}.")
            return
        if len(files) == 1:
            name, data = files[0]
            extension = ".pdf"
        else:
            data = buffer.getvalue()
            span = f"{start:%b-%Y}" if start == end else f"{start:%b-%Y}-to-{end:%b-%Y}"
            name = f"payslips-{span}.zip"
            extension = ".zip"
        storage = get_storage()
        key = storage.new_key(tenant_id, "payslip-batches", extension)
        await storage.put(key, data)
        batch.file_key = key
        batch.file_name = name
        batch.file_size = len(data)
        batch.file_expires_at = datetime.now(UTC) + FILE_TTL
        await audit.record(
            session,
            tenant_id=tenant_id,
            action=AuditAction.UPDATE,
            entity_type="payslip_batch",
            entity_id=batch_id,
            actor_user_id=requester_id,
            changes={"prepared": len(files), "people": batch.succeeded, "period": label},
            summary=(
                f"Prepared {len(files)} payslip{'s' if len(files) != 1 else ''} of "
                f"{batch.succeeded} people for {label} to download"
            ),
        )
    batch.status = COMPLETED
    batch.completed_at = datetime.now(UTC)
    batch.heartbeat_at = batch.completed_at
    await session.commit()


async def _company(session: AsyncSession, user: User, run: PayrollRun) -> str:
    from app.models.org import LegalEntity
    from app.models.tenant import Tenant

    entity = await session.scalar(
        tenant_scoped(
            select(LegalEntity.name).where(LegalEntity.id == run.legal_entity_id),
            LegalEntity,
            user,
        )
    )
    if entity:
        return entity
    tenant = await session.get(Tenant, user.tenant_id)
    return tenant.name if tenant else "your company"


async def _send(adapter, message: OutboundMessage) -> str | None:
    """Send one email, retrying a deferral. The reason it failed, or None."""
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            await adapter.send(message)
            return None
        except PermanentFailure as exc:
            return f"Not sent: {exc}"
        except TransientFailure as exc:
            if attempt == len(RETRY_WAITS):
                return f"Not sent after {attempt + 1} tries: {exc}"
            await asyncio.sleep(RETRY_WAITS[attempt])
        except Exception as exc:
            return f"Not sent: {exc}"
    return "Not sent."


# ── The scheduler's part ────────────────────────────────────────────────────


async def sweep(session: AsyncSession, actor: User) -> dict[str, int]:
    """Every minute: restart batches a restart cut off, start any never started,
    and remove prepared files a day old."""
    now = datetime.now(UTC)
    stale = (
        await session.scalars(
            tenant_scoped(
                select(PayslipBatch).where(
                    PayslipBatch.status == RUNNING,
                    func.coalesce(PayslipBatch.heartbeat_at, PayslipBatch.started_at)
                    < now - STALE_AFTER,
                ),
                PayslipBatch,
                actor,
            )
        )
    ).all()
    for batch in stale:
        batch.status = QUEUED

    expired = (
        await session.scalars(
            tenant_scoped(
                select(PayslipBatch).where(
                    PayslipBatch.file_key.is_not(None), PayslipBatch.file_expires_at <= now
                ),
                PayslipBatch,
                actor,
            )
        )
    ).all()
    storage = get_storage()
    for batch in expired:
        try:
            await storage.delete(batch.file_key)
        except Exception:
            logger.warning("Could not remove an expired payslip file", exc_info=True)
            continue
        batch.file_key = None
    await session.commit()

    waiting = (
        await session.execute(
            tenant_scoped(
                select(PayslipBatch.id).where(
                    PayslipBatch.status == QUEUED,
                    PayslipBatch.updated_at < now - UNSTARTED_AFTER,
                ),
                PayslipBatch,
                actor,
            )
        )
    ).all()
    for (batch_id,) in waiting:
        spawn(actor.tenant_id, batch_id)
    return {"restarted": len(stale), "files_removed": len(expired), "started": len(waiting)}
