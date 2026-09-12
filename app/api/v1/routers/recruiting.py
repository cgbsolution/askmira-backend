"""Recruitment analytics and the referral programme (slice 36).

Two endpoints carry the weight:

- `POST /recruiting/referrals` runs the eligibility and duplicate checks
  **before** the referral is accepted. An employee told three days later that a
  colleague got there first has already told the candidate they referred them.
- `GET /recruiting/share-link` returns the programme terms — including the
  duplicate rule — alongside the link, because that is the moment the employee
  is deciding to share.

`GET /recruiting/posture` says what is enforced and what is not built.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.recruiting import CostComponentEnum, ShareChannelEnum
from app.models.user import UserRole
from app.services.recruiting import funnel as funnel_engine
from app.services.recruiting import interviewers as interviewer_engine
from app.services.recruiting import service as recruiting

router = APIRouter(tags=["recruiting-analytics"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)


# ── Contracts ───────────────────────────────────────────────────────────────


class MilestoneIn(BaseModel):
    key: str = Field(min_length=1, max_length=48)
    label: str = Field(min_length=1, max_length=120)
    #: The set must total 100. An employee paid 80% of what they were promised
    #: will notice.
    share_percent: Decimal = Field(gt=0, le=100)
    days_after_joining: int = Field(ge=0, le=1095)


class ProgrammeIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    bonus_by_grade: dict[str, Decimal]
    milestones: list[MilestoneIn] | None = None
    eligible_roles: list[str] = Field(default_factory=list)
    description: str | None = Field(default=None, max_length=4000)


class ReferralIn(BaseModel):
    programme_id: uuid.UUID
    referrer_employee_id: uuid.UUID
    #: Normally the candidate's email. Without something to key on, two
    #: referrals of the same person cannot be recognised as duplicates.
    candidate_key: str = Field(min_length=1, max_length=320)
    requisition_id: uuid.UUID | None = None
    candidate_id: uuid.UUID | None = None
    role_key: str = Field(default="", max_length=80)
    on_pip: bool = False
    hiring_team_requisitions: list[str] = Field(default_factory=list)
    channel: ShareChannelEnum | None = None
    submitted_at: datetime | None = None


class HiredIn(BaseModel):
    hired_on: date
    grade: str = Field(min_length=1, max_length=32)


class PayIn(BaseModel):
    #: Required. Marking a payout paid without one records a payment nobody can
    #: trace.
    payroll_reference: str = Field(min_length=1, max_length=120)


class CostIn(BaseModel):
    period_label: str = Field(min_length=1, max_length=16)
    component: CostComponentEnum
    amount: Decimal = Field(ge=0)
    source: str = Field(default="", max_length=80)
    note: str | None = Field(default=None, max_length=2000)


# ── Posture and reference ───────────────────────────────────────────────────


@router.get("/recruiting/posture")
async def posture(user: CurrentUser) -> dict[str, Any]:
    return recruiting.recruiting_posture()


@router.get("/recruiting/cost-components")
async def cost_components(
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """The components a complete cost per hire needs.

    A figure missing any of them is a floor, not a cost, and the report says so.
    """
    return {
        "components": list(funnel_engine.COST_COMPONENTS),
        "note": (
            "A cost per hire that omits agency fees looks excellent and is "
            "wrong, and it is the number a client repeats to their board."
        ),
    }


@router.get("/recruiting/interviewer-framing")
async def interviewer_framing(user: CurrentUser) -> dict[str, Any]:
    """How interviewer analytics are meant to be used, in one sentence.

    Returned as its own endpoint so a UI has no excuse for presenting these
    numbers as a scorecard.
    """
    return {
        "framing": interviewer_engine.COACHING_FRAMING,
        "minimum_interviews": interviewer_engine.MIN_INTERVIEWS_FOR_SIGNAL,
    }


# ── Referral programme ──────────────────────────────────────────────────────


@router.post("/recruiting/referral-programmes", status_code=201)
async def create_programme(
    body: ProgrammeIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Create a programme and return the terms an employee reads before
    referring — including the duplicate rule."""
    return await recruiting.create_programme(
        session,
        user,
        name=body.name,
        bonus_by_grade=body.bonus_by_grade,
        milestones=(
            [m.model_dump() for m in body.milestones] if body.milestones else None
        ),
        eligible_roles=body.eligible_roles,
        description=body.description,
        request=request,
    )


@router.get("/recruiting/share-link")
async def share_link(
    session: DbSession,
    user: CurrentUser,
    programme_id: uuid.UUID,
    requisition_id: uuid.UUID,
    referrer_employee_id: uuid.UUID,
    channel: ShareChannelEnum,
    base_url: str,
) -> dict[str, Any]:
    """A tracked link to share, with the terms beside it.

    Nothing here posts to WhatsApp or LinkedIn; the employee's own app does
    that. A referral flow that requires uploading a CV to a portal gets used
    once.
    """
    permissions.require_self_or_hr(
        user,
        referrer_employee_id,
        detail="A share link can only carry your own name as the referrer.",
    )
    return await recruiting.generate_share_link(
        session,
        user,
        programme_id=programme_id,
        requisition_id=requisition_id,
        referrer_employee_id=referrer_employee_id,
        channel=channel,
        base_url=base_url,
    )


@router.post("/recruiting/referrals", status_code=status.HTTP_201_CREATED)
async def submit_referral(
    body: ReferralIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """Submit a referral.

    Eligibility and duplicates are checked now, not in a nightly job. Returns
    400 with the reason where the referrer is not eligible; a duplicate is
    accepted and marked, with the winning referral and its date named.
    """
    permissions.require_self_or_hr(
        user,
        body.referrer_employee_id,
        detail="You can only submit a referral in your own name.",
    )
    return await recruiting.submit_referral(
        session,
        user,
        programme_id=body.programme_id,
        referrer_employee_id=body.referrer_employee_id,
        candidate_key=body.candidate_key,
        requisition_id=body.requisition_id,
        candidate_id=body.candidate_id,
        role_key=body.role_key,
        on_pip=body.on_pip,
        hiring_team_requisitions=body.hiring_team_requisitions,
        channel=body.channel,
        submitted_at=body.submitted_at,
        request=request,
    )


@router.post("/recruiting/referrals/{referral_id}/hired")
async def mark_hired(
    referral_id: uuid.UUID,
    body: HiredIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Record the hire and write the whole payout schedule up front.

    Every milestone is written now, unpaid, so the ninety-day instalment is
    visible as owed from day one.
    """
    return await recruiting.mark_referral_hired(
        session,
        user,
        referral_id=referral_id,
        hired_on=body.hired_on,
        grade=body.grade,
        request=request,
    )


@router.post("/recruiting/referral-payouts/{payout_id}/pay")
async def pay_milestone(
    payout_id: uuid.UUID,
    body: PayIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Mark one milestone as taken by payroll, with its reference."""
    return await recruiting.pay_milestone(
        session,
        user,
        payout_id=payout_id,
        payroll_reference=body.payroll_reference,
        request=request,
    )


# ── Costs ───────────────────────────────────────────────────────────────────


@router.put("/recruiting/costs")
async def record_cost(
    body: CostIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    row = await recruiting.record_cost(
        session,
        user,
        period_label=body.period_label,
        component=body.component,
        amount=body.amount,
        source=body.source,
        note=body.note,
        request=request,
    )
    return {
        "cost_id": str(row.id),
        "period_label": row.period_label,
        "component": row.component.value,
        "amount": str(row.amount),
    }


@router.get("/recruiting/cost-per-hire")
async def cost_per_hire(
    session: DbSession,
    user: CurrentUser,
    period_label: str,
    hires: int,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Cost per hire from what has been recorded, naming what has not.

    A component with no row is missing, not zero, and the figure describes
    itself as a floor when any are absent.
    """
    return await recruiting.cost_report(
        session, user, period_label=period_label, hires=hires
    )


# ── Analytics ───────────────────────────────────────────────────────────────


class JourneyIn(BaseModel):
    """One candidate's path, as dates per stage.

    Supplied rather than queried, for the same reason there is no funnel table:
    the caller reads the application records and this endpoint does the
    arithmetic, so there is no cached copy to disagree with the source.
    """

    candidate_id: str = Field(min_length=1, max_length=64)
    source: str = Field(default="", max_length=80)
    #: Stage name to the date it was reached. A stage with no date was not
    #: recorded; fast-tracking is normal and the funnel does not punish it.
    stage_dates: dict[str, date]
    requisition_id: str = Field(default="", max_length=64)
    department_id: str = Field(default="", max_length=64)
    recruiter_id: str = Field(default="", max_length=64)
    rejected_at_stage: str | None = None


class DiversityIn(BaseModel):
    journeys: list[JourneyIn]
    dimension: str = Field(min_length=1, max_length=48)
    focus_value: str = Field(min_length=1, max_length=48)


class FeedbackIn(BaseModel):
    feedback_id: str = Field(min_length=1, max_length=64)
    interviewer_id: str = Field(min_length=1, max_length=64)
    candidate_id: str = Field(min_length=1, max_length=64)
    competency_ratings: dict[str, int] = Field(default_factory=dict)
    recommendation_score: int = Field(ge=1, le=5)
    comment_length: int = Field(ge=0)
    turnaround_days: float = Field(ge=0)
    advanced: bool


class SignalsIn(BaseModel):
    feedback: list[FeedbackIn]
    interviewer_id: str = Field(min_length=1, max_length=64)


def _journeys(rows: list[JourneyIn]) -> list[funnel_engine.CandidateJourney]:
    out = []
    for row in rows:
        try:
            stage_dates = {
                funnel_engine.Stage(name): value
                for name, value in row.stage_dates.items()
            }
            rejected = (
                funnel_engine.Stage(row.rejected_at_stage)
                if row.rejected_at_stage
                else None
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{exc}. The funnel stages are "
                    f"{[s.value for s in funnel_engine.Stage]}; a stage name "
                    "that is not one of them would be silently dropped, and a "
                    "dropped stage is a funnel that does not reconcile."
                ),
            ) from exc
        out.append(
            funnel_engine.CandidateJourney(
                candidate_id=row.candidate_id,
                source=row.source,
                stage_dates=stage_dates,
                requisition_id=row.requisition_id,
                department_id=row.department_id,
                recruiter_id=row.recruiter_id,
                rejected_at_stage=rejected,
            )
        )
    return out


@router.post("/recruiting/diversity-report")
async def diversity_report(
    body: DiversityIn,
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Representation through the funnel, suppressed below the group floor.

    Every figure carries its disclosure coverage: a representation drawn from a
    third of candidates is not a representation of all of them, and presenting
    it as one is how a diversity dashboard becomes actively misleading.
    """
    return await recruiting.diversity_report(
        session,
        user,
        journeys=_journeys(body.journeys),
        dimension=body.dimension,
        focus_value=body.focus_value,
    )


@router.post("/recruiting/interviewer-signals")
async def interviewer_signals(
    body: SignalsIn,
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Severity, turnaround and feedback quality for one interviewer.

    Coaching material. No rank, no percentile, nothing below the sample floor,
    and the framing sentence in every section rather than in a tooltip somebody
    removes.
    """
    return await recruiting.interviewer_signals(
        session,
        user,
        feedback=[
            interviewer_engine.FeedbackFacts(
                feedback_id=f.feedback_id,
                interviewer_id=f.interviewer_id,
                candidate_id=f.candidate_id,
                competency_ratings=dict(f.competency_ratings),
                recommendation_score=f.recommendation_score,
                comment_length=f.comment_length,
                turnaround_days=f.turnaround_days,
                advanced=f.advanced,
            )
            for f in body.feedback
        ],
        interviewer_id=body.interviewer_id,
    )
