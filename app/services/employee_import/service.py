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
- Invent, or drop what the old system held. A value that fails AskMira's
  check (a PAN that is not a PAN, an address with no PIN code) is imported as
  typed and listed as an issue against its person, for HR to correct; only a
  missing employee code, first name or joining date stops a row, and a
  missing or shared work email stops it only for somebody still working here.
- Keep an Aadhaar number in the clear. It is stored encrypted, as everywhere else.
- Give anybody a login. That stays an admin's decision, person by person.

A factoHR export's lists (qualifications, family, nominees, emergency
contacts, past employers' tax) and its continuation rows are read by
`groups`; its columns AskMira has no field for become custom fields on a
"factoHR" profile tab, created by an admin's import.
"""

from __future__ import annotations

import io
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.crypto import encrypt_secret
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.disbursement import EmployeeBankAccount, VerificationMethod
from app.models.employee import CustomFieldDefinition, Employee, EmploymentStatus
from app.models.org import Department, LegalEntity, Location
from app.models.pay_grades import PayGrade
from app.models.tax import PreviousEmployerIncome, ProofStatus
from app.models.timesheets import Project
from app.models.user import User
from app.schemas.employee import EmployeeCreate
from app.schemas.employee_import import (
    ImportColumn,
    ImportColumnNotKept,
    ImportNewLocation,
    ImportPreview,
    ImportResult,
    ImportRow,
)
from app.schemas.employee_profile import (
    Address,
    Addresses,
    BankAccountDetails,
    Education,
    EmergencyContact,
    FamilyMember,
    Nominee,
    PersonalDetails,
    ProfileSection,
    StatutoryDetails,
)
from app.services import audit, custom_fields
from app.services import employee_profile as profile_service
from app.services.employee_import import fields as f
from app.services.employee_import import groups as g
from app.services.employee_import.sheet import (
    SheetError,
    Table,
    claim_custom,
    read_table,
    write_left_out,
)
from app.services.employee_import.template import build_template
from app.services.onboarding import rules as onboarding_rules

__all__ = ["issues", "preview", "rejected", "run", "template"]

_EMAIL = TypeAdapter(EmailStr)
_CODE = re.compile(r"^[A-Za-z0-9_\-/]{1,32}$")
#: As the old system had it: words of those characters with single spaces between.
_CODE_TYPED = re.compile(r"^(?=.{1,32}$)[A-Za-z0-9_\-/]+( [A-Za-z0-9_\-/]+)*$")
#: Stands in for a leaver's missing email, or a code with a space, only while
#: the rest of the record is checked; never stored.
_CHECK_EMAIL = "checking@example.com"
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
    #: Each company's name -> id.
    entity_names: dict[str, uuid.UUID]
    only_entity: uuid.UUID | None
    #: Upper-cased employee code -> id, for skipping and for managers.
    codes: dict[str, uuid.UUID]
    #: Lower-cased work email -> (code, name) of who has it here.
    emails: dict[str, tuple[str, str]]
    #: The emails of people here who have not left.
    current_emails: set[str]
    department_codes: set[str]
    location_codes: set[str]
    #: Project name or code -> (id, name); and every project code, for new ones.
    projects: dict[str, tuple[uuid.UUID, str]]
    project_codes: set[str]
    #: Active grade codes; None while the workspace keeps no grade list.
    grades: set[str] | None
    custom: dict[str, CustomFieldDefinition]
    all_custom_keys: set[str] = field(default_factory=set)


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
    entity_names: dict[str, uuid.UUID] = {}
    for row in entity_rows:
        entities.setdefault(_key(row.name), row.id)
        entities.setdefault(_key(row.code), row.id)
        entity_names[row.name] = row.id

    codes: dict[str, uuid.UUID] = {}
    emails: dict[str, tuple[str, str]] = {}
    current_emails: set[str] = set()
    for row in (await session.execute(scoped(
        Employee, Employee.id, Employee.employee_code, Employee.work_email, Employee.display_name,
        Employee.first_name, Employee.last_name, Employee.status, Employee.deleted_at,
    ))).all():
        codes[row.employee_code.upper()] = row.id
        if not row.work_email:
            continue
        name = row.display_name or " ".join(filter(None, [row.first_name, row.last_name]))
        emails.setdefault(row.work_email.lower(), (row.employee_code, name))
        # A record taken out of the directory (deactivated) no longer holds its
        # email for somebody still working here; the clash is only noted.
        if row.status is not EmploymentStatus.EXITED and row.deleted_at is None:
            current_emails.add(row.work_email.lower())
    projects: dict[str, tuple[uuid.UUID, str]] = {}
    project_codes: set[str] = set()
    for row in (await session.execute(scoped(Project, Project.id, Project.name,
                                             Project.code))).all():
        projects.setdefault(_key(row.name), (row.id, row.name))
        projects.setdefault(_key(row.code), (row.id, row.name))
        project_codes.add(row.code.upper())
    grade_rows = (await session.execute(scoped(PayGrade, PayGrade.code, PayGrade.active))).all()
    definitions = await custom_fields.employee_definitions(session, user)
    return _Workspace(
        departments=departments,
        locations=locations,
        entities=entities,
        entity_names=entity_names,
        only_entity=entity_rows[0].id if len(entity_rows) == 1 else None,
        codes=codes,
        emails=emails,
        current_emails=current_emails,
        department_codes=department_codes,
        location_codes=location_codes,
        projects=projects,
        project_codes=project_codes,
        grades={row.code for row in grade_rows if row.active} if grade_rows else None,
        custom={key: d for key, d in definitions.items() if d.is_active},
        all_custom_keys=set(definitions),
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
    project: str | None = None
    entity: str | None = None
    manager_code: str | None = None
    #: Profile sections to write, each already in its model.
    sections: dict[ProfileSection, Any] = field(default_factory=dict)
    #: Identifiers as typed (Employee columns), and which of them pass their check.
    statutory: dict[str, str] = field(default_factory=dict)
    statutory_valid: dict[str, str] = field(default_factory=dict)
    #: The salary account as typed: number, IFSC, holder, bank.
    bank: dict[str, Any] | None = None
    #: Previous employers' income, one entry per financial year.
    prior_income: list[dict[str, Any]] = field(default_factory=list)
    #: Years the sheet lists with every amount 0: nothing to record.
    prior_zero_years: int = 0
    #: Imported as typed, but not as AskMira would have it: for HR to look at.
    issues: list[str] = field(default_factory=list)
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
            row.issues.append(
                "Status says they have left, but there is no Leaving Date — recorded as left "
                "with no leaving date. Payroll does not pay them."
            )
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
    typed_pin = f.digits(read.raw(f"{prefix}_pincode"))
    if typed_pin and not _PINCODE.match(typed_pin):
        read.row.issues.append(
            f"{label}: Pincode '{typed_pin}' is not a six-digit PIN code — not used."
        )
    city = (read.get(f"{prefix}_city") or f.city_in(whole, state) or "")[:128] or None
    try:
        address = Address(line1=line1, line2=line2, city=city, state_code=state, pincode=pincode)
    except ValidationError as exc:
        read.row.warnings.append(f"{label} left out: {_first_error(exc)}.")
        return None
    if address.incomplete:
        read.row.issues.append(
            f"{label} imported as typed, without its {' and '.join(address.incomplete)} — "
            "the sheet does not say. Add it on the profile."
        )
    return address


_STATUTORY_LABELS = {
    "pan": "Pan No",
    "aadhaar": "Aadhaar No",
    "uan": "UA No",
    "pf_number": "PF No",
    "esi_number": "ESIC No",
}
#: Longest each identifier column holds.
_STATUTORY_WIDTH = {"uan": 12, "pf_number": 32, "esi_number": 20}


def _entries(model: Any, items: list[dict[str, Any]], what: str, row: _Row) -> list[Any]:
    kept = []
    for item in items:
        try:
            kept.append(model(**item))
        except ValidationError as exc:
            row.warnings.append(f"{what} {item.get('name') or item.get('qualification')} left "
                                f"out: {_first_error(exc)}.")
    return kept


def _sections(read: _Reader, name: str, more: list[list[Any]]) -> None:
    """Fill the row's profile sections, identifiers and salary account, each as
    the sheet has it. `more` are the person's continuation rows."""
    row = read.row
    table = read.table
    every = [read.cells, *more]

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
            row.sections[ProfileSection.PERSONAL] = PersonalDetails(**personal)
        except ValidationError as exc:
            row.warnings.append(f"Personal details left out: {_first_error(exc)}.")

    current = _address(read, "current", "Local address")
    same = read.get("permanent_same", f.read_yes)
    permanent = None if same and current else _address(read, "permanent", "Permanent address")
    if current or permanent:
        row.sections[ProfileSection.ADDRESSES] = Addresses(
            current=current,
            permanent=permanent,
            permanent_same_as_current=bool(same and current),
        )

    # Family: the sheet's family list, and the Mother Name column unless the
    # list already has her.
    family = g.family(table, every)
    row.warnings.extend(family.notes)
    mother = read.limited("mother_name", 255)
    if mother and not any(
        item["relationship"] == "mother" and item["name"].lower() == mother.lower()
        for item in family.items
    ):
        family.items.insert(0, {"name": mother, "relationship": "mother"})
    members = _entries(FamilyMember, family.items, "Family member", row)
    if members:
        row.sections[ProfileSection.FAMILY_MEMBERS] = members

    if g.blocks(table, "Emergency Contact Detail"):
        contacts = g.emergency_contacts(table, every)
        row.issues.extend(contacts.notes)
        items = contacts.items
    else:
        contact = read.limited("emergency_name", 255)
        items = (
            [{
                "name": contact,
                "relationship": read.get("emergency_relationship", f.read_relationship) or "other",
                "phone": read.get("emergency_phone", f.read_phone),
                "is_primary": True,
            }]
            if contact
            else []
        )
        if contact and not items[0]["phone"]:
            row.issues.append(f"Emergency contact {contact} has no mobile number.")
    contacts_kept = _entries(EmergencyContact, items, "Emergency contact", row)
    if contacts_kept:
        row.sections[ProfileSection.EMERGENCY_CONTACTS] = contacts_kept

    schooling = g.education(table, every)
    row.warnings.extend(schooling.notes)
    for item in schooling.items:
        if not item["institution"]:
            row.issues.append(f"Qualification {item['qualification']}: no institute in the sheet.")
    studied = _entries(Education, schooling.items, "Qualification", row)
    if studied:
        row.sections[ProfileSection.EDUCATION] = studied

    nominations = g.nominees(table, every)
    row.issues.extend(nominations.notes)
    named = _entries(Nominee, nominations.items, "Nominee", row)
    if named:
        row.sections[ProfileSection.NOMINEES] = named

    income = g.prior_income(table, every)
    row.prior_income = income.items
    row.prior_zero_years = sum(
        int(note.split(" ", 1)[0]) for note in income.notes if note[:1].isdigit()
    )

    # Identifiers: kept as typed; one that fails its check is listed.
    for key, label in _STATUTORY_LABELS.items():
        value = (f.digits if key in {"aadhaar", "uan", "esi_number"} else f.text)(read.raw(key))
        if key == "pf_number" and value is not None and value.strip("0") == "":
            value = None
        if value is None:
            continue
        if key == "pan":
            value = value.upper()
        width = _STATUTORY_WIDTH.get(key)
        if width and len(value) > width:
            row.warnings.append(f"{label} '{value}' is longer than {width} characters — left out.")
            continue
        try:
            StatutoryDetails(**{key: value})
            row.statutory_valid[key] = value
        except ValidationError as exc:
            row.issues.append(f"{label} '{value}': {_first_error(exc)} Imported as typed.")
        row.statutory[key] = value

    account = f.digits(read.raw("account_number"))
    ifsc = f.text(read.raw("ifsc"))
    if account or ifsc:
        holder = read.limited("account_holder_name", 255)
        if not holder:
            holder = name
            row.issues.append(
                "Name As Per Bank Records is empty — the employee's name is used as the account "
                "holder."
            )
        bank = {
            "account_number": account or "",
            "ifsc": (ifsc or "").upper(),
            "account_holder_name": holder,
            "bank_name": read.limited("bank_name", 128),
        }
        if len(bank["ifsc"]) > 11:
            row.warnings.append(f"IFSC Code '{bank['ifsc']}' is longer than 11 characters — "
                                "bank account left out.")
        else:
            try:
                BankAccountDetails(**bank)
            except ValidationError as exc:
                row.issues.append(f"Bank account: {_first_error(exc)} Imported as typed.")
            row.bank = bank


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
    table: Table,
    line: int,
    cells: list[Any],
    ws: _Workspace,
    custom: dict[str, int],
    today: date,
    more: list[list[Any]],
    extras: list[g.ExtraColumn],
) -> _Row:
    row = _Row(line=line)
    read = _Reader(table, cells, row)

    code = read.get("employee_code", required=True)
    if code is not None and not _CODE.match(code):
        if _CODE_TYPED.match(code):
            row.issues.append(
                f"Emp Code '{code}' has a space in it — imported as typed. Correct it on the "
                "record before editing the record."
            )
        else:
            row.errors.append("Emp Code may hold letters, digits, - _ and / only, up to 32.")
            code = None
    row.code = code

    first = read.get("first_name", required=True)
    middle, last = read.get("middle_name"), read.get("last_name")
    surname = " ".join(p for p in (middle, last) if p) or None
    full = read.get("full_name")
    plain = " ".join(p for p in (first, surname) if p)
    row.name = full or plain or None

    joining = read.get("date_of_joining", f.read_date, required=True)
    employment_status, exit_on = _status(read, joining, today)
    leaver = employment_status is EmploymentStatus.EXITED

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
        row.issues.append("No work Email, so the personal email is used as the work email.")
    if work_email is None:
        if leaver:
            work_email = ""
            row.issues.append(
                "No Email or Personal Email in the sheet — imported without one. They have "
                "left, so they never need a login."
            )
        else:
            row.errors.append("No Email (work email) or Personal Email.")
    else:
        try:
            work_email = _EMAIL.validate_python(work_email.lower())
        except ValidationError:
            row.errors.append("Email is not a valid email address.")
            work_email = None
    born = read.get("date_of_birth", f.read_date)
    if born is not None and not (date(1900, 1, 1) <= born < today):
        row.warnings.append("Birth Date is not a plausible date of birth — left out.")
        born = None

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
        # Checked as AskMira would enter it; a leaver's missing email and a code
        # kept as typed are already listed, so they do not stop the row here.
        checked = dict(record)
        if not checked["work_email"]:
            checked["work_email"] = _CHECK_EMAIL
        if code and not _CODE.match(code):
            checked["employee_code"] = "CHECKING"
        try:
            EmployeeCreate(**checked)
        except ValidationError as exc:
            row.errors.append(_first_error(exc))
    record["status"] = employment_status
    record["date_of_exit"] = exit_on
    values = _custom(read, custom, ws)
    for column in extras:
        value = g.extra_value(column, cells[column.index] if column.index < len(cells) else None)
        if value is not None and column.differs_from is not None:
            other = g.extra_value(column, cells[column.differs_from])
            if other is None or other == value:
                value = None
        if value is not None:
            values[column.key] = value
    record["custom_fields"] = values
    row.record = record

    row.department = read.limited("department", 255)
    row.project = read.limited("project", 160)
    row.location = read.limited("location", 255)
    row.entity = read.get("legal_entity")
    row.manager_code = read.get("manager", f.manager_code)
    if not row.errors:
        _sections(read, row.name or "", more)
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
    new_projects: dict[str, str]
    columns_used: list[ImportColumn]
    #: Columns kept as new custom fields, and columns with data not kept (with why).
    extras: list[g.ExtraColumn] = field(default_factory=list)
    not_kept: list[g.NotKept] = field(default_factory=list)
    #: Points about the whole file, said once rather than on every row.
    notes: list[str] = field(default_factory=list)
    #: Continuation rows folded into their person.
    continuation_rows: int = 0


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

    people, more = g.continuations(table)
    extras, not_kept = g.extra_columns(table, people, set(ws.all_custom_keys))
    notes: list[str] = []
    not_kept.extend(g.section_leftovers(table, [c for _, c in table.rows]))
    for key, over in (("leaving_date", "last_working_date"),
                      ("expected_confirmation_date", "confirmation_date")):
        index, other = table.mapping.get(key), table.mapping.get(over)
        if index is None or other is None or not can_create:
            continue
        differ = 0
        for _, cells in people:
            try:
                mine, theirs = f.read_date(cells[index]), f.read_date(cells[other])
            except ValueError:
                continue
            if mine and theirs and mine != theirs:
                differ += 1
        if differ:
            header = table.columns_by_index[index].header
            extras.append(
                g.ExtraColumn(
                    index=index,
                    header=header,
                    key=g.unused_key(f"fh_{key}_as_typed", ws.all_custom_keys, extras),
                    label=f"{header} (where it differs)",
                    field_type="date",
                    sensitive=False,
                    filled=differ,
                    differs_from=other,
                )
            )
            other_header = table.columns_by_index[other].header
            who = "1 person" if differ == 1 else f"{differ} people"
            notes.append(
                f"{header}: differs from {other_header} for {who}. The record uses "
                f"{other_header}; {header} is kept on the {g.EXTRA_TAB} tab."
            )
    # A '0' in an identifier column is the old system's way of saying "none".
    for key in ("esi_number", "pf_number", "uan"):
        index = table.mapping.get(key)
        if index is None:
            continue
        zeros = sum(
            1
            for _, cells in people
            if (value := f.text(cells[index])) is not None and value.strip("0") == ""
        )
        if zeros:
            header = table.columns_by_index[index].header
            notes.append(f"{header}: '0' for {zeros} people means no number — recorded as none.")
    for key in ("notice_period_days", "probation_days"):
        index = table.mapping.get(key)
        if index is None:
            continue
        zeros = sum(1 for _, cells in people if f.text(cells[index]) == "0")
        if zeros:
            header = table.columns_by_index[index].header
            notes.append(f"{header}: 0 for {zeros} people — recorded as not set.")
    # The status word as the old system typed it: AskMira keeps fewer statuses
    # ("Withdrawn", "Suspended" and "Inactive" are all "left"), so the word
    # itself is kept on the factoHR tab and the mapping said once.
    status = table.mapping.get("status")
    if status is not None and can_create:
        words: Counter[str] = Counter()
        for _, cells in people:
            word = f.text(cells[status])
            if word:
                words[word] += 1
        if words:
            extras.append(
                g.ExtraColumn(
                    index=status,
                    header=table.columns_by_index[status].header,
                    key=g.unused_key("fh_status_as_typed", ws.all_custom_keys, extras),
                    label="Status (as in the file)",
                    field_type="text",
                    sensitive=False,
                    filled=sum(words.values()),
                )
            )
            def _left(word: str) -> bool:
                try:
                    return f.read_status(word) == "left"
                except ValueError:
                    return False

            left = sorted((w for w in words if _left(w)), key=lambda w: -words[w])
            if left:
                listed = ", ".join(f"'{w}' ({words[w]})" for w in left)
                notes.append(
                    f"Status: {listed} recorded as left. The word as typed is kept on the "
                    f"{g.EXTRA_TAB} tab (Status (as in the file))."
                )
    # With one company, everybody is recorded under it whatever the file calls
    # it; a name it does not go by is said once, and kept as typed.
    company = table.mapping.get("legal_entity")
    if company is not None and ws.only_entity is not None:
        named: Counter[str] = Counter()
        for _, cells in people:
            name = f.text(cells[company])
            if name and _key(name) not in ws.entities:
                named[name] += 1
        if named:
            only = next(n for n, i in ws.entity_names.items() if i == ws.only_entity)
            listed = ", ".join(f"'{name}' ({count})" for name, count in named.most_common())
            notes.append(
                f"Company: {listed} recorded under {only}, the only company here. The name as "
                f"typed is kept on the {g.EXTRA_TAB} tab."
            )
            if can_create:
                extras.append(
                    g.ExtraColumn(
                        index=company,
                        header=table.columns_by_index[company].header,
                        key=g.unused_key("fh_company_as_typed", ws.all_custom_keys, extras),
                        label="Company (as in the file)",
                        field_type="text",
                        sensitive=False,
                        filled=sum(named.values()),
                    )
                )
    if not can_create:
        not_kept = not_kept + [
            g.NotKept(column.header, "Only an admin's import adds custom fields.")
            for column in extras
        ]
        extras = []
    for group, where in sorted(g.SECTION_GROUPS.items()):
        if g.blocks(table, group) and where.startswith("Salary"):
            not_kept.append(g.NotKept(group.title(), f"Not employee details: {where}."))

    rows = [
        _read_row(table, line, cells, ws, custom, today, more.get(line, []), extras)
        for line, cells in people
    ]

    zero_years = sum(row.prior_zero_years for row in rows)
    if zero_years:
        notes.append(
            f"Past Employee History: {zero_years} financial year(s) with every amount 0 — "
            "nothing to record. Years with an amount are kept as previous-employer income."
        )

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
    # A leaver may share an address with their later record (a re-hire, a
    # consultant under their contact's email): only somebody still working
    # here signs in with it, so only they need it to themselves.
    emails: dict[str, _Row] = {}
    current_in_file: dict[str, _Row] = {}
    # A row already here is not added, so it is never stopped here; what is
    # said about it still goes on the issues list, which describes the file.
    for row in rows:
        email = row.record.get("work_email")
        if not email or row.errors:
            continue
        held = ws.emails.get(email)
        mine = row.code is not None and held is not None and held[0].upper() == row.code.upper()
        # A row already here shares the address with others from this file, said
        # below; who else holds it in AskMira would only say it twice.
        if held is not None and not mine and not row.skip:
            if row.leaver or email not in ws.current_emails:
                row.issues.append(f"Email {email} is also {held[0]} ({held[1]})'s in AskMira.")
            else:
                row.errors.append(
                    f"Email {email} is already used by {held[0]} ({held[1]}) in AskMira. If that "
                    "is the same person, it was not added again; otherwise give this row its own "
                    "email."
                )
                continue
        first = emails.get(email)
        if first is not None:
            other = current_in_file.get(email)
            if not row.leaver and not row.skip and other is not None:
                row.errors.append(f"Email {email} is also on row {other.line}, and both are "
                                  "still working here.")
                continue
            row.issues.append(f"Email {email} is also {first.code}'s (row {first.line}).")
            if not any(email in issue for issue in first.issues):
                first.issues.append(f"Email {email} is also {row.code}'s (row {row.line}).")
        else:
            emails[email] = row
        if not row.leaver:
            current_in_file[email] = row

    ready = [row for row in rows if row.outcome == "ready"]

    # Departments and locations by name; what is missing is created (admin) or left blank.
    new_departments: dict[str, str] = {}
    new_locations: dict[str, tuple[str, str]] = {}
    new_projects: dict[str, str] = {}
    for row in ready:
        if row.project and _key(row.project) not in ws.projects:
            if can_create:
                new_projects.setdefault(_key(row.project), row.project)
            else:
                row.warnings.append(
                    f"Project '{row.project}' is not in AskMira — left blank. An admin can "
                    "import this file to create it."
                )
        if row.department and _key(row.department) not in ws.departments:
            if can_create:
                new_departments.setdefault(_key(row.department), row.department)
            else:
                row.warnings.append(
                    f"Department '{row.department}' is not in AskMira — left blank. "
                    "An admin can import this file to create it."
                )
        if row.location and _key(row.location) not in ws.locations:
            state = f.state_of_place(row.location) or f.state_in(row.location)
            if state is None and f.country_of_place(row.location):
                state = ""  # abroad: no Indian state, so no Professional Tax there
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
            row.warnings.append(
                f"Company '{row.entity}' is not a legal entity here — left blank. No payroll "
                "run pays them until one is set (Settings → Organisation, then People → "
                "Employees → Move to company)."
            )
        elif not row.entity and ws.only_entity is None and ws.entities:
            row.warnings.append(
                "No company given and there are several — left blank. No payroll run pays "
                "them until one is set: fill the Company column, or use People → Employees → "
                "Move to company afterwards."
            )

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
        new_projects=new_projects,
        columns_used=columns_used,
        extras=extras,
        not_kept=not_kept,
        notes=notes,
        continuation_rows=len(table.rows) - len(people),
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
        issues=row.issues if row.outcome == "ready" else [],
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
        if row.statutory_valid.get("aadhaar"):
            wanted[row.code.upper()] = row.statutory_valid["aadhaar"]
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
        with_issues=sum(1 for row in ready if row.issues),
        continuation_rows=plan.continuation_rows,
        notes=plan.notes,
        new_custom_fields=[column.label for column in plan.extras],
        columns_not_kept=[
            ImportColumnNotKept(header=item.header, why=item.why) for item in plan.not_kept
        ],
        new_departments=sorted(plan.new_departments.values(), key=str.lower),
        new_projects=sorted(plan.new_projects.values(), key=str.lower),
        new_locations=[
            ImportNewLocation(name=name, state_code=state)
            for name, state in sorted(plan.new_locations.values(), key=lambda v: v[0].lower())
        ],
        columns_used=plan.columns_used,
        columns_ignored=[
            column.header
            for column in plan.table.unused
            if column.index not in {extra.index for extra in plan.extras}
        ],
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

    custom_fields_created: list[str] = []
    order = max((d.display_order for d in ws.custom.values()), default=0)
    for column in plan.extras:
        if not any(column.key in row.record.get("custom_fields", {}) for row in chosen):
            continue
        order += 1
        definition = CustomFieldDefinition(
            tenant_id=user.tenant_id,
            entity="employee",
            key=column.key,
            label=column.label,
            field_type=column.field_type,
            is_required=False,
            is_sensitive=column.sensitive,
            display_order=order,
            is_active=True,
            tab=g.EXTRA_TAB,
        )
        session.add(definition)
        custom_fields_created.append(column.label)
    if custom_fields_created:
        await session.flush()
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.CREATE,
            entity_type="custom_field_definition",
            actor=user,
            changes={"fields": custom_fields_created, "tab": g.EXTRA_TAB, "source": filename},
            summary=(
                f"Added {len(custom_fields_created)} custom fields on the {g.EXTRA_TAB} tab for "
                f"the columns of {filename} that AskMira has no field for"
            ),
            request=request,
        )
    else:
        # Not made, so their values are not written either.
        for row in chosen:
            for column in plan.extras:
                row.record.get("custom_fields", {}).pop(column.key, None)

    departments_created: list[str] = []
    taken = set(ws.department_codes)
    for key, name in sorted(plan.new_departments.items()):
        if not any(row.department and _key(row.department) == key for row in chosen):
            continue
        department = Department(id=uuid.uuid4(), tenant_id=user.tenant_id, name=name[:255],
                                code=_unique_code(name, taken))
        session.add(department)
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
            flush=False,
        )

    projects_created: list[str] = []
    taken = set(ws.project_codes)
    for key, name in sorted(plan.new_projects.items()):
        if not any(row.project and _key(row.project) == key for row in chosen):
            continue
        project = Project(
            id=uuid.uuid4(),
            tenant_id=user.tenant_id,
            name=name[:160],
            code=_unique_code(name, taken),
            # The company's own staff are not a client's billable work.
            billable=_key(name) != "internal",
            legal_entity_id=ws.only_entity,
        )
        session.add(project)
        ws.projects[key] = (project.id, project.name)
        projects_created.append(project.name)
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.CREATE,
            entity_type="project",
            entity_id=project.id,
            actor=user,
            changes={"name": project.name, "code": project.code, "source": filename},
            summary=f"Created project {project.code} while importing employees",
            request=request,
            flush=False,
        )

    locations_created: list[str] = []
    taken = set(ws.location_codes)
    for key, (name, state) in sorted(plan.new_locations.items()):
        users_of = [row for row in chosen if row.location and _key(row.location) == key]
        entity = next((e for e in (_entity_for(r, ws) for r in users_of) if e), None)
        if not users_of or entity is None:
            continue
        location = Location(
            id=uuid.uuid4(),
            tenant_id=user.tenant_id,
            legal_entity_id=entity,
            name=name[:255],
            code=_unique_code(name, taken),
            city=name[:128] if f.CITY_STATES.get(name.lower().strip()) else None,
            state_code=state,
            country_code=(f.country_of_place(name) or "IN") if not state else "IN",
        )
        session.add(location)
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
            flush=False,
        )

    # The departments, projects and locations made above, in one round trip,
    # before the people that point at them.
    await session.flush()

    employees: dict[str, Employee] = {}
    for row in chosen:
        record = dict(row.record)
        department = ws.departments.get(_key(row.department)) if row.department else None
        location = ws.locations.get(_key(row.location)) if row.location else None
        project = ws.projects.get(_key(row.project)) if row.project else None
        typed = row.statutory
        aadhaar_full = typed.get("aadhaar")
        employee = Employee(
            id=uuid.uuid4(),
            tenant_id=user.tenant_id,
            legal_entity_id=_entity_for(row, ws),
            department_id=department[0] if department else None,
            project_id=project[0] if project else None,
            location_id=location[0] if location else None,
            is_active=record["status"] is not EmploymentStatus.EXITED,
            pan=typed.get("pan"),
            uan=typed.get("uan"),
            pf_number=typed.get("pf_number"),
            esi_number=typed.get("esi_number"),
            aadhaar_number=aadhaar_full,
            aadhaar_last4=aadhaar_full[-4:] if aadhaar_full else None,
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

    prior_years = 0
    for row in chosen:
        employee = employees[row.code.upper()]  # type: ignore[union-attr]
        written = profile_service.add_imported_profile(session, user, employee, row.sections)
        if row.statutory:
            written.append("statutory")
        if row.bank is not None:
            session.add(
                EmployeeBankAccount(
                    tenant_id=user.tenant_id,
                    employee_id=employee.id,
                    account_number=encrypt_secret(row.bank["account_number"], user.tenant_id),
                    ifsc=row.bank["ifsc"],
                    account_holder_name=row.bank["account_holder_name"],
                    bank_name=row.bank["bank_name"],
                    is_primary=True,
                    verification_method=VerificationMethod.NONE,
                )
            )
            written.append("bank_account")
        for entry in row.prior_income:
            session.add(
                PreviousEmployerIncome(
                    tenant_id=user.tenant_id,
                    employee_id=employee.id,
                    financial_year=entry["financial_year"],
                    employer_name=entry.get("employer_name"),
                    gross_salary=entry.get("gross_salary") or 0,
                    tds_deducted=entry.get("tds_deducted") or 0,
                    pf_deducted=entry.get("pf_deducted") or 0,
                    professional_tax=entry.get("professional_tax") or 0,
                    # The old system's own figures, as it used them.
                    status=ProofStatus.VERIFIED,
                    verifier_user_id=user.id,
                    verifier_remarks=entry.get("remarks") or f"From {filename}",
                )
            )
            prior_years += 1
        if row.prior_income:
            written.append("previous_employer_income")
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
                # Which sections, never the values: identifiers and the
                # account number are not written to the audit log.
                "profile_sections": written,
                "issues": len(row.issues),
            },
            summary=f"Imported employee {employee.employee_code} from {filename}",
            request=request,
            # Written with everything else in one flush below: hundreds of
            # people, a handful of round trips to the database rather than
            # thousands (the live database is a network hop away).
            flush=False,
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
            "custom_fields_created": custom_fields_created,
            "previous_employer_years": prior_years,
            "with_issues": sum(1 for row in chosen if row.issues),
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
        projects_created=projects_created,
        locations_created=locations_created,
        custom_fields_created=custom_fields_created,
        with_issues=sum(1 for row in chosen if row.issues),
        rows=[_row_read(row) for row in plan.rows],
    )


async def issues(session: AsyncSession, user: User, data: bytes, filename: str) -> bytes:
    """Everybody the file has something to look at for, one line per point, as a
    workbook: values imported as typed that are not as AskMira would have them,
    values left out, and rows not added. Writes nothing."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    permissions.require_hr(user, detail="Only HR imports employees.")
    plan = await _plan(session, user, _read(data, filename))
    book = Workbook()
    sheet = book.active
    sheet.title = "Issues"
    sheet.append(["Emp Code", "Name", "Row", "Status", "Kind", "Issue"])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    # About the file's own values, before the import or after it (when every
    # row is already here): a row already in AskMira lists its points too.
    kinds = (("errors", "Not added"), ("issues", "Imported as typed"), ("warnings", "Left out"))
    for row in plan.rows:
        status = row.record.get("status")
        for attribute, kind in kinds:
            points = getattr(row, attribute)
            if attribute != "errors" and row.outcome == "error":
                points = []
            for point in points:
                sheet.append([
                    row.code, row.name, row.line,
                    status.value.replace("_", " ").capitalize() if status else None,
                    kind, point,
                ])
    if plan.notes:
        about = book.create_sheet("About the file")
        for note in plan.notes:
            about.append([note])
        about.column_dimensions["A"].width = 160
    columns = book.create_sheet("Columns not kept")
    columns.append(["Column", "Why"])
    for cell in columns[1]:
        cell.font = Font(bold=True)
    for item in plan.not_kept:
        columns.append([item.header, item.why])
    for target, widths in ((sheet, (14, 30, 7, 10, 18, 110)), (columns, (40, 90))):
        for index, width in enumerate(widths):
            target.column_dimensions[chr(65 + index)].width = width
    sheet.freeze_panes = "A2"
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()
