"""Recruitment analytics and the referral programme (slice 36).

Everyone can see open roles and refer — always in their own name, with
eligibility worked out from the records rather than declared in the request.
HR sees every referral, the payout schedule, costs, and the analytics.

`GET /recruiting/posture` says what is enforced and what is not built.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, EmailStr, Field

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.recruiting import CostComponentEnum, ShareChannelEnum
from app.models.user import UserRole
from app.services.recruiting import analytics, referral_views
from app.services.recruiting import funnel as funnel_engine
from app.services.recruiting import interviewers as interviewer_engine
from app.services.recruiting import service as recruiting

router = APIRouter(tags=["recruiting-analytics"])

HR_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
ADMIN_ROLES = (UserRole.ADMIN,)


# ── Contracts ───────────────────────────────────────────────────────────────


class MilestoneIn(BaseModel):
    key: str = Field(min_length=1, max_length=48)
    label: str = Field(min_length=1, max_length=120)
    #: The set must total 100.
    share_percent: Decimal = Field(gt=0, le=100)
    days_after_joining: int = Field(ge=0, le=1095)


class ProgrammeIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    bonus_by_grade: dict[str, Decimal]
    milestones: list[MilestoneIn] | None = Field(default=None, max_length=12)
    eligible_roles: list[str] = Field(default_factory=list, max_length=200)
    description: str | None = Field(default=None, max_length=4000)


class ReferralIn(BaseModel):
    programme_id: uuid.UUID
    referrer_employee_id: uuid.UUID
    #: Normally the candidate's email.
    candidate_key: str = Field(min_length=1, max_length=320)
    requisition_id: uuid.UUID | None = None
    candidate_id: uuid.UUID | None = None
    role_key: str = Field(default="", max_length=80)
    #: Can only make the check stricter; PIP and hiring-team facts are also read
    #: from the records.
    on_pip: bool = False
    hiring_team_requisitions: list[str] = Field(default_factory=list, max_length=200)
    channel: ShareChannelEnum | None = None
    submitted_at: datetime | None = None


class ReferIn(BaseModel):
    """Refer somebody into an open role, in your own name."""

    programme_id: uuid.UUID
    requisition_id: uuid.UUID
    candidate_name: str = Field(min_length=1, max_length=255)
    candidate_email: EmailStr
    candidate_phone: str | None = Field(
        default=None, max_length=32, pattern=r"^[0-9+()\-\s]{6,32}$"
    )
    channel: ShareChannelEnum | None = None
    note: str | None = Field(default=None, max_length=1000)


class HiredIn(BaseModel):
    hired_on: date
    grade: str = Field(min_length=1, max_length=32)


class PayIn(BaseModel):
    payroll_reference: str = Field(min_length=1, max_length=120)


class CostIn(BaseModel):
    period_label: str = Field(min_length=1, max_length=16)
    component: CostComponentEnum
    amount: Decimal = Field(ge=0, max_digits=16, decimal_places=2)
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
    """The components a complete cost per hire needs."""
    return {
        "components": list(funnel_engine.COST_COMPONENTS),
        "note": (
            "A cost per hire that omits agency fees looks excellent and is "
            "wrong, and it is the number a client repeats to their board."
        ),
    }


@router.get("/recruiting/interviewer-framing")
async def interviewer_framing(user: CurrentUser) -> dict[str, Any]:
    """How interviewer analytics are meant to be used, in one sentence."""
    return {
        "framing": interviewer_engine.COACHING_FRAMING,
        "minimum_interviews": interviewer_engine.MIN_INTERVIEWS_FOR_SIGNAL,
    }


@router.get("/recruiting/open-roles")
async def open_roles(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Open roles anybody may refer into. Title, department, location — no budget."""
    return await referral_views.open_roles(session, user)


@router.get("/recruiting/referral-programmes")
async def active_programmes(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Active programme terms, including the duplicate rule, the same for everyone."""
    return await referral_views.all_programmes(session, user, active_only=True)


# ── Referral programme ──────────────────────────────────────────────────────


@router.get("/recruiting/referral-programmes/all")
async def all_programmes(
    user: CurrentUser, session: DbSession, _hr=Depends(require_roles(*HR_ROLES))
) -> list[dict[str, Any]]:
    return await referral_views.all_programmes(session, user, active_only=False)


@router.post("/recruiting/referral-programmes", status_code=201)
async def create_programme(
    body: ProgrammeIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Create a programme and return the terms an employee reads before referring."""
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
    base_url: str = Query(max_length=512),
) -> dict[str, Any]:
    """A tracked link to share, with the terms beside it."""
    permissions.require_self_or_hr(
        user,
        referrer_employee_id,
        detail="A share link can only carry your own name as the referrer.",
    )
    if not base_url.startswith("https://") and not base_url.startswith("/"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A share link has to point at an https page.",
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
    """Submit a referral by candidate key. Eligibility and duplicates are checked now."""
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


@router.post("/recruiting/refer", status_code=status.HTTP_201_CREATED)
async def refer_candidate(
    body: ReferIn, session: DbSession, user: CurrentUser, request: Request
) -> dict[str, Any]:
    """Refer somebody into an open role. The referrer is always you."""
    return await referral_views.refer_candidate(
        session,
        user,
        programme_id=body.programme_id,
        requisition_id=body.requisition_id,
        candidate_name=body.candidate_name,
        candidate_email=str(body.candidate_email),
        candidate_phone=body.candidate_phone,
        channel=body.channel,
        note=body.note,
        request=request,
    )


@router.get("/recruiting/my-referrals")
async def my_referrals(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Your own referrals and what each has earned so far."""
    return await referral_views.my_referrals(session, user)


@router.get("/recruiting/referrals")
async def all_referrals(
    user: CurrentUser, session: DbSession, _hr=Depends(require_roles(*HR_ROLES))
) -> list[dict[str, Any]]:
    return await referral_views.all_referrals(session, user)


@router.post("/recruiting/referrals/{referral_id}/hired")
async def mark_hired(
    referral_id: uuid.UUID,
    body: HiredIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Record the hire and write the whole payout schedule up front."""
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


@router.get("/recruiting/costs")
async def list_costs(
    session: DbSession,
    user: CurrentUser,
    period_label: str | None = Query(default=None, max_length=16),
    _hr=Depends(require_roles(*HR_ROLES)),
) -> list[dict[str, Any]]:
    return await referral_views.list_costs(session, user, period_label=period_label)


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
    hires: int = Query(ge=0),
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Cost per hire from what has been recorded, with the hire count supplied."""
    return await recruiting.cost_report(
        session, user, period_label=period_label, hires=hires
    )


# ── Analytics, derived from the records ─────────────────────────────────────


@router.get("/recruiting/analytics/funnel")
async def analytics_funnel(
    session: DbSession,
    user: CurrentUser,
    group_by: str | None = Query(
        default=None, pattern="^(requisition_id|department_id|recruiter_id|source)$"
    ),
    requisition_id: uuid.UUID | None = None,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Funnel, reconciliation, stage timings, sources and offer outcomes."""
    return await analytics.funnel_report(
        session, user, group_by=group_by, requisition_id=requisition_id
    )


@router.get("/recruiting/analytics/interviewers")
async def analytics_interviewers(
    session: DbSession, user: CurrentUser, _hr=Depends(require_roles(*HR_ROLES))
) -> dict[str, Any]:
    """Coaching signals per interviewer. No rank, nothing below the sample floor."""
    return await analytics.interviewer_report(session, user)


@router.get("/recruiting/analytics/diversity/dimensions")
async def analytics_diversity_dimensions(
    session: DbSession, user: CurrentUser, _hr=Depends(require_roles(*HR_ROLES))
) -> list[dict[str, Any]]:
    return await analytics.diversity_dimensions(session, user)


@router.get("/recruiting/analytics/diversity")
async def analytics_diversity(
    session: DbSession,
    user: CurrentUser,
    dimension: str = Query(min_length=1, max_length=48),
    focus_value: str = Query(min_length=1, max_length=48),
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Representation through the funnel, suppressed below the group floor."""
    return await analytics.diversity_from_records(
        session, user, dimension=dimension, focus_value=focus_value
    )


@router.get("/recruiting/analytics/cost-per-hire")
async def analytics_cost_per_hire(
    session: DbSession,
    user: CurrentUser,
    period_label: str = Query(min_length=4, max_length=16),
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Cost per hire with hires counted from joined offers in the period."""
    return await analytics.cost_per_hire_report(session, user, period_label=period_label)


# ── Caller-supplied analytics (kept for integrations) ───────────────────────


class JourneyIn(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=64)
    source: str = Field(default="", max_length=80)
    stage_dates: dict[str, date]
    requisition_id: str = Field(default="", max_length=64)
    department_id: str = Field(default="", max_length=64)
    recruiter_id: str = Field(default="", max_length=64)
    rejected_at_stage: str | None = None


class DiversityIn(BaseModel):
    journeys: list[JourneyIn] = Field(max_length=20000)
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
    feedback: list[FeedbackIn] = Field(max_length=20000)
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
