"""Signing things: offers in the candidate portal, company policies, onboarding forms.

Each records what was typed, when, and the content it was typed against, so an
acknowledgement attests to exactly what the person was shown.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.company_documents import CompanyDocumentAcknowledgement
from app.models.documents import IssuedLetter
from app.models.employee import Employee, EmployeeProfile, EmploymentStatus, Gender
from app.models.offer import OfferStatus
from app.models.onboarding import (
    OnboardingOwnerType,
    OnboardingTaskStatus,
    OnboardingTaskType,
)
from app.models.user import User, UserRole
from app.schemas.letters import LetterAcknowledgeRequest
from app.schemas.onboarding import PlanCreate, TemplateCreate, TemplateTaskIn
from app.services.documents import acknowledgement
from app.services.documents import company as company_documents
from app.services.hiring import candidate_portal, careers
from app.services.hiring import offers as offer_service
from app.services.onboarding import service as onboarding
from tests.hiring.db_fixture import engine  # noqa: F401 - shadows the shared fixture
from tests.integration.conftest import requires_db
from tests.integration.test_offer_flow import (
    _candidate,
    _draft,
    _offer_template,
    _requisition,
    _through_approval,
)

pytestmark = requires_db

PDF = b"%PDF-1.4\n" + b"0" * 64


@pytest.fixture
def storage_root(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "FILE_STORAGE_ROOT", str(tmp_path), raising=False)
    return tmp_path


async def _released_offer(session, tenant, hr_user, legal_entity):
    offer, _, candidate, _ = await _draft(session, tenant, hr_user, legal_entity)
    await _through_approval(session, tenant, hr_user, offer)
    template = await _offer_template(session, tenant)
    await offer_service.release_offer(session, hr_user, offer_id=offer.id, template_id=template.id)
    await session.commit()
    _, token = await careers.issue_portal_token(session, hr_user, candidate_id=candidate.id)
    await session.commit()
    return offer, candidate, token


# ── Offers in the portal ────────────────────────────────────────────────────


async def test_a_candidate_accepts_by_typing_their_name_against_the_letter(
    session, tenant, hr_user, legal_entity
) -> None:
    offer, candidate, token = await _released_offer(session, tenant, hr_user, legal_entity)

    listed = await candidate_portal.portal_offers(session, token)
    assert [o["id"] for o in listed] == [str(offer.id)] and listed[0]["can_respond"] is True
    letter = await candidate_portal.offer_letter(session, token, offer.id)
    assert letter["html"] and letter["content_hash"]

    with pytest.raises(HTTPException) as exc:
        await candidate_portal.respond_to_offer(
            session,
            token,
            offer.id,
            accept=True,
            typed_name="Somebody Else",
            reason=None,
            ip_address="203.0.113.7",
            user_agent="test",
        )
    assert exc.value.status_code == 422

    result = await candidate_portal.respond_to_offer(
        session,
        token,
        offer.id,
        accept=True,
        typed_name=f"  {candidate.full_name.upper()} ",
        reason=None,
        ip_address="203.0.113.7",
        user_agent="test-browser",
    )
    await session.commit()
    assert result["status"] == "accepted"
    await session.refresh(offer)
    assert offer.status is OfferStatus.ACCEPTED
    assert offer.acceptance_record["letter_content_hash"] == letter["content_hash"]
    assert offer.acceptance_record["ip_address"] == "203.0.113.7"

    # Answered once.
    with pytest.raises(HTTPException):
        await candidate_portal.respond_to_offer(
            session,
            token,
            offer.id,
            accept=False,
            typed_name=None,
            reason="Changed my mind",
            ip_address=None,
            user_agent=None,
        )


async def test_a_candidate_declines_with_a_reason(session, tenant, hr_user, legal_entity) -> None:
    offer, _, token = await _released_offer(session, tenant, hr_user, legal_entity)
    with pytest.raises(HTTPException):
        await candidate_portal.respond_to_offer(
            session,
            token,
            offer.id,
            accept=False,
            typed_name=None,
            reason="  ",
            ip_address=None,
            user_agent=None,
        )
    await candidate_portal.respond_to_offer(
        session,
        token,
        offer.id,
        accept=False,
        typed_name=None,
        reason="Accepted another offer closer to home",
        ip_address=None,
        user_agent=None,
    )
    await session.commit()
    await session.refresh(offer)
    assert offer.status is OfferStatus.DECLINED


async def test_another_candidates_token_cannot_reach_the_offer(
    session, tenant, hr_user, legal_entity
) -> None:
    offer, _, _ = await _released_offer(session, tenant, hr_user, legal_entity)
    other_candidate = await _candidate(
        session,
        tenant,
        await _requisition(session, tenant, legal_entity, code="REQ-002"),
        name="Asha Menon",
        email="asha@example.test",
    )
    _, other_token = await careers.issue_portal_token(
        session, hr_user, candidate_id=other_candidate.id
    )
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await candidate_portal.offer_letter(session, other_token, offer.id)
    assert exc.value.status_code == 404


# ── Company policies ────────────────────────────────────────────────────────


async def _employee_login(session, tenant, employee) -> User:
    login = User(
        tenant_id=tenant.id,
        email=employee.work_email,
        password_hash="x",
        full_name=employee.full_name,
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(login)
    await session.commit()
    await session.refresh(login)
    return login


async def test_a_policy_is_acknowledged_against_the_exact_file(
    session, tenant, hr_user, employee, storage_root
) -> None:
    login = await _employee_login(session, tenant, employee)
    doc = await company_documents.upload_company_document(
        session,
        hr_user,
        document_type="POSH policy",
        title="Prevention of sexual harassment",
        data=PDF,
        requires_acknowledgement=True,
    )
    mine = await company_documents.list_company_documents(session, login)
    assert mine[0].requires_acknowledgement and mine[0].acknowledged_by_me is False

    with pytest.raises(HTTPException) as exc:
        await company_documents.acknowledge_company_document(
            session, login, doc.id, typed_name="Not Me"
        )
    assert exc.value.status_code == 422

    done = await company_documents.acknowledge_company_document(
        session, login, doc.id, typed_name=employee.full_name
    )
    assert done.acknowledged_by_me is True
    row = await session.scalar(select(CompanyDocumentAcknowledgement))
    assert row.content_sha256 and row.typed_name == employee.full_name

    register = await company_documents.acknowledgement_register(session, hr_user, doc.id)
    assert register.acknowledged == 1
    assert [r.employee_code for r in register.rows if r.acknowledged_at] == [employee.employee_code]

    # Only HR reads the register.
    with pytest.raises(HTTPException):
        await company_documents.acknowledgement_register(session, login, doc.id)


async def test_a_policy_needing_acknowledgement_must_be_published(
    session, hr_user, storage_root
) -> None:
    with pytest.raises(HTTPException) as exc:
        await company_documents.upload_company_document(
            session,
            hr_user,
            document_type="Code of conduct",
            title="Code",
            data=PDF,
            is_employee_visible=False,
            requires_acknowledgement=True,
        )
    assert exc.value.status_code == 422


# ── Onboarding forms ────────────────────────────────────────────────────────


async def test_a_joiner_signs_their_pf_nomination_and_the_task_completes(
    session, tenant, hr_user, legal_entity, location
) -> None:
    joiner = Employee(
        tenant_id=tenant.id,
        employee_code="JNR001",
        first_name="Kiran",
        last_name="Rao",
        work_email="kiran@acme.test",
        legal_entity_id=legal_entity.id,
        location_id=location.id,
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2026, 10, 1),
        date_of_birth=date(1995, 5, 4),
        gender=Gender.FEMALE,
    )
    session.add(joiner)
    await session.flush()
    session.add(
        EmployeeProfile(
            tenant_id=tenant.id,
            employee_id=joiner.id,
            personal={"father_name": "Suresh Rao", "marital_status": "single"},
            addresses={
                "current": {
                    "line1": "12 MG Road",
                    "city": "Bengaluru",
                    "state_code": "KA",
                    "pincode": "560001",
                    "country_code": "IN",
                },
                "permanent_same_as_current": True,
            },
            nominees=[
                {
                    "name": "Lata Rao",
                    "relationship": "mother",
                    "purpose": "pf",
                    "share_percent": "100",
                    "date_of_birth": "1968-02-11",
                },
            ],
            family_members=[
                {
                    "name": "Lata Rao",
                    "relationship": "mother",
                    "date_of_birth": "1968-02-11",
                    "is_dependent": True,
                },
            ],
        )
    )
    await session.commit()
    login = await _employee_login(session, tenant, joiner)

    template = await onboarding.create_template(
        session,
        hr_user,
        TemplateCreate(
            name=f"Forms {uuid.uuid4().hex[:4]}",
            tasks=[
                TemplateTaskIn(
                    title="Sign your PF nomination (Form 2)",
                    owner_type=OnboardingOwnerType.NEW_JOINER,
                    task_type=OnboardingTaskType.FORM,
                    form_code="FORM2_STD",
                )
            ],
        ),
    )
    plan = await onboarding.start_plan(
        session, hr_user, PlanCreate(employee_id=joiner.id, template_id=template.id)
    )
    await session.commit()
    detail = await onboarding.plan_detail(session, hr_user, plan.id)
    task_id = detail["tasks"][0]["id"]

    # Ticking it off without signing is refused.
    with pytest.raises(HTTPException):
        await onboarding.complete_task(session, login, task_id)

    task = await onboarding.issue_form(session, login, task_id)
    letter = await session.scalar(
        select(IssuedLetter).where(IssuedLetter.id == task.form_letter_id)
    )
    assert "Lata Rao" in letter.merged_html and "100" in letter.merged_html
    assert letter.pdf_storage_key, "a generated statutory form is kept as a PDF too"

    await acknowledgement.acknowledge(
        session, login, letter.id, LetterAcknowledgeRequest(typed_name="Kiran Rao", accept=True)
    )
    await session.refresh(task)
    assert task.status is OnboardingTaskStatus.DONE


async def test_a_form_with_nominee_shares_not_adding_up_is_refused(
    session, tenant, hr_user, legal_entity, location
) -> None:
    from app.services.documents.letter_merge import (
        COMPUTED_HANDLERS,
        LetterMergeError,
        MergeContext,
    )

    context = MergeContext(
        as_of=date(2026, 10, 1),
        nominees=[
            {"name": "A", "relationship": "mother", "purpose": "pf", "share_percent": Decimal("60")}
        ],
    )
    with pytest.raises(LetterMergeError, match="60"):
        COMPUTED_HANDLERS["pf_nominees_table"](context)
