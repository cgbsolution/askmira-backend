"""Recruitment analytics read from the records themselves (slice 36).

The engines beside this (`funnel`, `interviewers`, `diversity`, `offer_outcomes`)
do the arithmetic. This module builds their inputs from applications, stage
events, interviews, feedback and offers — on every call, with no summary table,
so nothing here can disagree with the records it was built from.

How a candidate's journey is dated, stated once:

- **applied** — their earliest application, or when the candidate was created.
- **screened** — the first stage event into screening or anything later.
- **interviewed** — the first completed interview, or the first stage event
  into an interview stage.
- **offered** — when an offer to them was released.
- **accepted** / **joined** — the offer's accepted and joined events.

The reconciliation compares the funnel against independent counts taken straight
from those tables, and returns a discrepancy rather than hiding it.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.hiring import Candidate, CandidateStage, CandidateStageEvent, Requisition
from app.models.interview import (
    Interview,
    InterviewFeedback,
    InterviewPanelist,
    InterviewStatus,
    Recommendation,
)
from app.models.jobboard import Application
from app.models.offer import Offer, OfferEvent, OfferStatus
from app.models.recruiting import (
    CandidateDiversityDisclosure,
    CostComponentEnum,
    RecruitmentCost,
)
from app.models.user import User
from app.services.recruiting import diversity as diversity_engine
from app.services.recruiting import funnel as funnel_engine
from app.services.recruiting import interviewers as interviewer_engine
from app.services.recruiting import offer_outcomes as outcome_engine

__all__ = [
    "cost_per_hire_report",
    "diversity_dimensions",
    "diversity_from_records",
    "funnel_report",
    "interviewer_report",
    "load_journeys",
]

_SCREENED_OR_LATER = {
    CandidateStage.SCREENING,
    CandidateStage.AI_INTERVIEW,
    CandidateStage.HUMAN_INTERVIEW,
    CandidateStage.OFFER,
    CandidateStage.HIRED,
}
_INTERVIEW_STAGES = {CandidateStage.AI_INTERVIEW, CandidateStage.HUMAN_INTERVIEW}
_REJECTION_MAP = {
    CandidateStage.APPLIED: funnel_engine.Stage.APPLIED,
    CandidateStage.SCREENING: funnel_engine.Stage.SCREENED,
    CandidateStage.AI_INTERVIEW: funnel_engine.Stage.INTERVIEWED,
    CandidateStage.HUMAN_INTERVIEW: funnel_engine.Stage.INTERVIEWED,
}


def _earliest(current: date | None, candidate: date) -> date:
    return candidate if current is None or candidate < current else current


async def load_journeys(
    session: AsyncSession, user: User, *, requisition_id: uuid.UUID | None = None
) -> tuple[list[funnel_engine.CandidateJourney], dict[str, int]]:
    """Every candidate's dated path, and independent per-stage counts to reconcile."""
    stmt = tenant_scoped(
        select(Candidate, Requisition).join(
            Requisition, Requisition.id == Candidate.requisition_id
        ),
        Candidate,
        user,
    )
    if requisition_id is not None:
        stmt = stmt.where(Candidate.requisition_id == requisition_id)
    rows = (await session.execute(stmt)).all()
    ids = [candidate.id for candidate, _ in rows]
    if not ids:
        return [], {}

    applied: dict[uuid.UUID, date] = {}
    for candidate_id, applied_at in await session.execute(
        tenant_scoped(
            select(Application.candidate_id, func.min(Application.applied_at))
            .where(Application.candidate_id.in_(ids))
            .group_by(Application.candidate_id),
            Application,
            user,
        )
    ):
        applied[candidate_id] = applied_at.date()

    screened: dict[uuid.UUID, date] = {}
    interviewed: dict[uuid.UUID, date] = {}
    rejected_from: dict[uuid.UUID, CandidateStage] = {}
    events = (
        await session.scalars(
            tenant_scoped(
                select(CandidateStageEvent)
                .where(CandidateStageEvent.candidate_id.in_(ids))
                .order_by(CandidateStageEvent.occurred_at),
                CandidateStageEvent,
                user,
            )
        )
    ).all()
    for event in events:
        day = event.occurred_at.date()
        if event.to_stage in _SCREENED_OR_LATER:
            screened[event.candidate_id] = _earliest(screened.get(event.candidate_id), day)
        if event.to_stage in _INTERVIEW_STAGES:
            interviewed[event.candidate_id] = _earliest(interviewed.get(event.candidate_id), day)
        closed = (CandidateStage.REJECTED, CandidateStage.WITHDRAWN)
        if event.to_stage in closed and event.from_stage:
            rejected_from[event.candidate_id] = event.from_stage

    for candidate_id, start in await session.execute(
        tenant_scoped(
            select(Interview.candidate_id, func.min(Interview.scheduled_start))
            .where(
                Interview.candidate_id.in_(ids),
                Interview.status == InterviewStatus.COMPLETED,
            )
            .group_by(Interview.candidate_id),
            Interview,
            user,
        )
    ):
        interviewed[candidate_id] = _earliest(interviewed.get(candidate_id), start.date())

    offered: dict[uuid.UUID, date] = {}
    accepted: dict[uuid.UUID, date] = {}
    joined: dict[uuid.UUID, date] = {}
    offers = (
        await session.scalars(
            tenant_scoped(select(Offer).where(Offer.candidate_id.in_(ids)), Offer, user)
        )
    ).all()
    offer_to_candidate = {offer.id: offer.candidate_id for offer in offers}
    for offer in offers:
        if offer.released_at is not None:
            offered[offer.candidate_id] = _earliest(
                offered.get(offer.candidate_id), offer.released_at.date()
            )
    if offer_to_candidate:
        offer_events = (
            await session.scalars(
                tenant_scoped(
                    select(OfferEvent).where(
                        OfferEvent.offer_id.in_(list(offer_to_candidate)),
                        OfferEvent.to_status.in_((OfferStatus.ACCEPTED, OfferStatus.JOINED)),
                    ),
                    OfferEvent,
                    user,
                )
            )
        ).all()
        for event in offer_events:
            candidate_id = offer_to_candidate[event.offer_id]
            target = accepted if event.to_status is OfferStatus.ACCEPTED else joined
            target[candidate_id] = _earliest(target.get(candidate_id), event.occurred_at.date())

    journeys: list[funnel_engine.CandidateJourney] = []
    for candidate, requisition in rows:
        dates: dict[funnel_engine.Stage, date] = {
            funnel_engine.Stage.APPLIED: applied.get(candidate.id, candidate.created_at.date())
        }
        for stage, source in (
            (funnel_engine.Stage.SCREENED, screened),
            (funnel_engine.Stage.INTERVIEWED, interviewed),
            (funnel_engine.Stage.OFFERED, offered),
            (funnel_engine.Stage.ACCEPTED, accepted),
            (funnel_engine.Stage.JOINED, joined),
        ):
            if candidate.id in source:
                dates[stage] = source[candidate.id]
        rejected_stage = rejected_from.get(candidate.id)
        journeys.append(
            funnel_engine.CandidateJourney(
                candidate_id=str(candidate.id),
                source=candidate.source or "unknown",
                stage_dates=dates,
                requisition_id=str(requisition.id),
                department_id=str(requisition.department_id or ""),
                recruiter_id=str(requisition.recruiter_id or ""),
                rejected_at_stage=(_REJECTION_MAP.get(rejected_stage) if rejected_stage else None),
            )
        )

    # Independent counts, straight from the tables, for the reconciliation. The
    # later stages are the ones the funnel dates from other tables and so the ones
    # worth checking; applied is every candidate.
    expected = {
        "applied": len(rows),
        "offered": sum(1 for c, _ in rows if c.id in offered),
        "joined": sum(
            1
            for c, _ in rows
            if c.id in joined
            or any(o.candidate_id == c.id and o.status is OfferStatus.JOINED for o in offers)
        ),
    }
    return journeys, expected


async def _spend_by_source(session: AsyncSession, user: User) -> dict[str, Decimal]:
    spend: dict[str, Decimal] = {}
    rows = await session.scalars(
        tenant_scoped(
            select(RecruitmentCost).where(
                RecruitmentCost.component == CostComponentEnum.JOB_BOARD_SPEND,
                RecruitmentCost.source != "",
            ),
            RecruitmentCost,
            user,
        )
    )
    for row in rows.all():
        spend[row.source] = spend.get(row.source, Decimal(0)) + Decimal(str(row.amount))
    return spend


async def funnel_report(
    session: AsyncSession,
    user: User,
    *,
    group_by: str | None = None,
    requisition_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    journeys, expected = await load_journeys(session, user, requisition_id=requisition_id)
    try:
        funnel = funnel_engine.funnel(journeys, group_by=group_by)
    except funnel_engine.FunnelError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    offer_stmt = tenant_scoped(
        select(Offer, Requisition, Candidate)
        .join(Requisition, Requisition.id == Offer.requisition_id)
        .join(Candidate, Candidate.id == Offer.candidate_id),
        Offer,
        user,
    )
    if requisition_id is not None:
        offer_stmt = offer_stmt.where(Offer.requisition_id == requisition_id)
    offer_rows = (await session.execute(offer_stmt)).all()

    return {
        "funnel": funnel,
        "reconciliation": funnel_engine.reconcile(journeys, expected_totals=expected),
        "stage_timings": funnel_engine.stage_timings(journeys),
        "source_effectiveness": funnel_engine.source_effectiveness(
            journeys, spend_by_source=await _spend_by_source(session, user)
        ),
        "offer_outcomes": outcome_engine.offer_outcomes(
            [
                outcome_engine.OfferOutcomeFacts(
                    offer_id=str(offer.id),
                    status=offer.status.value,
                    department_id=str(offer.department_id or requisition.department_id or ""),
                    recruiter_id=str(requisition.recruiter_id or ""),
                    notice_period_days=candidate.notice_period_days,
                )
                for offer, requisition, candidate in offer_rows
            ]
        ),
    }


_RECOMMENDATION_SCORE = {
    Recommendation.STRONG_NO: 1,
    Recommendation.NO: 2,
    Recommendation.YES: 4,
    Recommendation.STRONG_YES: 5,
}


def _rating_1_to_5(value: Any) -> int | None:
    """Ratings are stored as strings against the tenant's scale; clamp to 1-5."""
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if number > 5:
        # A 1-10 scale, halved. Anything else is clamped rather than guessed at.
        number = number / 2
    return max(1, min(5, int(number.to_integral_value())))


async def interviewer_report(session: AsyncSession, user: User) -> dict[str, Any]:
    """Coaching signals per interviewer, from submitted feedback. No ranking."""
    rows = (
        await session.execute(
            tenant_scoped(
                select(InterviewFeedback, InterviewPanelist, Interview, Candidate)
                .join(InterviewPanelist, InterviewPanelist.id == InterviewFeedback.panelist_id)
                .join(Interview, Interview.id == InterviewFeedback.interview_id)
                .join(Candidate, Candidate.id == Interview.candidate_id),
                InterviewFeedback,
                user,
            )
        )
    ).all()

    advanced_stages = {CandidateStage.OFFER, CandidateStage.HIRED}
    facts: list[interviewer_engine.FeedbackFacts] = []
    interviewer_ids: set[uuid.UUID] = set()
    for feedback, panelist, interview, candidate in rows:
        ratings = {
            code: score
            for code, raw in (feedback.ratings or {}).items()
            if (score := _rating_1_to_5(raw)) is not None
        }
        turnaround = (feedback.submitted_at - interview.scheduled_end).total_seconds() / 86400
        interviewer_ids.add(panelist.user_id)
        facts.append(
            interviewer_engine.FeedbackFacts(
                feedback_id=str(feedback.id),
                interviewer_id=str(panelist.user_id),
                candidate_id=str(candidate.id),
                competency_ratings=ratings,
                recommendation_score=_RECOMMENDATION_SCORE[feedback.recommendation],
                comment_length=len((feedback.strengths or "") + (feedback.concerns or "")),
                turnaround_days=max(0.0, round(turnaround, 2)),
                advanced=candidate.stage in advanced_stages,
            )
        )

    names = {}
    if interviewer_ids:
        names = {
            row.id: row.full_name
            for row in await session.execute(
                tenant_scoped(
                    select(User.id, User.full_name).where(User.id.in_(interviewer_ids)),
                    User,
                    user,
                )
            )
        }
    interviewers = [
        {
            "interviewer_id": str(uid),
            "name": names.get(uid, ""),
            "severity": interviewer_engine.severity(facts, interviewer_id=str(uid)),
            "turnaround": interviewer_engine.turnaround(facts, interviewer_id=str(uid)),
            "feedback_quality": interviewer_engine.feedback_quality(facts, interviewer_id=str(uid)),
        }
        for uid in interviewer_ids
    ]
    # Alphabetical, deliberately: any order derived from the figures is a ranking.
    interviewers.sort(key=lambda row: (row["name"] or "").lower())
    return {
        "framing": interviewer_engine.COACHING_FRAMING,
        "minimum_interviews": interviewer_engine.MIN_INTERVIEWS_FOR_SIGNAL,
        "feedback_count": len(facts),
        "interviewers": interviewers,
    }


async def diversity_dimensions(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Dimensions and values that have disclosures. No counts — they identify people."""
    rows = await session.execute(
        tenant_scoped(
            select(
                CandidateDiversityDisclosure.dimension, CandidateDiversityDisclosure.value
            ).distinct(),
            CandidateDiversityDisclosure,
            user,
        )
    )
    grouped: dict[str, set[str]] = {}
    for dimension, value in rows:
        grouped.setdefault(dimension, set()).add(value)
    return [
        {"dimension": dimension, "values": sorted(values)}
        for dimension, values in sorted(grouped.items())
    ]


async def diversity_from_records(
    session: AsyncSession, user: User, *, dimension: str, focus_value: str
) -> dict[str, Any]:
    journeys, _ = await load_journeys(session, user)
    disclosures = (
        await session.scalars(
            tenant_scoped(
                select(CandidateDiversityDisclosure).where(
                    CandidateDiversityDisclosure.dimension == dimension
                ),
                CandidateDiversityDisclosure,
                user,
            )
        )
    ).all()
    return diversity_engine.stage_representation(
        journeys,
        disclosures=[
            diversity_engine.DisclosedAttribute(
                candidate_id=str(d.candidate_id), dimension=d.dimension, value=d.value
            )
            for d in disclosures
        ],
        dimension=dimension,
        focus_value=focus_value,
    )


_PERIOD = re.compile(r"^(\d{4})(?:-(\d{2}))?$")


async def cost_per_hire_report(
    session: AsyncSession, user: User, *, period_label: str
) -> dict[str, Any]:
    """Cost per hire for a period, with hires counted from joined offers.

    The period must be `YYYY` or `YYYY-MM` so hires can be counted from joining
    dates. Costs recorded under any other label need the hire count supplied by
    hand, through the original endpoint.
    """
    match = _PERIOD.match(period_label.strip())
    if not match:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Use a period of YYYY or YYYY-MM, so hires can be counted from joining "
                "dates rather than typed in."
            ),
        )
    year, month = int(match.group(1)), match.group(2)
    stmt = (
        select(func.count())
        .select_from(Offer)
        .where(Offer.status == OfferStatus.JOINED, extract("year", Offer.joining_date) == year)
    )
    if month:
        stmt = stmt.where(extract("month", Offer.joining_date) == int(month))
    hires = int(await session.scalar(tenant_scoped(stmt, Offer, user)) or 0)

    rows = (
        await session.scalars(
            tenant_scoped(
                select(RecruitmentCost).where(RecruitmentCost.period_label == period_label),
                RecruitmentCost,
                user,
            )
        )
    ).all()
    components: dict[str, Decimal] = {}
    for row in rows:
        components[row.component.value] = components.get(row.component.value, Decimal(0)) + Decimal(
            str(row.amount)
        )
    report = funnel_engine.cost_per_hire(
        hires=hires, components=components, period_label=period_label
    )
    report["hires_counted_from"] = "Offers marked joined with a joining date in this period."
    report["cost_lines"] = [
        {
            "id": str(row.id),
            "component": row.component.value,
            "source": row.source,
            "amount": str(row.amount),
            "note": row.note,
        }
        for row in rows
    ]
    return report
