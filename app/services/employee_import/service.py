"""Importing employees from a spreadsheet: check first, then import.

`preview` reads the file and says, row by row, what would happen — nothing is
written. `run` reads it again the same way and does it, in one transaction:
the departments and locations the file names (an admin's import), every ready
row, then reporting lines, then each person's profile — personal details,
addresses, family, statutory identifiers and salary account.

What it will not do:

- Overwrite. An employee code already in AskMira is skipped, so a corrected
  file can be imported again without touching anybody already brought in.
  One exception fills a gap and changes nothing: somebody imported when only
  the last four Aadhaar digits were kept gets the full number from the file,
  when its last four match the ones on record (`_aadhaar_fills`).
- Add somebody twice. A work email that already belongs to somebody here
  under another code — the admin made when the workspace was, say — or that
  two rows share stops the row, naming who has it. `rejected` hands back
  every row left out, with its own cells and why.
- Invent. A value that fails its check is left out and listed against its
  row; only a missing employee code, first name, work email or joining date
  (or a status that cannot be squared with the dates) stops a row.
- Keep an Aadhaar number in the clear. It is stored encrypted, as everywhere else.
- Give anybody a login. That stays an admin's decision, person by person.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import CustomFieldDefinition, Employee, EmploymentStatus
from app.models.org import Department, LegalEntity, Location
from app.models.pay_grades import PayGrade
from app.models.user import User
from app.schemas.employee import EmployeeCreate
from app.schemas.employee_import import (
    ImportColumn,
    ImportNewLocation,
    ImportPreview,
    ImportResult,
    ImportRow,
)
from app.schemas.employee_profile import (
    Address,
    Addresses,
    AddressesWrite,
    BankAccountDetails,
    BankAccountWrite,
    EmergencyContact,
    EmergencyContactsWrite,
    FamilyMember,
    FamilyMembersWrite,
    PersonalDetails,
    PersonalWrite,
    StatutoryDetails,
    StatutoryWrite,
)
from app.services import audit, custom_fields
from app.services import employee_profile as profile_service
from app.services.employee_import import fields as f
from app.services.employee_import.sheet import (
    SheetError,
    Table,
    claim_custom,
    read_table,
    write_left_out,
)
from app.services.employee_import.template import build_template
from app.services.onboarding import rules as onboarding_rules

__all__ = ["preview", "rejected", "run", "template"]

_EMAIL = TypeAdapter(EmailStr)
_CODE = re.compile(r"^[A-Za-z0-9_\-/]{1,32}$")
_PINCODE = re.compile(r"^[1-9][0-9]{5}$")


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _key(value: str) -> str:
    """Names compared as HR would: case, spacing and punctuation ignored."""
    lowered = re.sub(r"[^a-z0-9& ]+", " ", value.lower())
    lowered = re.sub(r"\bprivate\b", "pvt", lowered)
    lowered = re.sub(r"\blimited\b", "ltd", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    message = str(error.get("msg", "is not valid"))
    return message.removeprefix("Value error, ")


# ═══════════════════════════════════════════════════════════════════════════
# The workspace as the import sees it
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class _Workspace:
    departments: dict[str, tuple[uuid.UUID, str]]
    locations: dict[str, tuple[uuid.UUID, str]]
    entities: dict[str, uuid.UUID]
    only_entity: uuid.UUID | None
    #: Upper-cased employee code -> id, for skipping and for managers.
    codes: dict[str, uuid.UUID]
    #: Lower-cased work email -> (code, name) of who has it here.
    emails: dict[str, tuple[str, str]]
    department_codes: set[str]
    location_codes: set[str]
    #: Active grade codes; None while the workspace keeps no grade list.
    grades: set[str] | None
    custom: dict[str, CustomFieldDefinition]


async def _workspace(session: AsyncSession, user: User) -> _Workspace:
    def scoped(model, *columns):
        return tenant_scoped(select(*columns), model, user)

    departments: dict[str, tuple[uuid.UUID, str]] = {}
    department_codes: set[str] = set()
    for row in (await session.execute(scoped(Department, Department.id, Department.name,
                                             Department.code))).all():
        departments.setdefault(_key(row.name), (row.id, row.name))
        departments.setdefault(_key(row.code), (row.id, row.name))
        department_codes.add(row.code.upper())

    locations: dict[str, tuple[uuid.UUID, str]] = {}
    location_codes: set[str] = set()
    for row in (await session.execute(scoped(Location, Location.id, Location.name, Location.code,
                                             Location.city))).all():
        for name in (row.name, row.code, row.city):
            if name:
                locations.setdefault(_key(name), (row.id, row.name))
        location_codes.add(row.code.upper())

    entity_rows = (await session.execute(scoped(LegalEntity, LegalEntity.id, LegalEntity.name,
                                                LegalEntity.code))).all()
    entities: dict[str, uuid.UUID] = {}
    for row in entity_rows:
        entities.setdefault(_key(row.name), row.id)
        entities.setdefault(_key(row.code), row.id)

    codes: dict[str, uuid.UUID] = {}
    emails: dict[str, tuple[str, str]] = {}
    for row in (await session.execute(scoped(
        Employee, Employee.id, Employee.employee_code, Employee.work_email, Employee.display_name,
        Employee.first_name, Employee.last_name,
    ))).all():
        codes[row.employee_code.upper()] = row.id
        name = row.display_name or " ".join(filter(None, [row.first_name, row.last_name]))
        emails.setdefault(row.work_email.lower(), (row.employee_code, name))
    grade_rows = (await session.execute(scoped(PayGrade, PayGrade.code, PayGrade.active))).all()
    definitions = await custom_fields.employee_definitions(session, user)
    return _Workspace(
        departments=departments,
        locations=locations,
        entities=entities,
        only_entity=entity_rows[0].id if len(entity_rows) == 1 else None,
        codes=codes,
        emails=emails,
        department_codes=department_codes,
        location_codes=location_codes,
        grades={row.code for row in grade_rows if row.active} if grade_rows else None,
        custom={key: d for key, d in definitions.items() if d.is_active},
    )


# ═══════════════════════════════════════════════════════════════════════════
# One row
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class _Row:
    line: int
    code: str | None = None
    name: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skip: bool = False
    #: Employee columns, references still as the names the file gives.
    record: dict[str, Any] = field(default_factory=dict)
    department: str | None = None
    location: str | None = None
    entity: str | None = None
    manager_code: str | None = None
    sections: list[Any] = field(default_factory=list)
    employee_id: uuid.UUID | None = None

    @property
    def outcome(self) -> str:
        return "error" if self.errors else "skip" if self.skip else "ready"

    @property
    def leaver(self) -> bool:
        return self.record.get("status") is EmploymentStatus.EXITED


class _Reader:
    """Reads one row's cells, noting a problem against the row rather than failing."""

    def __init__(self, table: Table, cells: list[Any], row: _Row) -> None:
        self.table, self.cells, self.row = table, cells, row

    def raw(self, key: str) -> Any:
        index = self.table.mapping.get(key)
        return self.cells[index] if index is not None and index < len(self.cells) else None

    def get(self, key: str, reader=f.text, *, required: bool = False) -> Any:
        label = f.FIELD_BY_KEY[key].label
        try:
            value = reader(self.raw(key))
        except ValueError as exc:
            if required:
                self.row.errors.append(f"{label}: {exc}.")
            else:
                self.row.warnings.append(f"{label}: {exc} — left out.")
            return None
        if value is None and required:
            self.row.errors.append(f"No {label}.")
        return value

    def limited(self, key: str, length: int) -> str | None:
        value = self.get(key)
        if value is not None and len(value) > length:
            self.row.warnings.append(
                f"{f.FIELD_BY_KEY[key].label} is longer than {length} characters — left out."
            )
            return None
        return value


def _status(read: _Reader, joining: date | None, today: date) -> tuple[EmploymentStatus | None,
                                                                        date | None]:
    """Where they stand, and their exit date when they have one."""
    row = read.row
    try:
        stated = f.read_status(read.raw("status"))
    except ValueError as exc:
        # An unreadable status decides too much to guess at.
        row.errors.append(f"Status: {exc}.")
        return None, None
    exit_on = read.get("last_working_date", f.read_date) or read.get("leaving_date", f.read_date)
    if joining and exit_on and exit_on < joining:
        row.errors.append("The leaving date is before the joining date.")
        return None, None
    if stated == "left" or (stated is None and exit_on is not None and exit_on <= today):
        if exit_on is None:
            row.errors.append("Status says they have left, but there is no Leaving Date.")
            return None, None
        return EmploymentStatus.EXITED, exit_on
    if stated == "on_leave":
        return EmploymentStatus.ON_LEAVE, None
    if stated == "on_notice" or (exit_on is not None and exit_on > today):
        return EmploymentStatus.ON_NOTICE, exit_on
    if exit_on is not None:
        row.errors.append("Status is Active, but the leaving date has passed.")
        return None, None
    return onboarding_rules.joining_status(joining, today), None


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _within(inner: list[str], outer: list[str]) -> bool:
    """Whether `inner`'s words appear, in order and together, inside `outer`."""
    n = len(inner)
    return n > 0 and any(outer[i : i + n] == inner for i in range(len(outer) - n + 1))


def _distinct(parts: list[str | None]) -> list[str]:
    """The address cells that add something.

    factoHR's export often repeats the whole address in "Address 2" (or a piece
    of it in "Address 3"); kept as is, every profile shows the address twice.
    """
    kept: list[str] = []
    for part in parts:
        if not part:
            continue
        words = _words(part)
        if not words or any(_within(words, _words(k)) for k in kept):
            continue
        kept = [k for k in kept if not _within(_words(k), words)] + [part]
    return kept


def _lines(parts: list[str | None]) -> tuple[str | None, str | None]:
    """Up to three address cells as the two lines AskMira keeps, 255 characters each."""
    present = _distinct(parts)
    if not present:
        return None, None
    first, rest = present[0], ", ".join(present[1:]) or None
    if len(first) > 255:
        cut = first.rfind(",", 0, 255)
        cut = cut if cut > 0 else 255
        rest = ", ".join(p for p in (first[cut:].strip(" ,"), rest) if p)
        first = first[:cut].strip(" ,")
    return first, (rest[:255] if rest else None)


def _address(read: _Reader, prefix: str, label: str) -> Address | None:
    parts = [read.get(f"{prefix}_line1"), read.get(f"{prefix}_line2"), read.get(f"{prefix}_line3")]
    line1, line2 = _lines(parts)
    if line1 is None:
        return None
    whole = ", ".join(p for p in parts if p)
    pincode = f.digits(read.raw(f"{prefix}_pincode")) or f.pincode_in(whole)
    if pincode and not _PINCODE.match(pincode):
        pincode = None
    state = (
        f.state_code(read.raw(f"{prefix}_state")) or f.state_in(whole) or f.state_of_pin(pincode)
    )
    city = read.get(f"{prefix}_city") or f.city_in(whole, state)
    missing = [
        name
        for name, value in (("city", city), ("state", state), ("PIN code", pincode))
        if not value
    ]
    if missing:
        read.row.warnings.append(
            f"{label} left out: no {' or '.join(missing)} could be found in it. Fill in its "
            "City, State and Pincode columns to bring it in."
        )
        return None
    try:
        return Address(line1=line1, line2=line2, city=city, state_code=state, pincode=pincode)
    except ValidationError as exc:
        read.row.warnings.append(f"{label} left out: {_first_error(exc)}.")
        return None


def _sections(read: _Reader, name: str) -> list[Any]:
    """The profile sections a row fills, each already checked."""
    row = read.row
    sections: list[Any] = []

    personal = {
        "father_name": read.limited("father_name", 255),
        "spouse_name": read.limited("spouse_name", 255),
        "marital_status": read.get("marital_status", f.read_marital),
        "blood_group": read.get("blood_group", f.read_blood_group),
        "nationality": read.limited("nationality", 64),
        "alternate_phone": read.get("alternate_phone", f.read_phone),
    }
    personal = {key: value for key, value in personal.items() if value is not None}
    if personal:
        try:
            sections.append(PersonalWrite(section="personal", data=PersonalDetails(**personal)))
        except ValidationError as exc:
            row.warnings.append(f"Personal details left out: {_first_error(exc)}.")

    current = _address(read, "current", "Local address")
    same = read.get("permanent_same", f.read_yes)
    permanent = None if same and current else _address(read, "permanent", "Permanent address")
    if current or permanent:
        sections.append(
            AddressesWrite(
                section="addresses",
                data=Addresses(
                    current=current,
                    permanent=permanent,
                    permanent_same_as_current=bool(same and current),
                ),
            )
        )

    mother = read.limited("mother_name", 255)
    if mother:
        sections.append(
            FamilyMembersWrite(
                section="family_members",
                data=[FamilyMember(name=mother, relationship="mother")],
            )
        )

    contact = read.limited("emergency_name", 255)
    contact_phone = read.get("emergency_phone", f.read_phone)
    if contact and contact_phone:
        sections.append(
            EmergencyContactsWrite(
                section="emergency_contacts",
                data=[
                    EmergencyContact(
                        name=contact,
                        relationship=read.get("emergency_relationship", f.read_relationship)
                        or "other",
                        phone=contact_phone,
                        is_primary=True,
                    )
                ],
            )
        )
    elif contact:
        row.warnings.append("Emergency contact left out: it has no mobile number.")

    statutory: dict[str, str] = {}
    for key, label, reader in (
        ("pan", "Pan No", f.text),
        ("aadhaar", "Aadhaar No", f.digits),
        ("uan", "UA No", f.digits),
        ("pf_number", "PF No", f.text),
        ("esi_number", "ESIC No", f.digits),
    ):
        value = reader(read.raw(key))
        if key == "pf_number" and value is not None and value.strip("0") == "":
            value = None
        if value is None:
            continue
        try:
            StatutoryDetails(**{key: value})
        except ValidationError as exc:
            row.warnings.append(f"{label}: {_first_error(exc)} — left out.")
            continue
        statutory[key] = value
    if statutory:
        sections.append(StatutoryWrite(section="statutory", data=StatutoryDetails(**statutory)))

    account = f.digits(read.raw("account_number"))
    ifsc = f.text(read.raw("ifsc"))
    if account or ifsc:
        holder = read.limited("account_holder_name", 255)
        if not holder:
            holder = name
            row.warnings.append("Bank account holder name taken from the employee's name.")
        try:
            sections.append(
                BankAccountWrite(
                    section="bank_account",
                    data=BankAccountDetails(
                        account_number=account or "",
                        ifsc=(ifsc or "").upper(),
                        account_holder_name=holder,
                        bank_name=read.limited("bank_name", 128),
                    ),
                )
            )
        except ValidationError as exc:
            row.warnings.append(f"Bank account left out: {_first_error(exc)}.")
    return sections


def _custom(read: _Reader, columns: dict[str, int], ws: _Workspace) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, index in columns.items():
        definition = ws.custom[key]
        raw = read.cells[index] if index < len(read.cells) else None
        try:
            if definition.field_type == "date":
                parsed = f.read_date(raw)
                value: Any = parsed.isoformat() if parsed else None
            elif definition.field_type == "number":
                text = f.text(raw)
                value = None if text is None else float(text)
                if isinstance(value, float) and value.is_integer():
                    value = int(value)
            elif definition.field_type == "boolean":
                value = f.read_yes(raw)
            elif definition.field_type == "multiselect":
                text = f.text(raw)
                options = {o.lower(): o for o in definition.options or []}
                value = (
                    [options.get(p.strip().lower(), p.strip()) for p in re.split(r"[,;]", text)]
                    if text
                    else None
                )
            elif definition.field_type == "select":
                text = f.text(raw)
                options = {o.lower(): o for o in definition.options or []}
                value = options.get(text.lower(), text) if text else None
            else:
                value = f.text(raw)
            if value is None:
                continue
            values[key] = custom_fields.check_value(definition, value)
        except (ValueError, HTTPException) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            read.row.warnings.append(f"{definition.label}: {detail} — left out.")
    missing = [
        d.label for d in ws.custom.values() if d.is_required and values.get(d.key) in (None, "", [])
    ]
    if missing:
        read.row.errors.append(f"Required field(s) missing: {', '.join(missing)}.")
    return values


def _read_row(
    table: Table, line: int, cells: list[Any], ws: _Workspace, custom: dict[str, int], today: date
) -> _Row:
    row = _Row(line=line)
    read = _Reader(table, cells, row)

    code = read.get("employee_code", required=True)
    if code is not None and not _CODE.match(code):
        row.errors.append("Emp Code may hold letters, digits, - _ and / only, up to 32.")
        code = None
    row.code = code

    first = read.get("first_name", required=True)
    middle, last = read.get("middle_name"), read.get("last_name")
    surname = " ".join(p for p in (middle, last) if p) or None
    full = read.get("full_name")
    plain = " ".join(p for p in (first, surname) if p)
    row.name = full or plain or None

    work_email = read.get("work_email")
    personal_email = read.get("personal_email")
    if personal_email is not None:
        try:
            personal_email = _EMAIL.validate_python(personal_email.lower())
        except ValidationError:
            row.warnings.append("Personal Email is not a valid email — left out.")
            personal_email = None
    if work_email is None and personal_email is not None:
        work_email = personal_email
        row.warnings.append("No work Email, so the personal email is used as the work email.")
    if work_email is None:
        row.errors.append("No Email (work email) or Personal Email.")
    else:
        try:
            work_email = _EMAIL.validate_python(work_email.lower())
        except ValidationError:
            row.errors.append("Email is not a valid email address.")
            work_email = None

    joining = read.get("date_of_joining", f.read_date, required=True)
    born = read.get("date_of_birth", f.read_date)
    if born is not None and not (date(1900, 1, 1) <= born < today):
        row.warnings.append("Birth Date is not a plausible date of birth — left out.")
        born = None

    employment_status, exit_on = _status(read, joining, today)

    try:
        kind = f.read_employment_type(read.raw("employment_type"))
    except ValueError as exc:
        row.warnings.append(f"Employment Type: {exc} — recorded as Permanent.")
        kind = None

    probation_end = read.get("confirmation_date", f.read_date) or read.get(
        "expected_confirmation_date", f.read_date
    )
    probation_days = read.get("probation_days", f.read_int)
    if probation_end is None and joining and probation_days and probation_days > 0:
        probation_end = joining + timedelta(days=probation_days)
    if probation_end and joining and probation_end < joining:
        row.warnings.append("Confirmation date is before the joining date — left out.")
        probation_end = None

    notice = read.get("notice_period_days", f.read_int)
    if notice is not None and not 0 <= notice <= 365:
        row.warnings.append("Notice Period For Employee is not between 0 and 365 days — left out.")
        notice = None

    grade = read.limited("grade", 32)
    if grade and ws.grades is not None and grade not in ws.grades:
        row.warnings.append(f"Grade {grade} is not one of this workspace's grades — left out.")
        grade = None

    phone = read.get("phone", f.read_phone)
    record = {
        "employee_code": code,
        "first_name": first,
        "last_name": surname,
        "display_name": full if full and full != plain else None,
        "work_email": work_email,
        "personal_email": personal_email,
        "phone": phone,
        "date_of_birth": born,
        "gender": read.get("gender", f.read_gender),
        "designation": read.limited("designation", 255),
        "grade": grade,
        "employment_type": kind or "full_time",
        "date_of_joining": joining,
        "probation_end_date": probation_end,
        "notice_period_days": notice or None,
    }
    if not row.errors:
        try:
            EmployeeCreate(**record)
        except ValidationError as exc:
            row.errors.append(_first_error(exc))
    record["status"] = employment_status
    record["date_of_exit"] = exit_on
    record["custom_fields"] = _custom(read, custom, ws)
    row.record = record

    row.department = read.limited("department", 255)
    row.location = read.limited("location", 255)
    row.entity = read.get("legal_entity")
    row.manager_code = read.get("manager", f.manager_code)
    if not row.errors:
        row.sections = _sections(read, row.name or "")
    return row


# ═══════════════════════════════════════════════════════════════════════════
# The whole file
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class _Plan:
    table: Table
    rows: list[_Row]
    can_create_org: bool
    #: Names to create, keyed by `_key`.
    new_departments: dict[str, str]
    new_locations: dict[str, tuple[str, str]]
    columns_used: list[ImportColumn]


def _unique_code(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-")[:28] or "IMPORTED"
    code, n = base, 2
    while code in taken:
        code = f"{base[: 28 - len(str(n))]}-{n}"
        n += 1
    taken.add(code)
    return code


async def _plan(session: AsyncSession, user: User, table: Table) -> _Plan:
    ws = await _workspace(session, user)
    custom = claim_custom(table, {key: d.label for key, d in ws.custom.items()})
    today = onboarding_rules.india_today()
    can_create = permissions.is_admin(user)

    rows = [_read_row(table, line, cells, ws, custom, today) for line, cells in table.rows]

    # Codes: one row per person, and nobody already here twice.
    seen: dict[str, int] = {}
    for row in rows:
        if row.code is None:
            continue
        upper = row.code.upper()
        if upper in seen:
            row.errors.append(f"Emp Code {row.code} is also on row {seen[upper]}.")
            continue
        seen[upper] = row.line
        if upper in ws.codes and not row.errors:
            row.skip = True

    # Emails: one person per address, here and in the file. Somebody already here
    # under another code is not added again; who has it is said, so HR can tell
    # whether it is the same person (the admin made with the workspace, say).
    emails: dict[str, int] = {}
    for row in rows:
        email = row.record.get("work_email")
        if email is None or row.skip:
            continue
        held = ws.emails.get(email)
        if held is not None and (row.code is None or held[0].upper() != row.code.upper()):
            row.errors.append(
                f"Email {email} is already used by {held[0]} ({held[1]}) in AskMira. If that is "
                "the same person, it was not added again; otherwise give this row its own email."
            )
        elif email in emails:
            row.errors.append(f"Email {email} is also on row {emails[email]}.")
        else:
            emails[email] = row.line

    ready = [row for row in rows if row.outcome == "ready"]

    # Departments and locations by name; what is missing is created (admin) or left blank.
    new_departments: dict[str, str] = {}
    new_locations: dict[str, tuple[str, str]] = {}
    for row in ready:
        if row.department and _key(row.department) not in ws.departments:
            if can_create:
                new_departments.setdefault(_key(row.department), row.department)
            else:
                row.warnings.append(
                    f"Department '{row.department}' is not in AskMira — left blank. "
                    "An admin can import this file to create it."
                )
        if row.location and _key(row.location) not in ws.locations:
            state = f.state_of_place(row.location)
            entity = _entity_for(row, ws)
            if not can_create:
                row.warnings.append(
                    f"Location '{row.location}' is not in AskMira — left blank. An admin can "
                    "import this file to create it."
                )
            elif state is None:
                row.warnings.append(
                    f"Location '{row.location}' is not in AskMira and its state cannot be told "
                    "— left blank. Add it under Settings → Organisation, then check again."
                )
            elif entity is None:
                row.warnings.append(
                    f"Location '{row.location}' needs a company to belong to — left blank."
                )
            else:
                new_locations.setdefault(_key(row.location), (row.location, state))
        if row.entity and _key(row.entity) not in ws.entities and ws.only_entity is None:
            row.warnings.append(f"Company '{row.entity}' is not a legal entity here — left blank.")

    # Reporting lines: to somebody in the file or already here.
    in_file = {row.code.upper() for row in ready if row.code}
    for row in ready:
        code = row.manager_code
        if code is None:
            continue
        upper = code.upper()
        if row.code and upper == row.code.upper():
            row.warnings.append("Reporting Manager is the employee themselves — left blank.")
            row.manager_code = None
        elif upper not in in_file and upper not in ws.codes:
            row.warnings.append(f"Reporting Manager {code} is not in the file or in AskMira — "
                                "left blank.")
            row.manager_code = None
    by_code = {row.code.upper(): row for row in ready if row.code}
    for row in ready:
        # A loop of managers inside the file is broken at the first row that closes it.
        current, steps = row.manager_code, 0
        while current and steps < len(by_code) + 1:
            if row.code and current.upper() == row.code.upper():
                row.warnings.append("Reporting Manager would make a loop — left blank.")
                row.manager_code = None
                break
            nxt = by_code.get(current.upper())
            current, steps = (nxt.manager_code if nxt else None), steps + 1

    by_index = {c.index: c for c in table.columns}
    columns_used = [
        ImportColumn(
            header=by_index[index].header,
            field=f.FIELD_BY_KEY[key].label,
            section=f.FIELD_BY_KEY[key].section,
        )
        for key, index in sorted(table.mapping.items(), key=lambda item: item[1])
    ] + [
        ImportColumn(header=by_index[index].header, field=ws.custom[key].label,
                     section="Custom fields")
        for key, index in custom.items()
    ]
    return _Plan(
        table=table,
        rows=rows,
        can_create_org=can_create,
        new_departments=new_departments,
        new_locations=new_locations,
        columns_used=columns_used,
    )


def _entity_for(row: _Row, ws: _Workspace) -> uuid.UUID | None:
    if row.entity and _key(row.entity) in ws.entities:
        return ws.entities[_key(row.entity)]
    return ws.only_entity


def _row_read(row: _Row) -> ImportRow:
    record = row.record
    return ImportRow(
        line=row.line,
        employee_code=row.code,
        name=row.name,
        status=record.get("status"),
        date_of_joining=record.get("date_of_joining"),
        date_of_exit=record.get("date_of_exit"),
        designation=record.get("designation"),
        department=row.department,
        location=row.location,
        manager=row.manager_code,
        outcome=row.outcome,  # type: ignore[arg-type]
        errors=row.errors,
        warnings=row.warnings if row.outcome == "ready" else [],
        employee_id=row.employee_id,
    )


async def template(session: AsyncSession, user: User) -> bytes:
    """The import template, with this workspace's active custom fields at the end."""
    permissions.require_hr(user, detail="Only HR imports employees.")
    definitions = await custom_fields.employee_definitions(session, user)
    active = sorted(
        (d for d in definitions.values() if d.is_active), key=lambda d: (d.display_order, d.label)
    )
    return build_template(active)


@dataclass
class _AadhaarFills:
    #: (employee, full number) to fill in.
    fills: list[tuple[Employee, str]]
    #: "CODE — Name" whose file number ends differently from the record.
    mismatches: list[str]


async def _aadhaar_fills(session: AsyncSession, user: User, plan: _Plan) -> _AadhaarFills:
    """Full Aadhaar numbers the file has for people already here who only have
    the last four on record. A number whose last four differ from the record's
    is not used: the file or the record is wrong, and a person decides which."""
    wanted: dict[str, str] = {}
    for row in plan.rows:
        if row.outcome != "skip" or not row.code:
            continue
        for change in row.sections:
            if change.section == "statutory" and getattr(change.data, "aadhaar", None):
                wanted[row.code.upper()] = change.data.aadhaar
    if not wanted:
        return _AadhaarFills([], [])
    people = (
        await session.scalars(
            tenant_scoped(
                select(Employee).where(func.upper(Employee.employee_code).in_(list(wanted))),
                Employee,
                user,
            )
        )
    ).all()
    fills: list[tuple[Employee, str]] = []
    mismatches: list[str] = []
    for person in people:
        full = wanted.get((person.employee_code or "").upper())
        if not full or person.aadhaar_number:
            continue
        if person.aadhaar_last4 and person.aadhaar_last4 != full[-4:]:
            mismatches.append(f"{person.employee_code} — {person.full_name}")
            continue
        fills.append((person, full))
    return _AadhaarFills(fills, sorted(mismatches))


def _read(data: bytes, filename: str) -> Table:
    try:
        return read_table(data, filename)
    except SheetError as exc:
        raise _unprocessable(str(exc)) from exc


async def preview(session: AsyncSession, user: User, data: bytes, filename: str) -> ImportPreview:
    """What importing this file would do. Writes nothing."""
    permissions.require_hr(user, detail="Only HR imports employees.")
    plan = await _plan(session, user, _read(data, filename))
    ready = [row for row in plan.rows if row.outcome == "ready"]
    aadhaar = await _aadhaar_fills(session, user, plan)
    return ImportPreview(
        filename=filename,
        source=plan.table.source,
        header_row=plan.table.header_row,
        total_rows=len(plan.rows),
        ready_current=sum(1 for row in ready if not row.leaver),
        ready_leavers=sum(1 for row in ready if row.leaver),
        skipped=sum(1 for row in plan.rows if row.outcome == "skip"),
        errors=sum(1 for row in plan.rows if row.outcome == "error"),
        with_warnings=sum(1 for row in ready if row.warnings),
        new_departments=sorted(plan.new_departments.values(), key=str.lower),
        new_locations=[
            ImportNewLocation(name=name, state_code=state)
            for name, state in sorted(plan.new_locations.values(), key=lambda v: v[0].lower())
        ],
        columns_used=plan.columns_used,
        columns_ignored=[column.header for column in plan.table.unused],
        can_create_org=plan.can_create_org,
        rows=[_row_read(row) for row in plan.rows],
        aadhaar_fills=len(aadhaar.fills),
        aadhaar_mismatches=aadhaar.mismatches,
    )


#: Why a row whose code is already here was not added.
_SKIPPED = "Emp Code is already in AskMira; that record is left as it is."


async def rejected(session: AsyncSession, user: User, data: bytes, filename: str) -> bytes:
    """The file's rows that would not be added, with their own cells and why,
    as a workbook to correct and import again. Writes nothing."""
    permissions.require_hr(user, detail="Only HR imports employees.")
    plan = await _plan(session, user, _read(data, filename))
    cells = dict(plan.table.rows)
    left_out = [
        (cells[row.line], "; ".join(row.errors) if row.errors else _SKIPPED)
        for row in plan.rows
        if row.outcome != "ready"
    ]
    if not left_out:
        raise _unprocessable("Every row in this file can be added; none is left out.")
    return write_left_out(plan.table, left_out)


async def run(
    session: AsyncSession,
    user: User,
    data: bytes,
    filename: str,
    *,
    include_leavers: bool = True,
    request: Request | None = None,
) -> ImportResult:
    """Import the file's ready rows. Does not commit: the router does, once."""
    permissions.require_hr(user, detail="Only HR imports employees.")
    plan = await _plan(session, user, _read(data, filename))
    ws = await _workspace(session, user)
    chosen = [
        row for row in plan.rows if row.outcome == "ready" and (include_leavers or not row.leaver)
    ]
    aadhaar = await _aadhaar_fills(session, user, plan)
    if not chosen and not aadhaar.fills:
        raise _unprocessable("Nothing in this file can be imported. Check it to see why.")
    for person, full in aadhaar.fills:
        person.aadhaar_number = full
        person.aadhaar_last4 = full[-4:]
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="employee_profile",
            entity_id=person.id,
            actor=user,
            # Which field, never the number.
            changes={"section": "statutory", "fields": ["aadhaar_number"], "file": filename},
            summary=f"Filled in {person.employee_code}'s full Aadhaar number from {filename}",
            request=request,
        )

    departments_created: list[str] = []
    taken = set(ws.department_codes)
    for key, name in sorted(plan.new_departments.items()):
        if not any(row.department and _key(row.department) == key for row in chosen):
            continue
        department = Department(tenant_id=user.tenant_id, name=name[:255],
                                code=_unique_code(name, taken))
        session.add(department)
        await session.flush()
        ws.departments[key] = (department.id, department.name)
        departments_created.append(department.name)
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.CREATE,
            entity_type="department",
            entity_id=department.id,
            actor=user,
            changes={"name": department.name, "code": department.code, "source": filename},
            summary=f"Created department {department.code} while importing employees",
            request=request,
        )

    locations_created: list[str] = []
    taken = set(ws.location_codes)
    for key, (name, state) in sorted(plan.new_locations.items()):
        users_of = [row for row in chosen if row.location and _key(row.location) == key]
        entity = next((e for e in (_entity_for(r, ws) for r in users_of) if e), None)
        if not users_of or entity is None:
            continue
        location = Location(
            tenant_id=user.tenant_id,
            legal_entity_id=entity,
            name=name[:255],
            code=_unique_code(name, taken),
            city=name[:128] if f.CITY_STATES.get(name.lower().strip()) else None,
            state_code=state,
        )
        session.add(location)
        await session.flush()
        ws.locations[key] = (location.id, location.name)
        locations_created.append(location.name)
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.CREATE,
            entity_type="location",
            entity_id=location.id,
            actor=user,
            changes={"name": location.name, "code": location.code, "state_code": state,
                     "source": filename},
            summary=f"Created location {location.code} in state {state} while importing employees",
            request=request,
        )

    employees: dict[str, Employee] = {}
    for row in chosen:
        record = dict(row.record)
        department = ws.departments.get(_key(row.department)) if row.department else None
        location = ws.locations.get(_key(row.location)) if row.location else None
        employee = Employee(
            id=uuid.uuid4(),
            tenant_id=user.tenant_id,
            legal_entity_id=_entity_for(row, ws),
            department_id=department[0] if department else None,
            location_id=location[0] if location else None,
            is_active=record["status"] is not EmploymentStatus.EXITED,
            **record,
        )
        session.add(employee)
        employees[row.code.upper()] = employee  # type: ignore[union-attr]
        row.employee_id = employee.id
    await session.flush()

    for row in chosen:
        if not row.manager_code:
            continue
        manager = employees.get(row.manager_code.upper())
        manager_id = manager.id if manager else ws.codes.get(row.manager_code.upper())
        if manager_id is None:
            # Their manager's row was not imported (a leaver left out, say).
            row.warnings.append(
                f"Reporting Manager {row.manager_code} was not imported — left blank."
            )
        employees[row.code.upper()].manager_id = manager_id  # type: ignore[union-attr]
    await session.flush()

    for row in chosen:
        employee = employees[row.code.upper()]  # type: ignore[union-attr]
        written = []
        for change in row.sections:
            try:
                await profile_service.apply_section(session, user, employee, change,
                                                    request=request)
                written.append(change.section)
            except HTTPException as exc:
                row.warnings.append(f"{change.section.replace('_', ' ').capitalize()} left out: "
                                    f"{exc.detail}")
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.CREATE,
            entity_type="employee",
            entity_id=employee.id,
            actor=user,
            changes={
                "source": plan.table.source,
                "file": filename,
                "line": row.line,
                "status": employee.status.value,
                "profile_sections": written,
            },
            summary=f"Imported employee {employee.employee_code} from {filename}",
            request=request,
        )

    leavers = sum(1 for row in chosen if row.leaver)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="employee_import",
        actor=user,
        changes={
            "file": filename,
            "source": plan.table.source,
            "created": len(chosen),
            "leavers": leavers,
            "skipped": sum(1 for row in plan.rows if row.outcome == "skip"),
            "failed": sum(1 for row in plan.rows if row.outcome == "error"),
            "departments_created": departments_created,
            "locations_created": locations_created,
            "aadhaar_filled": len(aadhaar.fills),
        },
        summary=f"Imported {len(chosen)} employees from {filename}"
        + (f"; filled in {len(aadhaar.fills)} full Aadhaar numbers" if aadhaar.fills else ""),
        request=request,
    )
    return ImportResult(
        aadhaar_filled=len(aadhaar.fills),
        aadhaar_mismatches=aadhaar.mismatches,
        filename=filename,
        created=len(chosen),
        created_leavers=leavers,
        skipped=sum(1 for row in plan.rows if row.outcome == "skip"),
        failed=sum(1 for row in plan.rows if row.outcome == "error"),
        departments_created=departments_created,
        locations_created=locations_created,
        rows=[_row_read(row) for row in plan.rows],
    )
