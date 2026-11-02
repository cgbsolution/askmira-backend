"""Ask Mira's general data tool: any figure over AskMira's data, as a chart.

`query_data` lets the model ask the report compiler for a figure — a count, a
total, an average — over any dataset in the semantic catalogue (payslips,
leave, attendance, exits, expenses, helpdesk tickets, assets, timesheets,
recognition, client billing and more), grouped by up to two things: a time
bucket (month, quarter, financial year...) and/or a dimension (department,
company, leave type...). The result is drawn as a line, area, bar, stacked
bar, pie or donut chart, or a single figure, with the table beside it.

Everything a report already guarantees holds, because the query *is* a report
(`reporting.service.run_definition`): identifiers come only from the
catalogue, restricted fields are refused before SQL exists for the asker's
role, rows are scoped to what the asker may see (their own record, their team,
their HR scope or the company), and every run is logged as a report run.

What the model may read of the answer is narrower than what the person sees.
The model reads figures only for groups of at least `MIN_GROUP` people: a group
smaller than that could be one person's pay or leave, which the DPDP register
does not let a model provider receive. Those groups are on the person's screen
only, and the model is told how many there were. Datasets that are not about
people (assets, requisitions, client billing) have no such limit.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException

from app.core import permissions
from app.core.hr_scope import current_scope
from app.models.user import User
from app.services.documents.indian_numbers import format_inr
from app.services.reporting import compiler
from app.services.reporting import service as report_service
from app.services.reporting.compiler import (
    BUCKETS,
    Definition,
    Filter,
    Operator,
    Selection,
)
from app.services.reporting.ranges import RELATIVE_RANGES
from app.services.reporting.semantic import (
    ENTITIES,
    DataType,
    Entity,
    SemanticError,
    may_see,
)

__all__ = [
    "CHARTS",
    "MIN_GROUP",
    "catalogue_text",
    "query_data",
]

#: Datasets that arrive once a month (a payroll run, a month's billing): "the last
#: N months" counts back from the latest month with data, not from today.
_MONTHLY = frozenset({"payslip", "account_entry"})

_BUCKET_WORDS = {
    "month": "month",
    "quarter": "quarter",
    "year": "year",
    "financial_year": "financial year",
    "week": "week",
    "day": "day",
}

#: What an organisation's headcount is broken down by. A count of employees by
#: these is the company's shape, not anybody's record, so it is read whatever the
#: group's size; by a name, a gender or anything else, the five-person floor holds.
_ORG_FIELDS = frozenset(
    {
        "department",
        "location",
        "company",
        "grade",
        "designation",
        "employment_type",
        "project",
        "project_client",
        "state",
        "status",
        "date_of_joining",
        "date_of_exit",
    }
)

#: The fewest people a group may cover for the model to read its figures.
MIN_GROUP = 5
#: Categories drawn before the rest are added up as "Others".
DEFAULT_TOP = 8
MAX_TOP = 25
#: The most months (or other periods) a trend runs over.
MAX_PERIODS = 60

CHARTS = (
    "auto",
    "line",
    "area",
    "bar",
    "stacked_bar",
    "horizontal_bar",
    "pie",
    "donut",
    "number",
    "table",
)

_MEASURE_AGGREGATIONS = ("sum", "avg", "count", "count_distinct", "min", "max")


class DataQueryError(ValueError):
    """A question the tool cannot turn into a query; the message says what to change."""


def _notice(text: str, tone: str = "info") -> dict[str, Any]:
    return {"type": "notice", "tone": tone, "text": text}


# ── What the model is told it can ask ───────────────────────────────────────


#: The employee's own fields, shared by every dataset about people. Listed once.
_EMPLOYEE_FIELDS = (
    "employee_name",
    "employee_code",
    "department",
    "location",
    "company",
    "grade",
    "designation",
    "employment_type",
    "status",
    "gender",
    "project",
    "project_client",
    "manager_name",
    "state",
    "date_of_joining",
    "date_of_exit",
    "headcount",
)
#: Never offered to the model to group or filter by: identifiers and contact details.
_NOT_OFFERED = frozenset({"pan", "uan", "bank_account_number", "work_email", "candidate_email"})
_DATES = (DataType.DATE, DataType.DATETIME)


def catalogue_text(role: str, hidden: frozenset[str] = frozenset()) -> str:
    """Every dataset this role may query, with its fields, for the tool's description.

    `hidden` leaves datasets out: AskMira's own expense claims when the workspace
    keeps its claims in the linked expense product instead.
    """
    shared = [
        f.key
        for f in ENTITIES["employee"].fields
        if f.key in _EMPLOYEE_FIELDS and may_see(role, f.sensitivity)
    ]
    lines = [
        "Datasets marked (people) also take the employee's own fields: " + ", ".join(shared) + "."
    ]
    for key, ent in ENTITIES.items():
        if key in hidden:
            continue
        visible = [
            f
            for f in ent.fields
            if may_see(role, f.sensitivity)
            and f.key not in _NOT_OFFERED
            and not (key != "employee" and f.key in _EMPLOYEE_FIELDS)
        ]
        if not visible:
            continue
        people = key != "employee" and any(f.key == "headcount" for f in ent.fields)
        dims = [f.key for f in visible if f.is_dimension and f.data_type not in _DATES]
        dates = [f.key for f in visible if f.data_type in _DATES]
        measures = [
            f"{f.key}({f.unit})" if f.unit and f.unit != "count" else f.key
            for f in visible
            if f.is_measure and f.data_type not in _DATES
        ]
        parts = [f"- {key}{' (people)' if people else ''}: {ent.description}"]
        if dates:
            parts.append(f"Dates: {', '.join(dates)}.")
        if measures:
            parts.append(f"Measures: {', '.join(measures)}.")
        if dims:
            parts.append(f"Group by: {', '.join(dims)}.")
        lines.append(" ".join(parts))
    return "\n".join(lines)


# ── Reading the model's inputs ──────────────────────────────────────────────


def _list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, list) else [parsed]
            except ValueError:
                pass
        return [part.strip() for part in text.split(",") if part.strip()]
    return [value]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    if len(text) == 7:  # "2026-04"
        try:
            return date.fromisoformat(text + "-01")
        except ValueError:
            return None
    return None


def _month_start(day: date, back: int = 0) -> date:
    year, month = day.year, day.month - back
    while month < 1:
        month += 12
        year -= 1
    return date(year, month, 1)


def _month_end(day: date) -> date:
    following = _month_start(day, -1) if day.month < 12 else date(day.year + 1, 1, 1)
    return following - timedelta(days=1)


def _measure_specs(ent: Entity, raw: list[Any]) -> list[tuple[str, str]]:
    """(field, aggregation) for each measure asked for; a sensible default when none is."""
    out: list[tuple[str, str]] = []
    for item in raw[:4]:
        if isinstance(item, dict):
            key = str(item.get("field") or item.get("measure") or "").strip()
            aggregation = str(item.get("aggregation") or "").strip().lower()
        else:
            key, _, aggregation = str(item).partition(":")
            key, aggregation = key.strip(), aggregation.strip().lower()
        if not key:
            continue
        try:
            spec = ent.field(key)
        except SemanticError as exc:
            raise DataQueryError(str(exc)) from exc
        if not spec.is_measure:
            raise DataQueryError(
                f"{key} is something to group by, not a figure. Put it in group_by instead."
            )
        allowed = spec.allowed_aggregations
        if aggregation not in allowed:
            if spec.data_type in (DataType.DECIMAL, DataType.INTEGER):
                aggregation = "sum" if "sum" in allowed else allowed[0]
            elif key == ent.people_field:
                aggregation = "count_distinct"
            else:
                aggregation = "count"
        if aggregation not in _MEASURE_AGGREGATIONS:
            raise DataQueryError(f"'{aggregation}' is not a way to add up {key}.")
        out.append((key, aggregation))
    if not out:
        if ent.people_field:
            out.append((ent.people_field, "count_distinct"))
        else:
            counter = next(
                (f.key for f in ent.fields if f.key.endswith("_count") and f.is_measure), None
            )
            if counter is None:
                raise DataQueryError("Say which figure to show (measures).")
            out.append((counter, "count"))
    return out


def _filters(ent: Entity, raw: list[Any]) -> list[Filter]:
    out: list[Filter] = []
    for item in raw[:8]:
        if isinstance(item, str):
            # "department=Sales" or "leave_status in approved,pending"
            for token, op in (("!=", "not_equals"), (">=", "gte"), ("<=", "lte"), ("=", "equals")):
                if token in item:
                    key, value = item.split(token, 1)
                    item = {"field": key.strip(), "op": op, "value": value.strip()}
                    break
            else:
                continue
        if not isinstance(item, dict):
            continue
        key = str(item.get("field") or "").strip()
        op = str(item.get("op") or item.get("operator") or "equals").strip().lower()
        if not key:
            continue
        try:
            spec = ent.field(key)
            operator = Operator(op)
        except SemanticError as exc:
            raise DataQueryError(str(exc)) from exc
        except ValueError as exc:
            raise DataQueryError(
                f"'{op}' is not a filter operator. Use one of {[o.value for o in Operator]}."
            ) from exc
        raw_value = item.get("value")
        values = item.get("values")
        if operator in (Operator.IN, Operator.NOT_IN, Operator.BETWEEN):
            listed = _list(values) or _list(raw_value)
            out.append(Filter(key, operator, None, tuple(str(v) for v in listed)))
        elif operator is Operator.RELATIVE_RANGE:
            if str(raw_value) not in RELATIVE_RANGES:
                raise DataQueryError(
                    f"'{raw_value}' is not a range. Use one of {sorted(RELATIVE_RANGES)}."
                )
            out.append(Filter(key, operator, str(raw_value)))
        elif operator in (Operator.IS_NULL, Operator.IS_NOT_NULL):
            out.append(Filter(key, operator))
        else:
            value: Any = raw_value
            if spec.data_type is DataType.BOOLEAN:
                value = str(raw_value).strip().lower() in ("true", "yes", "1")
            elif spec.data_type in (DataType.INTEGER, DataType.DECIMAL):
                try:
                    value = Decimal(str(raw_value))
                except InvalidOperation as exc:
                    raise DataQueryError(f"{key} needs a number, not '{raw_value}'.") from exc
            elif spec.data_type in _DATES:
                parsed = _date(raw_value)
                if parsed is None:
                    raise DataQueryError(f"{key} needs a date as YYYY-MM-DD.")
                value = parsed
            else:
                value = str(raw_value or "").strip()
            out.append(Filter(key, operator, value))
    return out


# ── Labels and numbers ──────────────────────────────────────────────────────


def _period_label(value: Any, bucket: str | None) -> str:
    day = _date(value)
    if day is None:
        return "(none)" if value in (None, "") else str(value)
    if bucket == "month":
        return f"{day:%b %Y}"
    if bucket == "quarter":
        end = _month_start(day, -2)
        return f"{day:%b}-{end:%b %Y}"
    if bucket == "financial_year":
        return f"FY {day.year}-{str(day.year + 1)[-2:]}"
    if bucket == "year":
        return str(day.year)
    if bucket == "week":
        return f"w/c {day.day} {day:%b %Y}"
    return f"{day.day} {day:%b %Y}"


def _dim_label(value: Any) -> str:
    if value is None or value == "":
        return "(none)"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    text = str(value)
    if text in ("True", "False", "true", "false"):
        return "Yes" if text.lower() == "true" else "No"
    looks_like_a_code = text.islower() and ("_" in text or (" " not in text and text.isalpha()))
    if looks_like_a_code:
        return text.replace("_", " ").capitalize()
    return text


def _number(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _formatted(value: Decimal | None, unit: str, aggregation: str) -> str:
    if value is None:
        return "—"
    if aggregation in ("count", "count_distinct"):
        return f"{int(value):,}"
    if unit == "inr":
        return format_inr(value)
    rounded = value.quantize(Decimal("0.1")) if aggregation == "avg" else value
    text = f"{rounded:,.2f}".rstrip("0").rstrip(".") if rounded % 1 else f"{int(rounded):,}"
    return {"days": f"{text} days", "hours": f"{text} h", "minutes": f"{text} min"}.get(unit, text)


def _series_unit(unit: str, aggregation: str) -> str:
    if aggregation in ("count", "count_distinct"):
        return "count"
    return "inr" if unit == "inr" else "number"


def _months_between(start: date, end: date) -> list[date]:
    out = []
    current = _month_start(start)
    while current <= end and len(out) < MAX_PERIODS:
        out.append(current)
        current = _month_start(current, -1)
    return out


# ── The tool ────────────────────────────────────────────────────────────────


async def _latest(session: Any, user: User, ent: Entity, field: str) -> date | None:
    """The latest month this person can see data for, on `field`."""
    compiled = compiler.compile_report(
        Definition(entity_key=ent.key, selections=(Selection(field, "max"),), limit=1),
        role=permissions.scope_role(user),
        scope=await report_service.build_scope(session, user),
    )
    value = await session.scalar(compiled.statement)
    return _date(value.isoformat() if hasattr(value, "isoformat") else value)


def _organisation_counts(
    ent: Entity, measures: list[tuple[str, str]], group_by: list[str], filters: list[Filter]
) -> bool:
    """Only counts of employees, by the organisation's own fields: never held back."""
    return (
        ent.key == "employee"
        and all(aggregation in ("count", "count_distinct") for _, aggregation in measures)
        and all(field in _ORG_FIELDS for field in group_by)
        and all(f.field_key in _ORG_FIELDS for f in filters)
    )


#: AskMira's own datasets that a linked product replaces, by product.
_REPLACED_BY = {"expense": "expense_claim"}


async def linked_elsewhere(session: Any, user: User) -> frozenset[str]:
    """Datasets whose records live in a product this workspace has linked."""
    from app.models.tenant import Tenant

    tenant = await session.get(Tenant, user.tenant_id)
    linked = (tenant.linked_products if tenant else None) or {}
    return frozenset(dataset for product, dataset in _REPLACED_BY.items() if linked.get(product))


def _scope_text(user: User) -> str:
    if permissions.is_hr(user):
        return (
            "only the people in your HR scope"
            if current_scope() is not None
            else "the whole company"
        )
    if getattr(user, "has_reports", False):
        return "you and your team"
    return "your own record"


async def query_data(
    session: Any, user: User, inputs: dict[str, Any], *, today: date
) -> list[dict[str, Any]]:
    try:
        return await _query(session, user, inputs, today=today)
    except DataQueryError as exc:
        return [_notice(str(exc), "warning")]


async def _query(
    session: Any, user: User, inputs: dict[str, Any], *, today: date
) -> list[dict[str, Any]]:
    key = str(inputs.get("dataset") or "").strip()
    ent = ENTITIES.get(key)
    if ent is None:
        raise DataQueryError(f"Choose a dataset: {', '.join(ENTITIES)}.")
    if key in await linked_elsewhere(session, user):
        raise DataQueryError(
            "This workspace keeps its expense claims in ExpenseGenie, not here. Ask with "
            "expense_summary (totals, who submitted how much, month by month) or "
            "expense_claims (the latest claims)."
        )

    measures = _measure_specs(ent, _list(inputs.get("measures")))
    # Figures this role may not see are dropped here, by name, rather than refused
    # by the compiler in words about sorting; none left is a plain no.
    role = permissions.scope_role(user)
    hidden = [k for k, _ in measures if not may_see(role, ent.field(k).sensitivity)]
    measures = [(k, a) for k, a in measures if k not in hidden]
    if not measures:
        names = ", ".join(ent.field(k).display_name.lower() for k in hidden)
        raise DataQueryError(f"Your role cannot see {names}.")
    group_by = [str(g).strip() for g in _list(inputs.get("group_by")) if str(g).strip()]
    for name in group_by:
        try:
            if not may_see(role, ent.field(name).sensitivity):
                raise DataQueryError(
                    f"Your role cannot group by {ent.field(name).display_name.lower()}."
                )
        except SemanticError as exc:
            raise DataQueryError(str(exc)) from exc
    bucket = str(inputs.get("bucket") or "none").strip().lower()
    bucket = None if bucket in ("", "none") else bucket
    if bucket is not None and bucket not in BUCKETS:
        raise DataQueryError(f"Group dates by one of: {', '.join(BUCKETS)}.")
    time_field = str(inputs.get("time_field") or "").strip() or ent.default_date
    chart = str(inputs.get("chart") or "auto").strip().lower()
    chart = chart if chart in CHARTS else "auto"
    top = max(1, min(_int(inputs.get("top"), DEFAULT_TOP) or DEFAULT_TOP, MAX_TOP))

    for name in group_by:
        try:
            spec = ent.field(name)
        except SemanticError as exc:
            raise DataQueryError(str(exc)) from exc
        if not spec.is_dimension:
            raise DataQueryError(f"{name} is a figure, not something to group by.")
        if spec.data_type in _DATES and bucket is None and name == time_field:
            bucket = "month"
    if bucket is not None:
        if not time_field:
            raise DataQueryError(f"{ent.display_name} has no date to show a trend along.")
        group_by = [time_field, *[g for g in group_by if g != time_field]]
    elif time_field in group_by:
        bucket = "month"
    if len(group_by) > 2:
        raise DataQueryError("Group by at most two things (for example month and department).")

    # The period: from/to, or the last N months, on the trend's date.
    filters = _filters(ent, _list(inputs.get("filters")))
    # "How many employees do we have" means people employed now. A trend over
    # joining or exit dates counts everyone (a joiner who has since left still
    # joined), and a question that names a status or an exit says what it wants.
    current_only = (
        ent.key == "employee"
        and bucket is None
        and not any(f.field_key in ("status", "date_of_exit") for f in filters)
        and "status" not in group_by
    )
    if current_only:
        filters.append(Filter("status", Operator.NOT_IN, None, ("exited", "pre_joining")))
    start, end = _date(inputs.get("from")), _date(inputs.get("to"))
    months = _int(inputs.get("months"))
    if months > 0 and start is None:
        anchor = today
        if ent.key in _MONTHLY and time_field == ent.default_date and end is None:
            anchor = await _latest(session, user, ent, time_field) or today
        start = _month_start(anchor, months - 1)
        end = end or _month_end(anchor)
    if start is None and end is None and bucket in ("month", "week", "day", "quarter"):
        start, end = _month_start(today, 11), _month_end(today)
    if start is not None or end is not None:
        if not time_field:
            raise DataQueryError(f"{ent.display_name} has no date to limit to a period.")
        start = start or date(2000, 1, 1)
        end = end or _month_end(today)
        if start > end:
            start, end = end, start
        filters.append(Filter(time_field, Operator.BETWEEN, None, (start, end)))

    selections = [Selection(g) for g in group_by]
    selections += [Selection(k, a) for k, a in measures]
    people_key = ent.people_field if ent.personal else None
    # How many people each group covers: the measure itself when that is what was
    # asked ("how many people"), else a column added for it.
    people_at: int | None = None
    if people_key:
        if (people_key, "count_distinct") in measures:
            people_at = measures.index((people_key, "count_distinct"))
        else:
            selections.append(Selection(people_key, "count_distinct", label="People"))
            people_at = len(measures)
    sort_by: list[tuple[str, str]]
    if bucket is not None:
        sort_by = [(group_by[0], "asc")]
    elif group_by:
        sort_by = [(measures[0][0], "desc")]
    else:
        sort_by = []
    definition = Definition(
        entity_key=ent.key,
        selections=tuple(selections),
        filters=tuple(filters),
        group_by=tuple(group_by),
        sort_by=tuple(sort_by),
        limit=1000,
        buckets=((group_by[0], bucket),) if bucket else (),
    )
    # In a savepoint: a query the database refuses must not spoil the rest of the
    # turn, which shares this transaction.
    savepoint = await session.begin_nested()
    try:
        result = await report_service.run_definition(session, user, definition)
    except HTTPException as exc:
        await savepoint.rollback()
        detail = exc.detail if isinstance(exc.detail, str) else "That could not be worked out."
        raise DataQueryError(detail) from exc
    await savepoint.commit()

    columns = result["columns"]
    rows = result["rows"]
    returned = {c["key"] for c in columns}
    missing = [k for k, _ in measures if k not in returned]
    if missing and len(missing) == len(measures):
        raise DataQueryError("Your role cannot see those figures.")
    measures = [(k, a) for k, a in measures if k in returned]
    # Where the people count sits in what came back, after any dropped column.
    dims = len(group_by)
    if people_key:
        people_at = next(
            (
                index - dims
                for index, column in enumerate(columns)
                if index >= dims
                and column["key"] == people_key
                and column["aggregation"] == "count_distinct"
            ),
            None,
        )
    notes = list(result.get("notes") or [])
    if hidden:
        notes.append(
            "Left out, as your role cannot see them: "
            + ", ".join(ent.field(k).display_name.lower() for k in hidden)
            + "."
        )

    if current_only:
        notes.append(
            "Counts people employed now: not those who have left or are yet to join. Ask for "
            "exited people to include them."
        )
    return _present(
        ent,
        user,
        rows=rows,
        group_by=group_by,
        bucket=bucket,
        measures=measures,
        people_at=people_at,
        floor=0 if _organisation_counts(ent, measures, group_by, filters) else MIN_GROUP,
        chart=chart,
        top=top,
        period=(start, end) if start or end else None,
        notes=notes,
    )


def _present(
    ent: Entity,
    user: User,
    *,
    rows: list[list[Any]],
    group_by: list[str],
    bucket: str | None,
    measures: list[tuple[str, str]],
    people_at: int | None,
    chart: str,
    top: int,
    period: tuple[date | None, date | None] | None,
    notes: list[str],
    floor: int = MIN_GROUP,
) -> list[dict[str, Any]]:
    dims = len(group_by)
    people = people_at is not None
    # The people column is shown only when it was added, not when it is the measure.
    extra_people = people_at == len(measures)
    specs = [ent.field(k) for k, _ in measures]
    labels = [
        f"{spec.display_name}"
        if agg in ("sum", "count", "count_distinct")
        else f"{spec.display_name} ({ {'avg': 'average', 'min': 'lowest', 'max': 'highest'}[agg] })"
        for spec, (_, agg) in zip(specs, measures, strict=True)
    ]
    units = [spec.unit for spec in specs]
    aggs = [agg for _, agg in measures]
    span = ""
    if period:
        start, end = period
        span = f"{start:%d %b %Y} to {end:%d %b %Y}" if start and end else ""
    scope = _scope_text(user)
    title = labels[0] + (f" and {len(labels) - 1} more" if len(labels) > 1 else "")
    if group_by:
        by = []
        for i, g in enumerate(group_by):
            if bucket and i == 0:
                word = _BUCKET_WORDS[bucket]
                # Say which date, unless it is the only one the dataset runs along.
                if g != ent.default_date or ent.key == "employee":
                    word += f" of {ent.field(g).display_name.lower()}"
                by.append(word)
            else:
                by.append(ent.field(g).display_name.lower())
        title += " by " + " and ".join(by)
    caption = " · ".join(x for x in (ent.display_name, span, f"for {scope}") if x)

    def dim_text(i: int, value: Any) -> str:
        return _period_label(value, bucket) if (bucket and i == 0) else _dim_label(value)

    parsed = []
    for row in rows:
        keys = [row[i] for i in range(dims)]
        values = [_number(row[dims + j]) for j in range(len(measures))]
        count = int(_number(row[dims + people_at]) or 0) if people_at is not None else None
        parsed.append((keys, values, count))

    cards: list[dict[str, Any]] = []
    if not parsed or all(all(v is None for v in vals) for _, vals, _ in parsed):
        cards.append(_notice(f"No {ent.display_name.lower()} matched that, for {scope}."))
        return cards

    # ── A single figure ─────────────────────────────────────────────────────
    if dims == 0:
        _, values, count = parsed[0]
        figures = [
            {"label": label, "value": _formatted(v, u, a)}
            for label, v, u, a in zip(labels, values, units, aggs, strict=True)
        ]
        if extra_people and count is not None:
            figures.append({"label": "People", "value": f"{count:,}"})
        card = {
            "type": "record",
            "title": title,
            "subtitle": caption,
            "heading": "",
            "figures": figures,
        }
        card["model_view"] = _model_view(
            ent,
            title,
            caption,
            ["Figure"],
            [(["Total"], values, count)],
            labels,
            units,
            aggs,
            people,
            floor,
        )
        cards.append(card)
        return cards + [_notice(n) for n in notes]

    # ── Chart ───────────────────────────────────────────────────────────────
    first_unit, first_agg = units[0], aggs[0]
    chart_unit = _series_unit(first_unit, first_agg)
    same_unit = [j for j in range(len(measures)) if _series_unit(units[j], aggs[j]) == chart_unit]
    if dims == 1 and bucket:
        x_values = [r[0][0] for r in parsed]
        if bucket == "month" and period and period[0] and period[1]:
            x_values = _months_between(period[0], period[1])
            lookup = {str(r[0][0])[:10]: r for r in parsed}
        else:
            lookup = {str(r[0][0])[:10]: r for r in parsed}
        x_keys = [str(v)[:10] for v in x_values]
        # Trim empty periods at either end, keep gaps in the middle.
        filled = [i for i, k in enumerate(x_keys) if k in lookup]
        if filled:
            x_keys = x_keys[filled[0] : filled[-1] + 1]
        series = [
            {
                "name": labels[j],
                "values": [
                    "" if k not in lookup or lookup[k][1][j] is None else str(lookup[k][1][j])
                    for k in x_keys
                ],
            }
            for j in same_unit
        ]
        kind = chart if chart in ("line", "area", "bar") else "line"
        cards.append(
            {
                "type": "chart",
                "title": title,
                "caption": caption,
                "chart": kind,
                "unit": chart_unit,
                "x": [_period_label(k, bucket) for k in x_keys],
                "series": series,
            }
        )
    elif dims == 1:
        ordered = sorted(
            parsed, key=lambda r: r[1][0] if r[1][0] is not None else Decimal(0), reverse=True
        )
        shown, rest = ordered[:top], ordered[top:]
        x = [_dim_label(r[0][0]) for r in shown]
        values = [r[1][0] for r in shown]
        additive = first_agg in ("sum", "count")
        if rest and additive:
            x.append(f"Others ({len(rest)})")
            values.append(sum((r[1][0] or Decimal(0)) for r in rest))
        kind = (
            chart
            if chart in ("bar", "horizontal_bar", "pie", "donut")
            else ("horizontal_bar" if len(x) > 6 else "bar")
        )
        if kind in ("pie", "donut") and any(v is not None and v < 0 for v in values):
            kind = "bar"
        cards.append(
            {
                "type": "chart",
                "title": title,
                "caption": caption + ("" if not rest else f" · top {top} shown"),
                "chart": kind,
                "unit": chart_unit,
                "x": x,
                "series": [
                    {"name": labels[0], "values": ["" if v is None else str(v) for v in values]}
                ],
            }
        )
    else:
        # Two dimensions: the first runs along the axis, the second makes the series.
        totals: dict[str, Decimal] = {}
        for keys, values, _ in parsed:
            name = _dim_label(keys[1])
            totals[name] = totals.get(name, Decimal(0)) + (values[0] or Decimal(0))
        names = [n for n, _ in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)]
        shown_names, other_names = names[:top], set(names[top:])
        additive = first_agg in ("sum", "count")
        x_order: list[str] = []
        cells: dict[tuple[str, str], Decimal] = {}
        for keys, values, _ in parsed:
            x_label = dim_text(0, keys[0])
            if x_label not in x_order:
                x_order.append(x_label)
            name = _dim_label(keys[1])
            if name in other_names:
                if not additive:
                    continue
                name = "Others"
            cell = (x_label, name)
            cells[cell] = cells.get(cell, Decimal(0)) + (values[0] or Decimal(0))
        series_names = shown_names + (["Others"] if other_names and additive else [])
        if bucket and period and period[0] and period[1] and bucket == "month":
            months = [_period_label(m, "month") for m in _months_between(period[0], period[1])]
            present = [i for i, m in enumerate(months) if m in x_order]
            if present:
                x_order = months[present[0] : present[-1] + 1]
        default_kind = "line" if bucket else "stacked_bar"
        kind = chart if chart in ("line", "area", "bar", "stacked_bar") else default_kind
        cards.append(
            {
                "type": "chart",
                "title": title,
                "caption": caption,
                "chart": kind,
                "unit": chart_unit,
                "x": x_order,
                "series": [
                    {
                        "name": name,
                        "values": [
                            str(cells[(x_label, name)]) if (x_label, name) in cells else ""
                            for x_label in x_order
                        ],
                    }
                    for name in series_names
                ],
            }
        )

    # ── The table, always ───────────────────────────────────────────────────
    head = [
        (
            {
                "month": "Month",
                "quarter": "Quarter",
                "year": "Year",
                "financial_year": "Financial year",
                "week": "Week",
                "day": "Day",
            }[bucket]
            if (bucket and i == 0)
            else ent.field(g).display_name
        )
        for i, g in enumerate(group_by)
    ]
    table_rows = [
        [dim_text(i, k) for i, k in enumerate(keys)]
        + [_formatted(v, u, a) for v, u, a in zip(values, units, aggs, strict=True)]
        + ([f"{count:,}"] if extra_people else [])
        for keys, values, count in parsed[:200]
    ]
    table = {
        "type": "table",
        "title": title,
        "caption": caption + (f" · first 200 of {len(parsed)}" if len(parsed) > 200 else ""),
        "columns": head + labels + (["People"] if extra_people else []),
        "rows": table_rows,
        "model_view": "",
    }
    cards[-1]["model_view"] = _model_view(
        ent,
        title,
        caption,
        head,
        [
            ([dim_text(i, k) for i, k in enumerate(keys)], values, count)
            for keys, values, count in parsed
        ],
        labels,
        units,
        aggs,
        people,
        floor,
    )
    if chart == "table":
        cards = [table | {"model_view": cards[-1]["model_view"]}]
    else:
        cards.append(table)
    return cards + [_notice(n) for n in notes]


def _model_view(
    ent: Entity,
    title: str,
    caption: str,
    head: list[str],
    rows: list[tuple[list[str], list[Decimal | None], int | None]],
    labels: list[str],
    units: list[str],
    aggs: list[str],
    people: bool,
    floor: int = MIN_GROUP,
) -> str:
    """The figures the model may read: every group of at least `floor` people."""
    lines = [f"Data: {title} ({caption})."]
    readable = [r for r in rows if not people or (r[2] or 0) >= floor]
    hidden = len(rows) - len(readable)
    lines.append(" | ".join(head + labels + (["People"] if people else [])))
    for keys, values, count in readable[:80]:
        cells = [
            "—" if v is None else (f"{v:.2f}" if u == "inr" else f"{v.normalize():f}")
            for v, u in zip(values, units, strict=True)
        ]
        lines.append(" | ".join(keys + cells + ([str(count)] if people else [])))
    if len(readable) > 80:
        lines.append(f"(and {len(readable) - 80} more rows, on the person's screen)")
    if people and hidden:
        lines.append(
            f"{hidden} group(s) cover fewer than {MIN_GROUP} people. Their figures are on the "
            "person's screen but not shown to you: do not guess them, and do not work them out "
            "from totals."
        )
    if people and not readable:
        lines.append(
            "Every group here is too small for you to read. Tell the person the chart and table "
            "are below."
        )
    # Movement along a single trend, worked out here so it is never miscounted.
    if readable and len(head) == 1 and len(readable) >= 2 and readable[0][1][0] is not None:
        first, last = readable[0], readable[-1]
        if first[1][0] is not None and last[1][0] is not None and first[1][0] != 0:
            change = (last[1][0] - first[1][0]) / abs(first[1][0]) * 100
            values = [(r[0][0], r[1][0]) for r in readable if r[1][0] is not None]
            high = max(values, key=lambda kv: kv[1])
            low = min(values, key=lambda kv: kv[1])
            lines.append(
                f"From {first[0][0]} to {last[0][0]}, {labels[0]} changed by "
                f"{change:+.1f}%. Highest: {high[0]}; lowest: {low[0]}."
            )
    return "\n".join(lines)
