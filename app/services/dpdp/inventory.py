"""The data inventory and the Record of Processing Activities, generated.

The RoPA a Data Fiduciary has to be able to produce is a document describing
what personal data is processed, for what purposes, on what basis, shared with
whom, and kept how long. Written by hand it is a snapshot of a system that has
since changed. Generated from the classification it is a view of the system as
it is right now, and the classification cannot fall behind the schema because a
test fails when it does.

This module takes a plain `{table: (column, ...)}` mapping — not SQLAlchemy
metadata. Reflection happens in the caller. Two reasons: the engine stays
testable against a five-table fixture, and the invariant that checks
classification coverage calls the *same* function the RoPA is built from,
rather than a parallel implementation that could quietly disagree with it.

Pure.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.services.dpdp.categories import (
    CATEGORIES,
    DataCategory,
    LawfulBasis,
    PrincipalKind,
    category,
)
from app.services.dpdp.consent import PURPOSES, Purpose
from app.services.dpdp.schema_map import (
    EXEMPT_COLUMNS,
    STRUCTURAL_COLUMNS,
    TABLES,
    is_direct_identifier,
)

__all__ = [
    "ClassifiedColumn",
    "CoverageGap",
    "GapKind",
    "Inventory",
    "ProcessingActivity",
    "build_inventory",
    "coverage_gaps",
    "record_of_processing",
]

#: A table→columns mapping. Named so signatures read as what they are rather
#: than as a nest of brackets.
SchemaShape = Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class ClassifiedColumn:
    """One column, with the category it resolved to and how."""

    table: str
    column: str
    category_key: str
    principal: PrincipalKind
    is_direct_identifier: bool
    #: True where the column was named explicitly rather than inheriting the
    #: table default. Surfaced because "how many of these were actually looked
    #: at" is a fair question to ask of any generated inventory.
    explicitly_classified: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "column": self.column,
            "category": self.category_key,
            "principal": self.principal.value,
            "is_direct_identifier": self.is_direct_identifier,
            "explicitly_classified": self.explicitly_classified,
        }


class GapKind(str, enum.Enum):
    """The ways the classification and the schema can disagree.

    Each is a distinct failure with a distinct fix, so the build error can say
    what to do instead of only that something is wrong.
    """

    #: A table in the schema that nobody classified. Almost always a new slice.
    UNCLASSIFIED_TABLE = "unclassified_table"
    #: A table in the map that no longer exists. Almost always a dropped model.
    STALE_TABLE = "stale_table"
    #: A column in a personal-data table with no category — only possible where
    #: the table declares a principal but no default category.
    UNCLASSIFIED_COLUMN = "unclassified_column"
    #: A direct identifier inheriting a table default instead of being named.
    #: The one that matters most: this is how an Aadhaar number ends up filed
    #: in the RoPA under "organisational placement".
    IMPLICIT_DIRECT_IDENTIFIER = "implicit_direct_identifier"
    #: A direct identifier in a table declared to hold no personal data. Either
    #: the table declaration is wrong or the column needs a written exemption.
    IDENTIFIER_IN_NON_PERSONAL_TABLE = "identifier_in_non_personal_table"
    #: An exemption for a column that no longer exists, or that no longer reads
    #: as an identifier. Dead exemptions accumulate into a place to hide things.
    STALE_EXEMPTION = "stale_exemption"
    #: The map explicitly classifies a column the table does not have. Found by
    #: this check on the first run, on twelve columns of `employees` alone — a
    #: classification naming `blood_group` where the schema says `blood_group`
    #: was never built reads, to a reviewer, exactly like a classification that
    #: covers it. Silent and wrong is the failure mode this whole file exists to
    #: prevent, so it fails the build like any other gap.
    STALE_COLUMN_CLASSIFICATION = "stale_column_classification"
    #: A category or purpose key naming something the catalogue does not define.
    UNKNOWN_CATEGORY = "unknown_category"


@dataclass(frozen=True, slots=True)
class CoverageGap:
    """One disagreement between the schema and its classification."""

    kind: GapKind
    table: str
    column: str | None
    detail: str

    def __str__(self) -> str:
        where = f"{self.table}.{self.column}" if self.column else self.table
        return f"[{self.kind.value}] {where}: {self.detail}"


def coverage_gaps(schema: SchemaShape) -> list[CoverageGap]:
    """Every way the classification fails to describe this schema.

    This is the build gate. It returns gaps rather than raising so a test can
    print all of them at once — being told about one missing table, fixing it,
    and being told about the next is a bad way to spend an afternoon.
    """
    gaps: list[CoverageGap] = []

    for table_name in sorted(set(schema) - set(TABLES)):
        gaps.append(
            CoverageGap(
                kind=GapKind.UNCLASSIFIED_TABLE,
                table=table_name,
                column=None,
                detail=(
                    "In the schema, not in schema_map.TABLES. Add it with a "
                    "principal and a default category, or with _none() and a "
                    "reason saying why it holds no personal data."
                ),
            )
        )

    for table_name in sorted(set(TABLES) - set(schema)):
        gaps.append(
            CoverageGap(
                kind=GapKind.STALE_TABLE,
                table=table_name,
                column=None,
                detail=(
                    "Classified in schema_map.TABLES but no longer in the schema. Remove the entry."
                ),
            )
        )

    known_categories = set(CATEGORIES)

    for table_name, columns in sorted(schema.items()):
        classification = TABLES.get(table_name)
        if classification is None:
            continue

        for key in {classification.default_category, *classification.columns.values()}:
            if key is not None and key not in known_categories:
                gaps.append(
                    CoverageGap(
                        kind=GapKind.UNKNOWN_CATEGORY,
                        table=table_name,
                        column=None,
                        detail=(
                            f"names category {key!r}, which categories.CATEGORIES does not define."
                        ),
                    )
                )

        for column in columns:
            if column in STRUCTURAL_COLUMNS:
                continue

            direct = is_direct_identifier(column)
            exempt = (table_name, column) in EXEMPT_COLUMNS
            named = column in classification.columns

            if classification.principal is None:
                if direct and not exempt and not named:
                    gaps.append(
                        CoverageGap(
                            kind=GapKind.IDENTIFIER_IN_NON_PERSONAL_TABLE,
                            table=table_name,
                            column=column,
                            detail=(
                                "reads as a direct identifier, in a table "
                                "declared to hold no personal data. Either the "
                                "table declaration is wrong, or add an "
                                "EXEMPT_COLUMNS entry saying why this one is not "
                                "about a person."
                            ),
                        )
                    )
                continue

            if direct and not named and not exempt:
                gaps.append(
                    CoverageGap(
                        kind=GapKind.IMPLICIT_DIRECT_IDENTIFIER,
                        table=table_name,
                        column=column,
                        detail=(
                            "reads as a direct identifier and is inheriting the "
                            f"table default {classification.default_category!r}. "
                            "Name it explicitly in the table's columns map so the "
                            "RoPA files it under the right category."
                        ),
                    )
                )
            elif classification.category_for(column) is None:
                gaps.append(
                    CoverageGap(
                        kind=GapKind.UNCLASSIFIED_COLUMN,
                        table=table_name,
                        column=column,
                        detail=(
                            "no category: the table declares a principal but no "
                            "default category, and this column is not named."
                        ),
                    )
                )

    for table_name, classification in sorted(TABLES.items()):
        columns = schema.get(table_name)
        if columns is None:
            continue  # Already reported as a stale table.
        for column in sorted(set(classification.columns) - set(columns)):
            gaps.append(
                CoverageGap(
                    kind=GapKind.STALE_COLUMN_CLASSIFICATION,
                    table=table_name,
                    column=column,
                    detail=(
                        "classified explicitly, but the table has no such "
                        "column. Remove it — a classification for a column that "
                        "does not exist reads to a reviewer exactly like one "
                        "that does."
                    ),
                )
            )

    for (table_name, column), _reason in sorted(EXEMPT_COLUMNS.items()):
        columns = schema.get(table_name)
        if columns is None or column not in columns:
            gaps.append(
                CoverageGap(
                    kind=GapKind.STALE_EXEMPTION,
                    table=table_name,
                    column=column,
                    detail="exempted, but no such column in the schema.",
                )
            )
        elif not is_direct_identifier(column):
            gaps.append(
                CoverageGap(
                    kind=GapKind.STALE_EXEMPTION,
                    table=table_name,
                    column=column,
                    detail=(
                        "exempted, but its name no longer reads as a direct "
                        "identifier, so the exemption does nothing. Remove it — "
                        "a list of dead exemptions is where a live one hides."
                    ),
                )
            )

    return gaps


@dataclass(frozen=True, slots=True)
class Inventory:
    """Everything personal in the schema, resolved."""

    columns: tuple[ClassifiedColumn, ...]
    categories_present: tuple[DataCategory, ...]
    #: Categories the catalogue declares that no column resolves to. Reported
    #: rather than hidden: a compliance surface claiming we process health data
    #: when the module holding it was never built is inventing a capability, and
    #: a RoPA that overstates is as wrong as one that understates. Today these
    #: are the modules later slices bring — AI interactions, background checks,
    #: disciplinary records — and saying so is more useful than a clean list.
    categories_declared_but_absent: tuple[DataCategory, ...]
    tables_holding_personal_data: int
    tables_not_personal: int
    generated_from_tables: int

    def by_category(self) -> dict[str, list[ClassifiedColumn]]:
        out: dict[str, list[ClassifiedColumn]] = {}
        for column in self.columns:
            out.setdefault(column.category_key, []).append(column)
        return out

    def as_dict(self) -> dict[str, Any]:
        grouped = self.by_category()
        return {
            "generated_from_tables": self.generated_from_tables,
            "tables_holding_personal_data": self.tables_holding_personal_data,
            "tables_not_personal": self.tables_not_personal,
            "personal_data_columns": len(self.columns),
            "direct_identifiers": sum(1 for c in self.columns if c.is_direct_identifier),
            "categories_declared_but_absent": [
                {"key": c.key, "name": c.name} for c in self.categories_declared_but_absent
            ],
            "categories": [
                {
                    **cat.as_dict(),
                    "column_count": len(grouped.get(cat.key, ())),
                    "tables": sorted({c.table for c in grouped.get(cat.key, ())}),
                }
                for cat in self.categories_present
            ],
        }


def build_inventory(schema: SchemaShape) -> Inventory:
    """Resolve every column in the schema to its category.

    Silently skips tables it cannot classify. That is deliberate and safe only
    because `coverage_gaps` runs as a build gate: the inventory is allowed to
    describe what it knows, and the *test* is what guarantees it knows
    everything. An inventory that raised here would take the application down
    for a classification omission, which is the wrong place to find out.
    """
    columns: list[ClassifiedColumn] = []
    personal_tables = 0
    non_personal_tables = 0

    for table_name, table_columns in sorted(schema.items()):
        classification = TABLES.get(table_name)
        if classification is None:
            continue
        if classification.principal is None:
            non_personal_tables += 1
            continue

        personal_tables += 1
        for column in table_columns:
            if column in STRUCTURAL_COLUMNS:
                continue
            if (table_name, column) in EXEMPT_COLUMNS:
                continue
            category_key = classification.category_for(column)
            if category_key is None or category_key not in CATEGORIES:
                continue
            columns.append(
                ClassifiedColumn(
                    table=table_name,
                    column=column,
                    category_key=category_key,
                    principal=classification.principal,
                    is_direct_identifier=is_direct_identifier(column),
                    explicitly_classified=column in classification.columns,
                )
            )

    present = {c.category_key for c in columns}
    return Inventory(
        columns=tuple(columns),
        categories_present=tuple(CATEGORIES[key] for key in CATEGORIES if key in present),
        categories_declared_but_absent=tuple(
            CATEGORIES[key] for key in CATEGORIES if key not in present
        ),
        tables_holding_personal_data=personal_tables,
        tables_not_personal=non_personal_tables,
        generated_from_tables=len(schema),
    )


@dataclass(frozen=True, slots=True)
class ProcessingActivity:
    """One row of the Record of Processing Activities.

    Organised by purpose rather than by table, because that is the question the
    document answers: not "what is in your database" but "what are you doing
    with people's data, and why are you allowed to".
    """

    purpose: Purpose
    categories: tuple[DataCategory, ...]
    principals: tuple[PrincipalKind, ...]
    tables: tuple[str, ...]
    column_count: int
    #: The longest retention among the categories this purpose touches. The
    #: binding one — data is not deleted while any purpose still needs it.
    max_retention_months: int
    statutory_retentions: tuple[str, ...]
    sub_processors: tuple[str, ...]

    @property
    def is_withdrawable(self) -> bool:
        return self.purpose.lawful_basis.is_withdrawable

    def as_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose.key,
            "purpose_name": self.purpose.name,
            "description": self.purpose.description,
            "lawful_basis": self.purpose.lawful_basis.value,
            "lawful_basis_label": self.purpose.lawful_basis.label,
            "can_be_withdrawn": self.is_withdrawable,
            "data_categories": [c.key for c in self.categories],
            "data_category_names": [c.name for c in self.categories],
            "data_principals": [p.value for p in self.principals],
            "source_tables": list(self.tables),
            "column_count": self.column_count,
            "max_retention_months": self.max_retention_months,
            "statutory_retentions": list(self.statutory_retentions),
            "sub_processors": list(self.sub_processors),
        }


def record_of_processing(schema: SchemaShape) -> list[ProcessingActivity]:
    """The RoPA, built from the classification and the purpose catalogue.

    A purpose with no classified columns behind it still appears, with a count
    of zero. That is not noise — it is the most interesting row in the document,
    because a purpose we declared and hold no data for is either a purpose we
    stopped serving or a claim in the privacy notice that is no longer true.
    """
    inventory = build_inventory(schema)
    grouped = inventory.by_category()

    activities: list[ProcessingActivity] = []
    for spec in PURPOSES.values():
        categories = tuple(category(key) for key in spec.data_categories if key in CATEGORIES)
        relevant = [c for key in spec.data_categories for c in grouped.get(key, ())]
        retentions = [cat.retention for cat in categories]

        activities.append(
            ProcessingActivity(
                purpose=spec,
                categories=categories,
                principals=tuple(
                    dict.fromkeys(kind for cat in categories for kind in cat.principal_kinds)
                ),
                tables=tuple(sorted({c.table for c in relevant})),
                column_count=len(relevant),
                max_retention_months=max((r.months for r in retentions), default=0),
                statutory_retentions=tuple(
                    dict.fromkeys(r.statutory_reference for r in retentions if r.is_statutory)
                ),
                sub_processors=spec.sub_processors,
            )
        )
    return activities


def lawful_basis_breakdown(schema: SchemaShape) -> dict[str, int]:
    """How many purposes rest on each basis.

    One number worth watching: if `consent` climbs toward the total, somebody
    has started using consent as the answer to every question, which is the
    failure this whole module is built to prevent.
    """
    counts = dict.fromkeys((b.value for b in LawfulBasis), 0)
    for activity in record_of_processing(schema):
        counts[activity.purpose.lawful_basis.value] += 1
    return counts
