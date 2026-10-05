"""The hiring web portal end to end, against a real database.

Requisition → approval (never by the raiser) → candidate → stage moves gated on
interview feedback → offer approved by someone other than the drafter → release
→ accept → convert. Plus the authorisation that matters: candidates only for HR
and the hiring team, panellists only on their own interviews, cross-tenant 404,
and the public apply route's consent, honeypot, file check and throttle.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.employee import Employee, EmploymentStatus
from app.models.hiring import Candidate, CandidateStage, CandidateStageEvent, RequisitionStatus
from app.models.interview import (
    InterviewMode,
    InterviewStageType,
    InterviewStatus,
    Recommendation,
)
from app.models.jobboard import Application, CareersPageConfig
from app.models.offer import OfferStatus
from app.models.org import LegalEntity, Location
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.hiring import CandidateCreate, RequisitionCreate
from app.services.hiring import candidates as candidate_service
from app.services.hiring import offers as offer_service
from app.services.hiring import public_guard
from app.services.hiring import requisitions as requisition_service
from app.services.interviews import service as interview_service
from app.services.recruiting import analytics, referral_views
from app.services.recruiting import service as recruiting
from tests.hiring.db_fixture import engine  # noqa: F401 - shadows the shared fixture
from tests.integration.conftest import as_caller, requires_db
from tests.integration.test_offer_flow import _chain, _offer_template, _structure

pytestmark = requires_db

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


# ═══════════════════════════════════════════════════════════════════════════
# People
# ═══════════════════════════════════════════════════════════════════════════


async def _person(
    session: AsyncSession,
    tenant: Tenant,
    legal_entity: LegalEntity,
    location: Location,
    *,
    code: str,
    role: UserRole,
) -> User:
    employee = Employee(
        tenant_id=tenant.id, employee_code=code, first_name=code.title(), last_name="Test",
        work_email=f"{code.lower()}@acme-demo.com", legal_entity_id=legal_entity.id,
        location_id=location.id, status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2024, 1, 1), grade="L5",
    )
    session.add(employee)
    await session.flush()
    user = User(
        tenant_id=tenant.id, email=f"{code.lower()}@acme-demo.com",
        password_hash="not-a-real-hash", full_name=f"{code.title()} Test", role=role,
        is_active=True, employee_id=employee.id,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest.fixture
async def people(session, tenant, legal_entity, location) -> dict[str, User]:
    tenant.enabled_modules = {**(tenant.enabled_modules or {}), "hiring": True}
    await session.commit()
    roster = {
        "hr": ("HROPS", UserRole.SUB_ADMIN),
        "admin": ("ADMIN", UserRole.ADMIN),
        "manager": ("MGR", UserRole.EMPLOYEE),
        "alice": ("ALICE", UserRole.EMPLOYEE),
        "bob": ("BOB", UserRole.EMPLOYEE),
        "outsider": ("OUT", UserRole.EMPLOYEE),
        "employee": ("EMP", UserRole.EMPLOYEE),
    }
    people = {
        key: await _person(session, tenant, legal_entity, location, code=code, role=role)
        for key, (code, role) in roster.items()
    }
    # Managing people is a place in the org chart: the manager and the outsider
    # (a manager on another team) each have somebody reporting to them.
    for key in ("manager", "outsider"):
        session.add(
            Employee(
                tenant_id=tenant.id, employee_code=f"{roster[key][0]}R", first_name="Report",
                work_email=f"{roster[key][0].lower()}r@acme-demo.com",
                legal_entity_id=legal_entity.id, location_id=location.id,
                status=EmploymentStatus.ACTIVE, date_of_joining=date(2024, 1, 1),
                manager_id=people[key].employee_id,
            )
        )
    await session.commit()
    for user in people.values():
        await as_caller(session, user)
    return people


async def _open_requisition(session, tenant, people) -> uuid.UUID:
    await _chain(session, tenant)
    requisition = await requisition_service.create_requisition(
        session,
        people["manager"],
        RequisitionCreate(
            title="Senior Engineer", openings=1, grade="L5",
            budget_ctc_min=Decimal("1500000"), budget_ctc_max=Decimal("2000000"),
            justification="Backfill after two exits on the platform team.",
            skills=["Python", "Postgres"],
        ),
    )
    await session.commit()
    await requisition_service.submit_requisition(
        session, people["manager"], requisition_id=requisition.id
    )
    await session.commit()
    detail = await requisition_service.requisition_detail(
        session, people["admin"], requisition_id=requisition.id
    )
    assert detail["can_decide"] is True
    await requisition_service.approve_requisition(
        session, people["admin"], requisition_id=requisition.id, approve=True
    )
    await session.commit()
    return requisition.id


# ═══════════════════════════════════════════════════════════════════════════
# The whole journey
# ═══════════════════════════════════════════════════════════════════════════


async def test_requisition_to_employee_end_to_end(session, tenant, people) -> None:
    manager, admin, hr = people["manager"], people["admin"], people["hr"]
    alice, bob, outsider = people["alice"], people["bob"], people["outsider"]

    # ── Requisition: raised by a manager, never approved by them ──────────
    await _chain(session, tenant)
    requisition = await requisition_service.create_requisition(
        session, manager,
        RequisitionCreate(
            title="Senior Engineer", openings=1, grade="L5",
            budget_ctc_min=Decimal("1500000"), budget_ctc_max=Decimal("2000000"),
            justification="Backfill after two exits.",
        ),
    )
    await session.commit()
    assert requisition.status is RequisitionStatus.DRAFT
    assert requisition.code.startswith("REQ-")
    assert requisition.hiring_manager_id == manager.employee_id

    await requisition_service.submit_requisition(session, manager, requisition_id=requisition.id)
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await requisition_service.approve_requisition(
            session, manager, requisition_id=requisition.id, approve=True
        )
    assert exc.value.status_code == 403 and "You raised this" in exc.value.detail
    # The chain's step is the tenant admin (ceiling of 20 lakh sits in the top band).
    with pytest.raises(HTTPException) as exc:
        await requisition_service.approve_requisition(
            session, hr, requisition_id=requisition.id, approve=True
        )
    assert exc.value.status_code == 403
    await requisition_service.approve_requisition(
        session, admin, requisition_id=requisition.id, approve=True
    )
    await session.commit()
    await session.refresh(requisition)
    assert requisition.status is RequisitionStatus.OPEN

    # ── Candidate, added by hand; a repeat links rather than duplicates ────
    added = await candidate_service.create_candidate(
        session, hr,
        CandidateCreate(
            requisition_id=requisition.id, full_name="Rohit Verma",
            email="rohit@example.com", phone="+91 98765 43210",
            expected_ctc=Decimal("1900000"), notice_period_days=60,
        ),
    )
    await session.commit()
    assert added["created"] is True
    again = await candidate_service.create_candidate(
        session, hr,
        CandidateCreate(
            requisition_id=requisition.id, full_name="Rohit V", email="ROHIT@example.com"
        ),
    )
    await session.commit()
    assert again["created"] is False
    candidate_id = uuid.UUID(added["candidate_id"])

    # Salary is HR's; the hiring manager sees the candidate without it.
    as_manager = await candidate_service.candidate_profile(
        session, manager, candidate_id=candidate_id
    )
    assert as_manager["candidate"]["expected_ctc"] is None
    assert as_manager["candidate"]["salary_hidden"] is True
    assert [c["status"] for c in as_manager["consent"]["purposes"]] == ["not_recorded"] * 3
    assert as_manager["fit_score"]["status"] == "not_computed"
    with pytest.raises(HTTPException) as exc:
        await candidate_service.candidate_profile(session, outsider, candidate_id=candidate_id)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await candidate_service.list_candidates(session, alice)
    assert exc.value.status_code == 403

    await candidate_service.move_stage(
        session, manager, candidate_id=candidate_id, to_stage=CandidateStage.SCREENING, reason=None
    )
    await session.commit()

    # ── Interview: panellists see only their own; feedback gates the move ─
    stage = await interview_service.create_stage(
        session, hr,
        values={
            "requisition_id": requisition.id, "sequence": 1, "name": "Technical",
            "stage_type": InterviewStageType.TECHNICAL, "duration_minutes": 60,
            "mode": InterviewMode.VIDEO, "required_competencies": ["design"],
            "is_eliminating": True, "feedback_required": True,
        },
    )
    await session.commit()
    interview = await interview_service.schedule_interview(
        session, hr, candidate_id=candidate_id, stage_id=stage.id,
        start=datetime.now(UTC) - timedelta(hours=2),
        panelist_user_ids=[alice.id, bob.id],
    )
    await session.commit()
    assert interview.calendar_warnings, "no calendar connected is said, not hidden"

    mine = await interview_service.list_interviews_for(session, alice, scope="mine")
    assert [i["id"] for i in mine] == [str(interview.id)]
    assert await interview_service.list_interviews_for(session, people["employee"]) == []
    assert len(await interview_service.list_interviews_for(session, manager)) == 1
    assert await interview_service.list_interviews_for(session, outsider) == []
    for viewer in (people["employee"], outsider):
        with pytest.raises(HTTPException) as exc:
            await interview_service.visible_feedback(session, viewer, interview_id=interview.id)
        assert exc.value.status_code == 404
    with pytest.raises(HTTPException):
        await interview_service.record_response(
            session, alice, interview_id=interview.id, panelist_user_id=bob.id,
            response=interview_service.ResponseStatus.DECLINED,
        )

    with pytest.raises(HTTPException) as exc:
        await candidate_service.move_stage(
            session, manager, candidate_id=candidate_id,
            to_stage=CandidateStage.HUMAN_INTERVIEW, reason=None,
        )
    assert exc.value.status_code == 409 and "still scheduled" in exc.value.detail

    await interview_service.record_outcome(
        session, hr, interview_id=interview.id, outcome=InterviewStatus.COMPLETED
    )
    await session.commit()
    await interview_service.submit_feedback(
        session, alice, interview_id=interview.id,
        recommendation=Recommendation.STRONG_YES, ratings={"design": "9"},
        strengths="Clear on trade-offs.",
    )
    await session.commit()
    # Anchoring: Bob sees nothing of Alice's until he submits.
    before = await interview_service.visible_feedback(session, bob, interview_id=interview.id)
    assert before["feedback"] == [] and before["withheld_count"] == 1
    with pytest.raises(HTTPException) as exc:
        await candidate_service.move_stage(
            session, manager, candidate_id=candidate_id,
            to_stage=CandidateStage.HUMAN_INTERVIEW, reason=None,
        )
    assert "outstanding" in exc.value.detail
    await interview_service.submit_feedback(
        session, bob, interview_id=interview.id,
        recommendation=Recommendation.YES, ratings={"design": "7"},
    )
    await session.commit()

    consolidated = await interview_service.consolidated_view(
        session, manager, interview_id=interview.id
    )
    assert consolidated["complete"] is True
    assert "ai_fit_score" not in consolidated
    with pytest.raises(HTTPException):
        await interview_service.consolidated_view(session, alice, interview_id=interview.id)

    await candidate_service.move_stage(
        session, manager, candidate_id=candidate_id,
        to_stage=CandidateStage.HUMAN_INTERVIEW, reason=None,
    )
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await candidate_service.move_stage(
            session, manager, candidate_id=candidate_id, to_stage=CandidateStage.OFFER, reason="x"
        )
    assert "Offers" in exc.value.detail

    # ── Offer: drafted by HR, approved by someone else ─────────────────────
    structure = await _structure(session, tenant)
    offer = await offer_service.create_offer(
        session, hr, candidate_id=candidate_id, code="OFF-100",
        salary_structure_id=structure.id, ctc_annual=Decimal("1800000"),
        joining_date=date.today() + timedelta(days=45), designation="Senior Engineer",
        grade="L5", valid_until=date.today() + timedelta(days=14),
    )
    await session.commit()
    await offer_service.submit_offer(session, hr, offer_id=offer.id)
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await offer_service.approve_offer(session, hr, offer_id=offer.id, approve=True)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        await offer_service.approval_view(session, outsider, offer_id=offer.id)
    assert exc.value.status_code == 404
    view = await offer_service.approval_view(session, manager, offer_id=offer.id)
    assert view["my_authority"]["can_decide"] is True
    assert "internal_parity" in view
    await offer_service.approve_offer(session, manager, offer_id=offer.id, approve=True)
    await session.commit()
    await session.refresh(offer)
    assert offer.status is OfferStatus.APPROVED

    pending = await requisition_service.pending_decisions(session, manager)
    assert all(item["id"] != offer.id for item in pending)

    template = await _offer_template(session, tenant)
    await offer_service.release_offer(session, hr, offer_id=offer.id, template_id=template.id)
    await session.commit()
    await offer_service.accept_offer(session, hr, offer_id=offer.id, signature_reference="ENV-1")
    await session.commit()
    detail = await offer_service.offer_detail(session, hr, offer_id=offer.id)
    assert detail["actions"]["can_convert"] is True
    assert [e["to_status"] for e in detail["history"]][-1] == "accepted"
    converted = await offer_service.convert_to_employee(
        session, hr, offer_id=offer.id, employee_code="ACM777", work_email="rohit@acme-demo.com"
    )
    await session.commit()

    candidate = await session.get(Candidate, candidate_id)
    await session.refresh(candidate)
    assert candidate.stage is CandidateStage.HIRED
    assert str(candidate.converted_employee_id) == converted["employee_id"]

    events = (
        await session.scalars(
            select(CandidateStageEvent.to_stage)
            .where(CandidateStageEvent.candidate_id == candidate_id)
            .order_by(CandidateStageEvent.occurred_at)
        )
    ).all()
    assert events[:3] == [
        CandidateStage.APPLIED, CandidateStage.SCREENING, CandidateStage.HUMAN_INTERVIEW
    ]

    # ── Analytics read the same records ────────────────────────────────────
    report = await analytics.funnel_report(session, hr)
    stages = {row["stage"]: row["count"] for row in report["funnel"]["stages"]}
    assert stages["applied"] == 1 and stages["joined"] == 1
    assert report["reconciliation"]["reconciles"] is True
    signals = await analytics.interviewer_report(session, hr)
    assert signals["feedback_count"] == 2
    assert all(i["severity"]["deviation"] is None for i in signals["interviewers"])


async def test_a_rejection_needs_a_note_and_ends_the_chain(session, tenant, people) -> None:
    await _chain(session, tenant)
    requisition = await requisition_service.create_requisition(
        session, people["hr"],
        RequisitionCreate(
            title="Analyst", openings=1, budget_ctc_max=Decimal("900000"),
            justification="Growth.",
        ),
    )
    await session.commit()
    await requisition_service.submit_requisition(
        session, people["hr"], requisition_id=requisition.id
    )
    await session.commit()
    # 9 lakh sits in the hiring-manager band; HR (not the raiser) may sign it.
    with pytest.raises(HTTPException) as exc:
        await requisition_service.approve_requisition(
            session, people["admin"], requisition_id=requisition.id, approve=False, note=" "
        )
    assert exc.value.status_code == 422
    result = await requisition_service.approve_requisition(
        session, people["admin"], requisition_id=requisition.id, approve=False,
        note="Hire into the open vacancy first.",
    )
    await session.commit()
    assert result["status"] == "rejected"
    # A rejected draft is editable again, and only by its team or HR.
    with pytest.raises(HTTPException) as exc:
        await requisition_service.requisition_detail(
            session, people["outsider"], requisition_id=requisition.id
        )
    assert exc.value.status_code == 404


async def test_hiring_records_are_tenant_isolated(
    session, tenant, people, other_hr_user
) -> None:
    requisition_id = await _open_requisition(session, tenant, people)
    added = await candidate_service.create_candidate(
        session, people["hr"],
        CandidateCreate(requisition_id=requisition_id, full_name="Asha", email="asha@example.com"),
    )
    await session.commit()
    for call in (
        candidate_service.candidate_profile(
            session, other_hr_user, candidate_id=uuid.UUID(added["candidate_id"])
        ),
        requisition_service.requisition_detail(
            session, other_hr_user, requisition_id=requisition_id
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 404
    items, total = await candidate_service.list_candidates(session, other_hr_user)
    assert items == [] and total == 0


async def test_an_employee_refers_in_their_own_name(session, tenant, people) -> None:
    requisition_id = await _open_requisition(session, tenant, people)
    programme = await recruiting.create_programme(
        session, people["admin"], name="Refer a friend", bonus_by_grade={"L5": Decimal("50000")}
    )
    roles = await referral_views.open_roles(session, people["employee"])
    assert [r["id"] for r in roles] == [str(requisition_id)]
    assert "budget_ctc_max" not in roles[0]

    result = await referral_views.refer_candidate(
        session, people["employee"],
        programme_id=uuid.UUID(programme["programme_id"]), requisition_id=requisition_id,
        candidate_name="Meena Pillai", candidate_email="meena@example.com",
        candidate_phone=None, channel=None, note="Worked with her at Acme.",
    )
    assert result["status"] == "submitted"
    assert result["candidate_added"] is True
    mine = await referral_views.my_referrals(session, people["employee"])
    assert len(mine) == 1 and mine[0]["candidate"] == "meena@example.com"
    assert await referral_views.my_referrals(session, people["alice"]) == []

    candidate = await session.scalar(
        select(Candidate).where(Candidate.email == "meena@example.com")
    )
    assert candidate is not None and candidate.source == "referral"

    # The hiring manager of that role is on its hiring team, whatever the request says.
    with pytest.raises(HTTPException) as exc:
        await referral_views.refer_candidate(
            session, people["manager"],
            programme_id=uuid.UUID(programme["programme_id"]), requisition_id=requisition_id,
            candidate_name="Someone", candidate_email="someone@example.com",
            candidate_phone=None, channel=None, note=None,
        )
    assert exc.value.status_code == 400


async def test_reminders_reach_panellists_in_the_window(session, tenant, people) -> None:
    requisition_id = await _open_requisition(session, tenant, people)
    added = await candidate_service.create_candidate(
        session, people["hr"],
        CandidateCreate(requisition_id=requisition_id, full_name="Dev", email="dev@example.com"),
    )
    await session.commit()
    stage = await interview_service.create_stage(
        session, people["hr"],
        values={
            "requisition_id": requisition_id, "sequence": 1, "name": "HR round",
            "stage_type": InterviewStageType.HR, "duration_minutes": 30,
            "mode": InterviewMode.PHONE, "required_competencies": [],
            "is_eliminating": False, "feedback_required": False,
        },
    )
    now = datetime.now(UTC).replace(microsecond=0)
    await interview_service.schedule_interview(
        session, people["hr"], candidate_id=uuid.UUID(added["candidate_id"]), stage_id=stage.id,
        start=now + timedelta(hours=24) - timedelta(minutes=5),
        panelist_user_ids=[people["alice"].id],
    )
    await session.commit()
    result = await interview_service.send_due_interview_reminders(
        session, people["hr"], now=now, window_minutes=15
    )
    assert result["interviews_reminded"] == 1
    assert result["notifications_raised"] == 1
    later = await interview_service.send_due_interview_reminders(
        session, people["hr"], now=now + timedelta(hours=2), window_minutes=15
    )
    assert later["interviews_reminded"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# Over HTTP: gates, the public apply route, the portal
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
async def client(session: AsyncSession, tmp_path, monkeypatch):
    async def override():
        yield session

    monkeypatch.setattr(settings, "FILE_STORAGE_ROOT", str(tmp_path))
    public_guard.LIMITER.reset()
    app.dependency_overrides[get_session] = override
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        public_guard.LIMITER.reset()


def _auth(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
    )
    return {"Authorization": f"Bearer {token}"}


async def test_role_gates_over_http(client, session, tenant, people) -> None:
    requisition_id = await _open_requisition(session, tenant, people)
    employee = _auth(people["employee"])

    response = await client.get("/api/v1/hiring/candidates", headers=employee)
    assert response.status_code == 403
    response = await client.get("/api/v1/offers", headers=_auth(people["manager"]))
    assert response.status_code == 403
    response = await client.post(
        "/api/v1/hiring/requisitions", headers=employee,
        json={"title": "Sneaky", "openings": 1, "employment_type": "full_time"},
    )
    assert response.status_code == 403

    listed = await client.get("/api/v1/hiring/requisitions", headers=employee)
    assert listed.status_code == 200
    body = listed.json()
    assert [r["id"] for r in body["items"]] == [str(requisition_id)]
    assert "budget_ctc_max" not in body["items"][0]

    created = await client.post(
        "/api/v1/hiring/requisitions", headers=_auth(people["manager"]),
        json={"title": "Platform Engineer", "openings": 2, "employment_type": "full_time"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "draft"
    # Employees still only see open roles.
    body = (await client.get("/api/v1/hiring/requisitions", headers=employee)).json()
    assert body["total"] == 1

    unknown = await client.get(
        f"/api/v1/hiring/candidates/{uuid.uuid4()}", headers=_auth(people["hr"])
    )
    assert unknown.status_code == 404


async def test_public_apply_portal_and_throttle(client, session, tenant, people) -> None:
    await _open_requisition(session, tenant, people)
    requisition_code = (
        await client.get("/api/v1/hiring/requisitions", headers=_auth(people["hr"]))
    ).json()["items"][0]["code"]
    session.add(
        CareersPageConfig(
            tenant_id=tenant.id, slug="acme-careers", is_published=True,
            privacy_notice_html="<p>We keep your data for two years.</p><script>x()</script>",
            about_html="<h2>About</h2><p>We build things.</p>",
        )
    )
    await session.commit()

    page = await client.get("/api/v1/public/careers/acme-careers")
    assert page.status_code == 200
    body = page.json()
    assert body["branding"]["about_paragraphs"] == ["About", "We build things."]
    assert "about_html" not in body["branding"]
    assert "privacy_notice_html" not in body
    job_url = f"/api/v1/public/careers/acme-careers/jobs/{requisition_code}"
    job = (await client.get(job_url)).json()
    assert "description_html" not in job["job"]
    assert "budget" not in str(job).lower()

    url = f"/api/v1/public/careers/acme-careers/jobs/{requisition_code}/apply"
    base = {"full_name": "Priya Nair", "email": "priya@example.com"}

    no_consent = await client.post(url, data={**base, "consent_application_processing": "false"})
    assert no_consent.status_code == 422

    bot = await client.post(
        url, data={**base, "consent_application_processing": "true", "website": "http://x"}
    )
    assert bot.status_code == 400

    not_pdf = await client.post(
        url, data={**base, "consent_application_processing": "true"},
        files={"resume": ("cv.pdf", b"<html>not a pdf</html>", "application/pdf")},
    )
    assert not_pdf.status_code == 422

    ok = await client.post(
        url,
        data={
            **base,
            "consent_application_processing": "true",
            "consent_talent_pool": "true",
        },
        files={"resume": ("../../etc/passwd.pdf", PDF, "application/pdf")},
    )
    assert ok.status_code == 201, ok.text
    token = ok.json()["portal_token"]
    assert ok.json()["resume_stored"] is True

    application = await session.scalar(select(Application))
    assert application.resume_storage_key.startswith(f"{tenant.id}/resumes/")
    assert "passwd" not in application.resume_storage_key

    portal = await client.get(f"/api/v1/public/candidate/{token}")
    assert portal.status_code == 200
    assert portal.json()["applications"][0]["status"] == "applied"
    assert "stage" not in portal.json()["applications"][0]

    bad_doc = await client.post(
        f"/api/v1/public/candidate/{token}/documents",
        data={"category": "pan_card"},
        files={"file": ("pan.png", b"MZ\x90\x00 executable", "image/png")},
    )
    assert bad_doc.status_code == 422
    good_doc = await client.post(
        f"/api/v1/public/candidate/{token}/documents",
        data={"category": "pan_card"},
        files={"file": ("pan.pdf", PDF, "application/pdf")},
    )
    assert good_doc.status_code == 201, good_doc.text

    # HR can fetch the resume as an attachment; a stranger's token sees nothing.
    candidate = await session.scalar(
        select(Candidate).where(Candidate.email == "priya@example.com")
    )
    resume = await client.get(
        f"/api/v1/hiring/candidates/{candidate.id}/resume", headers=_auth(people["hr"])
    )
    assert resume.status_code == 200
    assert resume.headers["content-disposition"].startswith("attachment")
    assert (await client.get("/api/v1/public/candidate/not-a-real-token")).status_code == 404

    # Five applications per address per ten minutes; the sixth is refused.
    statuses = [no_consent.status_code, bot.status_code, not_pdf.status_code, ok.status_code]
    extra = await client.post(url, data={**base, "consent_application_processing": "true"})
    statuses.append(extra.status_code)
    throttled = await client.post(url, data={**base, "consent_application_processing": "true"})
    assert throttled.status_code == 429, statuses
    assert "retry-after" in throttled.headers
