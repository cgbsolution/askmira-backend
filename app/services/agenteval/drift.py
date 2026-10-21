"""Production drift: the same assertions, run on sampled live output.

═══════════════════════════════════════════════════════════════════════════════
THE DEFECT WAS IN PRODUCTION, NOT IN CI
═══════════════════════════════════════════════════════════════════════════════

A suite that passes in CI tells you the agent behaves on the fifteen inputs
somebody wrote down. The Fit Score defect was visible in the live score
distribution long before anybody looked at it — four hundred candidates, every
score between 71 and 73. Nobody had a chart of that.

So the three signals here are the three shapes that failure takes in production,
and each is a distribution property rather than a per-output check:

**A collapsing distribution.** The spread of live scores narrowing over time is
the Fit Score defect happening again, and it is measurable without a single
labelled example. `distribution_collapse` compares the current window's spread
and standard deviation against a baseline.

**A refusal rate spiking.** An agent that suddenly declines a third of requests
has usually had its context broken rather than its safety improved, and the
users experiencing it will not report it — they will stop asking.

**Grounding failures rising.** Citations that resolve to nothing, climbing.

None of these gates a release: they are alerts about what is already live. The
gate is `gating.py`, on the suite, before the merge.

Pure. `now` and the samples are supplied.
"""

from __future__ import annotations

import enum
import statistics
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DriftAlert",
    "DriftSeverity",
    "DriftSignal",
    "ScoreWindow",
    "detect_drift",
    "distribution_collapse",
]


class DriftSignal(str, enum.Enum):
    """What kind of drift. Each has a different cause and a different fix."""

    #: The spread of live outputs is narrowing. The Fit Score failure, live.
    DISTRIBUTION_COLLAPSE = "distribution_collapse"
    #: The mean has moved without the spread changing — recalibration, which
    #: may be correct and needs somebody to say so.
    DISTRIBUTION_SHIFT = "distribution_shift"
    REFUSAL_RATE_SPIKE = "refusal_rate_spike"
    #: Refusals falling can be worse than refusals rising: an agent that stopped
    #: declining out-of-scope requests is answering them.
    REFUSAL_RATE_COLLAPSE = "refusal_rate_collapse"
    GROUNDING_FAILURE_RISE = "grounding_failure_rise"
    LATENCY_RISE = "latency_rise"
    COST_RISE = "cost_rise"


class DriftSeverity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class ScoreWindow:
    """A window of live outputs from one agent."""

    label: str
    values: tuple[float, ...]

    @property
    def count(self) -> int:
        return len(self.values)

    @property
    def spread(self) -> float:
        return max(self.values) - min(self.values) if self.values else 0.0

    @property
    def stdev(self) -> float:
        return statistics.pstdev(self.values) if len(self.values) > 1 else 0.0

    @property
    def mean(self) -> float:
        return statistics.fmean(self.values) if self.values else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "count": self.count,
            "mean": round(self.mean, 4),
            "spread": round(self.spread, 4),
            "stdev": round(self.stdev, 4),
        }


@dataclass(frozen=True, slots=True)
class DriftAlert:
    """One thing that has changed in production, with the numbers."""

    signal: DriftSignal
    severity: DriftSeverity
    message: str
    measured: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal.value,
            "severity": self.severity.value,
            "message": self.message,
            "measured": self.measured,
        }


#: Below this many samples, no alert is raised. A window of six outputs has a
#: spread that means nothing, and an alerting system that cries wolf on small
#: windows is one people mute — after which it detects nothing at all.
MIN_SAMPLES = 30


def distribution_collapse(
    baseline: ScoreWindow,
    current: ScoreWindow,
    *,
    collapse_ratio: float = 0.5,
) -> DriftAlert | None:
    """The live score distribution narrowing. The Fit Score defect, in production.

    Measured on the standard deviation rather than the raw spread, because a
    single outlier keeps the spread wide while every other value clusters — the
    exact shape of a scorer that has stopped discriminating for the bulk of its
    inputs.
    """
    if current.count < MIN_SAMPLES or baseline.count < MIN_SAMPLES:
        return None
    if baseline.stdev <= 0:
        return None

    ratio = current.stdev / baseline.stdev
    if ratio > collapse_ratio:
        return None

    severity = DriftSeverity.CRITICAL if ratio <= 0.25 else DriftSeverity.WARNING
    return DriftAlert(
        signal=DriftSignal.DISTRIBUTION_COLLAPSE,
        severity=severity,
        message=(
            f"Live scores have collapsed: the standard deviation fell from "
            f"{baseline.stdev:.3g} to {current.stdev:.3g} "
            f"({ratio:.0%} of baseline) across {current.count} samples. This is "
            "the shape of a scorer that has stopped telling inputs apart — the "
            "Fit Score defect, happening in production rather than in a test."
        ),
        measured={
            "baseline": baseline.as_dict(),
            "current": current.as_dict(),
            "stdev_ratio": round(ratio, 4),
        },
    )


def _distribution_shift(
    baseline: ScoreWindow, current: ScoreWindow, *, shift_threshold: float
) -> DriftAlert | None:
    if current.count < MIN_SAMPLES or baseline.count < MIN_SAMPLES:
        return None
    if baseline.stdev <= 0:
        return None

    shift = abs(current.mean - baseline.mean) / baseline.stdev
    if shift < shift_threshold:
        return None

    return DriftAlert(
        signal=DriftSignal.DISTRIBUTION_SHIFT,
        severity=DriftSeverity.WARNING,
        message=(
            f"The mean live score moved from {baseline.mean:.3g} to "
            f"{current.mean:.3g}, {shift:.2f} standard deviations. The spread "
            "is unchanged, so this is a recalibration rather than a collapse — "
            "which may be correct, and needs somebody to say so rather than "
            "nobody noticing."
        ),
        measured={
            "baseline": baseline.as_dict(),
            "current": current.as_dict(),
            "shift_in_stdevs": round(shift, 4),
        },
    )


def _rate_alert(
    signal: DriftSignal,
    *,
    baseline_rate: float,
    current_rate: float,
    sample_count: int,
    rising: bool,
    what: str,
    why: str,
) -> DriftAlert | None:
    if sample_count < MIN_SAMPLES:
        return None

    delta = current_rate - baseline_rate
    if rising and delta < 0.15:
        return None
    if not rising and delta > -0.15:
        return None

    severity = DriftSeverity.CRITICAL if abs(delta) >= 0.30 else DriftSeverity.WARNING
    return DriftAlert(
        signal=signal,
        severity=severity,
        message=(
            f"The {what} moved from {baseline_rate:.0%} to {current_rate:.0%} "
            f"across {sample_count} samples. {why}"
        ),
        measured={
            "baseline_rate": round(baseline_rate, 4),
            "current_rate": round(current_rate, 4),
            "delta": round(delta, 4),
            "samples": sample_count,
        },
    )


def detect_drift(
    *,
    baseline_scores: ScoreWindow | None = None,
    current_scores: ScoreWindow | None = None,
    baseline_refusal_rate: float | None = None,
    current_refusal_rate: float | None = None,
    baseline_grounding_failure_rate: float | None = None,
    current_grounding_failure_rate: float | None = None,
    sample_count: int = 0,
    baseline_latency_ms: float | None = None,
    current_latency_ms: float | None = None,
    baseline_cost_inr: float | None = None,
    current_cost_inr: float | None = None,
    shift_threshold: float = 1.0,
) -> list[DriftAlert]:
    """Every drift signal present in this window, worst first.

    Every argument is optional because not every agent produces every signal —
    an extraction agent has no score distribution, a scorer has no refusal rate.
    Passing `None` means "not measured", which is different from zero and is why
    the parameters are nullable rather than defaulted.
    """
    alerts: list[DriftAlert] = []

    if baseline_scores is not None and current_scores is not None:
        collapse = distribution_collapse(baseline_scores, current_scores)
        if collapse is not None:
            alerts.append(collapse)
        else:
            shift = _distribution_shift(
                baseline_scores, current_scores, shift_threshold=shift_threshold
            )
            if shift is not None:
                alerts.append(shift)

    if baseline_refusal_rate is not None and current_refusal_rate is not None:
        spike = _rate_alert(
            DriftSignal.REFUSAL_RATE_SPIKE,
            baseline_rate=baseline_refusal_rate,
            current_rate=current_refusal_rate,
            sample_count=sample_count,
            rising=True,
            what="refusal rate",
            why=(
                "An agent that suddenly declines far more requests has usually "
                "had its context broken rather than its safety improved, and "
                "the people hitting it will stop asking rather than report it."
            ),
        )
        if spike is not None:
            alerts.append(spike)
        collapse = _rate_alert(
            DriftSignal.REFUSAL_RATE_COLLAPSE,
            baseline_rate=baseline_refusal_rate,
            current_rate=current_refusal_rate,
            sample_count=sample_count,
            rising=False,
            what="refusal rate",
            why=(
                "An agent that has stopped declining out-of-scope requests is "
                "answering them, which is the more dangerous direction."
            ),
        )
        if collapse is not None:
            alerts.append(collapse)

    if baseline_grounding_failure_rate is not None and current_grounding_failure_rate is not None:
        rise = _rate_alert(
            DriftSignal.GROUNDING_FAILURE_RISE,
            baseline_rate=baseline_grounding_failure_rate,
            current_rate=current_grounding_failure_rate,
            sample_count=sample_count,
            rising=True,
            what="grounding failure rate",
            why=(
                "Citations are resolving to records that do not exist or do "
                "not support the claim, more often than they were."
            ),
        )
        if rise is not None:
            alerts.append(rise)

    if (
        baseline_latency_ms is not None
        and current_latency_ms is not None
        and baseline_latency_ms > 0
        and current_latency_ms > baseline_latency_ms * 1.5
    ):
        alerts.append(
            DriftAlert(
                signal=DriftSignal.LATENCY_RISE,
                severity=DriftSeverity.WARNING,
                message=(
                    f"Median latency rose from {baseline_latency_ms:.0f}ms to "
                    f"{current_latency_ms:.0f}ms."
                ),
                measured={
                    "baseline_ms": baseline_latency_ms,
                    "current_ms": current_latency_ms,
                },
            )
        )

    if (
        baseline_cost_inr is not None
        and current_cost_inr is not None
        and baseline_cost_inr > 0
        and current_cost_inr > baseline_cost_inr * 1.5
    ):
        alerts.append(
            DriftAlert(
                signal=DriftSignal.COST_RISE,
                severity=DriftSeverity.WARNING,
                message=(
                    f"Cost per call rose from ₹{baseline_cost_inr:.4f} to "
                    f"₹{current_cost_inr:.4f}. Worth knowing in the same place "
                    "as the quality numbers, because the trade is only "
                    "deliberate if both are visible at once."
                ),
                measured={
                    "baseline_inr": baseline_cost_inr,
                    "current_inr": current_cost_inr,
                },
            )
        )

    order = {
        DriftSeverity.CRITICAL: 0,
        DriftSeverity.WARNING: 1,
        DriftSeverity.INFO: 2,
    }
    return sorted(alerts, key=lambda a: order[a.severity])
