"""Uploading TRACES' Form 16 Part A, and joining it to our Part B.

A single upload names its employee. A ZIP — what TRACES hands an employer for a
whole TAN — is matched file by file on the PAN in each file name
(`ABCDE1234F_2026-27.pdf` and the like). A file whose PAN matches nobody, or
more than one person, is reported back by name and stored for nobody; a guess
here would hand one person another's tax record.

Replacing a year's Part A keeps the newer file. Every upload is audited.
"""

from __future__ import annotations

import io
import re
import uuid
import zipfile
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.config import settings
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.form16_part_a import Form16PartA
from app.models.user import User
from app.services import audit
from app.services.storage import get_storage
from app.services.storage.sniff import FileRejectedError, validate_upload

__all__ = [
    "MAX_PDF_BYTES",
    "merged_pdf",
    "part_a_for",
    "upload_one",
    "upload_zip",
]

#: One Part A PDF, and a whole TAN's ZIP.
MAX_PDF_BYTES = settings.MAX_UPLOAD_BYTES
MAX_ZIP_BYTES = settings.MAX_ARCHIVE_BYTES
_PAN = re.compile(r"[A-Z]{5}[0-9]{4}[A-Z]")
_FY = re.compile(r"^\d{4}-\d{2}$")


def _check_year(financial_year: str) -> None:
    if not _FY.match(financial_year):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The financial year reads like 2025-26.",
        )


def _check_pdf(data: bytes) -> None:
    try:
        kind = validate_upload(data, max_bytes=MAX_PDF_BYTES)
    except FileRejectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    if kind.mime_type != "application/pdf":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Form 16 Part A must be a PDF."
        )


async def _store(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    financial_year: str,
    data: bytes,
    filename: str | None,
) -> Form16PartA:
    key = f"{user.tenant_id}/form16-part-a/{financial_year}/{uuid.uuid4()}.pdf"
    await get_storage().put(key, data)
    row = await session.scalar(
        tenant_scoped(
            select(Form16PartA).where(
                Form16PartA.employee_id == employee_id,
                Form16PartA.financial_year == financial_year,
            ),
            Form16PartA,
            user,
        )
    )
    now = datetime.now(UTC)
    if row is None:
        row = Form16PartA(
            tenant_id=user.tenant_id,
            employee_id=employee_id,
            financial_year=financial_year,
            storage_key=key,
            original_filename=filename,
            uploaded_by_user_id=user.id,
            uploaded_at=now,
        )
        session.add(row)
    else:
        row.storage_key = key
        row.original_filename = filename
        row.uploaded_by_user_id = user.id
        row.uploaded_at = now
    return row


async def upload_one(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    financial_year: str,
    data: bytes,
    filename: str | None,
    request: Request | None = None,
) -> dict[str, Any]:
    permissions.require_hr(user, detail="Form 16 Part A is uploaded by HR.")
    _check_year(financial_year)
    _check_pdf(data)
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    row = await _store(session, user, employee.id, financial_year, data, filename)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="form16_part_a",
        entity_id=row.id,
        actor=user,
        changes={"employee_id": str(employee.id), "financial_year": financial_year},
        summary=f"Uploaded Form 16 Part A for {employee.employee_code}, {financial_year}",
        request=request,
    )
    await session.commit()
    return {"employee_id": str(employee.id), "financial_year": financial_year, "stored": True}


async def upload_zip(
    session: AsyncSession,
    user: User,
    *,
    financial_year: str,
    data: bytes,
    request: Request | None = None,
) -> dict[str, Any]:
    """Every PDF in a TRACES ZIP, matched to its person by the PAN in its name."""
    permissions.require_hr(user, detail="Form 16 Part A is uploaded by HR.")
    _check_year(financial_year)
    if len(data) > MAX_ZIP_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The ZIP is larger than the archive upload limit.",
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="That is not a ZIP file."
        ) from exc

    by_pan: dict[str, list[Employee]] = {}
    for person in (
        await session.scalars(
            tenant_scoped(select(Employee).where(Employee.pan.is_not(None)), Employee, user)
        )
    ).all():
        if person.pan:
            by_pan.setdefault(person.pan.strip().upper(), []).append(person)

    stored: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for info in archive.infolist():
        name = info.filename.rsplit("/", 1)[-1]
        if info.is_dir() or not name.lower().endswith(".pdf"):
            continue
        if info.file_size > MAX_PDF_BYTES:
            skipped.append({"file": name, "reason": "Larger than the upload limit."})
            continue
        match = _PAN.search(name.upper())
        if match is None:
            skipped.append({"file": name, "reason": "No PAN in the file name."})
            continue
        people = by_pan.get(match.group(0), [])
        if len(people) != 1:
            skipped.append(
                {
                    "file": name,
                    "reason": "No one here has that PAN."
                    if not people
                    else "More than one person has that PAN.",
                }
            )
            continue
        content = archive.read(info)
        try:
            _check_pdf(content)
        except HTTPException as exc:
            skipped.append({"file": name, "reason": str(exc.detail)})
            continue
        await _store(session, user, people[0].id, financial_year, content, name)
        stored.append({"file": name, "employee_code": people[0].employee_code})

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="form16_part_a",
        actor=user,
        changes={"financial_year": financial_year, "stored": len(stored), "skipped": len(skipped)},
        summary=(
            f"Uploaded Form 16 Part A for {financial_year}: {len(stored)} stored, "
            f"{len(skipped)} not matched"
        ),
        request=request,
    )
    await session.commit()
    return {"financial_year": financial_year, "stored": stored, "skipped": skipped}


async def part_a_for(
    session: AsyncSession, user: User, employee_id: uuid.UUID, financial_year: str
) -> Form16PartA | None:
    return await session.scalar(
        tenant_scoped(
            select(Form16PartA).where(
                Form16PartA.employee_id == employee_id,
                Form16PartA.financial_year == financial_year,
            ),
            Form16PartA,
            user,
        )
    )


async def merged_pdf(part_a: Form16PartA | None, part_b: bytes) -> tuple[bytes, bool]:
    """Part A (when uploaded) then Part B, as one PDF. Returns whether A was included."""
    if part_a is None:
        return part_b, False
    from pypdf import PdfReader, PdfWriter

    first = await get_storage().get(part_a.storage_key)
    if not first:
        return part_b, False
    writer = PdfWriter()
    for source in (first, part_b):
        for page in PdfReader(io.BytesIO(source)).pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), True
