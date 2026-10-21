"""Incremental export, and the audit that makes a bulk egress path accountable.

═══════════════════════════════════════════════════════════════════════════════
EVERY EXPORT IS AUDITED WITH ITS FIELD LIST, NOT JUST ITS ROW COUNT
═══════════════════════════════════════════════════════════════════════════════

A BI connector is the largest quantity of personal data that leaves this system,
on a schedule, unattended. The question asked after an incident is never "how
many rows" — it is **"did salary leave the building"**, and only the field list
answers that.

So `plan_export` produces the field list before anything runs, `ExportPlan` is
what the audit row is written from, and an export with no plan is not possible:
the row count and the fields come from the same object.

═══════════════════════════════════════════════════════════════════════════════
INCREMENTAL, BECAUSE A NIGHTLY FULL DUMP IS A NIGHTLY FULL BREACH SURFACE
═══════════════════════════════════════════════════════════════════════════════

Re-exporting every employee every night multiplies the amount of personal data
in flight by the number of nights, for no benefit. `since` is the watermark and
`next_watermark` is what the caller stores.

The watermark advances to the **maximum change key actually seen**, not to
"now". Advancing to now would skip any row written during the export itself,
and that row is then never exported again — a silent, permanent gap that shows
up as one employee missing from a client's dashboard forever.

═══════════════════════════════════════════════════════════════════════════════
AND A FULL EXPORT IS A DELIBERATE ACT
═══════════════════════════════════════════════════════════════════════════════

Sometimes genuinely needed — a client rebuilding a warehouse. It is available
and it says what it is, so the audit trail distinguishes "the nightly delta" from
"somebody took everything".

Pure. Watermarks and rows are supplied.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services.bi.exposed_schema import (
    SCHEMA_VERSION,
    ExposedField,
    exposed_entity,
    visible_columns,
)

__all__ = [
    "ExportError",
    "ExportKind",
    "ExportPlan",
    "audit_payload",
    "extract_rows",
    "plan_export",
]


class ExportError(ValueError):
    """An export this module refuses."""


class ExportKind(str, enum.Enum):
    #: Rows changed since the watermark. The normal path.
    INCREMENTAL = "incremental"
    #: Everything. Genuinely needed when a client rebuilds a warehouse, and
    #: distinguished in the audit so "the nightly delta" and "somebody took
    #: everything" do not look the same afterwards.
    FULL = "full"


class Destination(str, enum.Enum):
    #: A read-only Postgres role scoped by views.
    POSTGRES_ROLE = "postgres_role"
    #: Scheduled Parquet or CSV to the client's own storage.
    OBJECT_STORAGE = "object_storage"


@dataclass(frozen=True, slots=True)
class ExportPlan:
    """What will leave, before it leaves.

    The audit row is written from this object, so the field list and the row
    count cannot disagree - they come from the same place.
    """

    entity_name: str
    kind: ExportKind
    destination: Destination
    destination_ref: str
    role: str
    fields: tuple[ExposedField, ...]
    since: datetime | None
    schema_version: str
    withheld_fields: tuple[str, ...]

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    @property
    def carries_payroll_data(self) -> bool:
        """Surfaced separately because it is the question actually asked.

        "Did salary leave the building" is answered by a boolean at the top of
        the audit row, not by somebody reading forty field names.
        """
        return any(f.sensitivity.value == "payroll_only" for f in self.fields)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity_name,
            "kind": self.kind.value,
            "destination": self.destination.value,
            "destination_ref": self.destination_ref,
            "role": self.role,
            "schema_version": self.schema_version,
            "since": self.since.isoformat() if self.since else None,
            "fields": list(self.field_names),
            "field_count": len(self.fields),
            "withheld_fields": list(self.withheld_fields),
            "carries_payroll_data": self.carries_payroll_data,
        }


def plan_export(
    *,
    entity_name: str,
    role: str,
    kind: ExportKind,
    destination: Destination,
    destination_ref: str,
    since: datetime | None = None,
    requested_fields: list[str] | None = None,
) -> ExportPlan:
    """Decide what will leave, and refuse what must not.

    Field-level sensitivity is the report builder's, applied here. A field the
    caller asked for and may not see is **withheld and named** rather than
    silently dropped: a client whose dashboard has an empty column will ask, and
    "you do not have access to that field" is a better answer than a column that
    quietly stopped populating.
    """
    entity = exposed_entity(entity_name)
    allowed = visible_columns(entity_name, role)
    allowed_names = {f.name for f in allowed}

    if kind is ExportKind.INCREMENTAL and since is None:
        raise ExportError(
            "An incremental export needs a watermark. Without one it is a full "
            "export wearing an incremental label, and the audit trail then "
            "cannot distinguish the nightly delta from somebody taking "
            "everything."
        )
    if kind is ExportKind.FULL and since is not None:
        raise ExportError(
            "A full export does not take a watermark. Passing one suggests it "
            "will be honoured, and it will not."
        )
    if not destination_ref.strip():
        raise ExportError(
            "An export needs a destination reference - the role name or the "
            "bucket. 'To where' is half of what the audit row is for."
        )

    if requested_fields is None:
        selected = tuple(allowed)
        withheld: tuple[str, ...] = ()
    else:
        unknown = [name for name in requested_fields if name not in {f.name for f in entity.fields}]
        if unknown:
            raise ExportError(
                f"{unknown} are not columns of {entity_name}. The connector "
                "serves the published schema only; a name that is not in it "
                "never becomes part of a query."
            )
        selected = tuple(f for f in allowed if f.name in set(requested_fields))
        withheld = tuple(sorted(set(requested_fields) - allowed_names))

    if not selected:
        raise ExportError(
            f"This role may see none of the requested columns of "
            f"{entity_name}. An export of zero fields is a scheduled job that "
            "produces an empty file every night and looks like it is working."
        )

    return ExportPlan(
        entity_name=entity_name,
        kind=kind,
        destination=destination,
        destination_ref=destination_ref.strip(),
        role=role,
        fields=selected,
        since=since,
        schema_version=SCHEMA_VERSION,
        withheld_fields=withheld,
    )


@dataclass(frozen=True, slots=True)
class ExtractResult:
    """What actually left, and the watermark to store."""

    plan: ExportPlan
    rows: tuple[dict[str, Any], ...]
    row_count: int
    next_watermark: datetime | None
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.as_dict(),
            "row_count": self.row_count,
            "next_watermark": (self.next_watermark.isoformat() if self.next_watermark else None),
            "note": self.note,
        }


def extract_rows(
    plan: ExportPlan,
    *,
    source_rows: list[dict[str, Any]],
    change_key: str = "updated_at",
) -> ExtractResult:
    """Project the source rows onto the exposed field list.

    Two things happen here and both matter. The rows are **filtered to the
    planned fields**, so a source row carrying a salary cannot leak through an
    export that was not entitled to it. And the watermark advances to the
    largest change key actually seen, never to "now" - advancing to now skips
    any row written during the export, and that row is then never exported
    again.
    """
    if change_key not in {"updated_at", "created_at"}:
        raise ExportError(
            f"{change_key!r} is not a change key. Tracking changes by a column "
            "that does not move on update produces an incremental export that "
            "silently stops carrying edits."
        )

    selected = [
        row
        for row in source_rows
        if plan.kind is ExportKind.FULL
        or (
            plan.since is not None
            and row.get(change_key) is not None
            and row[change_key] > plan.since
        )
    ]

    projected = tuple({name: row.get(name) for name in plan.field_names} for row in selected)
    seen = [row[change_key] for row in selected if row.get(change_key) is not None]
    watermark = max(seen) if seen else plan.since

    return ExtractResult(
        plan=plan,
        rows=projected,
        row_count=len(projected),
        next_watermark=watermark,
        note=(
            f"{len(projected)} row(s) across {len(plan.fields)} field(s). The "
            "watermark advances to the largest change key actually seen, not to "
            "now - advancing to now would skip a row written during the export "
            "and that row would never be exported again."
            if plan.kind is ExportKind.INCREMENTAL
            else f"{len(projected)} row(s) across {len(plan.fields)} field(s). "
            "**Full export** - everything, not a delta."
        ),
    )


def audit_payload(result: ExtractResult, *, at: datetime) -> dict[str, Any]:
    """The audit row for one export, with the field list in it.

    "How many rows" is not the question asked after an incident. "Did salary
    leave the building" is, and only the field list answers it.
    """
    plan = result.plan
    return {
        "at": at.isoformat(),
        "entity": plan.entity_name,
        "kind": plan.kind.value,
        "destination": plan.destination.value,
        "destination_ref": plan.destination_ref,
        "role": plan.role,
        "schema_version": plan.schema_version,
        "since": plan.since.isoformat() if plan.since else None,
        "next_watermark": (result.next_watermark.isoformat() if result.next_watermark else None),
        "row_count": result.row_count,
        "fields": list(plan.field_names),
        "field_count": len(plan.fields),
        "withheld_fields": list(plan.withheld_fields),
        "carries_payroll_data": plan.carries_payroll_data,
        "note": (
            "A BI connector is a bulk egress path. The field list is recorded "
            "because the question after an incident is what left, not how much."
        ),
    }
