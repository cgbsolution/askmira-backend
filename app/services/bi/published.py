"""The only module that reads `benchmark_datasets`, and it reads nothing else.

═══════════════════════════════════════════════════════════════════════════════
WHY THIS FILE EXISTS AT ALL
═══════════════════════════════════════════════════════════════════════════════

`benchmark_datasets` is the one table in the product besides `tenants` with no
`tenant_id`, because a published market cell is deliberately identical for every
client — that is what makes it a benchmark rather than a report. So a read of it
cannot pass through `tenant_scoped()`: there is no tenant column to scope on.

Rather than let that exception live inside a large service where it would sit
next to forty scoped queries and be indistinguishable from a mistake, it lives
here, alone. This module:

- imports exactly one model, and that model has no tenant column;
- reads only, and has no write path;
- is never handed a `User`, so no caller's identity can influence what comes
  back — because there is nothing per-caller to influence.

An invariant asserts each of those, so the exemption stays as narrow as this
docstring claims.

═══════════════════════════════════════════════════════════════════════════════
A SUPPRESSED CELL IS STILL RETURNED
═══════════════════════════════════════════════════════════════════════════════

With its reason, not as an absence. "Why is there no benchmark for my segment"
is a question a client asks, and "fewer than eight companies contribute to it"
is a better answer than an empty chart.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bi import BenchmarkDataset

__all__ = ["available_periods", "fetch_cell", "fetch_cells"]


async def fetch_cell(
    session: AsyncSession,
    *,
    metric: str,
    dimension_key: str,
    period: date,
    source: str,
) -> BenchmarkDataset | None:
    """One published cell, or None if it was never computed.

    A cell that exists but is suppressed comes back as a row with
    `published=False` and the reason in `note`. Only a cell nobody has
    aggregated at all is None.
    """
    stmt = select(BenchmarkDataset).where(
        BenchmarkDataset.metric == metric,
        BenchmarkDataset.dimension_key == dimension_key,
        BenchmarkDataset.period == period,
        BenchmarkDataset.source == source,
    )
    return (await session.execute(stmt)).scalars().first()


async def fetch_cells(
    session: AsyncSession,
    *,
    metric: str,
    period: date,
    source: str | None = None,
) -> list[BenchmarkDataset]:
    """Every cell for one metric in one period, suppressed ones included."""
    stmt = select(BenchmarkDataset).where(
        BenchmarkDataset.metric == metric,
        BenchmarkDataset.period == period,
    )
    if source is not None:
        stmt = stmt.where(BenchmarkDataset.source == source)
    return list((await session.execute(stmt)).scalars().all())


async def available_periods(session: AsyncSession, *, metric: str) -> list[date]:
    """Which periods have been aggregated for a metric.

    A client comparing against "the market" should be able to see that the
    market figure is from two quarters ago before they act on it.
    """
    stmt = (
        select(BenchmarkDataset.period)
        .where(BenchmarkDataset.metric == metric)
        .distinct()
        .order_by(BenchmarkDataset.period.desc())
    )
    return list((await session.execute(stmt)).scalars().all())
