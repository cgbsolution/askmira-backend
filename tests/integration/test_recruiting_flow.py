"""Slice 36 — referrals and recruitment costs against a real database.

The checklist gates:

- funnel reconciles exactly to underlying records
- cost per hire configurable and complete
- referral share flow works from mobile to WhatsApp
- milestone bonus payouts reach payroll with correct tax
- interviewer analytics framed as coaching, not scoring
- diversity suppression threshold enforced
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.employee import Employee, EmploymentStatus
from app.models.recruiting import (
    CostComponentEnum,
    PayoutStatusEnum,
    Referral,
    ReferralPayout,
    ReferralProgramme,
    ReferralStatusEnum,
    ShareChannelEnum,
)
from app.services.recruiting import funnel as funnel_engine
from app.services.recruiting import interviewers as interviewer_engine
from app.services.recruiting import service as recruiting
from tests.hiring.db_fixture import engine  # noqa: F401 - shadows the shared fixture
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

D = Decimal
NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)


# ── Helpers ─────────────────────────────────────────────────────────────────


async def make_employee(
    session, tenant, legal_entity, location, code, *, probation_end=None
):
    person = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code,
        last_name="Test",
        work_email=f"{code.lower()}@acme.test",
        legal_entity_id=legal_entity.id,
        location_id=location.id,
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2024, 1, 1),
        probation_end_date=probation_end,
        grade="M3",
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


async def make_programme(session, hr_user, **kw):
    body = {
        "name": f"Refer a friend {uuid.uuid4().hex[:6]}",
        "bonus_by_grade": {"M3": D("50000")},
    }
    body.update(kw)
    return await recruiting.create_programme(session, hr_user, **body)


# ══════════════════════════════════════════════════════════════════════════════
# Gate: the referral share flow
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_programme_terms_carry_the_duplicate_rule(session, hr_user):
    """The argument it prevents happens between two colleagues who both
    believed they were entitled."""
    out = await make_programme(session, hr_user)
    assert "first submission" in out["terms"]["duplicate_rule"].lower()
    assert "tax deducted" in out["terms"]["tax_note"]


async def test_a_programme_with_its_own_milestones_is_stored(session, hr_user):
    """Milestones from the request carry Decimal shares; the JSON column takes strings."""
    out = await make_programme(
        session,
        hr_user,
        milestones=[
            {"key": "joined", "label": "On joining", "share_percent": D("50"),
             "days_after_joining": 0},
            {"key": "retained", "label": "After 90 days", "share_percent": D("50"),
             "days_after_joining": 90},
        ],
    )
    programme = await session.get(ReferralProgramme, uuid.UUID(out["programme_id"]))
    assert [m["share_percent"] for m in programme.payout_milestones] == ["50", "50"]


async def test_a_share_link_comes_with_the_terms(session, hr_user, employee):
    """This is the moment the employee is deciding to share; a duplicate rule
    read afterwards is a rule that produces a grievance."""
    programme = await make_programme(session, hr_user)
    out = await recruiting.generate_share_link(
        session,
        hr_user,
        programme_id=uuid.UUID(programme["programme_id"]),
        requisition_id=uuid.uuid4(),
        referrer_employee_id=employee.id,
        channel=ShareChannelEnum.WHATSAPP,
        base_url="https://careers.acme.test",
    )
    assert out["link"]["channel"] == "whatsapp"
    assert "?ref=" in out["link"]["url"]
    assert "first submission" in out["terms"]["duplicate_rule"].lower()


async def test_the_link_attributes_the_referrer(session, hr_user, employee):
    """The employee never handles a CV. A flow that requires uploading one to a
    portal gets used once."""
    programme = await make_programme(session, hr_user)
    requisition = uuid.uuid4()
    out = await recruiting.generate_share_link(
        session,
        hr_user,
        programme_id=uuid.UUID(programme["programme_id"]),
        requisition_id=requisition,
        referrer_employee_id=employee.id,
        channel=ShareChannelEnum.LINKEDIN,
        base_url="https://careers.acme.test/",
    )
    assert str(employee.id) in out["link"]["url"]
    assert str(requisition) in out["link"]["url"]


# ══════════════════════════════════════════════════════════════════════════════
# Eligibility
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_confirmed_employee_may_refer(session, hr_user, employee):
    programme = await make_programme(session, hr_user)
    out = await recruiting.submit_referral(
        session,
        hr_user,
        programme_id=uuid.UUID(programme["programme_id"]),
        referrer_employee_id=employee.id,
        candidate_key="ravi@example.test",
        submitted_at=NOW,
    )
    assert out["status"] == "submitted"
    assert out["eligibility"]["eligible"] is True


async def test_somebody_still_on_probation_cannot_refer(
    session, hr_user, tenant, legal_entity, location
):
    programme = await make_programme(session, hr_user)
    probationer = await make_employee(
        session, tenant, legal_entity, location, "PROB", probation_end=date(2027, 6, 1)
    )
    with pytest.raises(HTTPException) as exc:
        await recruiting.submit_referral(
            session,
            hr_user,
            programme_id=uuid.UUID(programme["programme_id"]),
            referrer_employee_id=probationer.id,
            candidate_key="x@example.test",
            submitted_at=NOW,
        )
    assert "still on probation" in exc.value.detail


async def test_a_referrer_on_a_pip_is_ineligible(session, hr_user, employee):
    """It is a configured rule rather than a judgement about their referrals."""
    programme = await make_programme(session, hr_user)
    with pytest.raises(HTTPException) as exc:
        await recruiting.submit_referral(
            session,
            hr_user,
            programme_id=uuid.UUID(programme["programme_id"]),
            referrer_employee_id=employee.id,
            candidate_key="x@example.test",
            on_pip=True,
            submitted_at=NOW,
        )
    assert "configured rule rather than a judgement" in exc.value.detail


async def test_the_hiring_team_cannot_refer_into_their_own_requisition(
    session, hr_user, employee
):
    programme = await make_programme(session, hr_user)
    requisition = uuid.uuid4()
    with pytest.raises(HTTPException) as exc:
        await recruiting.submit_referral(
            session,
            hr_user,
            programme_id=uuid.UUID(programme["programme_id"]),
            referrer_employee_id=employee.id,
            candidate_key="x@example.test",
            requisition_id=requisition,
            hiring_team_requisitions=[str(requisition)],
            submitted_at=NOW,
        )
    assert "not a position to put somebody in" in exc.value.detail


async def test_a_role_outside_the_programme_is_refused(session, hr_user, employee):
    programme = await make_programme(session, hr_user, eligible_roles=["engineering"])
    with pytest.raises(HTTPException) as exc:
        await recruiting.submit_referral(
            session,
            hr_user,
            programme_id=uuid.UUID(programme["programme_id"]),
            referrer_employee_id=employee.id,
            candidate_key="x@example.test",
            role_key="sales",
            submitted_at=NOW,
        )
    assert "this role is" in exc.value.detail


async def test_a_referral_with_no_candidate_key_is_refused(
    session, hr_user, employee
):
    """Without it two referrals of the same person cannot be recognised as
    duplicates, and both bonuses get paid."""
    programme = await make_programme(session, hr_user)
    with pytest.raises(HTTPException) as exc:
        await recruiting.submit_referral(
            session,
            hr_user,
            programme_id=uuid.UUID(programme["programme_id"]),
            referrer_employee_id=employee.id,
            candidate_key="   ",
            submitted_at=NOW,
        )
    assert "both bonuses get paid" in exc.value.detail


# ══════════════════════════════════════════════════════════════════════════════
# Duplicates
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_second_referral_of_the_same_candidate_is_a_duplicate(
    session, hr_user, tenant, legal_entity, location, employee
):
    """Checked now, not in a nightly job: an employee told three days later has
    already told the candidate they referred them."""
    programme = await make_programme(session, hr_user)
    colleague = await make_employee(session, tenant, legal_entity, location, "COL1")

    await recruiting.submit_referral(
        session,
        hr_user,
        programme_id=uuid.UUID(programme["programme_id"]),
        referrer_employee_id=employee.id,
        candidate_key="Ravi.Kumar@Example.test",
        submitted_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    second = await recruiting.submit_referral(
        session,
        hr_user,
        programme_id=uuid.UUID(programme["programme_id"]),
        referrer_employee_id=colleague.id,
        candidate_key=" ravi.kumar@example.test ",
        submitted_at=datetime(2026, 9, 5, tzinfo=UTC),
    )
    assert second["status"] == "duplicate"
    assert second["duplicate"]["winner_referrer_id"] == str(employee.id)
    assert "first submission wins" in second["duplicate"]["message"]


async def test_a_different_candidate_is_not_a_duplicate(session, hr_user, employee):
    programme = await make_programme(session, hr_user)
    await recruiting.submit_referral(
        session,
        hr_user,
        programme_id=uuid.UUID(programme["programme_id"]),
        referrer_employee_id=employee.id,
        candidate_key="one@example.test",
        submitted_at=NOW,
    )
    second = await recruiting.submit_referral(
        session,
        hr_user,
        programme_id=uuid.UUID(programme["programme_id"]),
        referrer_employee_id=employee.id,
        candidate_key="two@example.test",
        submitted_at=NOW,
    )
    assert second["status"] == "submitted"


async def test_a_duplicate_referral_cannot_be_paid(
    session, hr_user, tenant, legal_entity, location, employee
):
    """Paying both would pay twice for one hire, and the rule was in the terms
    before either of them referred."""
    programme = await make_programme(session, hr_user)
    colleague = await make_employee(session, tenant, legal_entity, location, "COL2")
    await recruiting.submit_referral(
        session,
        hr_user,
        programme_id=uuid.UUID(programme["programme_id"]),
        referrer_employee_id=employee.id,
        candidate_key="dup@example.test",
        submitted_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    loser = await recruiting.submit_referral(
        session,
        hr_user,
        programme_id=uuid.UUID(programme["programme_id"]),
        referrer_employee_id=colleague.id,
        candidate_key="dup@example.test",
        submitted_at=datetime(2026, 9, 5, tzinfo=UTC),
    )
    with pytest.raises(HTTPException) as exc:
        await recruiting.mark_referral_hired(
            session,
            hr_user,
            referral_id=uuid.UUID(loser["referral_id"]),
            hired_on=date(2026, 10, 1),
            grade="M3",
        )
    assert "belongs to the colleague who referred first" in exc.value.detail


# ══════════════════════════════════════════════════════════════════════════════
# Gate: milestone payouts through payroll
# ══════════════════════════════════════════════════════════════════════════════


async def hired_referral(session, hr_user, employee, **kw):
    programme = await make_programme(session, hr_user, **kw)
    submitted = await recruiting.submit_referral(
        session,
        hr_user,
        programme_id=uuid.UUID(programme["programme_id"]),
        referrer_employee_id=employee.id,
        candidate_key=f"{uuid.uuid4().hex[:8]}@example.test",
        submitted_at=NOW,
    )
    return uuid.UUID(submitted["referral_id"])


async def test_the_whole_schedule_is_written_when_the_candidate_joins(
    session, hr_user, employee
):
    """The ninety-day milestone is visible as owed from day one rather than
    appearing when somebody remembers to create it."""
    referral_id = await hired_referral(session, hr_user, employee)
    out = await recruiting.mark_referral_hired(
        session,
        hr_user,
        referral_id=referral_id,
        hired_on=date(2026, 10, 1),
        grade="M3",
    )
    assert [m["amount"] for m in out["schedule"]["milestones"]] == [
        "25000.00",
        "25000.00",
    ]

    rows = list(
        (
            await session.execute(
                select(ReferralPayout).where(ReferralPayout.referral_id == referral_id)
            )
        ).scalars()
    )
    assert len(rows) == 2
    assert all(r.status is PayoutStatusEnum.SCHEDULED for r in rows)
    assert all(r.paid_at is None for r in rows)


async def test_the_later_milestone_is_dated_ninety_days_out(
    session, hr_user, employee
):
    """Paying it all on joining rewards a referral that leaves in six weeks
    exactly as much as one that stays five years."""
    referral_id = await hired_referral(session, hr_user, employee)
    await recruiting.mark_referral_hired(
        session,
        hr_user,
        referral_id=referral_id,
        hired_on=date(2026, 10, 1),
        grade="M3",
    )
    rows = sorted(
        (
            await session.execute(
                select(ReferralPayout).where(ReferralPayout.referral_id == referral_id)
            )
        )
        .scalars()
        .all(),
        key=lambda r: r.payable_on,
    )
    assert rows[0].payable_on == date(2026, 10, 1)
    assert rows[1].payable_on == date(2026, 12, 30)


async def test_a_payout_needs_the_payroll_reference(session, hr_user, employee):
    """Marking it paid without one records a payment nobody can trace."""
    referral_id = await hired_referral(session, hr_user, employee)
    await recruiting.mark_referral_hired(
        session,
        hr_user,
        referral_id=referral_id,
        hired_on=date(2026, 10, 1),
        grade="M3",
    )
    payout = (
        await session.execute(
            select(ReferralPayout).where(ReferralPayout.referral_id == referral_id)
        )
    ).scalars().first()

    with pytest.raises(HTTPException) as exc:
        await recruiting.pay_milestone(
            session, hr_user, payout_id=payout.id, payroll_reference="  "
        )
    assert "nobody can trace" in exc.value.detail


async def test_a_paid_milestone_records_its_reference_and_says_it_is_taxed(
    session, hr_user, employee
):
    referral_id = await hired_referral(session, hr_user, employee)
    await recruiting.mark_referral_hired(
        session,
        hr_user,
        referral_id=referral_id,
        hired_on=date(2026, 10, 1),
        grade="M3",
    )
    payout = (
        await session.execute(
            select(ReferralPayout).where(ReferralPayout.referral_id == referral_id)
        )
    ).scalars().first()

    out = await recruiting.pay_milestone(
        session, hr_user, payout_id=payout.id, payroll_reference="OCT-2026-RUN-1"
    )
    assert out["status"] == "paid"
    assert "tax deducted" in out["note"]

    await session.refresh(payout)
    assert payout.payroll_reference == "OCT-2026-RUN-1"
    assert payout.paid_at is not None


async def test_a_milestone_cannot_be_paid_twice(session, hr_user, employee):
    referral_id = await hired_referral(session, hr_user, employee)
    await recruiting.mark_referral_hired(
        session,
        hr_user,
        referral_id=referral_id,
        hired_on=date(2026, 10, 1),
        grade="M3",
    )
    payout = (
        await session.execute(
            select(ReferralPayout).where(ReferralPayout.referral_id == referral_id)
        )
    ).scalars().first()
    await recruiting.pay_milestone(
        session, hr_user, payout_id=payout.id, payroll_reference="RUN-1"
    )
    with pytest.raises(HTTPException):
        await recruiting.pay_milestone(
            session, hr_user, payout_id=payout.id, payroll_reference="RUN-2"
        )


async def test_a_grade_with_no_bonus_is_refused(session, hr_user, employee):
    """Paying zero is not the same as having no rule, and the referrer needs an
    answer."""
    referral_id = await hired_referral(session, hr_user, employee)
    with pytest.raises(HTTPException) as exc:
        await recruiting.mark_referral_hired(
            session,
            hr_user,
            referral_id=referral_id,
            hired_on=date(2026, 10, 1),
            grade="M9",
        )
    assert "no bonus configured for grade" in exc.value.detail


async def test_a_referral_cannot_be_hired_twice(session, hr_user, employee):
    referral_id = await hired_referral(session, hr_user, employee)
    await recruiting.mark_referral_hired(
        session,
        hr_user,
        referral_id=referral_id,
        hired_on=date(2026, 10, 1),
        grade="M3",
    )
    with pytest.raises(HTTPException) as exc:
        await recruiting.mark_referral_hired(
            session,
            hr_user,
            referral_id=referral_id,
            hired_on=date(2026, 10, 1),
            grade="M3",
        )
    assert "second payout schedule" in exc.value.detail


# ══════════════════════════════════════════════════════════════════════════════
# Gate: cost per hire configurable and complete
# ══════════════════════════════════════════════════════════════════════════════


async def test_an_incomplete_cost_reports_itself_as_a_floor(session, hr_user):
    """A cost per hire that omits agency fees looks excellent and is wrong."""
    await recruiting.record_cost(
        session,
        hr_user,
        period_label="2026-Q3",
        component=CostComponentEnum.JOB_BOARD_SPEND,
        amount=D("300000"),
    )
    report = await recruiting.cost_report(
        session, hr_user, period_label="2026-Q3", hires=6
    )
    assert report["is_complete"] is False
    assert "agency_fees" in report["missing_components"]
    assert "floor" in report["note"]


async def test_a_complete_cost_says_so(session, hr_user):
    for component in CostComponentEnum:
        await recruiting.record_cost(
            session,
            hr_user,
            period_label="2026-Q4",
            component=component,
            amount=D("100000"),
        )
    report = await recruiting.cost_report(
        session, hr_user, period_label="2026-Q4", hires=4
    )
    assert report["is_complete"] is True
    assert report["cost_per_hire"] == "100000.00"


async def test_costs_from_several_sources_sum_within_a_component(
    session, hr_user
):
    await recruiting.record_cost(
        session,
        hr_user,
        period_label="2026-Q3",
        component=CostComponentEnum.JOB_BOARD_SPEND,
        amount=D("200000"),
        source="naukri",
    )
    await recruiting.record_cost(
        session,
        hr_user,
        period_label="2026-Q3",
        component=CostComponentEnum.JOB_BOARD_SPEND,
        amount=D("100000"),
        source="indeed",
    )
    report = await recruiting.cost_report(
        session, hr_user, period_label="2026-Q3", hires=3
    )
    assert report["components"]["job_board_spend"] == "300000.00"


async def test_recording_the_same_line_twice_updates_rather_than_doubles(
    session, hr_user
):
    for amount in (D("100000"), D("250000")):
        await recruiting.record_cost(
            session,
            hr_user,
            period_label="2026-Q3",
            component=CostComponentEnum.AGENCY_FEES,
            amount=amount,
        )
    report = await recruiting.cost_report(
        session, hr_user, period_label="2026-Q3", hires=1
    )
    assert report["components"]["agency_fees"] == "250000.00"


# ══════════════════════════════════════════════════════════════════════════════
# Gate: interviewer analytics framed as coaching
# ══════════════════════════════════════════════════════════════════════════════


async def test_interviewer_signals_carry_the_coaching_framing(session, hr_user):
    feedback = [
        interviewer_engine.FeedbackFacts(
            f"f{i}", "j1", f"c{i}", {"a": 2, "b": 2}, 2, 120, 1.0, False
        )
        for i in range(12)
    ] + [
        interviewer_engine.FeedbackFacts(
            f"p{i}", f"p{i % 3}", f"d{i}", {"a": 4, "b": 4}, 4, 120, 1.0, True
        )
        for i in range(15)
    ]
    out = await recruiting.interviewer_signals(
        session, hr_user, feedback=feedback, interviewer_id="j1"
    )
    assert "not a scorecard" in out["framing"]
    assert out["severity"]["deviation"] == -2.0
    assert "rank" not in out["severity"]


async def test_incoherent_feedback_is_the_signal_worth_acting_on(
    session, hr_user
):
    feedback = [
        interviewer_engine.FeedbackFacts(
            f"f{i}", "j2", f"c{i}", {"a": 2, "b": 2}, 5, 120, 1.0, True
        )
        for i in range(12)
    ]
    out = await recruiting.interviewer_signals(
        session, hr_user, feedback=feedback, interviewer_id="j2"
    )
    assert out["feedback_quality"]["incoherent_count"] == 12
    assert "proxies" in out["feedback_quality"]["proxy_caveat"]


# ══════════════════════════════════════════════════════════════════════════════
# Gate: diversity suppression
# ══════════════════════════════════════════════════════════════════════════════


def journeys_for(candidate_ids, furthest):
    stages = list(funnel_engine.Stage)
    return [
        funnel_engine.CandidateJourney(
            candidate_id=str(cid),
            source="x",
            stage_dates={
                s: date(2026, 1, 1)
                for s in stages
                if s.rank <= furthest.rank
            },
        )
        for cid in candidate_ids
    ]


async def test_diversity_suppression_holds_on_real_disclosures(
    session, hr_user, tenant
):
    report = await recruiting.diversity_report(
        session,
        hr_user,
        journeys=journeys_for(
            [uuid.uuid4() for _ in range(6)], funnel_engine.Stage.APPLIED
        ),
        dimension="gender",
        focus_value="female",
    )
    assert all(row["suppressed"] for row in report["stages"])
    assert report["largest_fall"] is None


async def test_undisclosed_candidates_are_excluded_from_the_denominator(
    session, hr_user, tenant
):
    """An absent answer is not an answer."""
    journeys = journeys_for(
        [uuid.uuid4() for _ in range(20)], funnel_engine.Stage.APPLIED
    )
    report = await recruiting.diversity_report(
        session,
        hr_user,
        journeys=journeys,
        dimension="gender",
        focus_value="female",
    )
    assert report["coverage"]["candidates"] == 20
    assert report["coverage"]["disclosed"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# Tenant isolation
# ══════════════════════════════════════════════════════════════════════════════


async def test_another_tenant_cannot_see_this_programme(
    session, hr_user, other_hr_user, employee
):
    programme = await make_programme(session, hr_user)
    with pytest.raises(HTTPException) as exc:
        await recruiting.generate_share_link(
            session,
            other_hr_user,
            programme_id=uuid.UUID(programme["programme_id"]),
            requisition_id=uuid.uuid4(),
            referrer_employee_id=employee.id,
            channel=ShareChannelEnum.WHATSAPP,
            base_url="https://x.test",
        )
    assert exc.value.status_code == 404


async def test_a_duplicate_is_not_detected_across_tenants(
    session, hr_user, other_hr_user, employee
):
    """Two companies may legitimately refer the same person."""
    mine = await make_programme(session, hr_user)
    theirs = await make_programme(session, other_hr_user)
    await recruiting.submit_referral(
        session,
        hr_user,
        programme_id=uuid.UUID(mine["programme_id"]),
        referrer_employee_id=employee.id,
        candidate_key="shared@example.test",
        submitted_at=NOW,
    )
    rows = list(
        (
            await session.execute(
                select(Referral).where(Referral.tenant_id == other_hr_user.tenant_id)
            )
        ).scalars()
    )
    assert rows == []
    assert theirs["programme_id"]


async def test_another_tenant_sees_no_costs(session, hr_user, other_hr_user):
    await recruiting.record_cost(
        session,
        hr_user,
        period_label="2026-Q3",
        component=CostComponentEnum.AGENCY_FEES,
        amount=D("500000"),
    )
    report = await recruiting.cost_report(
        session, other_hr_user, period_label="2026-Q3", hires=1
    )
    assert report["total_cost"] == "0.00"


async def test_the_posture_says_payroll_does_not_pick_up_a_milestone():
    posture = recruiting.recruiting_posture()
    joined = " ".join(posture["not_built"]).lower()
    assert "nothing in payroll picks up a referral milestone" in joined
    assert "no accounting system is read" in joined


async def test_a_referral_row_records_the_channel(session, hr_user, employee):
    programme = await make_programme(session, hr_user)
    out = await recruiting.submit_referral(
        session,
        hr_user,
        programme_id=uuid.UUID(programme["programme_id"]),
        referrer_employee_id=employee.id,
        candidate_key="chan@example.test",
        channel=ShareChannelEnum.WHATSAPP,
        submitted_at=NOW,
    )
    row = (
        await session.execute(
            select(Referral).where(Referral.id == uuid.UUID(out["referral_id"]))
        )
    ).scalar_one()
    assert row.share_channel is ShareChannelEnum.WHATSAPP
    assert row.status is ReferralStatusEnum.SUBMITTED
