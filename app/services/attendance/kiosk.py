"""Punching at a shared kiosk: an employee code, a PIN, and maybe a photo.

A kiosk is a tablet registered as a device of the `kiosk` type; its page carries
the device's push token, which is what establishes the workspace. Each person
sets their own PIN (four to six digits, hashed with Argon2). At the kiosk they
enter their employee code and PIN; whether it is an IN or an OUT is worked out
from their last punch, as on the punch screen.

Guessing is stopped in two places. Five wrong PINs for one person lock that
person's PIN for fifteen minutes. Twenty wrong entries at one tablet in a row
pause that tablet for ten minutes, so a common PIN cannot be tried against
every employee code. The kiosk never says whether a code exists: a wrong code
and a wrong PIN read the same.

The workspace's punch-photo rule applies here too. Nothing matches the face;
a reviewer looks at it, as with any punch photo.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_filter, tenant_scoped
from app.core.security import hash_password, verify_password
from app.models.attendance import AttendancePunch, PunchDirection, PunchSource, WorkLocation
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.identity import DeviceIntegration, DeviceVendor
from app.models.kiosk import EmployeeBadge, KioskPin
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.attendance.punch_photos import store_photo_for
from app.services.identity import devices as device_service

__all__ = [
    "KIOSK_PIN_LENGTHS",
    "badge_hash",
    "issue_badge",
    "kiosk_info",
    "kiosk_punch",
    "list_badges",
    "my_pin_status",
    "revoke_badge",
    "set_my_pin",
]

KIOSK_PIN_LENGTHS = (4, 6)
PERSON_ATTEMPTS = 5
PERSON_LOCK = timedelta(minutes=15)
DEVICE_ATTEMPTS = 20
DEVICE_LOCK = timedelta(minutes=10)
#: An IN older than this is a forgotten OUT; the next punch is an IN again.
OPEN_SHIFT = timedelta(hours=20)

_NO_MATCH = "That employee code and PIN do not match."


def _too_simple(pin: str) -> bool:
    """All one digit, or a straight run up or down."""
    if len(set(pin)) == 1:
        return True
    steps = {int(b) - int(a) for a, b in pairwise(pin)}
    return steps in ({1}, {-1})


async def my_pin_status(session: AsyncSession, user: User) -> dict[str, Any]:
    employee_id = permissions.own_employee_id(user)
    row = await session.scalar(
        tenant_scoped(
            select(KioskPin).where(KioskPin.employee_id == user.employee_id), KioskPin, user
        )
    )
    locked = (
        row is not None
        and row.locked_until is not None
        and row.locked_until > datetime.now(UTC)
    )
    return {
        "employee_id": str(employee_id),
        "is_set": row is not None,
        "set_at": (row.updated_at or row.created_at).isoformat() if row else None,
        "locked_until": row.locked_until.isoformat() if locked and row else None,
    }


async def set_my_pin(
    session: AsyncSession, user: User, pin: str, *, request: Request | None = None
) -> dict[str, Any]:
    """Set or change the caller's own kiosk PIN. Changing it clears any lock."""
    permissions.own_employee_id(user)
    if not re.fullmatch(r"\d+", pin) or not (
        KIOSK_PIN_LENGTHS[0] <= len(pin) <= KIOSK_PIN_LENGTHS[1]
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A kiosk PIN is four to six digits.",
        )
    if _too_simple(pin):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Choose a PIN that is not one digit repeated or a straight run like 1234.",
        )
    row = await session.scalar(
        tenant_scoped(
            select(KioskPin).where(KioskPin.employee_id == user.employee_id), KioskPin, user
        )
    )
    if row is None:
        row = KioskPin(tenant_id=user.tenant_id, employee_id=user.employee_id, pin_hash="")
        session.add(row)
    row.pin_hash = hash_password(pin)
    row.failed_attempts = 0
    row.locked_until = None
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="kiosk_pin",
        entity_id=row.id,
        actor=user,
        changes={"set": True},
        summary="Set their kiosk PIN",
        request=request,
    )
    await session.commit()
    return await my_pin_status(session, user)


async def _kiosk(session: AsyncSession, token: str) -> DeviceIntegration:
    try:
        device = await device_service.authenticate_device(session, token)
    except device_service.DeviceError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="This kiosk is not registered."
        ) from exc
    if device.vendor is not DeviceVendor.KIOSK:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="This device is not a kiosk."
        )
    return device


async def _photo_mode(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    tenant = await session.get(Tenant, tenant_id)
    return (((tenant.settings or {}).get("attendance") or {}) if tenant else {}).get(
        "photo_mode", "off"
    )


async def kiosk_info(session: AsyncSession, token: str) -> dict[str, Any]:
    """What the kiosk page shows before anyone punches."""
    device = await _kiosk(session, token)
    tenant = await session.get(Tenant, device.tenant_id)
    place = None
    if device.work_location_id:
        place = await session.scalar(
            select(WorkLocation.name).where(
                WorkLocation.id == device.work_location_id,
                tenant_filter(WorkLocation, device.tenant_id),
            )
        )
    return {
        "kiosk_name": device.name,
        "workspace": tenant.name if tenant else None,
        "location": place,
        "photo_mode": await _photo_mode(session, device.tenant_id),
        "pin_lengths": list(KIOSK_PIN_LENGTHS),
    }


def badge_hash(tenant_id: uuid.UUID, number: str) -> str:
    """The stored form of a card number: hashed with the workspace, never kept plain."""
    clean = "".join(number.split()).upper()
    return hashlib.sha256(f"{tenant_id}:{clean}".encode()).hexdigest()


async def kiosk_punch(
    session: AsyncSession,
    token: str,
    *,
    employee_code: str | None = None,
    pin: str | None = None,
    badge: str | None = None,
    photo: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    device = await _kiosk(session, token)
    moment = now or datetime.now(UTC)
    if device.kiosk_locked_until and device.kiosk_locked_until > moment:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many wrong entries at this kiosk. Try again in a few minutes.",
        )

    if badge:
        card = await session.scalar(
            select(EmployeeBadge).where(
                tenant_filter(EmployeeBadge, device.tenant_id),
                EmployeeBadge.badge_hash == badge_hash(device.tenant_id, badge),
                EmployeeBadge.revoked_at.is_(None),
            )
        )
        holder = (
            await session.scalar(
                select(Employee).where(
                    tenant_filter(Employee, device.tenant_id),
                    Employee.id == card.employee_id,
                    Employee.deleted_at.is_(None),
                    Employee.status != EmploymentStatus.EXITED,
                )
            )
            if card is not None
            else None
        )
        if holder is None:
            device.kiosk_failed_attempts += 1
            if device.kiosk_failed_attempts >= DEVICE_ATTEMPTS:
                device.kiosk_locked_until = moment + DEVICE_LOCK
                device.kiosk_failed_attempts = 0
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="That badge is not one this workspace knows. Use your code and PIN.",
            )
        return await _record_punch(session, device, holder, photo, moment, via="badge")

    employee = await session.scalar(
        select(Employee).where(
            tenant_filter(Employee, device.tenant_id),
            Employee.employee_code == employee_code.strip(),
            Employee.deleted_at.is_(None),
            Employee.status != EmploymentStatus.EXITED,
        )
    )
    record = None
    if employee is not None:
        record = await session.scalar(
            select(KioskPin).where(
                tenant_filter(KioskPin, device.tenant_id), KioskPin.employee_id == employee.id
            )
        )
    if record is not None and record.locked_until and record.locked_until > moment:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="This PIN is paused after too many wrong tries. Try again later, or punch "
            "another way.",
        )
    if employee is None or record is None or not verify_password(pin, record.pin_hash):
        device.kiosk_failed_attempts += 1
        if device.kiosk_failed_attempts >= DEVICE_ATTEMPTS:
            device.kiosk_locked_until = moment + DEVICE_LOCK
            device.kiosk_failed_attempts = 0
        if record is not None:
            record.failed_attempts += 1
            if record.failed_attempts >= PERSON_ATTEMPTS:
                record.locked_until = moment + PERSON_LOCK
                record.failed_attempts = 0
        await session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_NO_MATCH)

    record.failed_attempts = 0
    return await _record_punch(session, device, employee, photo, moment, via="pin")


async def _record_punch(
    session: AsyncSession,
    device: DeviceIntegration,
    employee: Employee,
    photo: str | None,
    moment: datetime,
    *,
    via: str,
) -> dict[str, Any]:
    mode = await _photo_mode(session, device.tenant_id)
    if mode == "required" and not photo:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This workspace takes a photo with every punch. Look at the camera and try "
            "again.",
        )

    device.kiosk_failed_attempts = 0
    last = await session.scalar(
        select(AttendancePunch)
        .where(
            tenant_filter(AttendancePunch, device.tenant_id),
            AttendancePunch.employee_id == employee.id,
            AttendancePunch.punched_at > moment - OPEN_SHIFT,
        )
        .order_by(AttendancePunch.punched_at.desc())
        .limit(1)
    )
    direction = (
        PunchDirection.OUT
        if last is not None and last.direction is PunchDirection.IN
        else PunchDirection.IN
    )
    photo_key = (
        await store_photo_for(device.tenant_id, photo) if photo and mode != "off" else None
    )
    label = None
    if device.work_location_id:
        label = await session.scalar(
            select(WorkLocation.name).where(
                WorkLocation.id == device.work_location_id,
                tenant_filter(WorkLocation, device.tenant_id),
            )
        )
    punch = AttendancePunch(
        tenant_id=device.tenant_id,
        employee_id=employee.id,
        punched_at=moment,
        direction=direction,
        source=PunchSource.KIOSK,
        device_id=device.device_id,
        location_label=label,
        raw_payload={"kiosk": device.device_id, "photo": photo_key is not None, "via": via},
        photo_key=photo_key,
    )
    session.add(punch)
    device_service.touch(device)
    await session.commit()
    return {
        "first_name": employee.first_name,
        "direction": direction.value,
        "punched_at": moment.isoformat(),
        "photo_taken": photo_key is not None,
    }


# ── Badges ──────────────────────────────────────────────────────────────────


def _badge_read(row: EmployeeBadge) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "employee_id": str(row.employee_id),
        "last_four": row.last_four,
        "issued_at": row.created_at.isoformat() if row.created_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
    }


async def list_badges(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> list[dict[str, Any]]:
    permissions.require_hr(user, detail="Badges are issued by HR.")
    rows = (
        await session.scalars(
            tenant_scoped(
                select(EmployeeBadge)
                .where(EmployeeBadge.employee_id == employee_id)
                .order_by(EmployeeBadge.created_at.desc()),
                EmployeeBadge,
                user,
            )
        )
    ).all()
    return [_badge_read(r) for r in rows]


async def issue_badge(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    number: str,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """Record a card for a person. A card already in someone's hands cannot be issued twice."""
    permissions.require_hr(user, detail="Badges are issued by HR.")
    clean = "".join(number.split()).upper()
    if len(clean) < 4 or not clean.isalnum():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A badge number is at least four letters or digits.",
        )
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    digest = badge_hash(user.tenant_id, clean)
    taken = await session.scalar(
        tenant_scoped(
            select(EmployeeBadge.id).where(
                EmployeeBadge.badge_hash == digest, EmployeeBadge.revoked_at.is_(None)
            ),
            EmployeeBadge,
            user,
        )
    )
    if taken is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That card is already issued to somebody. Revoke it there first.",
        )
    row = EmployeeBadge(
        tenant_id=user.tenant_id,
        employee_id=employee.id,
        badge_hash=digest,
        last_four=clean[-4:],
        issued_by_user_id=user.id,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="employee_badge",
        entity_id=row.id,
        actor=user,
        changes={"employee": employee.employee_code, "last_four": row.last_four},
        summary=f"Issued a kiosk badge ending {row.last_four} to {employee.employee_code}",
        request=request,
    )
    await session.commit()
    return _badge_read(row)


async def revoke_badge(
    session: AsyncSession, user: User, badge_id: uuid.UUID, *, request: Request | None = None
) -> dict[str, Any]:
    permissions.require_hr(user, detail="Badges are revoked by HR.")
    row = await session.scalar(
        tenant_scoped(
            select(EmployeeBadge).where(EmployeeBadge.id == badge_id), EmployeeBadge, user
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Badge not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="employee_badge",
            entity_id=row.id,
            actor=user,
            changes={"revoked": True, "last_four": row.last_four},
            summary=f"Revoked the kiosk badge ending {row.last_four}",
            request=request,
        )
        await session.commit()
    return _badge_read(row)
