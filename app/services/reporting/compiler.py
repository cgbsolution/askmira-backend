"""Turning a report definition into SQL that cannot be talked out of its limits.

═══════════════════════════════════════════════════════════════════════════════
THE THREE THINGS A DEFINITION CANNOT OPT OUT OF
═══════════════════════════════════════════════════════════════════════════════

**The tenant predicate.** Added here, on the base table, unconditionally — not
from the definition, not from the request, from the requester's JWT. There is no
parameter that removes it and no code path that skips it. A report is a saved
object that anybody in the tenant might run, and a report that could reach
another tenant's rows is the worst thing this system could do.

**Field-level sensitivity.** Checked against the requester's role before any
expression is built. A restricted field is not selected, not filtered on, not
grouped by, and not sorted by — `ORDER BY ctc_annual` leaks the ordering of
salaries without ever returning one, and `WHERE ctc_annual > 2000000` leaks the
answer through the row count.

**Row-level scope.** An employee sees themselves, a manager sees their reporting
tree, HR sees the tenant. On an entity with no employee column the scoped roles
are refused outright rather than silently shown everything.

═══════════════════════════════════════════════════════════════════════════════
WHY INJECTION IS STRUCTURALLY IMPOSSIBLE HERE
═══════════════════════════════════════════════════════════════════════════════

There are exactly two places user input can appear, and neither can become SQL:

- **Identifiers** are looked up in the catalogue. `entity()` and `Entity.field()`
  raise on anything not defined. A user-supplied string never reaches a table or
  column name, because the only way to name one is to match a key that already
  exists.
- **Values** go through SQLAlchemy Core as bound parameters. The famous payload
  `'; DROP TABLE employees; --` becomes a string literal compared against a
  column, which matches nothing.

This module builds `sqlalchemy.Select` objects. It never formats SQL text, and
an invariant refuses `text(`, `execute("`, f-strings in SQL position and `%`
formatting anywhere in the reporting package.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Select,
    Table,
    and_,
    func,
    literal_column,
    select,
)
from sqlalchemy.sql.elements import ColumnElement

from app.core.deps import tenant_filter
from app.models.base import Base
from app.services.reporting import ranges
from app.services.reporting.semantic import (
    DataType,
    Entity,
    Field,
    SemanticError,
    entity,
    may_see,
)

__all__ = [
    "MAX_ROWS",
    "STATEMENT_TIMEOUT_MS",
    "CompileError",
    "CompiledReport",
    "Definition",
    "Filter",
    "Scope",
    "Selection",
    "compile_report",
    "resolve_scope",
]

#: The row cap. A report that would return more than this is refused with a
#: message rather than truncated: a silently truncated payroll register is a
#: number somebody files.
MAX_ROWS = 50_000

#: Per-statement timeout. A report builder is a way for any user to write an
#: expensive query, and without this one of them eventually takes the database
#: down at month end.
STATEMENT_TIMEOUT_MS = 30_000

#: What a page of a table view returns.
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1_000


class CompileError(ValueError):
    """A definition this compiler refuses to turn into SQL."""


class ScopeKind(str, enum.Enum):
    SELF = "self"
    REPORTING_TREE = "reporting_tree"
    TENANT = "tenant"


#: Which roles see how much. Anything not listed gets SELF — a role added
#: elsewhere should narrow what a report returns, never widen it. `manager` is
#: an employee with a direct report (`permissions.scope_role`).
_ROLE_SCOPE: dict[str, ScopeKind] = {
    "super_admin": ScopeKind.TENANT,
    "admin": ScopeKind.TENANT,
    "sub_admin": ScopeKind.TENANT,
    "manager": ScopeKind.REPORTING_TREE,
    "employee": ScopeKind.SELF,
}


@dataclass(frozen=True, slots=True)
class Scope:
    """Which rows this requester may see, resolved before compilation."""

    kind: ScopeKind
    tenant_id: uuid.UUID
    #: The requester's own employee id, where they have one.
    employee_id: uuid.UUID | None = None
    #: Every employee in their reporting tree, including themselves. Resolved by
    #: the caller with a recursive query — the compiler takes the answer rather
    #: than embedding the traversal, so the traversal is testable on its own.
    visible_employee_ids: tuple[uuid.UUID, ...] = ()

    @property
    def is_unrestricted(self) -> bool:
        return self.kind is ScopeKind.TENANT


def resolve_scope(role: str) -> ScopeKind:
    return _ROLE_SCOPE.get(role, ScopeKind.SELF)


class Operator(str, enum.Enum):
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    IN = "in"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    GREATER_THAN = "gt"
    GREATER_OR_EQUAL = "gte"
    LESS_THAN = "lt"
    LESS_OR_EQUAL = "lte"
    BETWEEN = "between"
    RELATIVE_RANGE = "relative_range"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


#: Operators that make sense per data type. Offering `contains` on a decimal
#: would produce a cast nobody asked for and a result nobody could explain.
_OPERATORS_FOR: dict[DataType, tuple[Operator, ...]] = {
    DataType.STRING: (
        Operator.EQUALS,
        Operator.NOT_EQUALS,
        Operator.IN,
        Operator.NOT_IN,
        Operator.CONTAINS,
        Operator.IS_NULL,
        Operator.IS_NOT_NULL,
    ),
    DataType.ENUM: (
        Operator.EQUALS,
        Operator.NOT_EQUALS,
        Operator.IN,
        Operator.NOT_IN,
        Operator.IS_NULL,
        Operator.IS_NOT_NULL,
    ),
    DataType.BOOLEAN: (Operator.EQUALS, Operator.IS_NULL, Operator.IS_NOT_NULL),
    DataType.INTEGER: (
        Operator.EQUALS,
        Operator.NOT_EQUALS,
        Operator.IN,
        Operator.NOT_IN,
        Operator.GREATER_THAN,
        Operator.GREATER_OR_EQUAL,
        Operator.LESS_THAN,
        Operator.LESS_OR_EQUAL,
        Operator.BETWEEN,
        Operator.IS_NULL,
        Operator.IS_NOT_NULL,
    ),
    DataType.DATE: (
        Operator.EQUALS,
        Operator.GREATER_THAN,
        Operator.GREATER_OR_EQUAL,
        Operator.LESS_THAN,
        Operator.LESS_OR_EQUAL,
        Operator.BETWEEN,
        Operator.RELATIVE_RANGE,
        Operator.IS_NULL,
        Operator.IS_NOT_NULL,
    ),
}
_OPERATORS_FOR[DataType.DECIMAL] = _OPERATORS_FOR[DataType.INTEGER]
_OPERATORS_FOR[DataType.DATETIME] = _OPERATORS_FOR[DataType.DATE]


@dataclass(frozen=True, slots=True)
class Filter:
    field_key: str
    operator: Operator
    value: Any = None
    values: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class Selection:
    """One column on the report."""

    field_key: str
    #: `None` means the raw value. Anything else must be in the field's own
    #: allowed list — a `sum` over a date is refused rather than cast.
    aggregation: str | None = None
    label: str = ""


@dataclass(frozen=True, slots=True)
class Definition:
    entity_key: str
    selections: tuple[Selection, ...]
    filters: tuple[Filter, ...] = ()
    group_by: tuple[str, ...] = ()
    sort_by: tuple[tuple[str, str], ...] = ()
    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0


@dataclass(frozen=True, slots=True)
class CompiledReport:
    statement: Select
    count_statement: Select
    columns: tuple[dict[str, Any], ...]
    #: Fields the requester asked for and may not see. Named, so the message can
    #: say which — "you cannot see some of these fields" makes somebody guess.
    denied_fields: tuple[str, ...] = dataclass_field(default_factory=tuple)
    notes: tuple[str, ...] = dataclass_field(default_factory=tuple)


# ═══════════════════════════════════════════════════════════════════════════
# Table access
# ═══════════════════════════════════════════════════════════════════════════


def _table(name: str) -> Table:
    """A real mapped table, or nothing.

    Looked up in SQLAlchemy's own metadata rather than constructed from a string,
    so a catalogue entry naming a table that does not exist fails here rather
    than producing SQL against a table nobody has.
    """
    table = Base.metadata.tables.get(name)
    if table is None:
        raise CompileError(
            f"The catalogue names a table '{name}' that does not exist in this schema. "
            "This is a catalogue bug, not a user error."
        )
    return table


def _column(alias_map: dict[str, Any], table_name: str, column_name: str) -> ColumnElement:
    source = alias_map.get(table_name)
    if source is None:
        raise CompileError(
            f"'{table_name}' is not joined into this query. This is a catalogue bug: "
            "the field's join path does not reach its own table."
        )
    try:
        return source.c[column_name]
    except KeyError as exc:
        raise CompileError(
            f"The catalogue names a column '{table_name}.{column_name}' that does not "
            "exist. This is a catalogue bug, not a user error."
        ) from exc


# ═══════════════════════════════════════════════════════════════════════════
# Compilation
# ═══════════════════════════════════════════════════════════════════════════


def _resolve_fields(ent: Entity, keys: list[str], role: str) -> tuple[list[Field], list[str]]:
    """Look up fields and drop the ones this role may not see.

    Returns `(allowed, denied)`. Denied keys are named so the caller can say
    which; and because they are removed *here*, no expression is ever built for
    them — nothing to leak through a filter, a sort, or a payload.
    """
    allowed: list[Field] = []
    denied: list[str] = []
    for key in keys:
        found = ent.field(key)
        if may_see(role, found.sensitivity):
            allowed.append(found)
        else:
            denied.append(key)
    return allowed, denied


def _build_from(ent: Entity, fields: list[Field]) -> tuple[Any, dict[str, Any]]:
    """The FROM clause, with only the joins the chosen fields actually need.

    Joins are taken from each field's declared path and never inferred. A query
    that joined every table the entity could reach would give a manager a route
    to salary through a report about attendance.
    """
    base = _table(ent.base_table)
    alias_map: dict[str, Any] = {ent.base_table: base}
    from_clause: Any = base

    seen: set[str] = set()
    for f in fields:
        for join in f.joins:
            if join.alias_name in seen or join.alias_name == ent.base_table:
                seen.add(join.alias_name)
                continue
            target = _table(join.table)
            source = alias_map.get(join.local_table)
            if source is None:
                raise CompileError(
                    f"Join to {join.table} starts from {join.local_table}, which is not "
                    "in the query yet. This is a catalogue bug."
                )
            aliased = target.alias(join.alias_name) if join.alias else target
            # LEFT, always: an employee with no department must still appear in
            # a headcount, with a blank department, rather than vanishing.
            from_clause = from_clause.outerjoin(
                aliased,
                and_(
                    source.c[join.local_column] == aliased.c[join.remote_column],
                    # Every joined table is tenant-scoped in its own right. A
                    # join predicate without it would match another tenant's row
                    # if two ids ever collided, and "ids never collide" is not a
                    # security guarantee.
                    aliased.c.tenant_id == base.c.tenant_id,
                ),
            )
            alias_map[join.alias_name] = aliased
            seen.add(join.alias_name)
    return from_clause, alias_map


def _aggregate(expression: ColumnElement, aggregation: str | None) -> ColumnElement:
    if aggregation is None:
        return expression
    return {
        "count": func.count(expression),
        "count_distinct": func.count(func.distinct(expression)),
        "sum": func.sum(expression),
        "avg": func.avg(expression),
        "min": func.min(expression),
        "max": func.max(expression),
    }[aggregation]


def _refuse_encrypted(spec: Field, expression: ColumnElement, verb: str) -> None:
    """An encrypted column can be shown, never compared: in SQL it is ciphertext."""
    from app.models.types import EncryptedText

    if isinstance(getattr(expression, "type", None), EncryptedText):
        raise CompileError(
            f"{spec.display_name} is stored encrypted, so a report can show it but not {verb} it."
        )


def _filter_expression(
    f: Filter, spec: Field, expression: ColumnElement, *, today: date
) -> ColumnElement:
    """One filter, as a bound-parameter expression.

    Every value below goes into SQLAlchemy Core as a parameter. `'; DROP TABLE
    employees; --` becomes a string compared against a column, and matches
    nothing.
    """
    allowed = _OPERATORS_FOR.get(spec.data_type, ())
    if f.operator not in allowed:
        raise CompileError(
            f"'{f.operator.value}' cannot be used on {spec.display_name}, which is a "
            f"{spec.data_type.value}. Allowed here: "
            f"{sorted(o.value for o in allowed)}."
        )

    if f.operator is Operator.IS_NULL:
        return expression.is_(None)
    if f.operator is Operator.IS_NOT_NULL:
        return expression.is_not(None)

    if f.operator in (Operator.IN, Operator.NOT_IN):
        values = list(f.values) or ([f.value] if f.value is not None else [])
        if not values:
            raise CompileError(
                f"An '{f.operator.value}' filter on {spec.display_name} has no values. "
                "An empty list would match everything or nothing depending on the "
                "database, so it is refused."
            )
        if len(values) > 1_000:
            raise CompileError(
                f"An '{f.operator.value}' filter on {spec.display_name} has "
                f"{len(values)} values. Above a thousand this stops being a filter and "
                "starts being a join; narrow it another way."
            )
        clause = expression.in_(values)
        return clause if f.operator is Operator.IN else ~clause

    if f.operator is Operator.CONTAINS:
        if not isinstance(f.value, str):
            raise CompileError("A 'contains' filter needs text to look for.")
        # `.contains` escapes the wildcards itself, so a value of "100%" looks
        # for the literal string and not for "100 followed by anything".
        return expression.contains(f.value, autoescape=True)

    if f.operator is Operator.RELATIVE_RANGE:
        try:
            resolved = ranges.resolve_range(str(f.value), today=today)
        except ranges.RangeError as exc:
            raise CompileError(str(exc)) from exc
        return _date_between(expression, resolved.start, resolved.end, spec)

    if f.operator is Operator.BETWEEN:
        values = list(f.values)
        if len(values) != 2:
            raise CompileError(
                f"A 'between' filter on {spec.display_name} needs exactly two values."
            )
        low, high = values
        if spec.data_type in (DataType.DATE, DataType.DATETIME):
            return _date_between(expression, _as_date(low), _as_date(high), spec)
        return expression.between(low, high)

    comparison = {
        Operator.EQUALS: expression.__eq__,
        Operator.NOT_EQUALS: expression.__ne__,
        Operator.GREATER_THAN: expression.__gt__,
        Operator.GREATER_OR_EQUAL: expression.__ge__,
        Operator.LESS_THAN: expression.__lt__,
        Operator.LESS_OR_EQUAL: expression.__le__,
    }[f.operator]
    return comparison(f.value)


def _as_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise CompileError(f"'{value}' is not a date this system can read.") from exc


def _date_between(expression: ColumnElement, start: date, end: date, spec: Field) -> ColumnElement:
    """Inclusive of both days, for a date *or* a timestamp column.

    `BETWEEN` on a timestamp compares against midnight on the end date and drops
    everything that happened during that day — an "up to 31 March" report losing
    31 March. The half-open form is correct for both and is used for timestamps.
    """
    if spec.data_type is DataType.DATETIME:
        return and_(expression >= start, expression < end + timedelta(days=1))
    return expression.between(start, end)


def _scope_predicate(ent: Entity, base: Table, scope: Scope) -> ColumnElement | None:
    """Row-level scope, or a refusal.

    An entity with no employee column has nothing for a reporting-tree filter to
    attach to. Rather than returning everything — which is what an unfiltered
    query does — the scoped roles are refused, and the message says why.
    """
    if scope.kind is ScopeKind.TENANT:
        return None

    if ent.employee_column is None:
        raise CompileError(
            f"{ent.display_name} has no employee on it, so 'my team' and 'me' cannot be "
            "worked out for it. This report is available to HR and admins only — "
            "returning everything to everybody else is not the safe fallback."
        )

    column = base.c[ent.employee_column]

    if scope.kind is ScopeKind.SELF:
        if scope.employee_id is None:
            # A user with no employee record and no tenant-wide role can see
            # nothing. `false` rather than an error: they may legitimately be a
            # service account, and an empty report is the honest answer.
            return literal_column("false")
        return column == scope.employee_id

    if not scope.visible_employee_ids:
        return literal_column("false")
    return column.in_(list(scope.visible_employee_ids))


def compile_report(
    definition: Definition,
    *,
    role: str,
    scope: Scope,
    today: date | None = None,
) -> CompiledReport:
    """Build the statement. Nothing about it can be talked out of its limits."""
    resolution_day = today or date.today()

    try:
        ent = entity(definition.entity_key)
    except SemanticError as exc:
        raise CompileError(str(exc)) from exc

    if not definition.selections:
        raise CompileError("A report needs at least one column.")

    # ── Fields ──────────────────────────────────────────────────────────────
    try:
        selection_fields, denied = _resolve_fields(
            ent, [s.field_key for s in definition.selections], role
        )
        filter_fields, filter_denied = _resolve_fields(
            ent, [f.field_key for f in definition.filters], role
        )
        group_fields, group_denied = _resolve_fields(ent, list(definition.group_by), role)
        sort_fields, sort_denied = _resolve_fields(
            ent, [key for key, _ in definition.sort_by], role
        )
    except SemanticError as exc:
        raise CompileError(str(exc)) from exc

    # A restricted field in a filter or a sort is refused outright rather than
    # dropped. Dropping it would silently change what the report means:
    # "employees earning over 20 lakh" would quietly become "all employees".
    hard_denied = sorted(set(filter_denied) | set(group_denied) | set(sort_denied))
    if hard_denied:
        raise CompileError(
            f"This report filters, groups or sorts on fields your role cannot see: "
            f"{hard_denied}. They cannot simply be dropped — a filter on a salary "
            "band tells you the salaries through the row count without ever showing "
            "one, and removing it would silently return a different report."
        )

    if not selection_fields:
        raise CompileError(
            f"None of the columns on this report are visible to your role "
            f"(hidden: {sorted(set(denied))}). There is nothing to show."
        )

    all_fields = selection_fields + filter_fields + group_fields + sort_fields
    from_clause, alias_map = _build_from(ent, all_fields)
    base = alias_map[ent.base_table]

    # ── Columns ─────────────────────────────────────────────────────────────
    columns: list[ColumnElement] = []
    metadata: list[dict[str, Any]] = []
    allowed_keys = {f.key for f in selection_fields}

    for selection in definition.selections:
        if selection.field_key not in allowed_keys:
            continue
        spec = ent.field(selection.field_key)
        if selection.aggregation is not None:
            if selection.aggregation not in spec.allowed_aggregations:
                raise CompileError(
                    f"'{selection.aggregation}' is not available on "
                    f"{spec.display_name}. Allowed: "
                    f"{sorted(spec.allowed_aggregations) or 'none — it is a dimension'}."
                )
            if not spec.is_measure:
                raise CompileError(f"{spec.display_name} is a dimension; it cannot be aggregated.")
        elif definition.group_by and selection.field_key not in definition.group_by:
            raise CompileError(
                f"{spec.display_name} is neither grouped nor aggregated. In a grouped "
                "report every column has to be one or the other, or the database picks "
                "an arbitrary row's value and nobody can tell which."
            )

        expression = _column(alias_map, spec.table, spec.column)
        label = selection.label or spec.display_name
        columns.append(_aggregate(expression, selection.aggregation).label(label))
        metadata.append(
            {
                "key": selection.field_key,
                "label": label,
                "data_type": spec.data_type.value,
                "aggregation": selection.aggregation,
                "sensitivity": spec.sensitivity.value,
            }
        )

    statement = select(*columns).select_from(from_clause)

    # ── The tenant predicate. Not optional, not from the definition. ────────
    predicates: list[ColumnElement] = [tenant_filter(base, scope.tenant_id)]

    # Soft-deleted rows are excluded where the table supports it. A headcount
    # that counts deleted employees is a wrong number nobody would query.
    if "deleted_at" in base.c:
        predicates.append(base.c.deleted_at.is_(None))

    scope_predicate = _scope_predicate(ent, base, scope)
    if scope_predicate is not None:
        predicates.append(scope_predicate)

    for f in definition.filters:
        spec = ent.field(f.field_key)
        expression = _column(alias_map, spec.table, spec.column)
        _refuse_encrypted(spec, expression, "filter on")
        predicates.append(_filter_expression(f, spec, expression, today=resolution_day))

    statement = statement.where(and_(*predicates))

    # ── Grouping and sorting ────────────────────────────────────────────────
    if definition.group_by:
        group_expressions = [
            _column(alias_map, ent.field(key).table, ent.field(key).column)
            for key in definition.group_by
        ]
        for key, grouped in zip(definition.group_by, group_expressions, strict=True):
            _refuse_encrypted(ent.field(key), grouped, "group by")
        statement = statement.group_by(*group_expressions)

    for key, direction in definition.sort_by:
        spec = ent.field(key)
        expression = _column(alias_map, spec.table, spec.column)
        _refuse_encrypted(spec, expression, "sort by")
        # In a grouped report, sorting by a raw column the database is not
        # grouping on is an error in Postgres. Sort by the aggregate instead.
        matching = next(
            (s for s in definition.selections if s.field_key == key and s.aggregation),
            None,
        )
        if definition.group_by and matching is not None:
            expression = _aggregate(expression, matching.aggregation)
        statement = statement.order_by(
            expression.desc() if direction.lower() == "desc" else expression.asc()
        )

    page_size = max(1, min(definition.limit or DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE))
    statement = statement.limit(page_size).offset(max(0, definition.offset))

    count_statement = select(func.count()).select_from(from_clause).where(and_(*predicates))
    if definition.group_by:
        # Counting a grouped report means counting groups, not rows.
        inner = (
            select(*group_expressions)
            .select_from(from_clause)
            .where(and_(*predicates))
            .group_by(*group_expressions)
            .subquery()
        )
        count_statement = select(func.count()).select_from(inner)

    notes: list[str] = []
    if denied:
        notes.append(
            f"These columns are hidden from your role and were left off: {sorted(set(denied))}."
        )

    return CompiledReport(
        statement=statement,
        count_statement=count_statement,
        columns=tuple(metadata),
        denied_fields=tuple(sorted(set(denied))),
        notes=tuple(notes),
    )


def rendered_sql(statement: Select) -> str:
    """The SQL as text, for tests and for showing an admin what ran.

    Deliberately *not* used to execute anything — the statement object is what
    runs. This renders with parameters still bound as placeholders, which is
    what makes "no user value ever became SQL" checkable by reading it.
    """
    return str(statement.compile(compile_kwargs={"literal_binds": False}))


def serialise_value(value: Any) -> Any:
    """One cell, on its way out.

    Decimals become strings. A `Decimal("1234.50")` serialised as a JSON number
    is a float by the time it reaches a browser, and a salary register that
    renders 1234.4999999 is worse than one that fails.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.value
    return value
