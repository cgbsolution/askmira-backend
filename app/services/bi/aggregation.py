"""The one cross-tenant read in the product, and the fence around it.

═══════════════════════════════════════════════════════════════════════════════
THIS IS A JOB, NOT A REQUEST
═══════════════════════════════════════════════════════════════════════════════

Building a market benchmark means reading many companies' contributions in one
query. There is no honest way to make that tenant-scoped — scoping it would
produce a benchmark of one company, which is the thing the suppression rules
exist to prevent.

So the exemption is here, in a module small enough to read in a sitting, and it
is fenced by properties an invariant can check:

- **No router imports it.** It is not reachable from an HTTP request, so no
  caller can aim it at a segment of their choosing.
- **It never takes a `User`.** There is no identity in scope to leak into a
  cell, and no per-caller behaviour to get wrong.
- **It reads two models only** — `BenchmarkConsent` and
  `BenchmarkContribution`. No employee row, no payslip, no salary. The values
  it aggregates were already reduced to company-level statistics by the
  tenant-scoped service that wrote them.
- **The consent set is computed here and passed to `aggregate`**, which
  *raises* on a row from a tenant outside it. A silent filter cannot be told
  apart from a filter that was never applied, and whether an opted-out client
  was excluded is precisely what they will ask us to demonstrate.

═══════════════════════════════════════════════════════════════════════════════
CONSENT IS RE-READ ON EVERY RUN
═══════════════════════════════════════════════════════════════════════════════

Not cached, not passed in. A client who revoked yesterday must be absent from
today's cell, and the only way that is true by construction is if the consent
set and the contributions are read in the same transaction, each time.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bi import (
    BenchmarkConsent,
    BenchmarkContribution,
    BenchmarkDataset,
    BenchmarkSourceEnum,
)
from app.services.bi import benchmarks

__all__ = ["AggregationOutcome", "consenting_tenant_ids", "run_aggregation"]


@dataclass(frozen=True, slots=True)
class AggregationOutcome:
    """What one run produced, including what it refused to publish."""

    metric: str
    period: date
    contributing_tenants: int
    cells_written: int
    cells_published: int
    cells_suppressed: int

    def as_dict(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "period": self.period.isoformat(),
            "contributing_tenants": self.contributing_tenants,
            "cells_written": self.cells_written,
            "cells_published": self.cells_published,
            "cells_suppressed": self.cells_suppressed,
            "note": (
                f"{self.cells_suppressed} cell(s) were computed and not "
                "published, each stored with its reason so a client asking why "
                "their segment has no benchmark gets an answer rather than an "
                "empty chart."
            ),
        }


async def consenting_tenant_ids(session: AsyncSession) -> set[str]:
    """Who has opted in, right now.

    Read on every run rather than cached or supplied. A client who revoked
    yesterday is absent from today's cell only if this is re-read, and a
    consent set passed in by a caller is a consent set a caller can widen.
    """
    stmt = select(BenchmarkConsent.tenant_id).where(
        BenchmarkConsent.is_contributor.is_(True),
        BenchmarkConsent.revoked_at.is_(None),
    )
    return {str(row) for row in (await session.execute(stmt)).scalars().all()}


async def run_aggregation(
    session: AsyncSession,
    *,
    metric: str,
    period: date,
    at: datetime,
    source: BenchmarkSourceEnum = BenchmarkSourceEnum.OWN_CLIENT_BASE,
) -> AggregationOutcome:
    """Recompute every cell of one metric for one period.

    Contributions from tenants outside the consent set are **not filtered**:
    they are excluded from the query, and `aggregate` raises if one reaches it
    anyway. Two independent mechanisms, because this is the property a client
    will ask us to prove and one mechanism proves nothing about the other.
    """
    consenting = await consenting_tenant_ids(session)
    if not consenting:
        return AggregationOutcome(
            metric=metric,
            period=period,
            contributing_tenants=0,
            cells_written=0,
            cells_published=0,
            cells_suppressed=0,
        )

    stmt = select(BenchmarkContribution).where(
        BenchmarkContribution.metric == metric,
        BenchmarkContribution.period == period,
        BenchmarkContribution.tenant_id.in_([uuid.UUID(t) for t in consenting]),
    )
    rows = list((await session.execute(stmt)).scalars().all())

    contributions = [
        benchmarks.Contribution(
            tenant_id=str(row.tenant_id),
            metric=row.metric,
            dimension_key=row.dimension_key,
            value=Decimal(str(row.value)),
            employee_count=row.employee_count,
        )
        for row in rows
    ]

    dimension_keys = sorted({c.dimension_key for c in contributions})
    written = published = suppressed = 0
    for dimension_key in dimension_keys:
        cell = benchmarks.aggregate(
            contributions,
            consenting_tenant_ids=consenting,
            metric=metric,
            dimension_key=dimension_key,
            source=benchmarks.BenchmarkSource(source.value),
        )
        await _upsert_cell(session, cell=cell, period=period, at=at)
        written += 1
        if cell.published:
            published += 1
        else:
            suppressed += 1

    await session.commit()
    return AggregationOutcome(
        metric=metric,
        period=period,
        contributing_tenants=len({c.tenant_id for c in contributions}),
        cells_written=written,
        cells_published=published,
        cells_suppressed=suppressed,
    )


async def _upsert_cell(
    session: AsyncSession,
    *,
    cell: benchmarks.BenchmarkCell,
    period: date,
    at: datetime,
) -> None:
    """Replace the stored cell for this metric/dimension/period/source.

    Recomputed rather than accumulated: a cell is a statement about the
    contributors as they are now, and a client who revoked consent must
    disappear from it on the next run rather than persist in a row nobody
    rewrites.
    """
    stmt = select(BenchmarkDataset).where(
        BenchmarkDataset.metric == cell.metric,
        BenchmarkDataset.dimension_key == cell.dimension_key,
        BenchmarkDataset.period == period,
        BenchmarkDataset.source == cell.source.value,
    )
    existing = (await session.execute(stmt)).scalars().first()
    if existing is None:
        existing = BenchmarkDataset(
            metric=cell.metric,
            dimension_key=cell.dimension_key,
            period=period,
            source=BenchmarkSourceEnum(cell.source.value),
        )
        session.add(existing)

    existing.contributors = cell.contributors
    existing.published = cell.published
    existing.p25 = cell.p25
    existing.p50 = cell.p50
    existing.p75 = cell.p75
    existing.note = cell.note
    existing.computed_at = at
    await session.flush()
