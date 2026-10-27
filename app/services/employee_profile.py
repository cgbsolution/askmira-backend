"""The extended employee profile, profile change requests and the profile photo.

Three rules live here.

1. **Sensitive changes go through HR.** Legal name, date of birth, statutory
   identifiers, the salary account, addresses and nominees each reach a
   statutory return or decide where money goes, so an employee proposes them
   and HR approves. Everything else on the profile the employee edits
   directly. The split is `DIRECT_SECTIONS` / `SENSITIVE_SECTIONS` in the
   schema module. HR edits their own record directly, as they edit anyone's:
   a request would only come back to them, and a company with one admin has
   nobody else to decide it. The audit log records every such edit.
2. **An approval applies what was proposed, in the same transaction**, with
   the status change and the audit row — and refuses if the record moved
   since the request was raised, rather than overwriting a correction HR made
   in between.
3. **Identifiers are masked on the way out for everyone except HR**. The full
   Aadhaar number is kept encrypted and shown in full to HR only; everyone
   else sees its last four. A bank account number also reaches HR in full
   (salary transfers and bank queries need it); everyone else gets its last four.
"""

from __future__ import annotations

import contextlib
import secrets
import uuid
from datetime import UTC, date, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.disbursement import EmployeeBankAccount, VerificationMethod
from app.models.employee import (
    Employee,
    EmployeeProfile,
    ProfileChangeRequest,
    ProfileChangeStatus,
)
from app.models.user import User
from app.schemas.employee_profile import (
    DIRECT_SECTIONS,
    SECTION_LABELS,
    SENSITIVE_SECTIONS,
    Addresses,
    BankAccountSummary,
    ChangeRequestRead,
    Education,
    EmergencyContact,
    EmployeeProfileRead,
    FamilyMember,
    Nominee,
    PersonalDetails,
    PreviousEmployment,
    ProfileSection,
    SectionWrite,
    StatutoryRead,
)
from app.services import audit
from app.services import employee_identifiers as ids
from app.services.disbursement import validation as bank_validation
from app.services.notifications.dispatch import notify, register_event
from app.services.storage import (
    FileRejectedError,
    StorageError,
    get_storage,
    sniff,
    validate_upload,
)

__all__ = [
    "HIRING_KEYS",
    "apply_section",
    "create_change_request",
    "decide_change_request",
    "inbox_items",
    "list_change_requests",
    "photo",
    "read_profile",
    "remove_photo",
    "set_photo",
    "withdraw_change_request",
    "write_section",
]

EVENT_REQUESTED = "people.profile_change.requested"
EVENT_DECIDED = "people.profile_change.decided"

register_event(
    EVENT_REQUESTED,
    "Profile change to review: {employee_name}",
    "{employee_name} has asked to change their {section}. Reason: {reason}",
)
register_event(
    EVENT_DECIDED,
    "Your {section} change was {decision}",
    "HR {decision} your request to change your {section}. {note}",
)

#: What offer conversion used to write into `custom_fields`. Never shown to the
#: person themselves or to a colleague — it lives in `hiring_snapshot` now, and
#: these keys are stripped from any self-service read of an older record.
HIRING_KEYS: frozenset[str] = frozenset(
    {
        "source",
        "offer_code",
        "candidate_id",
        "expected_ctc",
        "current_ctc_at_offer",
        "notice_period_days_at_offer",
        "fit_score",
    }
)

#: Profile photos: small, and images only. A PDF avatar is not an avatar.
PHOTO_MAX_BYTES = 5 * 1024 * 1024
PHOTO_TYPES = frozenset({"image/png", "image/jpeg"})

#: Adult age for a nominee; below it a guardian must be named (EPF para 61).
_ADULT_YEARS = 18

_LIST_SECTIONS: dict[ProfileSection, Any] = {
    ProfileSection.EMERGENCY_CONTACTS: TypeAdapter(list[EmergencyContact]),
    ProfileSection.FAMILY_MEMBERS: TypeAdapter(list[FamilyMember]),
    ProfileSection.NOMINEES: TypeAdapter(list[Nominee]),
    ProfileSection.EDUCATION: TypeAdapter(list[Education]),
    ProfileSection.PREVIOUS_EMPLOYMENT: TypeAdapter(list[PreviousEmployment]),
}
_STATUTORY_FIELDS = ("pan", "uan", "aadhaar_last4", "pf_number", "esi_number")


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


# ═══════════════════════════════════════════════════════════════════════════
# Loading
# ═══════════════════════════════════════════════════════════════════════════


async def _employee(session: AsyncSession, user: User, employee_id: uuid.UUID) -> Employee:
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    return employee


async def _profile(
    session: AsyncSession, user: User, employee_id: uuid.UUID, *, create: bool = False
) -> EmployeeProfile | None:
    profile = await session.scalar(
        tenant_scoped(
            select(EmployeeProfile).where(EmployeeProfile.employee_id == employee_id),
            EmployeeProfile,
            user,
        )
    )
    if profile is None and create:
        profile = EmployeeProfile(
            tenant_id=user.tenant_id,
            employee_id=employee_id,
            personal={},
            addresses={},
            emergency_contacts=[],
            family_members=[],
            nominees=[],
            education=[],
            previous_employment=[],
            hiring_snapshot={},
        )
        session.add(profile)
        await session.flush()
    return profile


async def _primary_account(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> EmployeeBankAccount | None:
    """The account salary goes to — the same row disbursement reads."""
    return await session.scalar(
        tenant_scoped(
            select(EmployeeBankAccount)
            .where(
                EmployeeBankAccount.employee_id == employee_id,
                EmployeeBankAccount.is_primary.is_(True),
                EmployeeBankAccount.active.is_(True),
            )
            .order_by(EmployeeBankAccount.created_at.desc())
            .limit(1),
            EmployeeBankAccount,
            user,
        )
    )


async def _names(session: AsyncSession, user: User, ids_: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids_:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(select(User.id, User.full_name).where(User.id.in_(ids_)), User, user)
        )
    ).all()
    return {row_id: name for row_id, name in rows}


async def _hr_logins(session: AsyncSession, user: User) -> list[uuid.UUID]:
    """Every active HR login in the tenant except the caller."""
    rows = (
        await session.scalars(
            tenant_scoped(
                select(User.id).where(
                    User.role.in_(permissions.HR_ROLES),
                    User.is_active.is_(True),
                    User.id != user.id,
                ),
                User,
                user,
            )
        )
    ).all()
    return list(rows)


async def _login_of(session: AsyncSession, user: User, employee_id: uuid.UUID) -> list[uuid.UUID]:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(User.id).where(User.employee_id == employee_id, User.is_active.is_(True)),
                User,
                user,
            )
        )
    ).all()
    return list(rows)


# ═══════════════════════════════════════════════════════════════════════════
# Current values, stored forms and masking
# ═══════════════════════════════════════════════════════════════════════════


def _last4(account: EmployeeBankAccount, tenant_id: uuid.UUID) -> str | None:
    number = decrypt_secret(account.account_number, tenant_id) or ""
    return number[-4:] if number else None


def _bank_form(account: EmployeeBankAccount | None, tenant_id: uuid.UUID) -> dict[str, Any]:
    if account is None:
        return {}
    return {
        "account_id": str(account.id),
        "account_number_last4": _last4(account, tenant_id),
        "ifsc": account.ifsc,
        "account_holder_name": account.account_holder_name,
        "bank_name": account.bank_name,
    }


async def _current_form(
    session: AsyncSession,
    user: User,
    employee: Employee,
    profile: EmployeeProfile | None,
    section: ProfileSection,
) -> Any:
    """What the record holds for a section now, in its stored form."""
    if section is ProfileSection.IDENTITY:
        return {
            "first_name": employee.first_name,
            "last_name": employee.last_name,
            "date_of_birth": employee.date_of_birth.isoformat() if employee.date_of_birth else None,
        }
    if section is ProfileSection.STATUTORY:
        current = {field: getattr(employee, field) for field in _STATUTORY_FIELDS}
        # Ciphertext, never the number: this form is kept on a change request.
        current["aadhaar_encrypted"] = (
            encrypt_secret(employee.aadhaar_number, user.tenant_id)
            if employee.aadhaar_number
            else None
        )
        return current
    if section is ProfileSection.BANK_ACCOUNT:
        return _bank_form(await _primary_account(session, user, employee.id), user.tenant_id)
    if profile is None:
        return [] if section in _LIST_SECTIONS else {}
    return getattr(profile, section.value)


def _stored_form(change: Any, tenant_id: uuid.UUID) -> Any:
    """A validated write, as it is kept on a request and applied to the record.

    The Aadhaar number and the account number become ciphertext here (plus
    their last four), so neither ever reaches a JSON column in the clear.
    """
    section = ProfileSection(change.section)
    if section in _LIST_SECTIONS:
        return [item.model_dump(mode="json") for item in change.data]
    if section is ProfileSection.STATUTORY:
        written = change.data.model_dump(mode="json", include=change.data.model_fields_set)
        if "aadhaar" in written:
            full = written.pop("aadhaar")
            written["aadhaar_last4"] = full[-4:] if full else None
            written["aadhaar_encrypted"] = encrypt_secret(full, tenant_id) if full else None
        return written
    if section is ProfileSection.BANK_ACCOUNT:
        data = change.data
        return {
            "account_number_encrypted": encrypt_secret(data.account_number, tenant_id),
            "account_number_last4": data.account_number[-4:],
            "ifsc": data.ifsc,
            "account_holder_name": data.account_holder_name,
            "bank_name": data.bank_name,
        }
    return change.data.model_dump(mode="json")


def _mask_statutory(values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key == "aadhaar_last4":
            out[key] = ids.mask_aadhaar(value)
        elif key in _STATUTORY_FIELDS:
            out[key] = ids.mask_tail(value)
        else:
            out[key] = value
    return out


def _public(
    section: ProfileSection, value: Any, *, hr: bool, tenant_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """A stored form as a reader may see it."""
    if section is ProfileSection.BANK_ACCOUNT:
        value = dict(value or {})
        encrypted = value.pop("account_number_encrypted", None)
        value.pop("account_id", None)
        value["account_number_masked"] = ids.mask_account(value.pop("account_number_last4", None))
        if hr and encrypted and tenant_id is not None:
            full = decrypt_secret(encrypted, tenant_id)
            if full:
                # One "Account number" row for HR: the full number replaces the masked one.
                value.pop("account_number_masked", None)
                value["account_number"] = full
        return value
    if section is ProfileSection.STATUTORY:
        value = dict(value or {})
        encrypted_aadhaar = value.pop("aadhaar_encrypted", None)
        full = (
            ids.format_aadhaar(decrypt_secret(encrypted_aadhaar, tenant_id))
            if hr and encrypted_aadhaar and tenant_id is not None
            else None
        )
        if full:
            # HR sees the whole number, under one "Aadhaar" row.
            value.pop("aadhaar_last4", None)
            value["aadhaar"] = full
        elif "aadhaar_last4" in value:
            value["aadhaar_last4"] = ids.mask_aadhaar(value["aadhaar_last4"])
        return value if hr else _mask_statutory(value)
    if isinstance(value, list):
        return {"entries": value}
    return dict(value or {})


def _statutory_read(employee: Employee, *, hr: bool) -> StatutoryRead:
    if hr:
        return StatutoryRead(
            pan=employee.pan,
            uan=employee.uan,
            pf_number=employee.pf_number,
            esi_number=employee.esi_number,
            # Records saved before the full number was kept have only the last four.
            aadhaar=ids.format_aadhaar(employee.aadhaar_number)
            or ids.mask_aadhaar(employee.aadhaar_last4),
            aadhaar_full=bool(employee.aadhaar_number),
            masked=False,
        )
    return StatutoryRead(
        pan=ids.mask_tail(employee.pan),
        uan=ids.mask_tail(employee.uan),
        pf_number=ids.mask_tail(employee.pf_number),
        esi_number=ids.mask_tail(employee.esi_number),
        aadhaar=ids.mask_aadhaar(employee.aadhaar_last4),
        aadhaar_full=False,
        masked=True,
    )


def _bank_summary(
    account: EmployeeBankAccount | None, tenant_id: uuid.UUID, *, hr: bool
) -> BankAccountSummary | None:
    if account is None:
        return None
    return BankAccountSummary(
        id=account.id,
        bank_name=account.bank_name,
        ifsc=account.ifsc,
        account_holder_name=account.account_holder_name,
        account_number_masked=ids.mask_account(_last4(account, tenant_id)),
        account_number=(decrypt_secret(account.account_number, tenant_id) or None) if hr else None,
        verification_method=account.verification_method.value,
        verified_at=account.verified_at,
        created_at=account.created_at,
    )


def _missing(
    employee: Employee, profile: EmployeeProfile | None, account: EmployeeBankAccount | None
) -> list[str]:
    """Gaps that stop something downstream. Only what the record itself shows."""
    gaps: list[str] = []
    if not (employee.pan or "").strip():
        gaps.append("PAN: income tax cannot be computed and Form 16 cannot be issued without it.")
    if account is None:
        gaps.append("Salary bank account: pay cannot be credited without one.")
    if profile is None or not profile.emergency_contacts:
        gaps.append("Emergency contact: nobody is on record to call.")
    if profile is None or not (profile.addresses or {}).get("current"):
        gaps.append("Current address: needed for Form 16 and PF KYC.")
    return gaps


# ═══════════════════════════════════════════════════════════════════════════
# Applying a change
# ═══════════════════════════════════════════════════════════════════════════


async def _check_nominees(session: AsyncSession, user: User, nominees: list[Any]) -> None:
    """A minor nominee needs a guardian, judged on the workspace's today."""
    from app.services.attendance.service import local_today

    today = await local_today(session, user)
    for nominee in nominees:
        born = nominee.date_of_birth
        if born is None:
            continue
        if born > today:
            raise _unprocessable(f"{nominee.name}'s date of birth is in the future.")
        adult_on = _add_years(born, _ADULT_YEARS)
        if adult_on > today and not nominee.guardian_name:
            raise _unprocessable(
                f"{nominee.name} is under {_ADULT_YEARS}, so a guardian must be named for "
                "the nomination to be valid."
            )


def _add_years(on: date, years: int) -> date:
    try:
        return on.replace(year=on.year + years)
    except ValueError:  # 29 February
        return on.replace(year=on.year + years, day=28)


async def _validate_extra(session: AsyncSession, user: User, change: Any) -> None:
    section = ProfileSection(change.section)
    if section is ProfileSection.NOMINEES:
        await _check_nominees(session, user, change.data)
    if section is ProfileSection.IDENTITY and change.data.date_of_birth:
        from app.services.attendance.service import local_today

        if change.data.date_of_birth >= await local_today(session, user):
            raise _unprocessable("A date of birth has to be in the past.")


async def apply_section(
    session: AsyncSession,
    user: User,
    employee: Employee,
    change: SectionWrite,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """Write one validated section onto a record HR is setting up, without
    committing or writing the audit row — the caller does both, once, for
    everything it changed (the employee import). Returns what to audit."""
    permissions.require_hr(user, detail="Only HR can change somebody else's profile.")
    await _validate_extra(session, user, change)
    stored = _stored_form(change, user.tenant_id)
    return await _apply(
        session, user, employee, ProfileSection(change.section), stored, request=request
    )


async def _apply(
    session: AsyncSession,
    user: User,
    employee: Employee,
    section: ProfileSection,
    stored: Any,
    *,
    request: Request | None,
) -> dict[str, Any]:
    """Write a stored form onto the record. Returns what to put in the audit row.

    No values go into the audit row for identifiers or the account: the audit
    log is readable by more people than the record, and it is append-only, so a
    PAN written there could never be removed.
    """
    if section is ProfileSection.IDENTITY:
        changed = []
        for key in ("first_name", "last_name", "date_of_birth"):
            value = stored.get(key)
            if key == "date_of_birth" and value:
                value = date.fromisoformat(value)
            if getattr(employee, key) != value:
                setattr(employee, key, value)
                changed.append(key)
        return {"section": section.value, "fields": changed}

    if section is ProfileSection.STATUTORY:
        changed = []
        for key, value in stored.items():
            if key in _STATUTORY_FIELDS and getattr(employee, key) != value:
                setattr(employee, key, value)
                changed.append(key)
        if "aadhaar_encrypted" in stored:
            encrypted = stored["aadhaar_encrypted"]
            full = decrypt_secret(encrypted, user.tenant_id) if encrypted else None
            if employee.aadhaar_number != full:
                employee.aadhaar_number = full or None
                if "aadhaar_last4" not in changed:
                    changed.append("aadhaar_number")
        elif "aadhaar_last4" in stored:
            # A request raised before full numbers were kept: the old full
            # number no longer matches the new last four, so it goes.
            if employee.aadhaar_number and employee.aadhaar_number[-4:] != stored["aadhaar_last4"]:
                employee.aadhaar_number = None
        return {"section": section.value, "fields": changed}

    if section is ProfileSection.BANK_ACCOUNT:
        return await _apply_bank(session, user, employee, stored, request=request)

    profile = await _profile(session, user, employee.id, create=True)
    assert profile is not None
    setattr(profile, section.value, stored)
    if isinstance(stored, list):
        return {"section": section.value, "entries": len(stored)}
    return {"section": section.value, "fields": sorted(k for k, v in stored.items() if v)}


async def _apply_bank(
    session: AsyncSession,
    user: User,
    employee: Employee,
    stored: dict[str, Any],
    *,
    request: Request | None,
) -> dict[str, Any]:
    """A new primary salary account, superseding the old one.

    The same rule as `/payroll/disbursements`: the old row is kept and made
    inactive rather than edited, so a batch already sent still says which
    account it paid.
    """
    existing = (
        await session.scalars(
            tenant_scoped(
                select(EmployeeBankAccount).where(
                    EmployeeBankAccount.employee_id == employee.id,
                    EmployeeBankAccount.is_primary.is_(True),
                ),
                EmployeeBankAccount,
                user,
            )
        )
    ).all()
    for row in existing:
        row.is_primary = False
        row.active = False

    account = EmployeeBankAccount(
        tenant_id=user.tenant_id,
        employee_id=employee.id,
        account_number=stored["account_number_encrypted"],
        ifsc=stored["ifsc"],
        account_holder_name=stored["account_holder_name"],
        bank_name=stored.get("bank_name"),
        is_primary=True,
        verification_method=VerificationMethod.NONE,
    )
    session.add(account)
    await session.flush()
    score = bank_validation.name_similarity(employee.full_name, stored["account_holder_name"])
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="employee_bank_account",
        entity_id=account.id,
        summary=(
            f"Recorded a bank account for {employee.employee_code} from the profile "
            f"(name similarity {score})"
        ),
        changes={
            "ifsc": account.ifsc,
            "name_similarity": str(score),
            "superseded": len(existing),
        },
        request=request,
    )
    return {"section": ProfileSection.BANK_ACCOUNT.value, "fields": ["account"]}


# ═══════════════════════════════════════════════════════════════════════════
# Reads
# ═══════════════════════════════════════════════════════════════════════════


def _parse(adapter_or_model: Any, value: Any, empty: Any) -> Any:
    """Stored JSON back into its model. A value written before a field was
    tightened is shown as empty rather than failing the whole profile."""
    try:
        if isinstance(adapter_or_model, TypeAdapter):
            return adapter_or_model.validate_python(value or [])
        return adapter_or_model.model_validate(value or {})
    except ValueError:
        return empty


async def read_profile(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> EmployeeProfileRead:
    """The whole extended profile: the person themselves and HR only."""
    permissions.require_self_or_hr(
        user, employee_id, detail="Only the person and HR can see this profile."
    )
    employee = await _employee(session, user, employee_id)
    profile = await _profile(session, user, employee_id)
    account = await _primary_account(session, user, employee_id)
    is_self = employee_id == user.employee_id
    hr = permissions.is_hr(user)

    pending = await list_change_requests(
        session, user, employee_id=employee_id, status_filter=ProfileChangeStatus.PENDING
    )

    if is_self and not hr:
        direct = sorted(DIRECT_SECTIONS, key=lambda s: s.value)
        requestable = sorted(SENSITIVE_SECTIONS, key=lambda s: s.value)
    else:
        direct = sorted(ProfileSection, key=lambda s: s.value)
        requestable = []

    stored = profile or EmployeeProfile()
    return EmployeeProfileRead(
        employee_id=employee_id,
        personal=_parse(PersonalDetails, stored.personal, PersonalDetails()),
        addresses=_parse(Addresses, stored.addresses, Addresses()),
        emergency_contacts=_parse(
            _LIST_SECTIONS[ProfileSection.EMERGENCY_CONTACTS], stored.emergency_contacts, []
        ),
        family_members=_parse(
            _LIST_SECTIONS[ProfileSection.FAMILY_MEMBERS], stored.family_members, []
        ),
        nominees=_parse(_LIST_SECTIONS[ProfileSection.NOMINEES], stored.nominees, []),
        education=_parse(_LIST_SECTIONS[ProfileSection.EDUCATION], stored.education, []),
        previous_employment=_parse(
            _LIST_SECTIONS[ProfileSection.PREVIOUS_EMPLOYMENT], stored.previous_employment, []
        ),
        # Admin/HR see identifiers in full on every profile, their own included.
        statutory=_statutory_read(employee, hr=hr),
        bank_account=_bank_summary(account, user.tenant_id, hr=hr),
        pending_requests=pending,
        hiring_snapshot=(dict(stored.hiring_snapshot or {}) if hr and not is_self else None),
        direct_sections=direct,
        request_sections=requestable,
        missing=_missing(employee, profile, account),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Direct writes
# ═══════════════════════════════════════════════════════════════════════════


async def write_section(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    change: SectionWrite,
    *,
    request: Request | None = None,
) -> EmployeeProfileRead:
    """Save one section straight away.

    Yourself: the non-sensitive sections only. HR: any section of anybody's
    record, their own included.
    """
    section = ProfileSection(change.section)
    is_self = user.employee_id is not None and employee_id == user.employee_id
    if is_self:
        if section not in DIRECT_SECTIONS and not permissions.is_hr(user):
            raise permissions.forbidden(
                f"Changes to your {SECTION_LABELS[section]} go through a request HR "
                "approves, not a direct edit. Raise a change request instead."
            )
    else:
        permissions.require_hr(user, detail="Only HR can change somebody else's profile.")

    employee = await _employee(session, user, employee_id)
    await _validate_extra(session, user, change)
    stored = _stored_form(change, user.tenant_id)
    changes = await _apply(session, user, employee, section, stored, request=request)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="employee_profile",
        entity_id=employee.id,
        changes=changes,
        summary=(
            f"{employee.employee_code} updated their {SECTION_LABELS[section]}"
            if is_self
            else f"Updated {employee.employee_code}'s {SECTION_LABELS[section]}"
        ),
        request=request,
    )
    await session.commit()
    return await read_profile(session, user, employee_id)


# ═══════════════════════════════════════════════════════════════════════════
# Change requests
# ═══════════════════════════════════════════════════════════════════════════


async def _request(
    session: AsyncSession, user: User, request_id: uuid.UUID
) -> ProfileChangeRequest:
    row = await session.scalar(
        tenant_scoped(
            select(ProfileChangeRequest).where(ProfileChangeRequest.id == request_id),
            ProfileChangeRequest,
            user,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return row


async def _reads(
    session: AsyncSession, user: User, rows: list[ProfileChangeRequest]
) -> list[ChangeRequestRead]:
    if not rows:
        return []
    employees = {
        e.id: e
        for e in (
            await session.scalars(
                tenant_scoped(
                    select(Employee).where(Employee.id.in_({r.employee_id for r in rows})),
                    Employee,
                    user,
                )
            )
        ).all()
    }
    names = await _names(
        session,
        user,
        {uid for r in rows for uid in (r.requested_by_user_id, r.decided_by_user_id) if uid},
    )
    hr = permissions.is_hr(user)
    out = []
    for row in rows:
        section = ProfileSection(row.section)
        employee = employees.get(row.employee_id)
        own = user.employee_id is not None and row.employee_id == user.employee_id
        pending = row.status is ProfileChangeStatus.PENDING
        # HR sees identifiers in full, but not on their own request.
        full = hr and not own
        out.append(
            ChangeRequestRead(
                id=row.id,
                employee_id=row.employee_id,
                employee_code=employee.employee_code if employee else "",
                employee_name=employee.full_name if employee else "Former employee",
                section=section,
                section_label=SECTION_LABELS[section],
                status=row.status,
                proposed=_public(section, row.proposed, hr=full, tenant_id=user.tenant_id),
                current_snapshot=_public(
                    section, row.current_snapshot, hr=full, tenant_id=user.tenant_id
                ),
                reason=row.reason,
                requested_at=row.created_at,
                requested_by_name=names.get(row.requested_by_user_id)
                if row.requested_by_user_id
                else None,
                decided_at=row.decided_at,
                decided_by_name=names.get(row.decided_by_user_id)
                if row.decided_by_user_id
                else None,
                decision_note=row.decision_note,
                can_decide=pending and hr and not own,
                can_withdraw=pending and own,
            )
        )
    return out


async def list_change_requests(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID | None = None,
    status_filter: ProfileChangeStatus | None = None,
) -> list[ChangeRequestRead]:
    """HR: anybody's, or one person's. Everyone else: their own."""
    stmt = select(ProfileChangeRequest).order_by(ProfileChangeRequest.created_at.desc())
    if permissions.is_hr(user):
        if employee_id is not None:
            stmt = stmt.where(ProfileChangeRequest.employee_id == employee_id)
    else:
        own = permissions.resolve_employee(
            user, employee_id, detail="You can only see your own change requests."
        )
        stmt = stmt.where(ProfileChangeRequest.employee_id == own)
    if status_filter is not None:
        stmt = stmt.where(ProfileChangeRequest.status == status_filter)
    rows = (await session.scalars(tenant_scoped(stmt.limit(500), ProfileChangeRequest, user))).all()
    return await _reads(session, user, list(rows))


async def create_change_request(
    session: AsyncSession,
    user: User,
    change: SectionWrite,
    reason: str,
    *,
    request: Request | None = None,
) -> ChangeRequestRead:
    """Ask HR to change a sensitive part of your own record."""
    employee_id = permissions.own_employee_id(user)
    section = ProfileSection(change.section)
    if section in DIRECT_SECTIONS:
        raise _unprocessable(
            f"Your {SECTION_LABELS[section]} is yours to edit directly; no request is needed."
        )
    employee = await _employee(session, user, employee_id)

    open_one = await session.scalar(
        tenant_scoped(
            select(ProfileChangeRequest.id).where(
                ProfileChangeRequest.employee_id == employee_id,
                ProfileChangeRequest.section == section.value,
                ProfileChangeRequest.status == ProfileChangeStatus.PENDING,
            ),
            ProfileChangeRequest,
            user,
        )
    )
    if open_one is not None:
        raise _conflict(
            f"You already have a change to your {SECTION_LABELS[section]} waiting for HR. "
            "Withdraw it first if you want to ask for something different."
        )

    await _validate_extra(session, user, change)
    profile = await _profile(session, user, employee_id)
    current = await _current_form(session, user, employee, profile, section)
    proposed = _stored_form(change, user.tenant_id)
    if _unchanged(section, proposed, current, user.tenant_id):
        raise _unprocessable("That is what the record already says, so there is nothing to change.")

    row = ProfileChangeRequest(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        section=section.value,
        status=ProfileChangeStatus.PENDING,
        proposed=proposed if isinstance(proposed, dict) else {"entries": proposed},
        current_snapshot=current if isinstance(current, dict) else {"entries": current},
        reason=reason.strip(),
        requested_by_user_id=user.id,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="profile_change_request",
        entity_id=row.id,
        changes={"section": section.value},
        summary=f"{employee.employee_code} asked HR to change their {SECTION_LABELS[section]}",
        request=request,
    )
    recipients = await _hr_logins(session, user)
    if recipients:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key=EVENT_REQUESTED,
            recipient_user_ids=recipients,
            payload={
                "employee_name": employee.full_name,
                "section": SECTION_LABELS[section],
                "reason": row.reason,
            },
            commit=False,
        )
    await session.commit()
    return (await _reads(session, user, [row]))[0]


def _same_statutory(key: str, a: Any, b: Any, tenant_id: uuid.UUID) -> bool:
    """Compare one statutory value. Ciphertext differs on every encryption, so
    an encrypted Aadhaar is compared by what it decrypts to."""
    if key == "aadhaar_encrypted":
        return (decrypt_secret(a, tenant_id) if a else None) == (
            decrypt_secret(b, tenant_id) if b else None
        )
    return a == b


def _unchanged(
    section: ProfileSection, proposed: Any, current: Any, tenant_id: uuid.UUID
) -> bool:
    if section is ProfileSection.BANK_ACCOUNT:
        return (
            bool(current)
            and all(
                current.get(key) == proposed.get(key)
                for key in ("account_number_last4", "ifsc", "account_holder_name")
            )
            and current.get("bank_name") == proposed.get("bank_name")
        )
    if section is ProfileSection.STATUTORY:
        return all(
            _same_statutory(key, current.get(key), value, tenant_id)
            for key, value in proposed.items()
        )
    return proposed == current


def _stale(
    section: ProfileSection, request: ProfileChangeRequest, now: Any, tenant_id: uuid.UUID
) -> bool:
    """Whether the record moved since the request was raised."""
    then = request.current_snapshot
    if section is ProfileSection.STATUTORY:
        # Only the identifiers this request touches.
        return any(
            not _same_statutory(key, then.get(key), now.get(key), tenant_id)
            for key in request.proposed
        )
    if section is ProfileSection.BANK_ACCOUNT:
        return then.get("account_id") != now.get("account_id")
    if section in _LIST_SECTIONS:
        return then.get("entries", []) != now
    return then != now


async def decide_change_request(
    session: AsyncSession,
    user: User,
    request_id: uuid.UUID,
    *,
    approve: bool,
    note: str | None,
    request: Request | None = None,
) -> ChangeRequestRead:
    """HR approves (and the change is applied, here, now) or rejects with a reason."""
    permissions.require_hr(user, detail="Only HR decides profile change requests.")
    row = await _request(session, user, request_id)
    if user.employee_id is not None and row.employee_id == user.employee_id:
        raise permissions.forbidden(
            "This is a change to your own record. Another HR person has to decide it."
        )
    if row.status is not ProfileChangeStatus.PENDING:
        raise _conflict(f"This request is already {row.status.value}.")
    if not approve and not (note or "").strip():
        raise _unprocessable("Say why it is rejected, so the employee knows what to fix.")

    section = ProfileSection(row.section)
    employee = await _employee(session, user, row.employee_id)
    applied: dict[str, Any] = {}
    if approve:
        profile = await _profile(session, user, employee.id)
        now = await _current_form(session, user, employee, profile, section)
        if _stale(section, row, now, user.tenant_id):
            raise _conflict(
                f"{employee.full_name}'s {SECTION_LABELS[section]} changed after this request "
                "was raised, so approving it would overwrite that change. Reject it with a "
                "note and ask for a fresh request."
            )
        proposed = row.proposed.get("entries", []) if section in _LIST_SECTIONS else row.proposed
        applied = await _apply(session, user, employee, section, proposed, request=request)

    row.status = ProfileChangeStatus.APPROVED if approve else ProfileChangeStatus.REJECTED
    row.decided_by_user_id = user.id
    row.decided_at = datetime.now(UTC)
    row.decision_note = (note or "").strip() or None
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="profile_change_request",
        entity_id=row.id,
        changes={"status": row.status.value, **({"applied": applied} if approve else {})},
        summary=(
            f"{'Approved and applied' if approve else 'Rejected'} "
            f"{employee.employee_code}'s change to their {SECTION_LABELS[section]}"
        ),
        request=request,
    )
    recipients = await _login_of(session, user, employee.id)
    if recipients:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key=EVENT_DECIDED,
            recipient_user_ids=recipients,
            payload={
                "section": SECTION_LABELS[section],
                "decision": "approved" if approve else "rejected",
                "note": row.decision_note or "",
            },
            commit=False,
        )
    await session.commit()
    return (await _reads(session, user, [row]))[0]


async def withdraw_change_request(
    session: AsyncSession, user: User, request_id: uuid.UUID, *, request: Request | None = None
) -> ChangeRequestRead:
    """Your own pending request, taken back. Anybody else's is not found."""
    own = permissions.own_employee_id(user)
    row = await _request(session, user, request_id)
    if row.employee_id != own:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    if row.status is not ProfileChangeStatus.PENDING:
        raise _conflict(f"This request is already {row.status.value}.")
    row.status = ProfileChangeStatus.WITHDRAWN
    row.decided_at = datetime.now(UTC)
    row.decided_by_user_id = user.id
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="profile_change_request",
        entity_id=row.id,
        changes={"status": row.status.value},
        summary=f"Withdrew a change to their {SECTION_LABELS[ProfileSection(row.section)]}",
        request=request,
    )
    await session.commit()
    return (await _reads(session, user, [row]))[0]


async def inbox_items(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Pending requests for the approvals inbox: HR, never their own."""
    if not permissions.is_hr(user):
        return []
    stmt = tenant_scoped(
        select(ProfileChangeRequest, Employee)
        .join(Employee, Employee.id == ProfileChangeRequest.employee_id)
        .where(ProfileChangeRequest.status == ProfileChangeStatus.PENDING)
        .order_by(ProfileChangeRequest.created_at),
        ProfileChangeRequest,
        user,
    )
    if user.employee_id is not None:
        stmt = stmt.where(ProfileChangeRequest.employee_id != user.employee_id)
    rows = (await session.execute(stmt)).all()
    return [
        {
            "id": row.id,
            "title": f"Change to {SECTION_LABELS[ProfileSection(row.section)]}",
            "subject_name": employee.full_name,
            "detail": row.reason[:120],
            "submitted_at": row.created_at,
        }
        for row, employee in rows
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Profile photo
# ═══════════════════════════════════════════════════════════════════════════


def _photo_url(employee_id: uuid.UUID) -> str:
    # A fresh query string on every change, so a browser does not keep showing
    # the old face from its cache.
    return f"/api/v1/employees/{employee_id}/photo?v={secrets.token_hex(4)}"


async def set_photo(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    data: bytes,
    *,
    request: Request | None = None,
) -> Employee:
    """Store a PNG or JPEG in managed storage and point the record at it.

    Yourself, or HR for somebody else. The type is read from the bytes; the
    stored key is generated; the URL on the record is our own route, never
    something a person typed.
    """
    is_self = user.employee_id is not None and employee_id == user.employee_id
    if not is_self:
        permissions.require_hr(user, detail="You can only change your own photo.")
    employee = await _employee(session, user, employee_id)
    if len(data) > PHOTO_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="A profile photo can be at most 5 MB.",
        )
    try:
        kind = validate_upload(data, max_bytes=PHOTO_MAX_BYTES)
    except FileRejectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    if kind.mime_type not in PHOTO_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="A profile photo has to be a PNG or JPEG image.",
        )

    storage = get_storage()
    key = storage.new_key(user.tenant_id, "photos", kind.extension)
    try:
        await storage.put(key, data)
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    try:
        profile = await _profile(session, user, employee_id, create=True)
        assert profile is not None
        previous = profile.photo_storage_key
        profile.photo_storage_key = key
        employee.photo_url = _photo_url(employee.id)
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            actor=user,
            action=AuditAction.UPDATE,
            entity_type="employee",
            entity_id=employee.id,
            changes={"photo": "replaced" if previous else "added", "size_bytes": len(data)},
            summary=f"Updated the profile photo of {employee.employee_code}",
            request=request,
        )
        await session.commit()
    except BaseException:
        with contextlib.suppress(StorageError):
            await storage.delete(key)
        raise

    if previous:
        with contextlib.suppress(StorageError):
            await storage.delete(previous)
    await session.refresh(employee)
    return employee


async def remove_photo(
    session: AsyncSession, user: User, employee_id: uuid.UUID, *, request: Request | None = None
) -> Employee:
    is_self = user.employee_id is not None and employee_id == user.employee_id
    if not is_self:
        permissions.require_hr(user, detail="You can only remove your own photo.")
    employee = await _employee(session, user, employee_id)
    profile = await _profile(session, user, employee_id)
    previous = profile.photo_storage_key if profile else None
    if profile is not None:
        profile.photo_storage_key = None
    employee.photo_url = None
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="employee",
        entity_id=employee.id,
        changes={"photo": "removed"},
        summary=f"Removed the profile photo of {employee.employee_code}",
        request=request,
    )
    await session.commit()
    if previous:
        with contextlib.suppress(StorageError):
            await get_storage().delete(previous)
    await session.refresh(employee)
    return employee


async def photo(session: AsyncSession, user: User, employee_id: uuid.UUID) -> tuple[str, bytes]:
    """A colleague's photo, as the directory shows it beside their name.

    Anybody in the workspace may see it, as they may see the name; tenant
    isolation is the whole rule, and another tenant's id is a 404.
    """
    key = await session.scalar(
        tenant_scoped(
            select(EmployeeProfile.photo_storage_key).where(
                EmployeeProfile.employee_id == employee_id
            ),
            EmployeeProfile,
            user,
        )
    )
    missing = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No photo")
    if not key:
        raise missing
    try:
        data = await get_storage().get(key)
    except StorageError as exc:
        raise missing from exc
    if data is None:
        raise missing
    kind = sniff(data[:16])
    return (kind.mime_type if kind else "application/octet-stream"), data
