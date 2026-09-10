"""The BI connector and benchmarking surface (slice 37).

Three endpoints carry the weight:

- `GET /bi/schema` returns the published schema **filtered to the caller's
  role**, so a client with a reporting-only credential is documented the surface
  they actually have rather than one that half-fails at export time.
- `POST /bi/connections/{id}/export` produces the rows and the audit row in one
  transaction. There is no path to the first without the second.
- `PUT /bi/benchmark-consent` is the opt-in, and opting out deletes the
  contributions rather than only clearing a flag.

The aggregation job is deliberately **not** routed. It is the one cross-tenant
read in the product, and making it reachable from an HTTP request would let a
caller aim it at a segment of their choosing.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.bi import (
    BenchmarkSourceEnum,
    BiDestinationKind,
    BiExportKind,
)
from app.models.user import UserRole
from app.services.bi import benchmarks as benchmark_engine
from app.services.bi import exposed_schema as schema_engine
from app.services.bi import service as bi

router = APIRouter(tags=["bi"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)


# ── Contracts ───────────────────────────────────────────────────────────────


class ConnectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    #: A name from the published schema, never a table name.
    entity_name: str = Field(min_length=1, max_length=64)
    destination: BiDestinationKind
    #: The role name or the bucket URI. "To where" is half of what the audit
    #: row is for, so a connection cannot exist without it.
    destination_ref: str = Field(min_length=1, max_length=500)
    export_role: str = Field(min_length=1, max_length=50)
    #: Omit for "every field this role may see". A list narrows it further.
    field_names: list[str] | None = None


class ExportIn(BaseModel):
    kind: BiExportKind = BiExportKind.INCREMENTAL
    #: Supplied by the caller: the rows come from the report compiler, which
    #: already applies row-level scoping. Re-deriving them here would be a
    #: second copy of that logic, and the copy that drifts is the one nobody is
    #: testing.
    source_rows: list[dict[str, Any]] = Field(default_factory=list)


class ConsentIn(BaseModel):
    is_contributor: bool
    #: The contract clause this was agreed under. Consent that exists only as a
    #: checkbox is consent nobody can evidence.
    contract_reference: str | None = Field(default=None, max_length=200)


class ContributionIn(BaseModel):
    metric: str = Field(min_length=1, max_length=64)
    dimension_key: str = Field(min_length=1, max_length=200)
    period: date
    value: Decimal
    #: Not used to weight the distribution - one value per company enters that.
    #: Used to refuse a cell one company dominates.
    employee_count: int = Field(gt=0)


# ── Posture and schema ──────────────────────────────────────────────────────


@router.get("/bi/posture")
async def posture(user: CurrentUser) -> dict[str, Any]:
    return bi.bi_posture()


@router.get("/bi/schema")
async def schema(user: CurrentUser) -> dict[str, Any]:
    """The published schema, with its stability guarantee, for this role.

    Column names here are a contract. They are versioned separately from our
    internal tables: an internal rename repoints a mapping and changes nothing
    a client's dashboard depends on.
    """
    return bi.schema_for_role(user)


@router.get("/bi/schema/entities")
async def schema_entities(user: CurrentUser) -> dict[str, Any]:
    """Just the entity names and the schema version.

    Enough for a client to check they are on the current version without
    downloading the whole document on every poll.
    """
    return {
        "schema_version": schema_engine.SCHEMA_VERSION,
        "entities": sorted(schema_engine.EXPOSED_SCHEMA),
    }


# ── Connections ─────────────────────────────────────────────────────────────


@router.post("/bi/connections", status_code=201)
async def create_connection(
    body: ConnectionIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Configure an egress path, resolving its field list now rather than at
    2am on the first scheduled run."""
    return await bi.create_connection(
        session,
        user,
        name=body.name,
        entity_name=body.entity_name,
        destination=body.destination,
        destination_ref=body.destination_ref,
        export_role=body.export_role,
        field_names=body.field_names,
        request=request,
    )


@router.get("/bi/connections")
async def list_connections(
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> list[dict[str, Any]]:
    return await bi.list_connections(session, user)


@router.post("/bi/connections/{connection_id}/export")
async def run_export(
    connection_id: uuid.UUID,
    body: ExportIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Plan, project, audit and advance the watermark - in one transaction.

    The response is the audit payload plus the projected rows, so what the
    caller receives and what the audit row records are the same field list.
    """
    return await bi.run_export(
        session,
        user,
        connection_id=connection_id,
        kind=body.kind,
        source_rows=body.source_rows,
        request=request,
    )


@router.get("/bi/export-runs")
async def list_export_runs(
    session: DbSession,
    user: CurrentUser,
    limit: int = Query(default=100, ge=1, le=500),
    _hr=Depends(require_roles(*HR_ROLES)),
) -> list[dict[str, Any]]:
    """The egress history, with the field list on every row.

    A BI connector is the largest quantity of personal data that leaves this
    system, on a schedule, unattended. This is the page somebody reads after an
    incident.
    """
    return await bi.list_export_runs(session, user, limit=limit)


# ── Benchmarking ────────────────────────────────────────────────────────────


@router.get("/bi/benchmark-terms")
async def benchmark_terms(user: CurrentUser) -> dict[str, Any]:
    """The deal, stated rather than buried.

    Contributing clients see the peer aggregate; non-contributing clients see
    licensed data only. That is fair, and it stops being fair the moment
    somebody discovers it as a surprise while trying to open a chart.
    """
    return {
        "minimum_contributors": benchmark_engine.MIN_CONTRIBUTORS,
        "sources": [s.value for s in benchmark_engine.BenchmarkSource],
        "statement": (
            "A cell is published only when at least "
            f"{benchmark_engine.MIN_CONTRIBUTORS} companies contribute to it, "
            "one value per company enters the distribution, and a cell one "
            "company dominates by headcount is suppressed even when the "
            "contributor count clears the threshold. Opting in is a contract "
            "term and a product setting, and it is reversible."
        ),
    }


@router.put("/bi/benchmark-consent")
async def set_consent(
    body: ConsentIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Opt in or out of contributing anonymised aggregates.

    Opting out deletes the contributions as well as clearing the flag. A stored
    contribution from a client who has withdrawn is a row waiting for the next
    aggregation run to forget the flag.
    """
    return await bi.set_consent(
        session,
        user,
        is_contributor=body.is_contributor,
        contract_reference=body.contract_reference,
        request=request,
    )


@router.post("/bi/benchmark-contributions", status_code=201)
async def record_contribution(
    body: ContributionIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Store one company-level figure. Refused without an active opt-in."""
    return await bi.record_contribution(
        session,
        user,
        metric=body.metric,
        dimension_key=body.dimension_key,
        period=body.period,
        value=body.value,
        employee_count=body.employee_count,
        request=request,
    )


@router.get("/bi/benchmark-position")
async def benchmark_position(
    session: DbSession,
    user: CurrentUser,
    metric: str = Query(min_length=1, max_length=64),
    dimension_key: str = Query(min_length=1, max_length=200),
    period: date = Query(),
    own_value: Decimal = Query(),
    source: BenchmarkSourceEnum = BenchmarkSourceEnum.OWN_CLIENT_BASE,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Percentile position with the sample size, not an average.

    "The market pays 18 lakh" causes a company at 16 to raise everybody and a
    company at 22 to conclude they are overpaying. A banded position with the
    quartiles and the contributor count beside it causes neither.
    """
    return await bi.benchmark_position(
        session,
        user,
        metric=metric,
        dimension_key=dimension_key,
        period=period,
        own_value=own_value,
        source=source,
    )
