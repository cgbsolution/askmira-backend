"""The parts of an HR system's export that hold lists, and the columns AskMira has no field for.

factoHR's Employee Detail report puts a person's lists — qualifications,
family, nominees, emergency contacts, past employers' tax figures — in column
groups, one entry per row. A person with two qualifications takes two rows:
the second repeats only the Emp Code and Full Name and fills the group again
(a continuation row). `continuations` folds those rows into their person.

Every other column with something in it that no AskMira field takes is kept as
a custom field on the person's "factoHR" profile tab (`extra_columns`), so
nothing the old system held is lost. Left out, and listed with the reason:
columns the old system works out on the day of the export (age, length of
service), columns that say the same thing for every person, and columns that
repeat another column word for word.

Pure: no database. Values are read as typed; a value AskMira would check (a
date that is not a date) is kept as text rather than dropped.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.employee_import import fields as f
from app.services.employee_import.sheet import Column, Table

__all__ = [
    "EXTRA_TAB",
    "SECTION_GROUPS",
    "ExtraColumn",
    "NotKept",
    "Rows",
    "blocks",
    "continuations",
    "education",
    "emergency_contacts",
    "extra_columns",
    "extra_value",
    "family",
    "nominees",
    "prior_income",
    "section_leftovers",
    "unused_key",
]

#: The profile tab the kept columns appear on.
EXTRA_TAB = "factoHR"

#: Groups read as lists into profile sections; their columns are not custom fields.
SECTION_GROUPS = {
    "qualification detail": "education",
    "family detail": "family_members",
    "dependant detail": "nominees",
    "emergency contact detail": "emergency_contacts",
    "past employee history": "previous employer income",
    "salary master detail": "Salary master (uploaded separately)",
}

#: Worked out by the old system on the day of the export; stale the next day.
_CALCULATED = {
    "s#": "a row number",
    "age": "worked out from the birth date",
    "length of service": "worked out from the joining date",
    "total experience": "worked out from the joining date and past experience",
}

#: Columns of the address groups that belong to the address. factoHR's
#: headings are merged unevenly, so columns after the address ("Old Code")
#: would otherwise carry its group in their label.
_ADDRESS_PARTS = {
    "address",
    "address 2",
    "address 3",
    "country",
    "state",
    "city",
    "pincode",
    "city type",
}

Rows = list[list[Any]]


# ═══════════════════════════════════════════════════════════════════════════
# Continuation rows
# ═══════════════════════════════════════════════════════════════════════════


def continuations(table: Table) -> tuple[list[tuple[int, list[Any]]], dict[int, Rows]]:
    """The person rows, and each one's continuation rows by its line.

    A continuation row repeats the row above's Emp Code and has neither a
    first name nor a joining date: it only adds list entries.
    """
    code = table.mapping.get("employee_code")
    first = table.mapping.get("first_name")
    joining = table.mapping.get("date_of_joining")
    people: list[tuple[int, list[Any]]] = []
    extra: dict[int, Rows] = {}
    for line, cells in table.rows:
        if people and code is not None:
            here = f.text(cells[code])
            above = f.text(people[-1][1][code])
            bare = all(index is None or f.text(cells[index]) is None for index in (first, joining))
            if here and above and here.upper() == above.upper() and bare:
                extra.setdefault(people[-1][0], []).append(cells)
                continue
        people.append((line, cells))
    return people, extra


# ═══════════════════════════════════════════════════════════════════════════
# Groups
# ═══════════════════════════════════════════════════════════════════════════


def blocks(table: Table, group: str) -> list[dict[str, int]]:
    """The group's columns by name, one dict per repetition of the group.

    factoHR repeats "Emergency Contact Detail" for a second contact under the
    same heading; a name seen again starts the next block.
    """
    wanted = f.norm(group)
    # Asked for once per person per group: worked out once per file.
    cache: dict[str, list[dict[str, int]]] = table.__dict__.setdefault("_blocks", {})
    if wanted in cache:
        return cache[wanted]
    out: list[dict[str, int]] = []
    for column in table.columns:
        if f.norm(column.group) != wanted:
            continue
        name = f.norm(column.name)
        if not out or name in out[-1]:
            out.append({})
        out[-1][name] = column.index
    cache[wanted] = out
    return out


def _cell(cells: list[Any], block: dict[str, int], name: str) -> Any:
    index = block.get(name)
    return cells[index] if index is not None and index < len(cells) else None


def _text(cells: list[Any], block: dict[str, int], name: str) -> str | None:
    return f.text(_cell(cells, block, name))


def _date(cells: list[Any], block: dict[str, int], name: str) -> date | None:
    try:
        return f.read_date(_cell(cells, block, name))
    except ValueError:
        return None


def _yes(cells: list[Any], block: dict[str, int], name: str) -> bool | None:
    try:
        return f.read_yes(_cell(cells, block, name))
    except ValueError:
        return None


def _filled(cells: list[Any], block: dict[str, int]) -> bool:
    return any(f.text(cells[i]) is not None for i in block.values() if i < len(cells))


@dataclass
class Entries:
    """What a group gave: the entries, and notes on values kept differently."""

    items: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


_RELATIONSHIP_WORDS = {
    "spouse": "spouse",
    "wife": "spouse",
    "husband": "spouse",
    "son": "child",
    "daughter": "child",
    "child": "child",
    "father": "father",
    "mother": "mother",
    "brother": "sibling",
    "sister": "sibling",
    "sibling": "sibling",
    "father in law": "father_in_law",
    "mother in law": "mother_in_law",
    "grandfather": "grandparent",
    "grandmother": "grandparent",
    "friend": "friend",
}


def relationship(value: str | None) -> tuple[str, str | None]:
    """AskMira's relationship, and the source's word when it is not one of ours."""
    if value is None:
        return "other", None
    key = re.sub(r"[\s_\-]+", " ", value.lower()).strip()
    mapped = _RELATIONSHIP_WORDS.get(key)
    return (mapped, None) if mapped else ("other", value)


def _gender(value: str | None) -> str | None:
    try:
        return f.read_gender(value)
    except ValueError:
        return None


def _phone(value: str | None) -> str | None:
    try:
        return f.read_phone(value)
    except ValueError:
        return value


def family(table: Table, rows: Rows) -> Entries:
    out = Entries()
    for block in blocks(table, "Family Detail"):
        for cells in rows:
            name = _text(cells, block, "name")
            if not name:
                if _filled(cells, block):
                    out.notes.append("A family member with no name — left out.")
                continue
            relation, relation_text = relationship(_text(cells, block, "relation"))
            out.items.append(
                {
                    "name": name,
                    "relationship": relation,
                    "relationship_text": relation_text,
                    "title": _text(cells, block, "title"),
                    "date_of_birth": _date(cells, block, "date of birth"),
                    "gender": _gender(_text(cells, block, "gender")),
                    "occupation": _text(cells, block, "profession"),
                    "phone": _phone(_text(cells, block, "contact no")),
                    "blood_group": _text(cells, block, "blood group"),
                    "nationality": _text(cells, block, "nationality"),
                    "marital_status": _text(cells, block, "marital status"),
                    "marriage_date": _date(cells, block, "marriage date"),
                    "is_dependent": bool(_yes(cells, block, "dependant")),
                    "health_insurance": _yes(cells, block, "health insurance"),
                    "insurance_number": _text(cells, block, "insurance no"),
                    "remarks": _text(cells, block, "remarks"),
                }
            )
    return out


_NOMINATION = {
    "pf": ("pf", None),
    "provident fund": ("pf", None),
    "epf": ("pf", None),
    "eps": ("eps", None),
    "pension": ("eps", "Pension"),
    "gratuity": ("gratuity", None),
    "insurance": ("insurance", None),
    "gpa": ("insurance", "GPA"),
    "gmc": ("insurance", "GMC"),
    "mediclaim": ("insurance", "Mediclaim"),
    "sa": ("superannuation", "SA"),
    "superannuation": ("superannuation", None),
}


def nominees(table: Table, rows: Rows) -> Entries:
    out = Entries()
    for block in blocks(table, "Dependant Detail"):
        for cells in rows:
            name = _text(cells, block, "name")
            if not name:
                continue
            kind = _text(cells, block, "nominationtype") or _text(cells, block, "nomination type")
            purpose, purpose_text = _NOMINATION.get((kind or "").lower().strip(), ("other", kind))
            if purpose == "other":
                # Not a nomination AskMira keeps a list for: a dependant all the same.
                purpose, purpose_text = "insurance", kind or "Not stated"
            relation, relation_text = relationship(_text(cells, block, "relation"))
            remarks = [_text(cells, block, "remarks")]
            status = _text(cells, block, "status")
            if status:
                remarks.append(f"Status: {status}")
            out.items.append(
                {
                    "name": name,
                    "relationship": relation,
                    "relationship_text": relation_text,
                    "date_of_birth": _date(cells, block, "birth date"),
                    "address": _text(cells, block, "area"),
                    "city": _text(cells, block, "city"),
                    "country": _text(cells, block, "country"),
                    "phone": _phone(_text(cells, block, "phone")),
                    "purpose": purpose,
                    "purpose_text": purpose_text,
                    "share_percent": None,
                    "remarks": "; ".join(r for r in remarks if r) or None,
                }
            )
    if out.items:
        out.notes.append(
            "Nominee share not in the sheet — kept without one; enter it before printing "
            "Form 2 or Form F."
        )
    return out


def emergency_contacts(table: Table, rows: Rows) -> Entries:
    out = Entries()
    for block in blocks(table, "Emergency Contact Detail"):
        for cells in rows:
            name = _text(cells, block, "name")
            if not name:
                continue
            relation, relation_text = relationship(_text(cells, block, "relationship"))
            phone = _phone(_text(cells, block, "mobile 1"))
            out.items.append(
                {
                    "name": name,
                    "relationship": relation,
                    "relationship_text": relation_text,
                    "phone": phone,
                    "alternate_phone": _phone(_text(cells, block, "mobile 2")),
                    "landline": _phone(_text(cells, block, "land line no")),
                    "address": _text(cells, block, "address"),
                    "is_primary": not out.items,
                }
            )
            if not phone:
                out.notes.append(f"Emergency contact {name} has no mobile number.")
    return out


#: factoHR's course names to AskMira's levels; the course itself is kept as typed.
_LEVELS = {
    "post graduate": "postgraduate",
    "postgraduate": "postgraduate",
    "pg": "postgraduate",
    "masters": "postgraduate",
    "graduate": "graduate",
    "graduation": "graduate",
    "ug": "graduate",
    "diploma": "diploma",
    "post diploma": "diploma",
    "doctorate": "doctorate",
    "phd": "doctorate",
    "certificate course": "certification",
    "certificate": "certification",
    "certification": "certification",
    "12th": "higher_secondary",
    "hsc": "higher_secondary",
    "higher secondary": "higher_secondary",
    "10th": "secondary",
    "ssc": "secondary",
    "secondary": "secondary",
}


def education(table: Table, rows: Rows) -> Entries:
    out = Entries()
    for block in blocks(table, "Qualification Detail"):
        for cells in rows:
            qualification = _text(cells, block, "qualifications")
            if not qualification:
                if _filled(cells, block):
                    out.notes.append("A qualification with no name — left out.")
                continue
            course = _text(cells, block, "course")
            started, ended = _date(cells, block, "from date"), _date(cells, block, "to date")
            out.items.append(
                {
                    "level": _LEVELS.get((course or "").lower().strip(), "other"),
                    "qualification": qualification,
                    "course": course,
                    "stream": _text(cells, block, "stream"),
                    "specialisation": _text(cells, block, "specialization"),
                    "institution": _text(cells, block, "institute"),
                    "board_or_university": _text(cells, block, "university"),
                    "nature_of_course": _text(cells, block, "nature of course"),
                    "status": _text(cells, block, "status"),
                    "from_date": started,
                    "to_date": ended,
                    "start_year": started.year if started else None,
                    "end_year": ended.year if ended else None,
                    "year": _text(cells, block, "year"),
                    "date_of_passing": _date(cells, block, "date of passing"),
                    "percentage": _text(cells, block, "percentage"),
                    "grade": _text(cells, block, "grade"),
                    "duration": _text(cells, block, "duration"),
                    "is_highest": _yes(cells, block, "is highest qualification"),
                    "remarks": _text(cells, block, "remarks"),
                }
            )
    return out


# ── Previous employers' income (factoHR's "Past Employee History") ─────────

#: Column -> PreviousEmployerIncome field. Columns with no field are listed when non-zero.
_INCOME = {
    "income after exemption": "gross_salary",
    "tds / tax paid": "tds_deducted",
    "pf amount tranfered": "pf_deducted",
    "pf amount transferred": "pf_deducted",
    "pro. tax": "professional_tax",
}


def _money(value: Any) -> Decimal | None:
    text = f.text(value)
    if text is None:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _fy(text: str | None) -> str | None:
    """factoHR's "2024-2025" as AskMira writes it: "2024-25"."""
    match = re.fullmatch(r"(\d{4})\s*-\s*(\d{2}|\d{4})", text or "")
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)[-2:]}"


def prior_income(table: Table, rows: Rows) -> Entries:
    """One entry per financial year that has any amount in it; all-zero years
    say nothing a missing entry does not, and are counted in the notes."""
    out = Entries()
    zero = 0
    for block in blocks(table, "Past Employee History"):
        for cells in rows:
            year = _fy(_text(cells, block, "financial year"))
            amounts = {
                name: _money(_cell(cells, block, name))
                for name in block
                if name not in {"financial year"} and _money(_cell(cells, block, name)) is not None
            }
            if not year:
                continue
            if not any(amounts.values()):
                zero += 1
                continue
            entry: dict[str, Any] = {"financial_year": year, "employer_name": None}
            unplaced = []
            for name, value in amounts.items():
                target = _INCOME.get(name)
                if target:
                    entry[target] = value
                elif value:
                    unplaced.append(f"{name}: {value}")
            if unplaced:
                entry["remarks"] = "From factoHR — " + "; ".join(unplaced)
            out.items.append(entry)
    if zero:
        out.notes.append(f"{zero} past-employer year(s) with every amount 0 — nothing to record.")
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Columns kept as custom fields
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class ExtraColumn:
    index: int
    header: str
    key: str
    label: str
    field_type: str
    #: Identity-document numbers: hidden from the person's own view, like other
    #: sensitive custom fields.
    sensitive: bool
    filled: int
    #: Kept only where it differs from this column (by index): the column the
    #: record took its value from instead ("Leaving Date" beside "Last working date").
    differs_from: int | None = None


@dataclass
class NotKept:
    header: str
    why: str


def _label(column: Column) -> str:
    group = column.group
    if (
        group
        and f.norm(group) in {"local address", "permanent address"}
        and (f.norm(column.name) not in _ADDRESS_PARTS)
    ):
        group = None
    if group and column.repeated:
        return f"{group}{f.GROUP_SEP}{column.name}"
    return column.name


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _kind(values: list[Any]) -> str:
    if values and all(isinstance(v, datetime | date) for v in values):
        return "date"
    if values and all(
        isinstance(v, bool) or str(v).strip().lower() in {"true", "false", "yes", "no"}
        for v in values
    ):
        return "boolean"
    return "text"


#: Identifier fields: a column repeating one of them is not kept as plain text.
_IDENTIFIERS = ("pan", "aadhaar", "uan", "pf_number", "esi_number", "account_number")

_SENSITIVE_WORDS = (
    "dl no",
    "passport no",
    "ec no",
    "enrolment no",
    "resident card",
    "civil no",
    "insurance no",
)


def extra_value(column: ExtraColumn, raw: Any) -> Any:
    """A kept column's cell in its custom field's stored shape; None when empty."""
    if column.field_type == "date":
        if isinstance(raw, datetime):
            return raw.date().isoformat()
        if isinstance(raw, date):
            return raw.isoformat()
        return None
    if column.field_type == "boolean":
        if isinstance(raw, bool):
            return raw
        text = f.text(raw)
        return None if text is None else text.lower() in {"true", "yes"}
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    text = f.text(raw)
    if text is None:
        return None
    return text[:2000]


#: Columns of each list group that its reader takes; the rest are reported.
CONSUMED = {
    "family detail": {
        "name",
        "gender",
        "relation",
        "date of birth",
        "blood group",
        "contact no",
        "profession",
        "nationality",
        "dependant",
        "title",
        "marital status",
        "marriage date",
        "insurance no",
        "health insurance",
        "remarks",
    },
    "dependant detail": {
        "name",
        "nominationtype",
        "nomination type",
        "area",
        "city",
        "country",
        "relation",
        "phone",
        "birth date",
        "remarks",
        "status",
    },
    "emergency contact detail": {
        "name",
        "relationship",
        "mobile 1",
        "mobile 2",
        "address",
        "land line no",
    },
    "qualification detail": {
        "year",
        "qualifications",
        "stream",
        "course",
        "specialization",
        "nature of course",
        "status",
        "institute",
        "university",
        "from date",
        "to date",
        "date of passing",
        "percentage",
        "grade",
        "duration",
        "is highest qualification",
        "remarks",
    },
    "past employee history": {"financial year", *_INCOME},
}


def section_leftovers(table: Table, rows: Rows) -> list[NotKept]:
    """Columns of the list groups with something in them that no reader takes."""
    out: list[NotKept] = []
    for column in table.columns:
        group = f.norm(column.group) if column.group else ""
        if group not in CONSUMED or f.norm(column.name) in CONSUMED[group]:
            continue
        values = Counter(
            str(f.text(cells[column.index]))
            for cells in rows
            if column.index < len(cells) and f.text(cells[column.index]) is not None
        )
        if not values:
            continue
        header = f"{column.group}{f.GROUP_SEP}{column.name}"
        shown = ", ".join(f"{value} ({count})" for value, count in values.most_common(4))
        if f.norm(column.name) == "attachment available":
            why = (
                f"Whether factoHR holds a scanned certificate: {shown}. The files are not in "
                "the sheet; upload them under Documents."
            )
        elif group == "past employee history":
            why = f"No AskMira field for it; every value is 0 or empty: {shown}."
            if any(_amount(v) for v in values):
                why = f"Kept in the year's remarks where not 0: {shown}."
        elif len(values) == 1:
            why = f"factoHR's own flag, the same on every entry: {shown}."
        else:
            why = f"No AskMira field for it: {shown}."
        out.append(NotKept(header, why))
    return out


def _amount(value: str) -> Decimal:
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation:
        return Decimal(0)


def unused_key(base: str, taken: set[str], extras: list[ExtraColumn]) -> str:
    """`base`, or `base_2`…, not a key the workspace or this file already uses."""
    used = set(taken) | {extra.key for extra in extras}
    key, n = base, 2
    while key in used:
        key = f"{base[:60]}_{n}"
        n += 1
    return key


def extra_columns(
    table: Table, people: list[tuple[int, list[Any]]], taken_keys: set[str]
) -> tuple[list[ExtraColumn], list[NotKept]]:
    """The unclaimed columns to keep as custom fields, and those not kept, with why."""
    kept: list[ExtraColumn] = []
    not_kept: list[NotKept] = []
    mapped = [table.columns_by_index[i] for i in table.mapping.values()]
    keys = set(taken_keys)
    for column in table.unused:
        group = f.norm(column.group) if column.group else ""
        values = [
            cells[column.index]
            for _, cells in people
            if column.index < len(cells) and f.text(cells[column.index]) is not None
        ]
        if group in SECTION_GROUPS:
            continue  # read as a list, or uploaded separately — reported by the caller
        if not values:
            continue
        name = f.norm(column.name)
        if name in _CALCULATED and not column.group:
            not_kept.append(NotKept(column.header, f"Calculated by factoHR: {_CALCULATED[name]}."))
            continue
        texts = Counter(str(f.text(v)) for v in values)
        if len(values) == len(people) and len(texts) == 1:
            only = next(iter(texts))
            not_kept.append(NotKept(column.header, f"Every person has the same value: {only}."))
            continue
        same_as = next(
            (
                other.header
                for other in mapped + [table.columns_by_index[k.index] for k in kept]
                if all(
                    f.text(cells[column.index]) == f.text(cells[other.index]) for _, cells in people
                )
            ),
            None,
        )
        if same_as is not None:
            not_kept.append(NotKept(column.header, f"Same as the {same_as} column for everyone."))
            continue
        # A second copy of an identifier: never kept in the clear when the
        # number itself is stored (encrypted) from its own column.
        copy_of = next(
            (
                table.columns_by_index[table.mapping[key]].header
                for key in _IDENTIFIERS
                if key in table.mapping
                and all(
                    f.text(cells[column.index]) in (None, f.text(cells[table.mapping[key]]))
                    for _, cells in people
                )
            ),
            None,
        )
        if copy_of is not None:
            not_kept.append(
                NotKept(column.header, f"Same as the {copy_of} column wherever it is filled.")
            )
            continue
        label = _label(column)
        base = ("fh_" + _slug(label))[:64].rstrip("_")
        key, n = base, 2
        while key in keys:
            key = f"{base[:60]}_{n}"
            n += 1
        keys.add(key)
        kept.append(
            ExtraColumn(
                index=column.index,
                header=column.header,
                key=key,
                label=label[:255],
                field_type=_kind(values),
                sensitive=any(word in name for word in _SENSITIVE_WORDS),
                filled=len(values),
            )
        )
    return kept, not_kept
