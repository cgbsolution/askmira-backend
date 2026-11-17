"""Consultant hours entered by finance for a period, approved by a second person.

Finance (HR, or the consultant's finance approver) enters a consultant's billable
hours for a period, total only or day by day, attaches the customer's timesheet
file and submits. Somebody else approves: never whoever entered or last edited
the entry, and never an override. If nobody else can, the entry waits.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import areas, permissions
from app.core.access_control import rights_for
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.consultant_hours import (
    ConsultantHoursAttachment,
    ConsultantHoursEntry,
    HoursEntryStatus,
)
from app.models.consultant_payouts import ConsultantPayoutEntry
from app.models.employee import Employee
from app.models.payroll import PayrollRun, PayrollRunStatus, Payslip
from app.models.payroll_inputs import PayrollAdjustment
from app.models.timesheets import Project
from app.models.user import User, UserRole
from app.services import audit, uploads
from app.services.consultants import can_manage_consultant, is_consultant
from app.services.filings import support
from app.services.notifications.dispatch import notify, register_event
from app.services.storage.sniff import FileRejectedError, sniff

__all__ = [
    "WAITING_NOTE",
    "HoursIn",
    "add_attachment",
    "attachment_file",
    "create_entry",
    "decide_entry",
    "get_entry",
    "in_payout_run",
    "list_entries",
    "period_label",
    "remove_attachment",
    "reopen_entry",
    "submit_entry",
    "update_entry",
]

#: Shown when nobody but the person who entered or last edited an entry can approve it.
WAITING_NOTE = "Needs another person to approve — set a finance approver for this consultant."

register_event(
    "consultant_hours.submitted",
    "{employee_name}'s hours for {period} need your approval",
    "{submitted_by} entered {hours} billable hours for {employee_name} for {period}. "
    "Check them against the customer's timesheet and approve or reject them under "
    "Payroll, Consultant hours.",
)
register_event(
    "consultant_hours.decided",
    "{employee_name}'s hours for {period} were {outcome}",
    "{decided_by} {outcome} the {hours} hours for {employee_name} for {period}.{note_line}",
)

DayStatus = Literal["present", "holiday", "week_off", "leave", "half_day"]
_MAX_HOURS_PER_DAY = Decimal(24)
_TWO_PLACES = Decimal("0.01")
_EDITABLE = (HoursEntryStatus.DRAFT, HoursEntryStatus.REJECTED)
#: Consultant hours sit in the payroll area; a sub-admin narrowed from approving there is out.
_AREA = "payroll"
_DECIDE_PATH = "/api/v1/consultant-hours/entry/decide"
_OVERLAP_CONSTRAINT = "ex_consultant_hours_no_overlap"
_SHEET_ACCEPT = "Upload an Excel sheet (xlsx or xls), CSV, PDF, PNG or JPG."


class DayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    hours: Decimal = Field(ge=0, le=24, max_digits=5, decimal_places=2)
    status: DayStatus = "present"


class HoursIn(BaseModel):
    """What finance types in. The billable total is the sum of the days when days are given."""

    model_config = ConfigDict(extra="forbid")

    employee_id: uuid.UUID
    period_start: date
    period_end: date
    project_id: uuid.UUID | None = None
    total_billable_hours: Decimal | None = Field(default=None, gt=0, max_digits=7, decimal_places=2)
    non_billable_hours: Decimal | None = Field(default=None, ge=0, max_digits=7, decimal_places=2)
    days: list[DayIn] | None = Field(default=None, max_length=31)
    customer_reference: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _check(self) -> HoursIn:
        if self.period_end < self.period_start:
            raise ValueError("The period must end on or after the day it starts.")
        if (self.period_end - self.period_start).days > 30:
            raise ValueError("A period can be at most 31 days.")
        if self.days:
            seen = [d.date for d in self.days]
            if len(set(seen)) != len(seen):
                raise ValueError("A day can only appear once.")
            if any(not self.period_start <= d <= self.period_end for d in seen):
                raise ValueError("Every day must fall inside the period.")
        elif self.total_billable_hours is None:
            raise ValueError("Enter the billable hours, in total or day by day.")
        return self


def period_label(start: date, end: date) -> str:
    """A period as people read it, e.g. "01 Oct 2026 to 31 Oct 2026"."""
    return f"{start:%d %b %Y} to {end:%d %b %Y}"


def _error(code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


async def in_payout_run(session: AsyncSession, user: User, entry_id: uuid.UUID) -> bool:
    """Whether the entry is in any consultant payout run, draft included.

    Reads past the caller's HR scope: an entry held by a run is held for everybody.
    """
    found = await session.scalar(
        tenant_scoped(
            select(ConsultantPayoutEntry.id).where(ConsultantPayoutEntry.entry_id == entry_id),
            ConsultantPayoutEntry,
            user,
        ).execution_options(hr_scope_bypass=True)
    )
    return found is not None


# ── Reading and who may ─────────────────────────────────────────────────────


async def _employee(session: AsyncSession, user: User, employee_id: uuid.UUID) -> Employee:
    person = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if person is None:
        raise _error(status.HTTP_404_NOT_FOUND, "Person not found.")
    return person


def _may_act(user: User, person: Employee) -> bool:
    """HR, or the person's finance approver; never the person themself."""
    if person.id == user.employee_id:
        return False
    return permissions.is_hr(user) or person.internal_approver_user_id == user.id


async def _require_access(session: AsyncSession, user: User, person: Employee) -> None:
    """HR or the consultant's finance approver; never the consultant themself.

    Who the caller is comes first: somebody with no right to the person is told nothing
    about them, not even whether they are a consultant.
    """
    if _may_act(user, person) and not is_consultant(person):
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"{person.full_name} is not a consultant."
        )
    if not await can_manage_consultant(session, user, person):
        raise _error(
            status.HTTP_403_FORBIDDEN,
            "Only HR or the consultant's finance approver can work with their hours.",
        )


async def _load(
    session: AsyncSession, user: User, entry_id: uuid.UUID, *, lock: bool = False
) -> tuple[ConsultantHoursEntry, Employee]:
    """The entry and its consultant, the caller's right to them checked. `lock` holds the row.

    An entry the caller has no right to is a 404, as if it did not exist.
    """
    stmt = tenant_scoped(
        select(ConsultantHoursEntry).where(ConsultantHoursEntry.id == entry_id),
        ConsultantHoursEntry,
        user,
    )
    if lock:
        stmt = stmt.with_for_update()
    entry = await session.scalar(stmt)
    person = await _employee(session, user, entry.employee_id) if entry is not None else None
    if entry is None or person is None or not _may_act(user, person):
        raise _error(status.HTTP_404_NOT_FOUND, "Hours entry not found.")
    await _require_access(session, user, person)
    return entry, person


async def _hr_pool(session: AsyncSession, user: User) -> list[tuple[uuid.UUID, uuid.UUID | None]]:
    """(user id, their employee id) of every active HR user who may approve hours.

    A sub-admin narrowed away from approving in the payroll area is left out, the same
    test their own request would meet.
    """
    candidates = (
        await session.scalars(
            tenant_scoped(
                select(User).where(
                    User.role.in_([UserRole.ADMIN, UserRole.SUB_ADMIN]),
                    User.is_active.is_(True),
                ),
                User,
                user,
            )
        )
    ).all()
    needed = areas.required_right("POST", _DECIDE_PATH)
    pool: list[tuple[uuid.UUID, uuid.UUID | None]] = []
    for candidate in candidates:
        if candidate.role is UserRole.SUB_ADMIN:
            rights = await rights_for(session, candidate, _AREA)
            if rights is not None and not rights.allows(needed):
                continue
        pool.append((candidate.id, candidate.employee_id))
    return pool


async def _eligible_approvers(
    session: AsyncSession,
    user: User,
    person: Employee,
    entry: ConsultantHoursEntry,
    *,
    hr_pool: list[tuple[uuid.UUID, uuid.UUID | None]] | None = None,
) -> list[uuid.UUID]:
    """Who may approve this entry: the finance approver if set, active and not the consultant,
    else HR users; never anyone who entered, edited, attached a file to or submitted it."""
    pool: list[uuid.UUID] = []
    if person.internal_approver_user_id:
        approver = await session.scalar(
            tenant_scoped(
                select(User).where(
                    User.id == person.internal_approver_user_id, User.is_active.is_(True)
                ),
                User,
                user,
            )
        )
        if approver is not None and approver.employee_id != person.id:
            pool = [approver.id]
    if not pool:
        found = hr_pool if hr_pool is not None else await _hr_pool(session, user)
        pool = [uid for uid, employee_id in found if employee_id != person.id]
    barred = {str(uid) for uid in entry.maker_user_ids or []}
    return [uid for uid in pool if str(uid) not in barred]


def _add_maker(entry: ConsultantHoursEntry, user: User) -> None:
    """Record that this person worked on the entry, so they can never approve it."""
    makers = list(entry.maker_user_ids or [])
    if str(user.id) not in makers:
        entry.maker_user_ids = [*makers, str(user.id)]


# ── Serialising ─────────────────────────────────────────────────────────────


async def _attachments(
    session: AsyncSession, user: User, entry_id: uuid.UUID
) -> list[ConsultantHoursAttachment]:
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(ConsultantHoursAttachment)
                    .where(ConsultantHoursAttachment.entry_id == entry_id)
                    .order_by(ConsultantHoursAttachment.uploaded_at),
                    ConsultantHoursAttachment,
                    user,
                )
            )
        ).all()
    )


def _hours_text(value: Decimal) -> str:
    """Hours to two places, however the value was just assigned (a number or loaded text)."""
    return str(Decimal(str(value)).quantize(_TWO_PLACES))


async def _read(
    session: AsyncSession,
    user: User,
    entry: ConsultantHoursEntry,
    person: Employee,
    *,
    hr_pool: list[tuple[uuid.UUID, uuid.UUID | None]] | None = None,
) -> dict[str, Any]:
    eligible = await _eligible_approvers(session, user, person, entry, hr_pool=hr_pool)
    live = entry.status in (HoursEntryStatus.DRAFT, HoursEntryStatus.SUBMITTED)
    project_name = None
    if entry.project_id:
        project_name = await session.scalar(
            tenant_scoped(select(Project.name).where(Project.id == entry.project_id), Project, user)
        )
    return {
        "id": str(entry.id),
        "employee_id": str(entry.employee_id),
        "employee_name": person.full_name,
        "employee_code": person.employee_code,
        "period_start": entry.period_start.isoformat(),
        "period_end": entry.period_end.isoformat(),
        "project_id": str(entry.project_id) if entry.project_id else None,
        "project_name": project_name,
        "total_billable_hours": _hours_text(entry.total_billable_hours),
        "non_billable_hours": (
            _hours_text(entry.non_billable_hours) if entry.non_billable_hours is not None else None
        ),
        "days": entry.days,
        "customer_reference": entry.customer_reference,
        "status": entry.status.value,
        "entered_by_user_id": str(entry.entered_by_user_id) if entry.entered_by_user_id else None,
        "last_edited_by_user_id": (
            str(entry.last_edited_by_user_id) if entry.last_edited_by_user_id else None
        ),
        "submitted_at": entry.submitted_at.isoformat() if entry.submitted_at else None,
        "decided_by_user_id": str(entry.decided_by_user_id) if entry.decided_by_user_id else None,
        "decided_at": entry.decided_at.isoformat() if entry.decided_at else None,
        "decision_note": entry.decision_note,
        "attachments": [
            {
                "id": str(a.id),
                "file_name": a.file_name,
                "content_type": a.content_type,
                "size_bytes": a.size_bytes,
                "uploaded_at": a.uploaded_at.isoformat(),
            }
            for a in await _attachments(session, user, entry.id)
        ],
        "can_approve": entry.status is HoursEntryStatus.SUBMITTED and user.id in eligible,
        "approval_note": WAITING_NOTE if live and not eligible else None,
    }


# ── Writing ─────────────────────────────────────────────────────────────────


def _months_of(start: date, end: date) -> set[tuple[int, int]]:
    return {
        ((start + timedelta(days=i)).year, (start + timedelta(days=i)).month)
        for i in range((end - start).days + 1)
    }


async def _refuse_if_paid_through_payroll(
    session: AsyncSession, user: User, person: Employee, start: date, end: date
) -> None:
    """Refuse a period with a day in a month payroll already paid this person for."""
    months = _months_of(start, end)
    paid_slip = await session.scalar(
        tenant_scoped(
            select(Payslip.id)
            .join(PayrollRun, PayrollRun.id == Payslip.run_id)
            .where(
                Payslip.employee_id == person.id,
                PayrollRun.status.in_([PayrollRunStatus.APPROVED, PayrollRunStatus.PAID]),
                or_(
                    *(
                        and_(PayrollRun.period_year == y, PayrollRun.period_month == m)
                        for y, m in months
                    )
                ),
            )
            .limit(1),
            Payslip,
            user,
        )
    )
    paid_adjustment = None
    if paid_slip is None:
        paid_adjustment = await session.scalar(
            tenant_scoped(
                select(PayrollAdjustment.id)
                .where(
                    PayrollAdjustment.employee_id == person.id,
                    PayrollAdjustment.reference.like("timesheet:%"),
                    PayrollAdjustment.amount_applied > 0,
                    or_(
                        *(
                            and_(
                                PayrollAdjustment.start_year == y,
                                PayrollAdjustment.start_month == m,
                            )
                            for y, m in months
                        )
                    ),
                )
                .limit(1),
                PayrollAdjustment,
                user,
            )
        )
    if paid_slip is not None or paid_adjustment is not None:
        raise _error(status.HTTP_409_CONFLICT, "That period was already paid through payroll.")


def _figures(data: HoursIn) -> tuple[Decimal, list[dict[str, Any]] | None]:
    """The billable total and the day grid to keep; refuses a total that is not the days' sum."""
    days = None
    total = data.total_billable_hours
    if data.days:
        days = [
            {
                "date": d.date.isoformat(),
                "hours": float(d.hours.quantize(_TWO_PLACES)),
                "status": d.status,
            }
            for d in sorted(data.days, key=lambda d: d.date)
        ]
        summed = sum((d.hours for d in data.days), Decimal(0))
        if total is not None and total != summed:
            raise _error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"The days add up to {summed} hours but the total says {total}.",
            )
        total = summed
    if total is None or total <= 0:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Enter more than zero billable hours.")
    span = (data.period_end - data.period_start).days + 1
    if total > _MAX_HOURS_PER_DAY * span:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"That is more than 24 hours a day over the {span} days of the period.",
        )
    return total, days


async def _check_project(session: AsyncSession, user: User, project_id: uuid.UUID | None) -> None:
    if project_id is None:
        return
    found = await session.scalar(
        tenant_scoped(select(Project.id).where(Project.id == project_id), Project, user)
    )
    if found is None:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "That project does not exist.")


async def _commit_or_overlap(
    session: AsyncSession, person_name: str, start: date, end: date, *, flush_only: bool = False
) -> None:
    """Save, turning the database's no-overlap refusal into a plain 409."""
    try:
        if flush_only:
            await session.flush()
        else:
            await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if _OVERLAP_CONSTRAINT not in str(exc):
            raise
        raise _error(
            status.HTTP_409_CONFLICT,
            f"Hours for {person_name} already cover part of {period_label(start, end)}.",
        ) from exc


async def create_entry(
    session: AsyncSession, user: User, data: HoursIn, *, request: Request | None = None
) -> dict[str, Any]:
    person = await _employee(session, user, data.employee_id)
    await _require_access(session, user, person)
    total, days = _figures(data)
    await _check_project(session, user, data.project_id)
    await _refuse_if_paid_through_payroll(session, user, person, data.period_start, data.period_end)
    entry = ConsultantHoursEntry(
        tenant_id=user.tenant_id,
        employee_id=person.id,
        period_start=data.period_start,
        period_end=data.period_end,
        project_id=data.project_id or person.project_id,
        total_billable_hours=total,
        non_billable_hours=data.non_billable_hours,
        days=days,
        customer_reference=(data.customer_reference or "").strip() or None,
        status=HoursEntryStatus.DRAFT,
        entered_by_user_id=user.id,
        last_edited_by_user_id=user.id,
        maker_user_ids=[str(user.id)],
    )
    session.add(entry)
    name, start, end = person.full_name, data.period_start, data.period_end
    await _commit_or_overlap(session, name, start, end, flush_only=True)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="consultant_hours_entry",
        entity_id=entry.id,
        actor=user,
        changes={
            "employee_id": str(person.id),
            "period": period_label(start, end),
            "total_billable_hours": str(total),
        },
        summary=f"Entered {total} hours for {name} for {period_label(start, end)}",
        request=request,
    )
    entry_id = entry.id
    await _commit_or_overlap(session, name, start, end)
    entry, person = await _load(session, user, entry_id)
    return await _read(session, user, entry, person)


async def update_entry(
    session: AsyncSession,
    user: User,
    entry_id: uuid.UUID,
    data: HoursIn,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    entry, person = await _load(session, user, entry_id, lock=True)
    if entry.status not in _EDITABLE:
        raise _error(
            status.HTTP_409_CONFLICT,
            "Only hours that are still a draft, or were rejected, can be edited.",
        )
    if data.employee_id != entry.employee_id:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "The consultant cannot be changed.")
    if await in_payout_run(session, user, entry.id):
        raise _error(
            status.HTTP_409_CONFLICT,
            "These hours are already in a consultant payout. Leave them out of it first.",
        )
    total, days = _figures(data)
    await _check_project(session, user, data.project_id)
    await _refuse_if_paid_through_payroll(session, user, person, data.period_start, data.period_end)
    before = {
        "period": period_label(entry.period_start, entry.period_end),
        "total_billable_hours": str(entry.total_billable_hours),
    }
    entry.period_start = data.period_start
    entry.period_end = data.period_end
    entry.project_id = data.project_id or person.project_id
    entry.total_billable_hours = total
    entry.non_billable_hours = data.non_billable_hours
    entry.days = days
    entry.customer_reference = (data.customer_reference or "").strip() or None
    entry.last_edited_by_user_id = user.id
    _add_maker(entry, user)
    # A rejected entry being fixed is a draft again; the reason stays until it is sent back.
    entry.status = HoursEntryStatus.DRAFT
    name, start, end = person.full_name, data.period_start, data.period_end
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="consultant_hours_entry",
        entity_id=entry.id,
        actor=user,
        changes={
            "before": before,
            "after": {"period": period_label(start, end), "total_billable_hours": str(total)},
        },
        summary=f"Edited the hours for {name} for {period_label(start, end)}",
        request=request,
        flush=False,
    )
    await _commit_or_overlap(session, name, start, end)
    entry, person = await _load(session, user, entry_id)
    return await _read(session, user, entry, person)


# ── Attachments ─────────────────────────────────────────────────────────────

_XLSX_PART = "xl/workbook.xml"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_XLS_STREAM = "Workbook".encode("utf-16-le")
_XLS_STREAM_OLD = "Book".encode("utf-16-le")


def _spreadsheet_kind(data: bytes) -> tuple[str, str] | None:
    """(extension, media type) when the bytes are an xlsx, xls or csv; decided from the contents."""
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if _XLSX_PART in archive.namelist():
                    return (
                        ".xlsx",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
        except zipfile.BadZipFile:
            return None
        return None
    if data.startswith(_OLE_MAGIC):
        if _XLS_STREAM in data or _XLS_STREAM_OLD in data:
            return ".xls", "application/vnd.ms-excel"
        return None
    if b"\x00" not in data:
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None
        return ".csv", "text/csv"
    return None


async def _store_attachment(
    session: AsyncSession, user: User, data: bytes
) -> tuple[str, str, str, int]:
    """Validate and store the file encrypted; returns (key, media type, extension, size).

    pdf, png and jpg go through the vault like tax proofs. The vault does not know
    spreadsheets, so for this screen only xlsx, xls and csv are recognised here, by their
    contents, and sealed the same way.
    """
    if not data:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "The file is empty.")
    if sniff(data[:16]) is not None:
        stored = await support.store_upload(session, user.tenant_id, "consultant-hours", data)
        return stored.key, stored.mime_type, stored.extension, stored.size
    kind = _spreadsheet_kind(data)
    if kind is None:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"This file type is not accepted. {_SHEET_ACCEPT} The type is read from the "
            "file's contents, not its name.",
        )
    limit = await uploads.limit_bytes(session, user.tenant_id)
    try:
        uploads.check_size(data, limit)
    except FileRejectedError as exc:
        raise uploads.rejected(exc) from exc
    extension, mime_type = kind
    key = await support.store_generated(user.tenant_id, "consultant-hours", data, extension)
    return key, mime_type, extension, len(data)


def _display_name(name: str, extension: str) -> str:
    cleaned = (name or "").strip().replace("\\", "/").rsplit("/", 1)[-1][:240]
    return cleaned or f"customer-timesheet{extension}"


async def add_attachment(
    session: AsyncSession,
    user: User,
    entry_id: uuid.UUID,
    *,
    name: str,
    data: bytes,
    request: Request | None = None,
) -> dict[str, Any]:
    entry, person = await _load(session, user, entry_id, lock=True)
    if entry.status not in _EDITABLE:
        raise _error(
            status.HTTP_409_CONFLICT, "Files can only be changed while the hours are a draft."
        )
    key, mime_type, extension, size = await _store_attachment(session, user, data)
    attachment = ConsultantHoursAttachment(
        tenant_id=user.tenant_id,
        entry_id=entry.id,
        file_name=_display_name(name, extension),
        storage_key=key,
        content_type=mime_type,
        size_bytes=size,
        uploaded_by_user_id=user.id,
    )
    session.add(attachment)
    _add_maker(entry, user)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="consultant_hours_attachment",
        entity_id=attachment.id,
        actor=user,
        changes={"entry_id": str(entry.id), "size_bytes": size, "content_type": mime_type},
        summary=f"Attached a file to the hours for {person.full_name}",
        request=request,
    )
    await session.commit()
    entry, person = await _load(session, user, entry_id)
    return await _read(session, user, entry, person)


async def remove_attachment(
    session: AsyncSession,
    user: User,
    entry_id: uuid.UUID,
    attachment_id: uuid.UUID,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    entry, person = await _load(session, user, entry_id, lock=True)
    if entry.status not in _EDITABLE:
        raise _error(
            status.HTTP_409_CONFLICT, "Files can only be changed while the hours are a draft."
        )
    attachment = await _attachment(session, user, entry.id, attachment_id)
    await session.delete(attachment)
    _add_maker(entry, user)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="consultant_hours_attachment",
        entity_id=attachment_id,
        actor=user,
        changes={"entry_id": str(entry.id), "file_name": attachment.file_name},
        summary=f"Removed a file from the hours for {person.full_name}",
        request=request,
    )
    await session.commit()
    entry, person = await _load(session, user, entry_id)
    return await _read(session, user, entry, person)


async def _attachment(
    session: AsyncSession, user: User, entry_id: uuid.UUID, attachment_id: uuid.UUID
) -> ConsultantHoursAttachment:
    attachment = await session.scalar(
        tenant_scoped(
            select(ConsultantHoursAttachment).where(
                ConsultantHoursAttachment.id == attachment_id,
                ConsultantHoursAttachment.entry_id == entry_id,
            ),
            ConsultantHoursAttachment,
            user,
        )
    )
    if attachment is None:
        raise _error(status.HTTP_404_NOT_FOUND, "File not found.")
    return attachment


async def attachment_file(
    session: AsyncSession, user: User, entry_id: uuid.UUID, attachment_id: uuid.UUID
) -> tuple[str, str, bytes]:
    """(file name, media type, bytes) of one attached file."""
    entry, _ = await _load(session, user, entry_id)
    attachment = await _attachment(session, user, entry.id, attachment_id)
    loaded = await support.load_file(user.tenant_id, attachment.storage_key)
    if loaded is None:
        raise _error(status.HTTP_404_NOT_FOUND, "The file could not be found.")
    return attachment.file_name, attachment.content_type, loaded[0]


# ── Submit, decide, reopen ──────────────────────────────────────────────────


async def submit_entry(
    session: AsyncSession, user: User, entry_id: uuid.UUID, *, request: Request | None = None
) -> dict[str, Any]:
    entry, person = await _load(session, user, entry_id, lock=True)
    if entry.status not in _EDITABLE:
        raise _error(status.HTTP_409_CONFLICT, "These hours were already sent for approval.")
    if not await _attachments(session, user, entry.id):
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Attach the customer's timesheet before submitting.",
        )
    await _refuse_if_paid_through_payroll(
        session, user, person, entry.period_start, entry.period_end
    )
    _add_maker(entry, user)
    entry.status = HoursEntryStatus.SUBMITTED
    entry.submitted_at = datetime.now(UTC)
    entry.decided_by_user_id = None
    entry.decided_at = None
    entry.decision_note = None
    name, start, end = person.full_name, entry.period_start, entry.period_end
    hours = str(entry.total_billable_hours)
    approvers = await _eligible_approvers(session, user, person, entry)
    if approvers:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key="consultant_hours.submitted",
            recipient_user_ids=approvers,
            payload={
                "employee_name": name,
                "period": period_label(start, end),
                "hours": hours,
                "submitted_by": user.full_name or user.email,
                "entry_id": str(entry.id),
            },
            commit=False,
        )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="consultant_hours_entry",
        entity_id=entry.id,
        actor=user,
        changes={"status": "submitted"},
        summary=f"Submitted the hours for {name} for {period_label(start, end)}",
        request=request,
        flush=False,
    )
    await _commit_or_overlap(session, name, start, end)
    entry, person = await _load(session, user, entry_id)
    return await _read(session, user, entry, person)


async def decide_entry(
    session: AsyncSession,
    user: User,
    entry_id: uuid.UUID,
    *,
    approve: bool,
    note: str | None,
    request: Request | None = None,
) -> dict[str, Any]:
    entry, person = await _load(session, user, entry_id, lock=True)
    if entry.status is not HoursEntryStatus.SUBMITTED:
        raise _error(status.HTTP_409_CONFLICT, "These hours are not waiting for a decision.")
    if str(user.id) in (entry.maker_user_ids or []):
        raise _error(
            status.HTTP_403_FORBIDDEN,
            "You worked on these hours, so someone else has to approve them.",
        )
    if user.id not in await _eligible_approvers(session, user, person, entry):
        raise _error(
            status.HTTP_403_FORBIDDEN,
            "Only this consultant's finance approver, or HR when none is set, can decide "
            "these hours.",
        )
    clean = (note or "").strip() or None
    if not approve and clean is None:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Say what needs changing.")
    if approve:
        await _refuse_if_paid_through_payroll(
            session, user, person, entry.period_start, entry.period_end
        )
    entry.status = HoursEntryStatus.APPROVED if approve else HoursEntryStatus.REJECTED
    entry.decided_by_user_id = user.id
    entry.decided_at = datetime.now(UTC)
    entry.decision_note = clean
    outcome = "approved" if approve else "rejected"
    name, start, end = person.full_name, entry.period_start, entry.period_end
    makers = {uuid.UUID(m) for m in entry.maker_user_ids or []} - {user.id}
    if makers:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key="consultant_hours.decided",
            recipient_user_ids=sorted(makers),
            payload={
                "employee_name": name,
                "period": period_label(start, end),
                "hours": str(entry.total_billable_hours),
                "outcome": outcome,
                "decided_by": user.full_name or user.email,
                "note_line": f" Reason: {clean}" if clean and not approve else "",
                "entry_id": str(entry.id),
            },
            commit=False,
        )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="consultant_hours_entry",
        entity_id=entry.id,
        actor=user,
        changes={"status": outcome, "note": clean},
        summary=f"{outcome.capitalize()} the hours for {name} for {period_label(start, end)}",
        request=request,
    )
    await session.commit()
    entry, person = await _load(session, user, entry_id)
    return await _read(session, user, entry, person)


async def reopen_entry(
    session: AsyncSession, user: User, entry_id: uuid.UUID, *, request: Request | None = None
) -> dict[str, Any]:
    entry, person = await _load(session, user, entry_id, lock=True)
    if not permissions.is_hr(user):
        raise _error(status.HTTP_403_FORBIDDEN, "Only HR can reopen decided hours.")
    if entry.status not in (HoursEntryStatus.APPROVED, HoursEntryStatus.REJECTED):
        raise _error(status.HTTP_409_CONFLICT, "Only approved or rejected hours can be reopened.")
    if await in_payout_run(session, user, entry.id):
        raise _error(
            status.HTTP_409_CONFLICT,
            "These hours are already in a consultant payout. Leave them out of it first.",
        )
    was = entry.status.value
    entry.status = HoursEntryStatus.DRAFT
    entry.submitted_at = None
    entry.decided_by_user_id = None
    entry.decided_at = None
    entry.decision_note = None
    entry.maker_user_ids = [str(user.id)]  # a fresh cycle: only the reopener is a maker
    name, start, end = person.full_name, entry.period_start, entry.period_end
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="consultant_hours_entry",
        entity_id=entry.id,
        actor=user,
        changes={"status": "draft", "was": was},
        summary=f"Reopened the hours for {name} for {period_label(start, end)}",
        request=request,
        flush=False,
    )
    await _commit_or_overlap(session, name, start, end)
    entry, person = await _load(session, user, entry_id)
    return await _read(session, user, entry, person)


# ── Listing ─────────────────────────────────────────────────────────────────


def _month_bounds(month: str) -> tuple[date, date]:
    try:
        first = datetime.strptime(month, "%Y-%m").date()
    except ValueError as exc:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Month must look like 2026-10.") from exc
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt - timedelta(days=1)


async def list_entries(
    session: AsyncSession,
    user: User,
    *,
    month: str | None = None,
    status_filter: str | None = None,
    employee_id: uuid.UUID | None = None,
    waiting_for_me: bool = False,
) -> list[dict[str, Any]]:
    """Entries the caller may see (HR: all; a finance approver: their consultants')."""
    stmt = select(ConsultantHoursEntry, Employee).join(
        Employee, Employee.id == ConsultantHoursEntry.employee_id
    )
    stmt = tenant_scoped(stmt, ConsultantHoursEntry, user)
    if not permissions.is_hr(user):
        stmt = stmt.where(Employee.internal_approver_user_id == user.id)
    if month:
        first, last = _month_bounds(month)
        stmt = stmt.where(
            ConsultantHoursEntry.period_start <= last, ConsultantHoursEntry.period_end >= first
        )
    if status_filter:
        try:
            wanted = HoursEntryStatus(status_filter)
        except ValueError as exc:
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown status.") from exc
        stmt = stmt.where(ConsultantHoursEntry.status == wanted)
    if employee_id:
        stmt = stmt.where(ConsultantHoursEntry.employee_id == employee_id)
    rows = (
        await session.execute(
            stmt.order_by(ConsultantHoursEntry.period_start.desc(), Employee.employee_code)
        )
    ).all()
    hr_pool = await _hr_pool(session, user)
    out: list[dict[str, Any]] = []
    for entry, person in rows:
        if not is_consultant(person) or person.id == user.employee_id:
            continue
        item = await _read(session, user, entry, person, hr_pool=hr_pool)
        if waiting_for_me and not item["can_approve"]:
            continue
        out.append(item)
    return out


async def get_entry(session: AsyncSession, user: User, entry_id: uuid.UUID) -> dict[str, Any]:
    entry, person = await _load(session, user, entry_id)
    return await _read(session, user, entry, person)
