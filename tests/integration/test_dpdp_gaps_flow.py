"""The 2026-10-29 DPDP audit's findings, each closed and tested against a real database.

docs/dpdp-alignment-audit-2026-10-29.md lists twelve places where the privacy
machinery and the modules did not meet. One test (or a few) per finding:

1. My privacy switches stop what they say (surveys, photo, celebrations).
2. Withdrawing consent deletes what was held only under it.
3. A new workspace starts with a grievance contact; rights work without one.
4. Retention runs from day one: defaults seeded at creation and by the sweep.
5. Erasure and purges delete the uploaded files, not only the rows.
6. A deleted workspace is exported, then purged — rows and files.
7. The sub-processor register names where data really goes.
8. MCP and résumé screening respect each person's AI choice.
9. Requests for candidates, nominees and contacts are answerable.
10. The notice names the Board and nomination; website leads are purged.
11. A statutory register download is audited.
12. Applying is acknowledged (legitimate use); AI help is a separate consent.
"""

from __future__ import annotations

import uuid
import zipfile
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.companion import CompanionConversation, CompanionMessage
from app.models.culture_depth import (
    EventPrivacyPreference,
    SurveyKindEnum,
    SurveyParticipant,
)
from app.models.demo_request import DemoRequest
from app.models.dpdp import (
    ConsentCapturedVia,
    ConsentRecord,
    CrossBorderTransfer,
    DataPrincipalKind,
    DprRequestStatus,
    DprRequestType,
)
from app.models.employee import Employee, EmploymentStatus
from app.models.grounding import AgentInvocation, InvocationOutcome
from app.models.hiring import Candidate, Requisition
from app.models.jobboard import Application, CandidateConsent, ConsentPurpose, JobBoard
from app.models.retention import RetentionPolicyRow
from app.models.talent_pools import TalentPool, TalentPoolMember
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.culture import SurveyResponseCreate
from app.services import demo_requests, integrations, platform, tenant_purge
from app.services import employee as employee_service
from app.services.culture import service as culture
from app.services.culture import surveys_portal
from app.services.dpdp import erasure, transfers
from app.services.dpdp import service as dpdp
from app.services.dpdp.gates import hidden_photo_ids
from app.services.hiring import candidate_portal, careers
from app.services.notifications.adapters import generic_push_text
from app.services.payroll import statutory_service
from app.services.retention import service as retention
from app.services.storage import get_storage
from app.services.storage.cleanup import delete_files, keys_in
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


@pytest.fixture
def storage_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local", raising=False)
    monkeypatch.setattr(settings, "FILE_STORAGE_ROOT", str(tmp_path / "store"), raising=False)
    return tmp_path / "store"


async def _login_for(session, employee: Employee, *, role=UserRole.EMPLOYEE) -> User:
    user = User(
        tenant_id=employee.tenant_id,
        email=f"{employee.employee_code.lower()}-{uuid.uuid4().hex[:4]}@acme.test",
        password_hash="not-a-real-hash",
        full_name=employee.full_name,
        role=role,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _colleague(session, tenant: Tenant, code: str = "ACM002") -> Employee:
    person = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name="Ravi",
        last_name="Kumar",
        work_email=f"{code.lower()}@acme.test",
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2025, 1, 1),
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


async def _decide(session, actor, principal: Employee, purpose: str, granted: bool):
    return await dpdp.record_consent(
        session,
        actor,
        principal_id=principal.id,
        principal_kind=DataPrincipalKind.EMPLOYEE,
        purpose_key=purpose,
        granted=granted,
        captured_via=ConsentCapturedVia.SELF_SERVICE,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1 + 2. Switches stop things; withdrawal deletes what rested on them
# ══════════════════════════════════════════════════════════════════════════════


async def test_withdrawing_ai_consent_deletes_ask_mira_history(session, hr_user, employee):
    me = await _login_for(session, employee)
    await _decide(session, me, employee, "ai_assistance", True)
    conversation = CompanionConversation(
        tenant_id=me.tenant_id, user_id=me.id, title="My leave", last_message_at=datetime.now(UTC)
    )
    session.add(conversation)
    await session.flush()
    session.add(
        CompanionMessage(
            tenant_id=me.tenant_id,
            conversation_id=conversation.id,
            user_id=me.id,
            role="user",
            text="How many leave days do I have?",
        )
    )
    session.add(
        AgentInvocation(
            tenant_id=me.tenant_id,
            agent_key="assistant",
            prompt_fingerprint="x" * 16,
            model="test",
            requested_by_user_id=me.id,
            scope_kind="self",
            question="What is my salary?",
            raw_output="It is in your payslip.",
            outcome=InvocationOutcome.ANSWERED,
        )
    )
    await session.commit()

    effect = await dpdp.withdraw_consent(
        session,
        me,
        principal_id=employee.id,
        principal_kind=DataPrincipalKind.EMPLOYEE,
        purpose_key="ai_assistance",
    )

    assert effect["erased_counts"]["ask_mira_conversations"] == 1
    assert (await session.scalar(select(func.count()).select_from(CompanionMessage))) == 0
    call = (await session.scalars(select(AgentInvocation))).one()
    assert call.question is None and call.raw_output is None, "the log line stays, the text goes"
    erased_audit = (
        await session.scalars(
            select(AuditLog).where(AuditLog.entity_type == "consent_withdrawal_erasure")
        )
    ).all()
    assert len(erased_audit) == 1
    # The consent rows themselves are evidence and stay.
    rows = (await session.scalars(select(ConsentRecord))).all()
    assert [r.granted for r in sorted(rows, key=lambda r: r.decided_at)] == [True, False]


async def test_saying_no_to_surveys_stops_invitations_and_removes_unanswered_ones(
    session, hr_user, employee
):
    me = await _login_for(session, employee)
    today = date.today()
    first = await culture.create_survey(
        session,
        hr_user,
        name="Pulse",
        kind=SurveyKindEnum.ENPS,
        opens_on=today,
        closes_on=today + timedelta(days=7),
        instrument_key="enps",
    )
    assert len(await surveys_portal.list_my_surveys(session, me)) == 1

    await _decide(session, me, employee, "culture_analytics", True)
    await dpdp.withdraw_consent(
        session,
        me,
        principal_id=employee.id,
        principal_kind=DataPrincipalKind.EMPLOYEE,
        purpose_key="culture_analytics",
    )

    # The unanswered invitation is gone, and nothing is listed any more.
    left = (
        await session.scalars(
            select(SurveyParticipant).where(SurveyParticipant.survey_id == first.id)
        )
    ).all()
    assert left == []
    assert await surveys_portal.list_my_surveys(session, me) == []

    # A new survey does not invite them.
    second = await culture.create_survey(
        session,
        hr_user,
        name="Pulse 2",
        kind=SurveyKindEnum.ENPS,
        opens_on=today,
        closes_on=today + timedelta(days=7),
        instrument_key="enps",
    )
    invited = (
        await session.scalars(
            select(SurveyParticipant.employee_id).where(SurveyParticipant.survey_id == second.id)
        )
    ).all()
    assert employee.id not in invited

    # And an answer is refused, saying where to change it.
    with pytest.raises(HTTPException) as exc:
        await surveys_portal.respond(
            session, me, first.id, SurveyResponseCreate(answers={"enps": 9})
        )
    assert exc.value.status_code in (403, 409)


async def test_answering_a_survey_is_how_someone_never_asked_agrees(session, hr_user, employee):
    me = await _login_for(session, employee)
    today = date.today()
    survey = await culture.create_survey(
        session,
        hr_user,
        name="Pulse",
        kind=SurveyKindEnum.ENPS,
        opens_on=today,
        closes_on=today + timedelta(days=7),
        instrument_key="enps",
    )
    await surveys_portal.respond(session, me, survey.id, SurveyResponseCreate(answers={"enps": 8}))
    state = await dpdp.list_consent(session, me, principal_id=employee.id)
    surveys = next(p for p in state["purposes"] if p["purpose"] == "culture_analytics")
    assert surveys["status"] == "granted"


async def test_a_colleague_sees_a_photo_only_with_its_owners_agreement(
    session, tenant, hr_user, employee
):
    other = await _colleague(session, tenant)
    viewer = await _login_for(session, other)
    owner = await _login_for(session, employee)

    assert employee.id in await hidden_photo_ids(session, viewer, [employee.id])
    assert await hidden_photo_ids(session, hr_user, [employee.id]) == set(), "HR always sees it"
    assert await hidden_photo_ids(session, owner, [employee.id]) == set(), "so does the person"

    employee.photo_url = f"/api/v1/employees/{employee.id}/photo"
    await session.commit()
    await session.refresh(employee)
    view = await employee_service.employee_view(session, viewer, employee)
    assert view.photo_url is None

    await _decide(session, owner, employee, "directory_visibility", True)
    assert await hidden_photo_ids(session, viewer, [employee.id]) == set()
    await session.refresh(employee)
    view = await employee_service.employee_view(session, viewer, employee)
    assert view.photo_url == employee.photo_url


async def test_the_celebrations_switch_and_the_calendar_settings_are_one_choice(
    session, hr_user, employee
):
    me = await _login_for(session, employee)
    await _decide(session, me, employee, "celebrations", True)
    pref = await session.scalar(
        select(EventPrivacyPreference).where(EventPrivacyPreference.employee_id == employee.id)
    )
    assert pref.share_birthday and pref.share_work_anniversary

    await dpdp.withdraw_consent(
        session,
        me,
        principal_id=employee.id,
        principal_kind=DataPrincipalKind.EMPLOYEE,
        purpose_key="celebrations",
    )
    await session.refresh(pref)
    assert not pref.share_birthday and not pref.share_work_anniversary

    # The calendar's own settings screen writes the consent log too.
    await culture.set_event_privacy(
        session, me, employee_id=employee.id, share_birthday=True, share_work_anniversary=False
    )
    state = await dpdp.list_consent(session, me, principal_id=employee.id)
    celebrations = next(p for p in state["purposes"] if p["purpose"] == "celebrations")
    assert celebrations["status"] == "granted"


async def test_my_privacy_offers_no_switch_for_features_that_do_not_exist(session, employee):
    me = await _login_for(session, employee)
    state = await dpdp.list_consent(session, me, principal_id=employee.id)
    keys = {p["purpose"] for p in state["purposes"]}
    assert not {"wellness", "marketing_communication", "talent_pool"} & keys
    photo = next(p for p in state["purposes"] if p["purpose"] == "directory_visibility")
    assert photo["withdrawal_keeps"], "the person is told the HR record keeps the photo"


# ══════════════════════════════════════════════════════════════════════════════
# 3 + 4. A new workspace starts set up
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_new_workspace_starts_with_a_privacy_contact_and_retention(session, tenant):
    root = User(
        tenant_id=tenant.id,
        email=f"platform-{uuid.uuid4().hex[:4]}@acme.test",
        password_hash="x",
        full_name="Platform",
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )
    session.add(root)
    await session.commit()
    payload = platform.WorkspaceIn(
        company_name="Newco",
        legal_name="Newco Private Limited",
        company_type="private_limited",
        pan="AAACN1234F",
        address_line1="1 MG Road",
        city="Bengaluru",
        state_code="KA",
        pincode="560001",
        admin_full_name="Asha Rao",
        admin_email="asha@newco-dpdp.com",
        admin_phone="9876543210",
        grievance_contact="dpo@newco-dpdp.com",
        data_protection_officer_name="Asha Rao",
        send_welcome_email=False,
    )
    created = await platform.create(session, root, payload, request=None)
    tenant_id = uuid.UUID(created["workspace"]["id"])
    admin = await session.scalar(select(User).where(User.email == "asha@newco-dpdp.com"))

    status = await dpdp.configuration_status(session, admin)
    assert status == {"configured": True, "grievance_contact": "dpo@newco-dpdp.com"}
    policies = await session.scalar(
        select(func.count())
        .select_from(RetentionPolicyRow)
        .where(RetentionPolicyRow.tenant_id == tenant_id)
    )
    assert policies and policies > 0


async def test_the_nightly_sweep_seeds_retention_for_a_workspace_with_none(session, hr_user):
    assert (await session.scalar(select(func.count()).select_from(RetentionPolicyRow))) == 0
    await retention.scheduled_sweep(session, hr_user, today=date.today())
    await session.commit()
    assert (await session.scalar(select(func.count()).select_from(RetentionPolicyRow))) > 0


# ══════════════════════════════════════════════════════════════════════════════
# 5. Files go with their rows
# ══════════════════════════════════════════════════════════════════════════════


async def test_storage_keys_are_found_only_for_their_own_tenant(storage_root):
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    row = {
        "photo": f"{mine}/photos/{'a' * 32}.jpg",
        "nested": [{"key": f"{mine}/punch-photos/{uuid.uuid4()}.jpg"}],
        "other": f"{theirs}/photos/{'b' * 32}.jpg",
        "text": "not a key at all",
        "url": f"/api/v1/employees/{mine}/photo",
    }
    found = keys_in(row, mine)
    assert len(found) == 2 and all(key.startswith(f"{mine}/") for key in found)

    storage = get_storage()
    for key in found:
        await storage.put(key, b"data")
    result = await delete_files(found)
    assert result == {"files_deleted": 2, "files_not_deleted": 0}


async def test_a_candidate_purge_deletes_the_resume_file(session, tenant, hr_user, storage_root):
    storage = get_storage()
    key = storage.new_key(tenant.id, "resumes", ".pdf")
    await storage.put(key, b"%PDF-1.4 resume")
    requisition = Requisition(tenant_id=tenant.id, code="REQ-1", title="Engineer")
    session.add(requisition)
    await session.flush()
    candidate = Candidate(
        tenant_id=tenant.id, requisition_id=requisition.id, full_name="Meera", email="m@x.test"
    )
    session.add(candidate)
    await session.flush()
    session.add(
        Application(
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            requisition_id=requisition.id,
            board=JobBoard.CAREERS,
            external_application_id=uuid.uuid4().hex,
            resume_storage_key=key,
            applied_at=datetime.now(UTC),
        )
    )
    await session.commit()

    result = await careers.purge_on_request(session, hr_user, candidate_id=candidate.id)
    await session.commit()
    assert result["files_deleted"] == 1
    assert not await storage.exists(key)


# ══════════════════════════════════════════════════════════════════════════════
# 6. A deleted workspace is handed back, then purged
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_deleted_workspace_is_exported_then_purged(
    session, tenant, other_tenant, employee, storage_root
):
    root = User(
        tenant_id=other_tenant.id,
        email=f"root-{uuid.uuid4().hex[:4]}@globex.test",
        password_hash="x",
        full_name="Root",
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )
    session.add(root)
    storage = get_storage()
    key = storage.new_key(tenant.id, "documents", ".pdf")
    await storage.put(key, b"%PDF-1.4 offer letter")
    tenant.logo_key = key
    await session.commit()

    deleted = await platform.delete(
        session, root, tenant.id, confirm=tenant.slug, reason="Contract ended", request=None
    )
    assert date.fromisoformat(deleted["purge_after"]) == date.today() + timedelta(
        days=tenant_purge.PURGE_GRACE_DAYS
    )
    listed = await tenant_purge.deleted_workspaces(session, root)
    assert [row["id"] for row in listed] == [str(tenant.id)]

    path, _ = await tenant_purge.export_workspace(session, root, tenant.id)
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        assert "data/employees.json" in names
        assert f"files/{key}" in names
        assert b"Anita" in archive.read("data/employees.json")

    # Not before its date.
    assert (await tenant_purge.purge_due(session, today=date.today()))["workspaces_purged"] == 0
    later = date.today() + timedelta(days=tenant_purge.PURGE_GRACE_DAYS)
    assert (await tenant_purge.purge_due(session, today=later))["workspaces_purged"] == 1

    assert (
        await session.scalar(
            select(func.count()).select_from(Employee).where(Employee.tenant_id == tenant.id)
        )
    ) == 0
    assert not await storage.exists(key)
    refreshed = await session.get(Tenant, tenant.id)
    await session.refresh(refreshed)
    assert refreshed.purged_at is not None and refreshed.name == "Deleted workspace"
    # The audit trail is the one thing kept — including the record of the purge.
    kept = (
        await session.scalars(
            select(AuditLog.summary).where(
                AuditLog.tenant_id == tenant.id, AuditLog.entity_type == "tenant"
            )
        )
    ).all()
    assert any("purged" in (summary or "") for summary in kept)
    # The other workspace is untouched.
    assert await session.get(Tenant, other_tenant.id) is not None


# ══════════════════════════════════════════════════════════════════════════════
# 7. The register says where data goes
# ══════════════════════════════════════════════════════════════════════════════


def test_the_sub_processor_register_names_where_data_really_goes():
    keys = set(transfers.SUB_PROCESSORS)
    assert {"google_email", "expo_push", "google_maps", "digio", "cgb_products", "vercel"} <= keys
    assert {"mcp_client", "app_server", "openstreetmap"} <= keys
    assert "resend" not in keys, "email goes through Gmail SMTP, not Resend"
    for spec in transfers.SUB_PROCESSORS.values():
        assert spec.what_is_sent, f"{spec.name} must say what it receives"


def test_a_push_notification_says_what_kind_of_thing_is_waiting_and_nothing_more():
    title, body = generic_push_text("leave.request_submitted")
    assert title == "A leave update"
    assert body == "Open AskMira to see it."
    assert generic_push_text("unknown.event")[0] == "You have a new update"


# ══════════════════════════════════════════════════════════════════════════════
# 8. MCP and résumé AI respect each person's choice
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
async def client(session) -> AsyncGenerator[httpx.AsyncClient, None]:
    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


async def test_mcp_needs_ai_on_and_leaves_out_people_who_did_not_agree(
    session, tenant, hr_user, employee, client
):
    other = await _colleague(session, tenant)
    created = await integrations.create_key(
        session,
        hr_user,
        integrations.ApiKeyIn(name="Claude", scopes=["employees:read"]),
        request=None,
    )
    headers = {"Authorization": f"Bearer {created['key']}"}
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "search_employees", "arguments": {}},
    }

    off = await client.post("/api/v1/mcp", json=call, headers=headers)
    assert off.json()["result"]["isError"] is True
    assert "switched off" in off.text

    tenant.settings = {**(tenant.settings or {}), "ai": {"enabled": True}}
    await session.commit()
    await _decide(session, hr_user, employee, "ai_assistance", True)

    on = await client.post("/api/v1/mcp", json=call, headers=headers)
    result = on.json()["result"]["structuredContent"]
    codes = [person["employee_code"] for person in result["people"]]
    assert codes == [employee.employee_code]
    assert other.employee_code not in on.text
    assert "not shown" in result["not_shown"]

    transfer = (await session.scalars(select(CrossBorderTransfer))).one()
    assert transfer.sub_processor_key == "mcp_client"
    assert transfer.principal_count == 1


async def test_a_candidate_withdraws_the_talent_pool_from_their_own_page(session, tenant, hr_user):
    requisition = Requisition(tenant_id=tenant.id, code="REQ-2", title="Designer")
    session.add(requisition)
    await session.flush()
    candidate = Candidate(
        tenant_id=tenant.id, requisition_id=requisition.id, full_name="Kiran", email="k@x.test"
    )
    session.add(candidate)
    await session.flush()
    pool = TalentPool(tenant_id=tenant.id, name="Designers")
    session.add(pool)
    await session.flush()
    session.add(TalentPoolMember(tenant_id=tenant.id, pool_id=pool.id, candidate_id=candidate.id))
    session.add(
        CandidateConsent(
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            purpose=ConsentPurpose.TALENT_POOL_RETENTION,
            granted=True,
            notice_text="Keep me in the talent pool",
            notice_version="1",
            recorded_at=datetime.now(UTC),
        )
    )
    _, token = await careers.issue_portal_token(session, hr_user, candidate_id=candidate.id)
    await session.commit()

    view = await candidate_portal.portal_overview(session, token)
    pool_choice = next(c for c in view["consents"] if c["purpose"] == "talent_pool_retention")
    assert pool_choice["granted"] is True

    result = await candidate_portal.withdraw_consent(session, token, "talent_pool_retention")
    await session.commit()
    assert result["erased"] == 1
    assert (await session.scalar(select(func.count()).select_from(TalentPoolMember))) == 0
    after = next(c for c in result["consents"] if c["purpose"] == "talent_pool_retention")
    assert after["granted"] is False


# ══════════════════════════════════════════════════════════════════════════════
# 9. Requests for candidates and for people inside an employee's record
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_candidate_request_exports_and_erases_their_hiring_records(
    session, tenant, hr_user
):
    requisition = Requisition(tenant_id=tenant.id, code="REQ-3", title="Analyst")
    session.add(requisition)
    await session.flush()
    candidate = Candidate(
        tenant_id=tenant.id, requisition_id=requisition.id, full_name="Zoya", email="z@x.test"
    )
    session.add(candidate)
    await session.commit()

    export = await dpdp.build_access_export(session, hr_user, principal_id=candidate.id)
    tables = {section["table"] for section in export["contents"]}
    assert "candidates" in tables

    other_admin = User(
        tenant_id=tenant.id,
        email="admin2@acme.test",
        password_hash="x",
        full_name="Second admin",
        role=UserRole.ADMIN,
        is_active=True,
    )
    session.add(other_admin)
    await session.commit()
    row = await dpdp.raise_request(
        session,
        hr_user,
        principal_id=candidate.id,
        principal_kind=DataPrincipalKind.CANDIDATE,
        principal_label="Zoya",
        request_type=DprRequestType.ERASURE,
    )
    result = await erasure.execute_erasure(session, other_admin, request_id=row.id)
    assert result["status"] == DprRequestStatus.COMPLETED.value
    await session.refresh(candidate)
    assert candidate.full_name == careers.REDACTED


async def test_a_nominees_request_is_answered_on_the_employees_record(session, hr_user):
    row = await dpdp.raise_request(
        session,
        hr_user,
        principal_id=uuid.uuid4(),
        principal_kind=DataPrincipalKind.DEPENDENT,
        principal_label="Nominee of ACM001",
        request_type=DprRequestType.ERASURE,
    )
    with pytest.raises(HTTPException) as exc:
        await erasure.execute_erasure(session, hr_user, request_id=row.id)
    assert exc.value.status_code == 400
    assert "employee's record" in exc.value.detail


# ══════════════════════════════════════════════════════════════════════════════
# 10 + 11. Leads are purged; a statutory register download is audited
# ══════════════════════════════════════════════════════════════════════════════


async def test_old_website_enquiries_are_purged_and_one_can_be_erased(session, tenant):
    old = DemoRequest(
        full_name="Old Lead",
        work_email="old@lead.test",
        company="OldCo",
        company_size="11-50",
        consented_at=datetime.now(UTC),
    )
    fresh = DemoRequest(
        full_name="New Lead",
        work_email="new@lead.test",
        company="NewCo",
        company_size="11-50",
        consented_at=datetime.now(UTC),
    )
    session.add_all([old, fresh])
    await session.commit()
    removed = await demo_requests.purge_stale(
        session, now=datetime.now(UTC) + timedelta(days=demo_requests.RETENTION_DAYS + 1)
    )
    await session.commit()
    assert removed == 2

    session.add(
        DemoRequest(
            full_name="Ask Me",
            work_email="ask@lead.test",
            company="AskCo",
            company_size="11-50",
            consented_at=datetime.now(UTC),
        )
    )
    await session.commit()
    root = User(
        tenant_id=tenant.id,
        email="root@acme.test",
        password_hash="x",
        full_name="Root",
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )
    session.add(root)
    await session.commit()
    target = await session.scalar(select(DemoRequest))
    await demo_requests.erase(session, root, target.id)
    assert (await session.scalar(select(func.count()).select_from(DemoRequest))) == 0


async def test_a_statutory_register_download_is_audited(session, hr_user):
    rendered = SimpleNamespace(
        spec=SimpleNamespace(code="FORM_B", title="Register of wages"), row_count=12
    )
    await statutory_service.record_register_export(
        session, hr_user, rendered, run_id=None, financial_year="2026-27"
    )
    row = (
        await session.scalars(select(AuditLog).where(AuditLog.entity_type == "statutory_register"))
    ).one()
    assert row.action.value == "export"
    assert row.changes["rows"] == 12


# ══════════════════════════════════════════════════════════════════════════════
# 12. Applying is acknowledged; AI help is a separate, optional consent
# ══════════════════════════════════════════════════════════════════════════════


async def test_ai_help_on_a_resume_is_offered_only_where_ai_is_on(session, tenant, monkeypatch):
    from app.services.ai import client as ai_client

    monkeypatch.setattr(ai_client, "configured", lambda: True)
    assert await careers.ai_screening_offered(session, tenant.id) is False
    tenant.settings = {**(tenant.settings or {}), "ai": {"enabled": True, "hiring_drafts": True}}
    await session.commit()
    assert await careers.ai_screening_offered(session, tenant.id) is True
