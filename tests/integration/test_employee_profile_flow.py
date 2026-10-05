"""Employee profile and self-service, against a real database.

Change requests (raise, approve, applied in the same transaction), the
self-read that no longer leaks pre-hire data, document self-upload with HR
verification, and self-resignation (submit, withdraw, accept).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import decrypt_secret
from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.disbursement import EmployeeBankAccount
from app.models.employee import CustomFieldDefinition, Employee, EmploymentStatus
from app.models.exit_workflow import ClearanceTemplate, ExitRecord
from app.models.notifications import Notification
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from tests.integration.conftest import requires_db

pytestmark = requires_db

# A syntactically valid Aadhaar-shaped number (Verhoeff check digit correct),
# made up for the test.
AADHAAR = "234567890124"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00"
    b"\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7"
    b"5\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture(autouse=True)
def _storage_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FILE_STORAGE_ROOT", str(tmp_path / "store"))


@pytest.fixture
async def client(session: AsyncSession) -> AsyncGenerator[httpx.AsyncClient, None]:
    async def override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_session] = override
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _auth(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
    )
    return {"Authorization": f"Bearer {token}"}


async def _login(
    session: AsyncSession, tenant: Tenant, employee: Employee | None, role: UserRole, tag: str
) -> User:
    user = User(
        tenant_id=tenant.id, email=f"{tag}@acme.test", password_hash="x", full_name=tag,
        role=role, is_active=True, employee_id=employee.id if employee else None,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _person(session: AsyncSession, tenant: Tenant, code: str, **extra) -> Employee:
    person = Employee(
        tenant_id=tenant.id, employee_code=code, first_name=code,
        work_email=f"{code.lower()}@acme.test", **extra,
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


# ═══════════════════════════════════════════════════════════════════════════
# Change requests
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_change_request_is_applied_when_hr_approves_it(
    client, session, tenant, hr_user, employee
) -> None:
    me = await _login(session, tenant, employee, UserRole.EMPLOYEE, "anita")

    # Non-sensitive sections are the employee's to edit directly...
    education = await client.put(
        "/api/v1/employees/me/profile",
        headers=_auth(me),
        json={
            "change": {
                "section": "education",
                "data": [
                    {
                        "level": "graduate",
                        "qualification": "B.Com",
                        "institution": "Christ University",
                        "start_year": 2014,
                        "end_year": 2017,
                    }
                ],
            }
        },
    )
    assert education.status_code == 200, education.text
    assert education.json()["education"][0]["qualification"] == "B.Com"

    # ...sensitive ones are not.
    direct_pan = await client.put(
        "/api/v1/employees/me/profile",
        headers=_auth(me),
        json={"change": {"section": "statutory", "data": {"pan": "ABCPE1234F"}}},
    )
    assert direct_pan.status_code == 403

    bad_pan = await client.post(
        "/api/v1/employees/me/change-requests",
        headers=_auth(me),
        json={"change": {"section": "statutory", "data": {"pan": "ABCDE1234F"}}, "reason": "x"},
    )
    assert bad_pan.status_code == 422

    raised = await client.post(
        "/api/v1/employees/me/change-requests",
        headers=_auth(me),
        json={
            "change": {"section": "statutory", "data": {"pan": "abcpe1234f", "aadhaar": AADHAAR}},
            "reason": "PAN card received",
        },
    )
    assert raised.status_code == 201, raised.text
    request_id = raised.json()["id"]
    # The employee reads their own request back masked; the full Aadhaar is
    # nowhere, not even for HR.
    assert raised.json()["proposed"]["pan"] == "XXXXXX234F"
    assert raised.json()["proposed"]["aadhaar_last4"] == "XXXX XXXX 0124"

    again = await client.post(
        "/api/v1/employees/me/change-requests",
        headers=_auth(me),
        json={"change": {"section": "statutory", "data": {"uan": "101234567890"}}, "reason": "UAN"},
    )
    assert again.status_code == 409

    # It is in HR's approvals inbox, and HR sees the PAN in full.
    inbox = await client.get("/api/v1/approvals", headers=_auth(hr_user))
    assert inbox.status_code == 200
    assert any(
        item["source"] == "profile_change" and item["id"] == request_id
        for item in inbox.json()["items"]
    )
    queue = await client.get(
        "/api/v1/employees/change-requests?status=pending", headers=_auth(hr_user)
    )
    assert queue.json()[0]["proposed"]["pan"] == "ABCPE1234F"
    assert queue.json()[0]["can_decide"] is True

    # The employee cannot decide it; HR approves and it is applied.
    self_approve = await client.post(
        f"/api/v1/employees/change-requests/{request_id}/approve", headers=_auth(me), json={}
    )
    assert self_approve.status_code == 403
    approved = await client.post(
        f"/api/v1/employees/change-requests/{request_id}/approve",
        headers=_auth(hr_user),
        json={"note": "Checked against the card"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    await session.refresh(employee)
    assert employee.pan == "ABCPE1234F"
    assert employee.aadhaar_last4 == "0124"

    decided = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == "profile_change_request",
                AuditLog.summary.like("Approved and applied%"),
            )
        )
    ).all()
    assert len(decided) == 1
    assert "ABCPE1234F" not in str(decided[0].changes)

    # Reads: masked for the person, full for HR.
    mine = await client.get("/api/v1/employees/me/profile", headers=_auth(me))
    assert mine.json()["statutory"] == {
        "pan": "XXXXXX234F",
        "uan": None,
        "pf_number": None,
        "esi_number": None,
        "aadhaar": "XXXX XXXX 0124",
        "masked": True,
    }
    theirs = await client.get(f"/api/v1/employees/{employee.id}/profile", headers=_auth(hr_user))
    assert theirs.json()["statutory"]["pan"] == "ABCPE1234F"
    assert theirs.json()["pending_requests"] == []

    # A bank change lands as a new, encrypted primary salary account.
    bank = await client.post(
        "/api/v1/employees/me/change-requests",
        headers=_auth(me),
        json={
            "change": {
                "section": "bank_account",
                "data": {
                    "account_number": "001234567890",
                    "ifsc": "hdfc0001234",
                    "account_holder_name": "Anita Sharma",
                },
            },
            "reason": "Salary account moved",
        },
    )
    assert bank.status_code == 201, bank.text
    assert "001234567890" not in bank.text
    ok = await client.post(
        f"/api/v1/employees/change-requests/{bank.json()['id']}/approve",
        headers=_auth(hr_user),
        json={},
    )
    assert ok.status_code == 200, ok.text
    account = await session.scalar(
        select(EmployeeBankAccount).where(EmployeeBankAccount.employee_id == employee.id)
    )
    assert account is not None and account.is_primary
    assert account.account_number != "001234567890"
    assert decrypt_secret(account.account_number, tenant.id) == "001234567890"
    profile = await client.get("/api/v1/employees/me/profile", headers=_auth(me))
    assert profile.json()["bank_account"]["account_number_masked"] == "XXXXXX7890"

    # A colleague sees none of it.
    colleague = await _login(
        session, tenant, await _person(session, tenant, "COL1"), UserRole.EMPLOYEE, "col"
    )
    refused = await client.get(
        f"/api/v1/employees/{employee.id}/profile", headers=_auth(colleague)
    )
    assert refused.status_code == 403


async def test_an_approval_refuses_when_the_record_moved_underneath_it(
    client, session, tenant, hr_user, employee
) -> None:
    me = await _login(session, tenant, employee, UserRole.EMPLOYEE, "anita")
    address = {"line1": "12 MG Road", "city": "Bengaluru", "state_code": "KA", "pincode": "560001"}
    raised = await client.post(
        "/api/v1/employees/me/change-requests",
        headers=_auth(me),
        json={"change": {"section": "addresses", "data": {"current": address}}, "reason": "Moved"},
    )
    assert raised.status_code == 201, raised.text

    # HR corrects the address directly in the meantime.
    hr_edit = await client.put(
        f"/api/v1/employees/{employee.id}/profile",
        headers=_auth(hr_user),
        json={
            "change": {
                "section": "addresses",
                "data": {"current": {**address, "line1": "14 MG Road"}},
            }
        },
    )
    assert hr_edit.status_code == 200, hr_edit.text

    stale = await client.post(
        f"/api/v1/employees/change-requests/{raised.json()['id']}/approve",
        headers=_auth(hr_user),
        json={},
    )
    assert stale.status_code == 409
    rejected_blank = await client.post(
        f"/api/v1/employees/change-requests/{raised.json()['id']}/reject",
        headers=_auth(hr_user),
        json={"note": ""},
    )
    assert rejected_blank.status_code == 422
    rejected = await client.post(
        f"/api/v1/employees/change-requests/{raised.json()['id']}/reject",
        headers=_auth(hr_user),
        json={"note": "Already corrected; raise again if still wrong"},
    )
    assert rejected.json()["status"] == "rejected"

    delivered = (
        await session.scalars(select(Notification).where(Notification.recipient_user_id == me.id))
    ).all()
    assert any("rejected" in (n.rendered_subject or "") for n in delivered)


# ═══════════════════════════════════════════════════════════════════════════
# Self-read
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_self_read_carries_no_hiring_or_sensitive_custom_fields(
    client, session, tenant, hr_user
) -> None:
    session.add_all(
        [
            CustomFieldDefinition(
                tenant_id=tenant.id, entity="employee", key="tshirt_size", label="T-shirt",
                field_type="text",
            ),
            CustomFieldDefinition(
                tenant_id=tenant.id, entity="employee", key="bgv_vendor_ref",
                label="BGV reference", field_type="text", is_sensitive=True,
            ),
        ]
    )
    await session.commit()
    person = await _person(
        session,
        tenant,
        "HIRE1",
        custom_fields={
            "tshirt_size": "M",
            "bgv_vendor_ref": "BGV-991",
            "fit_score": "82.50",
            "expected_ctc": "1800000",
            "current_ctc_at_offer": "1500000",
            "offer_code": "OFF-1",
        },
    )
    me = await _login(session, tenant, person, UserRole.EMPLOYEE, "hire1")
    colleague = await _login(
        session, tenant, await _person(session, tenant, "COL2"), UserRole.EMPLOYEE, "col2"
    )

    own = await client.get("/api/v1/employees/me", headers=_auth(me))
    assert own.status_code == 200
    assert own.json()["custom_fields"] == {"tshirt_size": "M"}
    by_id = await client.get(f"/api/v1/employees/{person.id}", headers=_auth(me))
    assert by_id.json()["custom_fields"] == {"tshirt_size": "M"}
    for leaked in ("fit_score", "expected_ctc", "current_ctc_at_offer", "1800000"):
        assert leaked not in own.text

    assert (
        await client.get(f"/api/v1/employees/{person.id}", headers=_auth(colleague))
    ).json()["custom_fields"] == {}
    assert (
        await client.get(f"/api/v1/employees/{person.id}", headers=_auth(hr_user))
    ).json()["custom_fields"]["fit_score"] == "82.50"


# ═══════════════════════════════════════════════════════════════════════════
# Documents
# ═══════════════════════════════════════════════════════════════════════════


async def test_an_employee_uploads_a_document_and_hr_verifies_it(
    client, session, tenant, hr_user, employee
) -> None:
    me = await _login(session, tenant, employee, UserRole.EMPLOYEE, "anita")
    refused = await client.post(
        "/api/v1/documents/mine",
        headers=_auth(me),
        data={"category": "payslip", "title": "My payslip"},
        files={"file": ("p.png", PNG, "image/png")},
    )
    assert refused.status_code == 422

    uploaded = await client.post(
        "/api/v1/documents/mine",
        headers=_auth(me),
        data={"category": "education", "title": "Degree certificate"},
        files={"file": ("degree.png", PNG, "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    doc = uploaded.json()
    assert doc["verification_status"] == "pending"
    assert doc["self_uploaded"] is True
    assert doc["employee_id"] == str(employee.id)

    listed = await client.get("/api/v1/documents", headers=_auth(me))
    assert [d["id"] for d in listed.json()] == [doc["id"]]

    inbox = await client.get("/api/v1/approvals", headers=_auth(hr_user))
    assert any(i["source"] == "document_verification" for i in inbox.json()["items"])

    by_employee = await client.post(
        f"/api/v1/documents/{doc['id']}/verify", headers=_auth(me), json={"decision": "verified"}
    )
    assert by_employee.status_code == 403
    no_note = await client.post(
        f"/api/v1/documents/{doc['id']}/verify",
        headers=_auth(hr_user),
        json={"decision": "rejected"},
    )
    assert no_note.status_code == 422
    verified = await client.post(
        f"/api/v1/documents/{doc['id']}/verify",
        headers=_auth(hr_user),
        json={"decision": "verified"},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["verification_status"] == "verified"
    twice = await client.post(
        f"/api/v1/documents/{doc['id']}/verify",
        headers=_auth(hr_user),
        json={"decision": "rejected", "note": "changed my mind"},
    )
    assert twice.status_code == 409


# ═══════════════════════════════════════════════════════════════════════════
# Self-resignation
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_resignation_is_submitted_withdrawn_and_accepted(
    client, session, tenant, hr_user
) -> None:
    boss = await _person(session, tenant, "BOSS1")
    boss_login = await _login(session, tenant, boss, UserRole.EMPLOYEE, "boss")
    # The IT admin who collects laptops signs in as a plain employee.
    it_admin = await _login(
        session, tenant, await _person(session, tenant, "ITADM"), UserRole.EMPLOYEE, "itadmin"
    )
    session.add(
        ClearanceTemplate(
            tenant_id=tenant.id, department="IT", description="Laptop returned",
            owner_role="named_user", owner_user_id=it_admin.id,
        )
    )
    await session.commit()
    leaver = await _person(
        session, tenant, "LEAVE1", manager_id=boss.id, notice_period_days=30,
        status=EmploymentStatus.ACTIVE,
    )
    me = await _login(session, tenant, leaver, UserRole.EMPLOYEE, "leaver")

    submitted = await client.post(
        "/api/v1/exits/resign",
        headers=_auth(me),
        json={"reason": "Moving to Pune", "comments": "Happy to hand over"},
    )
    assert submitted.status_code == 201, submitted.text
    body = submitted.json()
    assert body["status"] == "initiated"
    assert body["awaiting_acceptance"] is True
    assert body["initiated_by_employee"] is True
    proposed = body["resignation_date"]
    assert date.fromisoformat(body["requested_last_working_day"]) == date.fromisoformat(
        proposed
    ) + timedelta(days=30)
    await session.refresh(leaver)
    assert leaver.status is EmploymentStatus.ON_NOTICE

    # The line manager and HR were told, and it is in the manager's inbox.
    told = (
        await session.scalars(
            select(Notification.recipient_user_id).where(
                Notification.event_key == "exit.resignation.submitted"
            )
        )
    ).all()
    assert boss_login.id in told and hr_user.id in told
    inbox = await client.get("/api/v1/approvals", headers=_auth(boss_login))
    assert any(i["source"] == "resignation" for i in inbox.json()["items"])

    mine = await client.get("/api/v1/exits/mine", headers=_auth(me))
    assert mine.json()["id"] == body["id"]

    # A colleague cannot withdraw it; the leaver can, while it is unaccepted.
    other = await _login(
        session, tenant, await _person(session, tenant, "COL3"), UserRole.EMPLOYEE, "col3"
    )
    assert (
        await client.post(
            f"/api/v1/exits/{body['id']}/withdraw", headers=_auth(other), json={"reason": "no"}
        )
    ).status_code == 404
    withdrawn = await client.post(
        f"/api/v1/exits/{body['id']}/withdraw",
        headers=_auth(me),
        json={"reason": "Counter-offer accepted"},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["status"] == "cancelled"
    assert withdrawn.json()["withdrawn_at"] is not None
    await session.refresh(leaver)
    assert leaver.status is EmploymentStatus.ACTIVE

    # A second leaver resigns asking for an earlier day; the manager accepts,
    # after which the leaver can no longer withdraw it themselves.
    second = await _person(
        session, tenant, "LEAVE2", manager_id=boss.id, notice_period_days=30,
    )
    second_login = await _login(session, tenant, second, UserRole.EMPLOYEE, "leaver2")
    early = await client.post(
        "/api/v1/exits/resign",
        headers=_auth(second_login),
        json={
            "reason": "Higher studies",
            "requested_last_working_day": (
                date.fromisoformat(proposed) + timedelta(days=20)
            ).isoformat(),
        },
    )
    assert early.status_code == 201, early.text
    assert early.json()["notice_shortfall_days"] == 10
    exit_id = early.json()["id"]

    self_accept = await client.post(
        f"/api/v1/exits/{exit_id}/accept", headers=_auth(second_login), json={}
    )
    assert self_accept.status_code == 403
    accepted = await client.post(
        f"/api/v1/exits/{exit_id}/accept",
        headers=_auth(boss_login),
        json={
            "approved_last_working_day": (
                date.fromisoformat(proposed) + timedelta(days=30)
            ).isoformat(),
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["awaiting_acceptance"] is False
    assert accepted.json()["notice_shortfall_days"] == 0
    assert accepted.json()["status"] == "notice_period"
    too_late = await client.post(
        f"/api/v1/exits/{exit_id}/withdraw",
        headers=_auth(second_login),
        json={"reason": "Changed my mind"},
    )
    assert too_late.status_code == 409

    record = await session.get(ExitRecord, exit_id)
    assert record is not None and record.accepted_by_user_id == boss_login.id

    # The named owner finds the item waiting on them and signs it off, though
    # their login is not a manager's.
    tasks = await client.get("/api/v1/exits/clearance/mine", headers=_auth(it_admin))
    assert tasks.status_code == 200, tasks.text
    assert [t["exit_id"] for t in tasks.json()] == [exit_id]
    inbox = await client.get("/api/v1/approvals", headers=_auth(it_admin))
    assert any(i["source"] == "clearance_task" for i in inbox.json()["items"])
    checklist = await client.get(f"/api/v1/exits/{exit_id}/clearance", headers=_auth(it_admin))
    assert checklist.status_code == 200
    assert [row["can_update"] for row in checklist.json()] == [True]
    signed = await client.patch(
        f"/api/v1/exits/clearance/{tasks.json()[0]['item_id']}",
        headers=_auth(it_admin),
        json={"outcome": "cleared"},
    )
    assert signed.status_code == 200, signed.text
    assert (await client.get("/api/v1/exits/clearance/mine", headers=_auth(it_admin))).json() == []
    # Somebody with nothing on this exit is told it does not exist.
    assert (
        await client.get(f"/api/v1/exits/{exit_id}/clearance", headers=_auth(other))
    ).status_code == 404
