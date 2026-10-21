"""Benchmarks: opt-in, suppression, noise, and the percentile that matters.

═══════════════════════════════════════════════════════════════════════════════
"SaaS, 200-500 EMPLOYEES, BENGALURU" WITH THREE CONTRIBUTORS IS NOT ANONYMOUS
═══════════════════════════════════════════════════════════════════════════════

It is three named companies, and anybody in that market can name them. The
median salary of that cell is then approximately one competitor's salary band,
published to the other two.

So a cell below `MIN_CONTRIBUTORS` is not published — not rounded, not widened,
not shown as a range. And the count is reported alongside every cell that *is*
published, because a benchmark from six companies and one from six hundred are
different claims and presenting them identically invites the same decision.

═══════════════════════════════════════════════════════════════════════════════
OPT-IN IS A CONTRACT TERM AND A PRODUCT SETTING, NOT A DEFAULT
═══════════════════════════════════════════════════════════════════════════════

`aggregate` takes the contributing tenants explicitly and **refuses any row from
a tenant not in that set**. Not filters — refuses, loudly, because a silent
filter is indistinguishable from a filter that was never applied, and this is
the one thing a client will ask us to prove.

And the deal is stated rather than hidden: contributing clients see the
aggregate, non-contributing clients see licensed data only. That is a fair
arrangement and it stops being fair the moment it is a surprise.

═══════════════════════════════════════════════════════════════════════════════
AN AVERAGE WITHOUT A DISTRIBUTION INVITES BAD DECISIONS
═══════════════════════════════════════════════════════════════════════════════

"The market pays 18 lakh" causes a company at 16 to raise everybody, and a
company at 22 to conclude they are overpaying. The percentile position with the
quartiles beside it causes neither.

Pure. No clock, no session.
"""

from __future__ import annotations

import enum
import statistics
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "MIN_CONTRIBUTORS",
    "BenchmarkCell",
    "BenchmarkError",
    "BenchmarkSource",
    "Contribution",
    "aggregate",
    "position_against",
]


class BenchmarkError(ValueError):
    """A benchmark this module refuses to produce."""


def money(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


#: Below this many **contributing companies**, no cell is published. Not
#: employees - companies. A cell drawn from one company's four hundred people is
#: that company's pay band with a market label on it.
MIN_CONTRIBUTORS = 8


class BenchmarkSource(str, enum.Enum):
    """Where a benchmark came from. Always shown.

    A client comparing against our own aggregated base is comparing against
    other AskMira customers, which is a specific and possibly unrepresentative
    population. A licensed dataset has its own methodology and its own
    limitations. Presenting them identically hides both.
    """

    OWN_CLIENT_BASE = "own_client_base"
    LICENSED = "licensed"


@dataclass(frozen=True, slots=True)
class Contribution:
    """One company's value for one metric in one cell."""

    tenant_id: str
    metric: str
    dimension_key: str
    value: Decimal
    #: How many employees stand behind this company's number. Used to reject a
    #: cell where one company dominates.
    employee_count: int = 1


@dataclass(frozen=True, slots=True)
class BenchmarkCell:
    """One published cell, or the reason it is not published."""

    metric: str
    dimension_key: str
    source: BenchmarkSource
    contributors: int
    published: bool
    p25: Decimal | None
    p50: Decimal | None
    p75: Decimal | None
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "dimension": self.dimension_key,
            "source": self.source.value,
            "contributors": self.contributors,
            "published": self.published,
            "p25": str(self.p25) if self.p25 is not None else None,
            "p50": str(self.p50) if self.p50 is not None else None,
            "p75": str(self.p75) if self.p75 is not None else None,
            "note": self.note,
        }


def _quartiles(values: list[Decimal]) -> tuple[Decimal, Decimal, Decimal]:
    ordered = sorted(float(v) for v in values)
    quantiles = statistics.quantiles(ordered, n=4, method="inclusive")
    return (
        money(quantiles[0]),
        money(statistics.median(ordered)),
        money(quantiles[2]),
    )


def aggregate(
    contributions: list[Contribution],
    *,
    consenting_tenant_ids: set[str],
    metric: str,
    dimension_key: str,
    source: BenchmarkSource = BenchmarkSource.OWN_CLIENT_BASE,
    min_contributors: int = MIN_CONTRIBUTORS,
    max_single_share: float = 0.5,
) -> BenchmarkCell:
    """Build one cell from consenting contributors only.

    A contribution from a tenant that has not opted in **raises**. It is not
    filtered out: a silent filter is indistinguishable from a filter that was
    never applied, and whether an opted-out client's data was excluded is the
    one thing about this feature they will ask us to prove.

    `max_single_share` catches the other identification route: a cell of eight
    companies where one of them is four hundred people and the rest are twelve
    is that one company's number with seven rounding errors attached.
    """
    if not consenting_tenant_ids:
        raise BenchmarkError(
            "No tenant has opted in to contribute. An aggregate over an empty "
            "consent set would publish nothing, which is correct - but it is "
            "worth saying rather than returning a silent empty cell."
        )

    relevant = [c for c in contributions if c.metric == metric and c.dimension_key == dimension_key]
    intruders = sorted({c.tenant_id for c in relevant if c.tenant_id not in consenting_tenant_ids})
    if intruders:
        raise BenchmarkError(
            f"{len(intruders)} tenant(s) that have not opted in appear in this "
            "aggregation. Their rows are refused rather than filtered: a silent "
            "filter cannot be told apart from one that was never applied, and "
            "whether an opted-out client was excluded is precisely what they "
            "will ask us to demonstrate."
        )

    by_tenant: dict[str, list[Contribution]] = {}
    for row in relevant:
        by_tenant.setdefault(row.tenant_id, []).append(row)

    contributors = len(by_tenant)
    if contributors < min_contributors:
        return BenchmarkCell(
            metric=metric,
            dimension_key=dimension_key,
            source=source,
            contributors=contributors,
            published=False,
            p25=None,
            p50=None,
            p75=None,
            note=(
                f"{contributors} contributing companies, below the "
                f"{min_contributors} needed. Not rounded and not widened - a "
                "cell like 'SaaS, 200-500 employees, Bengaluru' with a handful "
                "of contributors is a few named companies, and anybody in that "
                "market can name them."
            ),
        )

    headcounts = {t: sum(c.employee_count for c in rows) for t, rows in by_tenant.items()}
    total_heads = sum(headcounts.values())
    largest = max(headcounts.values()) if headcounts else 0
    if total_heads and largest / total_heads > max_single_share:
        return BenchmarkCell(
            metric=metric,
            dimension_key=dimension_key,
            source=source,
            contributors=contributors,
            published=False,
            p25=None,
            p50=None,
            p75=None,
            note=(
                f"One company contributes {round(largest / total_heads * 100)}% "
                "of the employees in this cell. The contributor count clears the "
                "threshold and the number would still be substantially that one "
                "company's, published to its competitors."
            ),
        )

    # One value per company, so a large contributor does not weight the
    # distribution by its headcount - that is the same identification problem
    # arriving through the arithmetic.
    per_company = [
        money(statistics.fmean([float(c.value) for c in rows])) for rows in by_tenant.values()
    ]
    p25, p50, p75 = _quartiles(per_company)

    return BenchmarkCell(
        metric=metric,
        dimension_key=dimension_key,
        source=source,
        contributors=contributors,
        published=True,
        p25=p25,
        p50=p50,
        p75=p75,
        note=(
            f"From {contributors} contributing companies. One value per "
            "company, so a large contributor does not weight the distribution "
            "by its headcount."
        ),
    )


@dataclass(frozen=True, slots=True)
class Position:
    """Where one company sits against a published cell."""

    metric: str
    dimension_key: str
    own_value: Decimal
    percentile: int | None
    cell: BenchmarkCell
    reading: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "dimension": self.dimension_key,
            "own_value": str(self.own_value),
            "percentile": self.percentile,
            "benchmark": self.cell.as_dict(),
            "reading": self.reading,
        }


def position_against(*, own_value: Decimal, cell: BenchmarkCell) -> Position:
    """Where a company sits in the distribution, with the sample size attached.

    Percentile position rather than a distance from the average. "The market
    pays 18 lakh" causes a company at 16 to raise everybody and a company at 22
    to conclude they are overpaying; "you are around the 60th percentile, from
    fourteen companies" causes neither.

    The percentile is banded, not exact. An exact figure from fourteen companies
    implies a precision the sample does not support, and somebody will quote it.
    """
    value = money(own_value)

    if not cell.published:
        return Position(
            metric=cell.metric,
            dimension_key=cell.dimension_key,
            own_value=value,
            percentile=None,
            cell=cell,
            reading=(
                "No benchmark is published for this cell, so there is nothing "
                f"to compare against. {cell.note}"
            ),
        )

    p25, p50, p75 = cell.p25, cell.p50, cell.p75
    if value < p25:
        band, description = 25, f"below the lower quartile of {p25}"
    elif value < p50:
        band, description = 50, f"between the lower quartile {p25} and the median {p50}"
    elif value < p75:
        band, description = 75, f"between the median {p50} and the upper quartile {p75}"
    else:
        band, description = 100, f"at or above the upper quartile of {p75}"

    source_label = (
        "our own client base"
        if cell.source is BenchmarkSource.OWN_CLIENT_BASE
        else "a licensed dataset"
    )
    return Position(
        metric=cell.metric,
        dimension_key=cell.dimension_key,
        own_value=value,
        percentile=band,
        cell=cell,
        reading=(
            f"{value} is {description}, across {cell.contributors} contributing "
            f"companies ({source_label}). The band is deliberately coarse: an "
            "exact percentile from this many companies implies a precision the "
            "sample does not support, and somebody would quote it."
        ),
    )


def access_statement(*, is_contributor: bool) -> dict[str, Any]:
    """What this client can see, and why. Said plainly.

    A contributing client sees the aggregate; a non-contributing one sees
    licensed data only. That is a fair arrangement, and it stops being fair the
    moment somebody discovers it as a surprise while trying to open a chart.
    """
    return {
        "is_contributor": is_contributor,
        "available_sources": (
            [BenchmarkSource.OWN_CLIENT_BASE.value, BenchmarkSource.LICENSED.value]
            if is_contributor
            else [BenchmarkSource.LICENSED.value]
        ),
        "statement": (
            "You contribute anonymised aggregates to the peer benchmark, so you "
            "can see peer benchmarks as well as licensed data. Your own numbers "
            "are never shown to anybody: only cells drawn from at least "
            f"{MIN_CONTRIBUTORS} companies are published, and one value per "
            "company enters the distribution."
            if is_contributor
            else "You have not opted in to contribute anonymised aggregates, so "
            "you see licensed benchmark data only. This is stated here rather "
            "than discovered: contributing is a contract term and a setting, and "
            "it is reversible."
        ),
    }
