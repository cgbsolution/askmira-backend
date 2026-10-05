"""The DB-facing half of the BI connector and benchmarking.

The exposed-schema mapping, the export planning, and the benchmark suppression
arithmetic all live in the pure modules beside this. An invariant checks the
isolation.

═══════════════════════════════════════════════════════════════════════════════
AN EXPORT AND ITS AUDIT ROW ARE ONE TRANSACTION
═══════════════════════════════════════════════════════════════════════════════

Not "the export runs and then we log it". `run_export` builds the plan, projects
the rows, writes the `BiExportRun` with the field list, advances the connection
watermark and commits — once. There is no path that produces rows without a row
naming the fields, because they are the same commit.

═══════════════════════════════════════════════════════════════════════════════
THE WATERMARK MOVES ONLY WHEN THE EXPORT SUCCEEDS
═══════════════════════════════════════════════════════════════════════════════

An export that fails halfway must be repeated from where it started, not from
where it got to. A watermark advanced on a failed run is a permanent gap: the
rows between the old and new watermark are never sent again, and the client
notices as one employee missing from a dashboard forever.

**What is and is not wired**, stated once here:

- `run_export` **produces the rows and the audit row**. It does not write a
  Parquet file, upload to a bucket, or create a Postgres role. The destination
  is recorded; delivery to it is not built.
- Benchmark contributions are written from figures **the caller supplies**.
  Nothing derives them from payroll automatically — a company-level median that
  leaves the building on a schedule is a decision, not a default.
- The aggregation job lives in `aggregation.py` and is not reachable from any
  router. It is run deliberately.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.bi import (
    BenchmarkConsent,
    BenchmarkContribution,
    BenchmarkSourceEnum,
    BiConnection,
    BiDestinationKind,
    BiExportKind,
    BiExportRun,
    BiExportStatus,
)
from app.models.user import User
from app.services import audit
from app.services.bi import benchmarks, export, exposed_schema, published

__all__ = [
    "benchmark_position",
    "bi_posture",
    "create_connection",
    "export_file",
    "list_connections",
    "list_export_runs",
    "record_contribution",
    "run_export",
    "schema_for_role",
    "set_consent",
]


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


# ═══════════════════════════════════════════════════════════════════════════
# The published schema
# ═══════════════════════════════════════════════════════════════════════════


def schema_for_role(user: User) -> dict[str, Any]:
    """The exposed schema as this caller's role would see it.

    Filtered rather than complete, so a client with a reporting-only credential
    is documented the surface they actually have instead of one that
    half-fails at export time.
    """
    return exposed_schema.schema_document(role=permissions.scope_role(user))


# ═══════════════════════════════════════════════════════════════════════════
# Connections
# ═══════════════════════════════════════════════════════════════════════════


async def create_connection(
    session: AsyncSession,
    user: User,
    *,
    name: str,
    entity_name: str,
    destination: BiDestinationKind,
    destination_ref: str,
    export_role: str,
    field_names: list[str] | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Configure one egress path, validating it before it can ever run.

    The plan is built here, at creation time, with a throwaway watermark. A
    connection that names a field its role may not see, or an entity that is
    not in the published schema, is refused now rather than at 2am on the first
    scheduled run.
    """
    try:
        probe = export.plan_export(
            entity_name=entity_name,
            role=export_role,
            kind=export.ExportKind.FULL,
            destination=export.Destination(destination.value),
            destination_ref=destination_ref,
            requested_fields=field_names,
        )
    except (export.ExportError, exposed_schema.SchemaError) as exc:
        raise _bad_request(str(exc)) from exc

    row = BiConnection(
        tenant_id=user.tenant_id,
        name=name.strip(),
        entity_name=entity_name,
        destination=destination,
        destination_ref=destination_ref.strip(),
        export_role=export_role,
        field_names=list(field_names) if field_names is not None else None,
        schema_version=exposed_schema.SCHEMA_VERSION,
        created_by_user_id=user.id,
    )
    session.add(row)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="bi_connection",
        entity_id=row.id,
        actor=user,
        summary=(
            f"BI connection {name!r} created: {entity_name} to "
            f"{destination.value}, {len(probe.fields)} field(s) as "
            f"{export_role}."
        ),
        changes={"fields": list(probe.field_names)},
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return {
        "connection_id": str(row.id),
        "entity": entity_name,
        "schema_version": row.schema_version,
        "fields": list(probe.field_names),
        "withheld_fields": list(probe.withheld_fields),
        "carries_payroll_data": probe.carries_payroll_data,
        "note": (
            "The field list was resolved and refused-if-invalid at creation "
            "time, so a connection cannot fail for the first time on its first "
            "scheduled run."
        ),
    }


async def list_connections(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    stmt = tenant_scoped(select(BiConnection), BiConnection, user).order_by(
        BiConnection.name
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "connection_id": str(row.id),
            "name": row.name,
            "entity": row.entity_name,
            "destination": row.destination.value,
            "destination_ref": row.destination_ref,
            "export_role": row.export_role,
            "schema_version": row.schema_version,
            "last_watermark": (
                row.last_watermark.isoformat() if row.last_watermark else None
            ),
            "is_active": row.is_active,
        }
        for row in rows
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Export
# ═══════════════════════════════════════════════════════════════════════════


async def run_export(
    session: AsyncSession,
    user: User,
    *,
    connection_id: uuid.UUID,
    kind: BiExportKind,
    source_rows: list[dict[str, Any]],
    at: datetime | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Plan, project, audit and advance the watermark - in one transaction.

    `source_rows` is supplied rather than queried: the rows come from the
    report compiler, which already applies row-level scoping, and re-deriving
    them here would be a second copy of that logic. What this function owns is
    the part a connector gets wrong — the field list, the audit row and the
    watermark.
    """
    now = at or datetime.now(UTC)
    stmt = tenant_scoped(
        select(BiConnection).where(BiConnection.id == connection_id),
        BiConnection,
        user,
    )
    connection = (await session.execute(stmt)).scalars().first()
    if connection is None:
        raise _not_found("No such BI connection.")
    if not connection.is_active:
        raise _bad_request(
            "This connection is inactive. Reactivate it deliberately rather "
            "than exporting through a pipe somebody switched off."
        )

    since = connection.last_watermark if kind is BiExportKind.INCREMENTAL else None
    if kind is BiExportKind.INCREMENTAL and since is None:
        raise _bad_request(
            "This connection has never run, so it has no watermark. The first "
            "run is a full export by definition - ask for one, so the audit "
            "trail records it as what it was."
        )

    try:
        plan = export.plan_export(
            entity_name=connection.entity_name,
            role=connection.export_role,
            kind=export.ExportKind(kind.value),
            destination=export.Destination(connection.destination.value),
            destination_ref=connection.destination_ref,
            since=since,
            requested_fields=connection.field_names,
        )
        result = export.extract_rows(plan, source_rows=source_rows)
    except (export.ExportError, exposed_schema.SchemaError) as exc:
        raise _bad_request(str(exc)) from exc

    run = BiExportRun(
        tenant_id=user.tenant_id,
        connection_id=connection.id,
        entity_name=plan.entity_name,
        kind=kind,
        destination=connection.destination,
        destination_ref=plan.destination_ref,
        export_role=plan.role,
        schema_version=plan.schema_version,
        since=plan.since,
        next_watermark=result.next_watermark,
        row_count=result.row_count,
        field_names=list(plan.field_names),
        field_count=len(plan.fields),
        withheld_fields=list(plan.withheld_fields),
        carries_payroll_data=plan.carries_payroll_data,
        status=BiExportStatus.SUCCEEDED,
        started_at=now,
    )
    run.initiated_by_user_id = user.id
    session.add(run)

    # Only on success. A watermark advanced by a run that failed halfway is a
    # permanent gap: the rows in between are never sent again.
    connection.last_watermark = result.next_watermark

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.EXPORT,
        entity_type="bi_export_run",
        entity_id=run.id,
        actor=user,
        summary=(
            f"{kind.value} export of {plan.entity_name}: {result.row_count} "
            f"row(s), {len(plan.fields)} field(s) to "
            f"{connection.destination.value}."
        ),
        changes={
            "fields": list(plan.field_names),
            "carries_payroll_data": plan.carries_payroll_data,
        },
        request=request,
    )
    await session.commit()

    payload = export.audit_payload(result, at=now)
    payload["rows"] = list(result.rows)
    payload["export_run_id"] = str(run.id)
    payload["delivery"] = (
        "Recorded, not delivered. Nothing here writes a Parquet file, uploads "
        "to a bucket or creates a Postgres role - the destination is what the "
        "audit row names, and the transport is not built."
    )
    return payload


async def export_file(
    session: AsyncSession,
    user: User,
    *,
    connection_id: uuid.UUID,
    request: Request | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """A full export of one connection's entity, delivered as a CSV download.

    The rows come from the report compiler, compiled **as the caller** — so the
    row scope and field sensitivity are the report builder's — and are then
    projected by `run_export` onto the connection's planned fields, which writes
    the export run and its audit row in one commit before a byte leaves.
    """
    from app.services.reporting import service as reporting
    from app.services.reporting.compiler import MAX_PAGE_SIZE, Definition, Selection

    stmt = tenant_scoped(
        select(BiConnection).where(BiConnection.id == connection_id), BiConnection, user
    )
    connection = (await session.execute(stmt)).scalars().first()
    if connection is None:
        raise _not_found("No such BI connection.")
    entity = exposed_schema.exposed_entity(connection.entity_name)
    try:
        plan = export.plan_export(
            entity_name=connection.entity_name,
            role=connection.export_role,
            kind=export.ExportKind.FULL,
            destination=export.Destination(connection.destination.value),
            destination_ref=connection.destination_ref,
            requested_fields=connection.field_names,
        )
    except (export.ExportError, exposed_schema.SchemaError) as exc:
        raise _bad_request(str(exc)) from exc

    source_rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        result = await reporting.run_definition(
            session,
            user,
            Definition(
                entity_key=entity.entity_key,
                selections=tuple(
                    Selection(field_key=f.source, label=f.name) for f in plan.fields
                ),
                limit=MAX_PAGE_SIZE,
                offset=offset,
            ),
            request=request,
        )
        keys = [column["key"] for column in result["columns"]]
        by_source = {f.source: f.name for f in plan.fields}
        for values in result["rows"]:
            source_rows.append(
                {by_source.get(key, key): value for key, value in zip(keys, values, strict=False)}
            )
        offset += len(result["rows"])
        if not result["rows"] or offset >= result["total_rows"]:
            break

    payload = await run_export(
        session,
        user,
        connection_id=connection.id,
        kind=BiExportKind.FULL,
        source_rows=source_rows,
        request=request,
    )
    columns = [{"label": name} for name in plan.field_names]
    rows = [[row.get(name) for name in plan.field_names] for row in payload["rows"]]
    content = reporting.to_csv(columns, rows)
    filename = f"{connection.entity_name}-{datetime.now(UTC):%Y%m%d-%H%M}.csv"
    return content, filename, {
        "export_run_id": payload["export_run_id"],
        "row_count": len(rows),
    }


async def list_export_runs(
    session: AsyncSession,
    user: User,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """The egress history, newest first, with the field list on every row."""
    stmt = (
        tenant_scoped(select(BiExportRun), BiExportRun, user)
        .order_by(BiExportRun.started_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "export_run_id": str(row.id),
            "entity": row.entity_name,
            "kind": row.kind.value,
            "destination": row.destination.value,
            "destination_ref": row.destination_ref,
            "role": row.export_role,
            "schema_version": row.schema_version,
            "started_at": row.started_at.isoformat(),
            "row_count": row.row_count,
            "fields": list(row.field_names),
            "field_count": row.field_count,
            "withheld_fields": list(row.withheld_fields),
            "carries_payroll_data": row.carries_payroll_data,
            "status": row.status.value,
        }
        for row in rows
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Benchmark consent and contribution
# ═══════════════════════════════════════════════════════════════════════════


async def set_consent(
    session: AsyncSession,
    user: User,
    *,
    is_contributor: bool,
    contract_reference: str | None = None,
    at: datetime | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Opt in or out of contributing anonymised aggregates.

    Reversible, and revocation **deletes the contributions** rather than only
    flagging the consent. A stored contribution from a client who has withdrawn
    is a row waiting for the next aggregation run to forget the flag.
    """
    now = at or datetime.now(UTC)
    stmt = tenant_scoped(select(BenchmarkConsent), BenchmarkConsent, user)
    row = (await session.execute(stmt)).scalars().first()
    if row is None:
        row = BenchmarkConsent(tenant_id=user.tenant_id, is_contributor=False)
        session.add(row)

    if is_contributor:
        row.is_contributor = True
        row.accepted_by_user_id = user.id
        row.accepted_at = now
        row.revoked_at = None
        row.revoked_by_user_id = None
        row.contract_reference = contract_reference
        removed = 0
    else:
        row.is_contributor = False
        row.revoked_at = now
        row.revoked_by_user_id = user.id
        contributions = (
            await session.execute(
                tenant_scoped(
                    select(BenchmarkContribution), BenchmarkContribution, user
                )
            )
        ).scalars().all()
        removed = len(contributions)
        for contribution in contributions:
            await session.delete(contribution)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="benchmark_consent",
        entity_id=row.id,
        actor=user,
        summary=(
            "Opted in to contribute anonymised benchmark aggregates."
            if is_contributor
            else f"Opted out of benchmark contribution; {removed} "
            "contribution(s) deleted."
        ),
        changes={"is_contributor": is_contributor},
        request=request,
    )
    await session.commit()

    statement = benchmarks.access_statement(is_contributor=is_contributor)
    statement["contributions_removed"] = removed
    return statement


async def record_contribution(
    session: AsyncSession,
    user: User,
    *,
    metric: str,
    dimension_key: str,
    period: date,
    value: Decimal,
    employee_count: int,
    request: Request | None = None,
) -> dict[str, Any]:
    """Store one company-level figure for one cell.

    Refused outright unless this tenant has an active opt-in. Contributing
    without consent is the single thing this feature must never do, and a check
    at the point of writing is worth more than one at the point of reading.
    """
    stmt = tenant_scoped(select(BenchmarkConsent), BenchmarkConsent, user)
    consent = (await session.execute(stmt)).scalars().first()
    if consent is None or not consent.is_contributor or consent.revoked_at is not None:
        raise _bad_request(
            "This tenant has not opted in to contribute anonymised aggregates. "
            "Contribution is an explicit setting and a contract term, not a "
            "default, so nothing is stored until it is switched on."
        )
    if employee_count <= 0:
        raise _bad_request(
            "A contribution needs the headcount behind it. It does not weight "
            "the distribution - one value per company enters that - but it is "
            "what refuses a cell one company dominates."
        )

    existing = (
        await session.execute(
            tenant_scoped(
                select(BenchmarkContribution).where(
                    BenchmarkContribution.metric == metric,
                    BenchmarkContribution.dimension_key == dimension_key,
                    BenchmarkContribution.period == period,
                ),
                BenchmarkContribution,
                user,
            )
        )
    ).scalars().first()
    if existing is None:
        existing = BenchmarkContribution(
            tenant_id=user.tenant_id,
            metric=metric,
            dimension_key=dimension_key,
            period=period,
        )
        session.add(existing)
    existing.value = value
    existing.employee_count = employee_count

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="benchmark_contribution",
        entity_id=existing.id,
        actor=user,
        summary=f"Benchmark contribution recorded for {metric}/{dimension_key}.",
        request=request,
    )
    await session.commit()
    return {
        "metric": metric,
        "dimension": dimension_key,
        "period": period.isoformat(),
        "note": (
            "Stored as a company-level figure. It enters a published cell only "
            f"if at least {benchmarks.MIN_CONTRIBUTORS} companies contribute to "
            "the same cell, and one value per company enters the distribution."
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Reading a benchmark
# ═══════════════════════════════════════════════════════════════════════════


async def benchmark_position(
    session: AsyncSession,
    user: User,
    *,
    metric: str,
    dimension_key: str,
    period: date,
    own_value: Decimal,
    source: BenchmarkSourceEnum = BenchmarkSourceEnum.OWN_CLIENT_BASE,
) -> dict[str, Any]:
    """Where this client sits against the market cell, with the sample size.

    A non-contributing client may read licensed cells and not the aggregate of
    our own client base. That is the deal, and it is stated in the refusal
    rather than discovered as an empty chart.
    """
    stmt = tenant_scoped(select(BenchmarkConsent), BenchmarkConsent, user)
    consent = (await session.execute(stmt)).scalars().first()
    is_contributor = bool(
        consent is not None
        and consent.is_contributor
        and consent.revoked_at is None
    )
    if source is BenchmarkSourceEnum.OWN_CLIENT_BASE and not is_contributor:
        raise _bad_request(
            "Peer benchmarks drawn from our own client base are available to "
            "contributing clients. You have not opted in, so licensed data is "
            "what is available. This is a contract term and a setting, and it "
            "is reversible."
        )

    row = await published.fetch_cell(
        session,
        metric=metric,
        dimension_key=dimension_key,
        period=period,
        source=source.value,
    )
    if row is None:
        raise _not_found(
            f"No benchmark has been aggregated for {metric} / {dimension_key} "
            f"in {period.isoformat()}."
        )

    cell = benchmarks.BenchmarkCell(
        metric=row.metric,
        dimension_key=row.dimension_key,
        source=benchmarks.BenchmarkSource(row.source.value),
        contributors=row.contributors,
        published=row.published,
        p25=Decimal(str(row.p25)) if row.p25 is not None else None,
        p50=Decimal(str(row.p50)) if row.p50 is not None else None,
        p75=Decimal(str(row.p75)) if row.p75 is not None else None,
        note=row.note,
    )
    position = benchmarks.position_against(own_value=own_value, cell=cell)
    payload = position.as_dict()
    payload["access"] = benchmarks.access_statement(is_contributor=is_contributor)
    payload["computed_at"] = row.computed_at.isoformat()
    return payload


# ═══════════════════════════════════════════════════════════════════════════
# Posture
# ═══════════════════════════════════════════════════════════════════════════


def bi_posture() -> dict[str, Any]:
    """What this slice enforces, and what it does not."""
    return {
        "enforced": [
            "The connector serves a published schema, never raw tables. An "
            "exposed column name is the client's contract and an internal "
            "rename repoints a mapping without changing it.",
            "Field-level sensitivity is the report builder's, resolved at "
            "definition time rather than restated - a field that becomes more "
            "sensitive there becomes more sensitive here with no edit.",
            "A field a role may not see is withheld and named, so a client "
            "with an empty column gets an answer instead of a column that "
            "quietly stopped populating.",
            "Every export writes an audit row carrying the field list, the "
            "field count, the destination and a carries_payroll_data boolean, "
            "in the same transaction as the extract.",
            "The watermark advances to the largest change key actually seen "
            "and only on success, so no row falls into a permanent gap.",
            "A benchmark cell below the contributor threshold is not "
            "published - not rounded, not widened, not shown as a range.",
            "A cell one company dominates by headcount is suppressed even when "
            "the contributor count clears the threshold.",
            "One value per company enters the distribution, so a large "
            "contributor cannot weight it by headcount.",
            "A contribution from a tenant outside the consent set raises rather "
            "than being filtered, and the consent set is re-read on every run.",
            "Opting out deletes the contributions as well as clearing the flag.",
            "Every benchmark is labelled with its source and its sample size, "
            "and the percentile is a coarse band rather than an exact figure.",
        ],
        "not_built": [
            "An export is delivered only as a CSV download to the admin who runs "
            "it. No Parquet file is written, no bucket is uploaded to, and no "
            "read-only Postgres role is created - those destinations are recorded "
            "and their transports are not built.",
            "Nothing schedules an export: there is no bucket or role transport for "
            "a schedule to deliver to. A run is initiated deliberately.",
            "Nothing derives a benchmark contribution from payroll "
            "automatically. A company-level figure that leaves the building on "
            "a schedule is a decision, not a default.",
            "No licensed dataset is loaded. The source enum distinguishes one, "
            "and no vendor feed is wired.",
            "No differential-privacy noise is added. Suppression is the control "
            "here: a cell is published intact or not at all, because a noised "
            "cell that is still identifiable is worse than an absent one.",
            "The exposed schema has never been through a breaking change, so "
            "the version-bump and announcement path is written down and "
            "untested against a real client dashboard.",
        ],
    }
