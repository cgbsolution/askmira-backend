"""Offer acceptance and offer-drop rates (slices 19 and 36).

Acceptance and drop are different failures and are never folded together. A
decline costs a search; a drop — accepted, then did not join — costs a search
plus a start date the team planned around. In this market drop rate by
department, recruiter and notice period is a material metric.

Rates below the sample floor are `None` with the count beside them, not a
percentage of three.

Pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["MIN_FOR_RATE", "OfferOutcomeFacts", "notice_band", "offer_outcomes"]

MIN_FOR_RATE = 5

_ACCEPTED_LIKE = frozenset({"accepted", "joined", "dropped"})
_REFUSED = frozenset({"declined", "lapsed"})


@dataclass(frozen=True, slots=True)
class OfferOutcomeFacts:
    offer_id: str
    status: str
    department_id: str = ""
    recruiter_id: str = ""
    notice_period_days: int | None = None


def notice_band(days: int | None) -> str:
    if days is None:
        return "unknown"
    if days <= 30:
        return "up to 30 days"
    if days <= 60:
        return "31-60 days"
    if days <= 90:
        return "61-90 days"
    return "over 90 days"


def _rates(rows: list[OfferOutcomeFacts], minimum: int) -> dict[str, Any]:
    accepted = sum(1 for r in rows if r.status in _ACCEPTED_LIKE)
    refused = sum(1 for r in rows if r.status in _REFUSED)
    joined = sum(1 for r in rows if r.status == "joined")
    dropped = sum(1 for r in rows if r.status == "dropped")
    revoked = sum(1 for r in rows if r.status == "revoked")
    responded = accepted + refused
    settled = joined + dropped
    return {
        "offers": len(rows),
        "accepted": accepted,
        "declined_or_lapsed": refused,
        "joined": joined,
        "dropped": dropped,
        "revoked": revoked,
        "acceptance_rate": round(accepted / responded, 3) if responded >= minimum else None,
        "drop_rate": round(dropped / settled, 3) if settled >= minimum else None,
    }


def offer_outcomes(rows: list[OfferOutcomeFacts], *, minimum: int = MIN_FOR_RATE) -> dict[str, Any]:
    """Overall rates and the same rates by department, recruiter and notice band."""

    def grouped(key) -> list[dict[str, Any]]:
        groups: dict[str, list[OfferOutcomeFacts]] = {}
        for row in rows:
            groups.setdefault(key(row) or "unassigned", []).append(row)
        return [{"key": k, **_rates(v, minimum)} for k, v in sorted(groups.items())]

    return {
        "overall": _rates(rows, minimum),
        "by_department": grouped(lambda r: r.department_id),
        "by_recruiter": grouped(lambda r: r.recruiter_id),
        "by_notice_period": grouped(lambda r: notice_band(r.notice_period_days)),
        "note": (
            "Acceptance counts accepted, joined and dropped offers against every offer "
            "the candidate answered or let lapse. Drop rate counts accepted offers that "
            f"did not join. Rates need at least {minimum} offers; below that the counts "
            "are shown on their own."
        ),
    }
