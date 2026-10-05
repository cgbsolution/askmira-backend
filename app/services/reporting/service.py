"""Saving, running, exporting and scheduling reports.

The compiler holds the security properties; this module holds the operational
ones.

**Every run is recorded, and the record says what sensitivity it returned.**
"Who has been pulling salary data, and how often" has to be one query, not a
reconstruction from application logs. A report builder with scheduled
distribution is a data-exfiltration path if nobody is watching it.

**The row cap is checked before the rows are fetched, not after.** Counting
first costs one extra query; fetching first and then discovering the report was
too big means the memory was already spent and the database already did the work.

**The statement timeout is set on the connection for the run and reset after.**
A report builder lets any user write an expensive query, and without a timeout
one of them takes the database down at month end.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request, status
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import has_direct_reports, tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.reporting import (
    Cadence,
    ChartType,
    EmptyBehaviour,
    ExportFormat,
    Report,
    ReportDelivery,
    ReportRun,
    ReportSchedule,
    ReportVisibility,
    RunStatus,
)
from app.models.user import User
from app.services import audit
from app.services.reporting import compiler, library
from app.services.reporting.compiler import (
    MAX_ROWS,
    STATEMENT_TIMEOUT_MS,
    CompileError,
    Definition,
    Filter,
    Operator,
    Scope,
    ScopeKind,
    Selection,
)
from app.services.reporting.semantic import SemanticError, Sensitivity

__all__ = [
    "build_scope",
    "clone_starter",
    "definition_from_json",
    "definition_to_json",
    "due_schedules",
    "export_report",
    "list_schedules",
    "next_run_after",
    "reporting_tree",
    "run_definition",
    "run_saved_report",
    "run_schedule",
    "set_schedule_active",
]

_ORDER = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.HR_ONLY: 1,
    Sensitivity.PAYROLL_ONLY: 2,
}


# ═══════════════════════════════════════════════════════════════════════════
# Scope
# ═══════════════════════════════════════════════════════════════════════════


async def reporting_tree(
    session: AsyncSession, user: User, root_employee_id: uuid.UUID
) -> list[uuid.UUID]:
    """Everybody at or below one employee in the org chart.

    Iterative rather than a recursive CTE, deliberately: a cycle in the manager
    chain — which happens, because somebody eventually sets two people as each
    other's manager — makes a naive CTE loop forever, and `visited` makes that
    impossible here. The depth cap is a second belt on the same trousers.
    """
    visited: set[uuid.UUID] = {root_employee_id}
    frontier = [root_employee_id]
    depth = 0

    while frontier and depth < 20:
        rows = await session.scalars(
            tenant_scoped(
                select(Employee.id).where(Employee.manager_id.in_(frontier)),
                Employee,
                user,
            )
        )
        found = [row for row in rows.all() if row not in visited]
        visited.update(found)
        frontier = found
        depth += 1

    return sorted(visited, key=str)


async def build_scope(session: AsyncSession, user: User) -> Scope:
    """What this requester may see, resolved once per run."""
    kind = compiler.resolve_scope(_role_of(user))

    if kind is ScopeKind.TENANT:
        return Scope(kind=kind, tenant_id=user.tenant_id)

    employee_id = getattr(user, "employee_id", None)
    if kind is ScopeKind.SELF:
        return Scope(kind=kind, tenant_id=user.tenant_id, employee_id=employee_id)

    if employee_id is None:
        # A manager with no employee record manages nobody we can identify.
        # An empty tree rather than an error: the report runs and returns
        # nothing, which is the honest answer.
        return Scope(kind=kind, tenant_id=user.tenant_id)

    tree = await reporting_tree(session, user, employee_id)
    return Scope(
        kind=kind,
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        visible_employee_ids=tuple(tree),
    )


def _role_of(user: User) -> str:
    return permissions.scope_role(user)


# ═══════════════════════════════════════════════════════════════════════════
# Definition serialisation
# ═══════════════════════════════════════════════════════════════════════════


def definition_to_json(definition: Definition) -> dict[str, Any]:
    return {
        "entity_key": definition.entity_key,
        "selections": [
            {"field_key": s.field_key, "aggregation": s.aggregation, "label": s.label}
            for s in definition.selections
        ],
        "filters": [
            {
                "field_key": f.field_key,
                "operator": f.operator.value,
                "value": _plain(f.value),
                "values": [_plain(v) for v in f.values],
            }
            for f in definition.filters
        ],
        "group_by": list(definition.group_by),
        "sort_by": [list(pair) for pair in definition.sort_by],
        "limit": definition.limit,
        "offset": definition.offset,
    }


def _plain(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def definition_from_json(payload: dict[str, Any]) -> Definition:
    """Rebuild a definition from stored JSON.

    Everything here is validated by the compiler on use, so this stays a
    straight decode. An unknown operator raises here, which is the right place:
    a definition that cannot be decoded should not reach the compiler at all.
    """
    try:
        return Definition(
            entity_key=str(payload["entity_key"]),
            selections=tuple(
                Selection(
                    field_key=str(s["field_key"]),
                    aggregation=s.get("aggregation") or None,
                    label=s.get("label") or "",
                )
                for s in payload.get("selections", [])
            ),
            filters=tuple(
                Filter(
                    field_key=str(f["field_key"]),
                    operator=Operator(f["operator"]),
                    value=f.get("value"),
                    values=tuple(f.get("values") or ()),
                )
                for f in payload.get("filters", [])
            ),
            group_by=tuple(str(g) for g in payload.get("group_by", [])),
            sort_by=tuple(
                (str(pair[0]), str(pair[1])) for pair in payload.get("sort_by", [])
            ),
            limit=int(payload.get("limit") or compiler.DEFAULT_PAGE_SIZE),
            offset=int(payload.get("offset") or 0),
        )
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"This report definition cannot be read: {exc}",
        ) from exc


# ═══════════════════════════════════════════════════════════════════════════
# Running
# ═══════════════════════════════════════════════════════════════════════════


async def run_definition(
    session: AsyncSession,
    user: User,
    definition: Definition,
    *,
    report: Report | None = None,
    schedule_id: uuid.UUID | None = None,
    today: date | None = None,
    export_format: ExportFormat | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Compile, count, run, and record.

    Counting first is not an optimisation. Fetching and then discovering the
    report was too big means the memory was already spent and the database
    already did the work — and the user gets an error after the cost, which is
    the worst of both.
    """
    role = _role_of(user)
    scope = await build_scope(session, user)
    started = datetime.now(UTC)

    run = ReportRun(
        tenant_id=user.tenant_id,
        report_id=report.id if report else None,
        schedule_id=schedule_id,
        run_by_user_id=user.id,
        run_as_role=role,
        row_scope=scope.kind.value,
        status=RunStatus.RUNNING,
        definition_snapshot=definition_to_json(definition),
        export_format=export_format,
    )
    session.add(run)
    await session.flush()

    try:
        compiled = compiler.compile_report(
            definition, role=role, scope=scope, today=today
        )
    except (CompileError, SemanticError) as exc:
        run.status = RunStatus.FAILED
        run.error = str(exc)
        await session.flush()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    highest = Sensitivity.PUBLIC
    for column in compiled.columns:
        level = Sensitivity(column["sensitivity"])
        if _ORDER[level] > _ORDER[highest]:
            highest = level
    run.max_sensitivity = highest.value

    # A report builder lets any user write an expensive query. Without this, one
    # of them takes the database down at month end.
    await session.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))

    try:
        total = await session.scalar(compiled.count_statement)
        total = int(total or 0)

        if total > MAX_ROWS:
            run.status = RunStatus.TOO_LARGE
            run.row_count = total
            run.error = f"{total} rows"
            await session.flush()
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"This report would return {total:,} rows and the limit is "
                    f"{MAX_ROWS:,}. Narrow it with a filter or a date range — a "
                    "truncated register is a number somebody would file."
                ),
            )

        result = await session.execute(compiled.statement)
        rows = [
            [compiler.serialise_value(value) for value in row]
            for row in result.all()
        ]
    except DBAPIError as exc:
        run.status = RunStatus.FAILED
        message = str(getattr(exc, "orig", exc))
        run.error = message[:2000]
        await session.flush()
        timed_out = "statement timeout" in message.lower() or "canceling" in message.lower()
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT
            if timed_out
            else status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"This report took longer than {STATEMENT_TIMEOUT_MS // 1000} seconds "
                "and was stopped. Narrow the date range or remove a grouping — it is "
                "not going to finish on its own."
                if timed_out
                else "This report could not be run."
            ),
        ) from exc

    run.status = RunStatus.SUCCEEDED
    run.row_count = len(rows)
    run.duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    if report is not None:
        report.last_run_at = started
    await session.flush()

    # A run that returned restricted data is audited, not just recorded. The
    # audit log is the thing somebody actually reviews.
    if highest is not Sensitivity.PUBLIC:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            actor=user,
            action=AuditAction.EXPORT if export_format else AuditAction.SENSITIVE_READ,
            entity_type="report_run",
            entity_id=run.id,
            summary=(
                f"{role} ran a report returning {highest.value} data: "
                f"{len(rows)} row(s) from {definition.entity_key}"
                + (f", exported as {export_format.value}" if export_format else "")
            ),
            changes={
                "columns": [c["key"] for c in compiled.columns],
                "row_scope": scope.kind.value,
            },
            request=request,
        )

    return {
        "run_id": str(run.id),
        "columns": list(compiled.columns),
        "rows": rows,
        "total_rows": total,
        "returned_rows": len(rows),
        "max_sensitivity": highest.value,
        "row_scope": scope.kind.value,
        "notes": list(compiled.notes),
        "denied_fields": list(compiled.denied_fields),
        "duration_ms": run.duration_ms,
    }


async def _load_report(session: AsyncSession, user: User, report_id: uuid.UUID) -> Report:
    report = await session.scalar(
        tenant_scoped(select(Report).where(Report.id == report_id), Report, user)
    )
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")

    if report.visibility is ReportVisibility.PRIVATE and report.owner_user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Report not found"
        )
    if (
        report.visibility is ReportVisibility.SHARED
        and report.owner_user_id != user.id
        and str(user.id) not in [str(u) for u in (report.shared_with_user_ids or [])]
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Report not found"
        )
    return report


async def run_saved_report(
    session: AsyncSession,
    user: User,
    *,
    report_id: uuid.UUID,
    limit: int | None = None,
    offset: int | None = None,
    today: date | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Run a saved report as the person asking, not as its author.

    A shared report is a shared *question*. Running it as its author would turn
    every shared salary report into a privilege-escalation route.
    """
    report = await _load_report(session, user, report_id)
    definition = definition_from_json(report.definition)
    if limit is not None or offset is not None:
        definition = Definition(
            entity_key=definition.entity_key,
            selections=definition.selections,
            filters=definition.filters,
            group_by=definition.group_by,
            sort_by=definition.sort_by,
            limit=limit if limit is not None else definition.limit,
            offset=offset if offset is not None else definition.offset,
        )
    return await run_definition(
        session, user, definition, report=report, today=today, request=request
    )


# ═══════════════════════════════════════════════════════════════════════════
# Export
# ═══════════════════════════════════════════════════════════════════════════


def to_csv(columns: list[dict[str, Any]], rows: list[list[Any]]) -> str:
    """CSV with the formula-injection guard Excel needs.

    A cell beginning `=`, `+`, `-` or `@` is executed as a formula when the file
    is opened. An employee whose display name is `=cmd|'/c calc'!A1` is a real
    attack, and the export is the delivery mechanism. Prefixing a single quote
    makes Excel treat it as text.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([c["label"] for c in columns])
    for row in rows:
        writer.writerow([_csv_safe(value) for value in row])
    return buffer.getvalue()


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


async def export_report(
    session: AsyncSession,
    user: User,
    *,
    report_id: uuid.UUID | None = None,
    definition: Definition | None = None,
    export_format: ExportFormat = ExportFormat.CSV,
    today: date | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Run and serialise. Restricted-field exports are audited by `run_definition`."""
    if report_id is not None:
        report = await _load_report(session, user, report_id)
        definition = definition_from_json(report.definition)
    else:
        report = None
    if definition is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Nothing to export.",
        )

    # An export is not a page. It takes the whole result, up to the cap.
    full = Definition(
        entity_key=definition.entity_key,
        selections=definition.selections,
        filters=definition.filters,
        group_by=definition.group_by,
        sort_by=definition.sort_by,
        limit=compiler.MAX_PAGE_SIZE,
        offset=0,
    )
    result = await run_definition(
        session, user, full, report=report, today=today,
        export_format=export_format, request=request,
    )

    body = to_csv(result["columns"], result["rows"])
    # No spreadsheet library is installed, so every export is CSV — and is named
    # and labelled CSV. A file called .xlsx that is really CSV is a small lie
    # that Excel repeats to the user as a warning dialog.
    return {
        **result,
        "format": ExportFormat.CSV.value,
        "requested_format": export_format.value,
        "format_note": (
            None
            if export_format is ExportFormat.CSV
            else "Excel workbooks are not produced by this server; this file is CSV, "
            "which Excel opens directly."
        ),
        "filename": (
            f"{(report.name if report else definition.entity_key)}"
            f"-{datetime.now(UTC):%Y%m%d}.csv"
        ).replace(" ", "-").lower(),
        "content": body,
        # Above this, a synchronous download is a request that times out on a
        # slow connection. The caller queues it and notifies instead.
        "should_deliver_asynchronously": len(result["rows"]) > 5_000,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Scheduling
# ═══════════════════════════════════════════════════════════════════════════


def next_run_after(
    schedule: ReportSchedule, *, after: datetime
) -> datetime:
    """When this schedule is next due, in its own timezone.

    Computed in the recipient's zone and converted, not the other way round. A
    "Monday 9am" report scheduled in UTC arrives at 2:30pm IST, and one
    scheduled naively in IST arrives at 3:30am for a recipient in London.
    """
    zone = ZoneInfo(schedule.timezone_name)
    local = after.astimezone(zone)
    candidate = local.replace(
        hour=schedule.hour_of_day, minute=0, second=0, microsecond=0
    )

    if schedule.cadence is Cadence.DAILY:
        if candidate <= local:
            candidate += timedelta(days=1)

    elif schedule.cadence is Cadence.WEEKLY:
        target = schedule.day_of_week if schedule.day_of_week is not None else 0
        delta = (target - candidate.weekday()) % 7
        candidate += timedelta(days=delta)
        if candidate <= local:
            candidate += timedelta(days=7)

    elif schedule.cadence in (Cadence.MONTHLY, Cadence.QUARTERLY):
        step = 1 if schedule.cadence is Cadence.MONTHLY else 3
        day = schedule.day_of_month or 1
        candidate = _on_day(candidate, day)
        while candidate <= local:
            candidate = _on_day(_add_months(candidate, step), day)

    # Converted back at the end, once, after the local arithmetic is done —
    # which is what keeps it correct across a DST change in the recipient's zone.
    return candidate.astimezone(UTC)


def _add_months(moment: datetime, months: int) -> datetime:
    month = moment.month - 1 + months
    year = moment.year + month // 12
    month = month % 12 + 1
    return moment.replace(year=year, month=month, day=1)


def _on_day(moment: datetime, day: int) -> datetime:
    """Clamp to the last day of a short month.

    A schedule set for the 31st must still fire in February, on the 28th, rather
    than skipping the month entirely.
    """
    next_month = _add_months(moment.replace(day=1), 1)
    last = (next_month - timedelta(days=1)).day
    return moment.replace(day=min(day, last))


async def due_schedules(
    session: AsyncSession, user: User, *, as_of: datetime | None = None
) -> list[ReportSchedule]:
    moment = as_of or datetime.now(UTC)
    rows = await session.scalars(
        tenant_scoped(
            select(ReportSchedule).where(
                ReportSchedule.is_active.is_(True),
                ReportSchedule.next_run_at.is_not(None),
                ReportSchedule.next_run_at <= moment,
            ),
            ReportSchedule,
            user,
        )
    )
    return list(rows.all())


async def list_schedules(
    session: AsyncSession, user: User, *, include_all: bool
) -> list[dict[str, Any]]:
    """Schedules the caller created — or every schedule, for an admin."""
    stmt = tenant_scoped(
        select(ReportSchedule, Report).join(Report, Report.id == ReportSchedule.report_id),
        ReportSchedule,
        user,
    )
    if not include_all:
        stmt = stmt.where(ReportSchedule.created_by_user_id == user.id)
    rows = (await session.execute(stmt.order_by(ReportSchedule.created_at.desc()))).all()
    return [
        {
            "id": str(schedule.id),
            "report_id": str(report.id),
            "report_name": report.name,
            "cadence": schedule.cadence.value,
            "hour_of_day": schedule.hour_of_day,
            "day_of_week": schedule.day_of_week,
            "day_of_month": schedule.day_of_month,
            "timezone": schedule.timezone_name,
            "recipients": len(schedule.recipient_user_ids or []),
            "format": ExportFormat.CSV.value,
            "on_empty": schedule.on_empty.value,
            "is_active": schedule.is_active,
            "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
            "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
            "mine": schedule.created_by_user_id == user.id,
        }
        for schedule, report in rows
    ]


async def set_schedule_active(
    session: AsyncSession,
    user: User,
    *,
    schedule_id: uuid.UUID,
    active: bool,
    is_admin: bool,
    request: Request | None = None,
) -> dict[str, Any]:
    schedule = await session.scalar(
        tenant_scoped(
            select(ReportSchedule).where(ReportSchedule.id == schedule_id), ReportSchedule, user
        )
    )
    if schedule is None or (not is_admin and schedule.created_by_user_id != user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    schedule.is_active = active
    if active:
        schedule.next_run_at = next_run_after(schedule, after=datetime.now(UTC))
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="report_schedule",
        entity_id=schedule.id,
        summary=f"Report schedule {'resumed' if active else 'paused'}",
        request=request,
    )
    await session.commit()
    return {"id": str(schedule.id), "is_active": schedule.is_active}


async def run_schedule(
    session: AsyncSession,
    user: User,
    *,
    schedule_id: uuid.UUID,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Send a scheduled report, recording every recipient.

    Each recipient's copy is compiled **as that recipient**. A schedule that
    compiled once as its creator and mailed the same file to everybody would send
    a manager the whole tenant's salaries because an HR admin set it up — which
    is precisely the exfiltration path this slice has to close.
    """
    moment = as_of or datetime.now(UTC)
    schedule = await session.scalar(
        tenant_scoped(
            select(ReportSchedule).where(ReportSchedule.id == schedule_id),
            ReportSchedule,
            user,
        )
    )
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")

    report = await session.scalar(
        tenant_scoped(select(Report).where(Report.id == schedule.report_id), Report, user)
    )
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The report this schedule points at no longer exists.",
        )

    definition = definition_from_json(report.definition)
    deliveries: list[dict[str, Any]] = []
    skipped: list[str] = []

    for raw_id in schedule.recipient_user_ids or []:
        recipient = await session.get(User, uuid.UUID(str(raw_id)))
        if recipient is None or not recipient.is_active:
            skipped.append(f"{raw_id} (no longer an active user)")
            continue
        # Loaded here rather than signed in, so their reach is worked out here.
        recipient.has_reports = await has_direct_reports(session, recipient)
        if recipient.tenant_id != user.tenant_id:
            # Should be impossible; checked because a scheduled send to another
            # tenant is the single worst outcome available here.
            skipped.append(f"{recipient.email} (different tenant — not sent)")
            continue

        try:
            result = await run_definition(
                session,
                recipient,
                definition,
                report=report,
                schedule_id=schedule.id,
                today=moment.astimezone(ZoneInfo(schedule.timezone_name)).date(),
            )
        except HTTPException as exc:
            skipped.append(f"{recipient.email} ({exc.detail})")
            continue

        if not result["rows"] and schedule.on_empty is EmptyBehaviour.SKIP:
            skipped.append(f"{recipient.email} (empty, and this schedule skips empties)")
            continue

        session.add(
            ReportDelivery(
                tenant_id=user.tenant_id,
                run_id=uuid.UUID(result["run_id"]),
                schedule_id=schedule.id,
                recipient_user_id=recipient.id,
                recipient_email=recipient.email,
                sent_at=moment,
                export_format=schedule.export_format,
                row_count=result["returned_rows"],
                max_sensitivity=result["max_sensitivity"],
            )
        )
        deliveries.append(
            {
                "recipient": recipient.email,
                "rows": result["returned_rows"],
                "max_sensitivity": result["max_sensitivity"],
            }
        )

    schedule.last_run_at = moment
    schedule.next_run_at = next_run_after(schedule, after=moment)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.EXPORT,
        entity_type="report_schedule",
        entity_id=schedule.id,
        summary=(
            f"Scheduled report '{report.name}' sent to {len(deliveries)} recipient(s)"
            + (f", {len(skipped)} skipped" if skipped else "")
        ),
        changes={
            "recipients": [d["recipient"] for d in deliveries],
            "skipped": skipped,
            "max_sensitivity": max(
                (d["max_sensitivity"] for d in deliveries), default="public"
            ),
        },
    )
    return {
        "schedule_id": str(schedule.id),
        "delivered": deliveries,
        "skipped": skipped,
        "next_run_at": schedule.next_run_at.isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Starters
# ═══════════════════════════════════════════════════════════════════════════


async def clone_starter(
    session: AsyncSession,
    user: User,
    *,
    starter_key: str,
    name: str | None = None,
) -> Report:
    """Copy a starter into an editable report the client owns."""
    starter = next((s for s in library.STARTERS if s.key == starter_key), None)
    if starter is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{starter_key}' is not a starter report.",
        )

    from app.services.reporting.semantic import may_see

    if not may_see(_role_of(user), starter.required_sensitivity):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"'{starter.name}' needs {starter.required_sensitivity.value} access. "
                "Cloning it would produce a report you could not run."
            ),
        )

    report = Report(
        tenant_id=user.tenant_id,
        name=name or starter.name,
        description=starter.description,
        entity_key=starter.definition.entity_key,
        definition=definition_to_json(starter.definition),
        chart_type=ChartType(starter.chart_type),
        visibility=ReportVisibility.PRIVATE,
        owner_user_id=user.id,
        created_from_starter=starter.key,
    )
    session.add(report)
    await session.flush()
    return report
