"""Core HR people, organisation, positions and lifecycle gaps, against a real database.

Covers what W1 added: self-service and HR-path refusals, tenant checks on every
reference, organisation writes, headcount budget enforcement (slice 23) and the
letter issued inside the lifecycle cascade (slice 24 §2).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import date
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
from app.models.audit import AuditLog
from app.models.documents import IssuedLetter, LetterCategory, LetterTemplate
from app.models.employee import Employee
from app.models.lifecycle import LifecycleEventType, LifecycleStatus
from app.models.org import Department, LegalEntity
from app.models.position import HeadcountBudget, Position, PositionStatus
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.positions import BudgetUpsert, PositionCreate
from app.services.lifecycle import service as lifecycle_service
from app.services.positions import service as position_service
from tests.integration.conftest import requires_db

pytestmark = requires_db

TODAY = date.today()


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


async def _department(session: AsyncSession, tenant: Tenant, code: str) -> Department:
    dept = Department(tenant_id=tenant.id, name=f"{code} dept", code=code)
    session.add(dept)
    await session.commit()
    await session.refresh(dept)
    return dept


def _fy(on: date) -> str:
    start = on.year if on.month >= 4 else on.year - 1
    return f"FY {start}-{str(start + 1)[-2:]}"


# ═══════════════════════════════════════════════════════════════════════════
# Employees
# ═══════════════════════════════════════════════════════════════════════════


async def test_self_service_changes_contact_details_only(
    client, session, tenant, employee
) -> None:
    me = await _login(session, tenant, employee, UserRole.EMPLOYEE, "anita")
    record = await client.get("/api/v1/employees/me", headers=_auth(me))
    assert record.status_code == 200
    assert record.json()["id"] == str(employee.id)

    ok = await client.patch(
        "/api/v1/employees/me", headers=_auth(me), json={"phone": "+91 98450 00000"}
    )
    assert ok.status_code == 200
    assert ok.json()["phone"] == "+91 98450 00000"

    refused = await client.patch("/api/v1/employees/me", headers=_auth(me), json={"grade": "L9"})
    assert refused.status_code == 422
    hr_path = await client.patch(
        f"/api/v1/employees/{employee.id}", headers=_auth(me), json={"phone": "1"}
    )
    assert hr_path.status_code == 403


async def test_hr_cannot_edit_or_deactivate_their_own_record(
    client, session, tenant, employee
) -> None:
    own = await _login(session, tenant, employee, UserRole.HR_ADMIN, "selfhr")
    edit = await client.patch(
        f"/api/v1/employees/{employee.id}", headers=_auth(own), json={"grade": "L9"}
    )
    assert edit.status_code == 403
    gone = await client.delete(f"/api/v1/employees/{employee.id}", headers=_auth(own))
    assert gone.status_code == 403


async def test_references_from_another_tenant_are_refused(
    client, session, tenant, other_tenant, hr_user, employee
) -> None:
    foreign = await _department(session, other_tenant, "FOR")
    response = await client.patch(
        f"/api/v1/employees/{employee.id}",
        headers=_auth(hr_user),
        json={"department_id": str(foreign.id)},
    )
    assert response.status_code == 422

    create = await client.post(
        "/api/v1/employees",
        headers=_auth(hr_user),
        json={
            "employee_code": "NEW1",
            "first_name": "New",
            "work_email": "new1@acme-demo.com",
            "department_id": str(foreign.id),
        },
    )
    assert create.status_code == 422


async def test_a_reporting_loop_is_refused(client, session, tenant, hr_user, employee) -> None:
    report = await _person(session, tenant, "REP1", manager_id=employee.id)
    response = await client.patch(
        f"/api/v1/employees/{employee.id}",
        headers=_auth(hr_user),
        json={"manager_id": str(report.id)},
    )
    assert response.status_code == 422
    assert "loop" in response.json()["detail"]


async def test_the_directory_filters_and_searches(client, session, tenant, hr_user, employee):
    dept = await _department(session, tenant, "ENG")
    await _person(session, tenant, "ZED1", department_id=dept.id, designation="Data engineer")
    me = await _login(session, tenant, employee, UserRole.EMPLOYEE, "dir")
    by_dept = await client.get(
        f"/api/v1/employees?department_id={dept.id}", headers=_auth(me)
    )
    assert [p["employee_code"] for p in by_dept.json()["items"]] == ["ZED1"]
    by_title = await client.get("/api/v1/employees?search=data%20eng", headers=_auth(me))
    assert by_title.json()["total"] == 1
    bad_status = await client.get("/api/v1/employees?status=nonsense", headers=_auth(me))
    assert bad_status.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════
# Organisation
# ═══════════════════════════════════════════════════════════════════════════


async def test_admin_creates_and_edits_organisation_structure(
    client, session, tenant, hr_user, legal_entity
) -> None:
    location = await client.post(
        "/api/v1/org/locations",
        headers=_auth(hr_user),
        json={
            "legal_entity_id": str(legal_entity.id),
            "name": "Pune",
            "code": "PNQ",
            "state_code": "mh",
            "pincode": "411001",
        },
    )
    assert location.status_code == 201, location.text
    assert location.json()["state_code"] == "MH"

    moved = await client.patch(
        f"/api/v1/org/locations/{location.json()['id']}",
        headers=_auth(hr_user),
        json={"state_code": "KA"},
    )
    assert moved.status_code == 200
    summary = await session.scalar(
        select(AuditLog.summary).where(AuditLog.entity_type == "location").order_by(
            AuditLog.created_at.desc()
        )
    )
    assert "Professional Tax" in summary

    bad_state = await client.post(
        "/api/v1/org/locations",
        headers=_auth(hr_user),
        json={
            "legal_entity_id": str(legal_entity.id),
            "name": "X",
            "code": "X",
            "state_code": "ZZ",
        },
    )
    assert bad_state.status_code == 422

    parent = await client.post(
        "/api/v1/org/departments", headers=_auth(hr_user), json={"name": "Eng", "code": "ENG"}
    )
    child = await client.post(
        "/api/v1/org/departments",
        headers=_auth(hr_user),
        json={"name": "Platform", "code": "PLAT", "parent_id": parent.json()["id"]},
    )
    assert child.status_code == 201
    loop = await client.patch(
        f"/api/v1/org/departments/{parent.json()['id']}",
        headers=_auth(hr_user),
        json={"parent_id": child.json()["id"]},
    )
    assert loop.status_code == 422

    duplicate = await client.post(
        "/api/v1/org/legal-entities", headers=_auth(hr_user), json={"name": "Dup", "code": "ACME"}
    )
    assert duplicate.status_code == 409
    entity = await client.post(
        "/api/v1/org/legal-entities",
        headers=_auth(hr_user),
        json={"name": "Acme Labs", "code": "LABS", "pan": "abcde1234f"},
    )
    assert entity.status_code == 201
    assert entity.json()["pan"] == "ABCDE1234F"


async def test_organisation_writes_are_admin_only(
    client, session, tenant, legal_entity, other_hr_user
) -> None:
    ops = await _login(session, tenant, None, UserRole.HR_OPS, "ops")
    response = await client.post(
        "/api/v1/org/departments", headers=_auth(ops), json={"name": "X", "code": "X"}
    )
    assert response.status_code == 403
    foreign = await client.post(
        "/api/v1/org/locations",
        headers=_auth(other_hr_user),
        json={
            "legal_entity_id": str(legal_entity.id),
            "name": "X",
            "code": "X",
            "state_code": "KA",
        },
    )
    assert foreign.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════
# Headcount budget enforcement (slice 23)
# ═══════════════════════════════════════════════════════════════════════════


async def test_creating_a_position_past_the_budget_is_refused(session, tenant, hr_user) -> None:
    dept = await _department(session, tenant, "ENG")
    await position_service.upsert_budget(
        session,
        hr_user,
        BudgetUpsert(financial_year=_fy(TODAY), department_id=dept.id, approved_positions=1),
    )
    await session.commit()

    first = PositionCreate(code="ENG-1", title="Engineer", department_id=dept.id,
                           fte=Decimal("0.5"), effective_from=TODAY)
    second = PositionCreate(code="ENG-2", title="Engineer", department_id=dept.id,
                            fte=Decimal("0.5"), effective_from=TODAY)
    third = PositionCreate(code="ENG-3", title="Engineer", department_id=dept.id,
                           fte=Decimal("0.5"), effective_from=TODAY)
    await position_service.create_position(session, hr_user, first)
    await position_service.create_position(session, hr_user, second)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await position_service.create_position(session, hr_user, third)
    assert exc.value.status_code == 409
    assert "headcount budget" in exc.value.detail
    assert "ENG-3" in exc.value.detail


async def test_a_company_cost_budget_is_enforced(session, tenant, hr_user) -> None:
    await position_service.upsert_budget(
        session,
        hr_user,
        BudgetUpsert(
            financial_year=_fy(TODAY), approved_positions=10, approved_cost=Decimal("1000000")
        ),
    )
    await session.commit()
    await position_service.create_position(
        session, hr_user,
        PositionCreate(code="A", title="A", budgeted_ctc_max=Decimal("800000"),
                       effective_from=TODAY),
    )
    with pytest.raises(HTTPException) as exc:
        await position_service.create_position(
            session, hr_user,
            PositionCreate(code="B", title="B", budgeted_ctc_max=Decimal("300000"),
                           effective_from=TODAY),
        )
    assert "cost" in exc.value.detail


async def test_filling_a_seat_after_a_budget_cut_is_refused(
    session, tenant, hr_user, employee
) -> None:
    dept = await _department(session, tenant, "OPS")
    for code in ("OPS-1", "OPS-2"):
        await position_service.create_position(
            session, hr_user,
            PositionCreate(code=code, title=code, department_id=dept.id, effective_from=TODAY),
        )
    await session.commit()
    seats = (await session.scalars(select(Position).order_by(Position.code))).all()
    other = await _person(session, tenant, "OPSP")
    await position_service.assign(
        session, hr_user, position_id=seats[0].id, employee_id=other.id, from_date=TODAY
    )
    result = await position_service.upsert_budget(
        session, hr_user,
        BudgetUpsert(financial_year=_fy(TODAY), department_id=dept.id, approved_positions=1),
    )
    await session.commit()
    assert result.over_budget, "a cut below what exists is reported"

    with pytest.raises(HTTPException) as exc:
        await position_service.assign(
            session, hr_user, position_id=seats[1].id, employee_id=employee.id, from_date=TODAY
        )
    assert exc.value.status_code == 409


async def test_a_scenario_that_breaks_the_budget_writes_nothing(
    client, session, tenant, hr_user
) -> None:
    dept = await _department(session, tenant, "SAL")
    session.add(
        HeadcountBudget(tenant_id=tenant.id, financial_year=_fy(TODAY), department_id=dept.id,
                        approved_positions=1, approved_cost=Decimal(0))
    )
    await session.commit()
    created = await client.post(
        "/api/v1/positions/scenarios",
        headers=_auth(hr_user),
        json={
            "name": "Growth",
            "as_of": TODAY.isoformat(),
            "changes": [
                {"kind": "create", "values": {"code": f"S{i}", "title": "Rep",
                 "department_id": str(dept.id), "budgeted_ctc_max": "900000"}}
                for i in range(2)
            ],
        },
    )
    assert created.status_code == 201, created.text
    scenario_id = created.json()["scenario_id"]
    commit = await client.post(
        f"/api/v1/positions/scenarios/{scenario_id}/commit", headers=_auth(hr_user)
    )
    assert commit.status_code == 409
    assert await session.scalar(select(Position.id)) is None


async def test_assigning_an_employee_from_another_tenant_is_refused(
    session, tenant, other_tenant, hr_user
) -> None:
    await position_service.create_position(
        session, hr_user, PositionCreate(code="P1", title="P1", effective_from=TODAY)
    )
    await session.commit()
    seat = await session.scalar(select(Position))
    stranger = await _person(session, other_tenant, "STR1")
    with pytest.raises(HTTPException) as exc:
        await position_service.assign(
            session, hr_user, position_id=seat.id, employee_id=stranger.id, from_date=TODAY
        )
    assert exc.value.status_code == 422


async def test_freeze_and_abolish_are_audited_status_changes(
    client, session, tenant, hr_user
) -> None:
    await position_service.create_position(
        session, hr_user, PositionCreate(code="F1", title="F1", effective_from=TODAY)
    )
    await session.commit()
    seat = await session.scalar(select(Position))
    frozen = await client.post(
        f"/api/v1/positions/{seat.id}/status",
        headers=_auth(hr_user),
        json={"action": "freeze", "reason": "Hiring freeze"},
    )
    assert frozen.json()["status"] == "frozen"
    abolished = await client.post(
        f"/api/v1/positions/{seat.id}/status",
        headers=_auth(hr_user),
        json={"action": "abolish", "reason": "Reorg"},
    )
    assert abolished.json()["status"] == "abolished"
    await session.refresh(seat)
    assert seat.status is PositionStatus.ABOLISHED


# ═══════════════════════════════════════════════════════════════════════════
# Lifecycle: letter on cascade (slice 24 §2) and self-refusal
# ═══════════════════════════════════════════════════════════════════════════


async def _confirmation_template(session: AsyncSession, tenant: Tenant, body: str) -> None:
    session.add(
        LetterTemplate(
            tenant_id=tenant.id, code="CONFIRM", name="Confirmation",
            category=LetterCategory.CONFIRMATION, body_html=body, version=1, active=True,
        )
    )
    await session.commit()


async def _approved_confirmation(session, hr_user, employee) -> uuid_type:
    event = await lifecycle_service.raise_event(
        session, hr_user, employee_id=employee.id,
        event_type=LifecycleEventType.CONFIRMATION, effective_from=TODAY,
        new_values={"probation_end_date": TODAY.isoformat()}, reason="Probation passed",
    )
    await lifecycle_service.approve_event(session, hr_user, event_id=event.id)
    await session.commit()
    return event.id


uuid_type = object


async def test_effecting_a_confirmation_issues_its_letter(
    session, tenant, hr_user, employee
) -> None:
    await _confirmation_template(
        session, tenant,
        "<p>Dear {{employee.first_name}}, confirmed from {{manual.effective_date}}.</p>",
    )
    event_id = await _approved_confirmation(session, hr_user, employee)
    result = await lifecycle_service.effect_event(session, hr_user, event_id=event_id)
    await session.commit()

    assert result["letter"]["reference_number"]
    letter = await session.scalar(select(IssuedLetter))
    assert letter.category is LetterCategory.CONFIRMATION
    assert "Anita" in letter.merged_html
    event = await lifecycle_service._event(session, hr_user, event_id)
    assert event.letter_id == letter.id


async def test_a_letter_that_cannot_merge_rolls_back_the_whole_event(
    session, tenant, hr_user, employee
) -> None:
    await _confirmation_template(session, tenant, "<p>{{salary.ctc_annual}}</p>")
    event_id = await _approved_confirmation(session, hr_user, employee)
    before = employee.probation_end_date

    with pytest.raises(HTTPException):
        await lifecycle_service.effect_event(session, hr_user, event_id=event_id)
    await session.commit()

    event = await lifecycle_service._event(session, hr_user, event_id)
    assert event.status is LifecycleStatus.FAILED
    assert "salary" in event.failure_reason
    await session.refresh(employee)
    assert employee.probation_end_date == before
    assert await session.scalar(select(IssuedLetter.id)) is None


async def test_no_template_means_effected_with_a_plain_note(
    session, tenant, hr_user, employee
) -> None:
    event_id = await _approved_confirmation(session, hr_user, employee)
    result = await lifecycle_service.effect_event(session, hr_user, event_id=event_id)
    assert result["status"] == "effected"
    assert result["letter"]["letter_id"] is None
    assert "No letter was issued" in result["letter"]["note"]


async def test_nobody_raises_an_event_about_themselves(session, tenant, employee) -> None:
    own = await _login(session, tenant, employee, UserRole.HR_ADMIN, "selfevent")
    with pytest.raises(HTTPException) as exc:
        await lifecycle_service.raise_event(
            session, own, employee_id=employee.id,
            event_type=LifecycleEventType.GRADE_CHANGE, effective_from=TODAY,
            new_values={"grade": "L9"}, reason="Self promotion",
        )
    assert exc.value.status_code == 403


async def test_a_manager_sees_only_their_reporting_lines_history(
    client, session, tenant, hr_user, employee
) -> None:
    boss = await _person(session, tenant, "BOSS")
    report = await _person(session, tenant, "REPT", manager_id=boss.id)
    manager = await _login(session, tenant, boss, UserRole.MANAGER, "boss")
    ok = await client.get(
        f"/api/v1/lifecycle/employees/{report.id}/history", headers=_auth(manager)
    )
    assert ok.status_code == 200
    refused = await client.get(
        f"/api/v1/lifecycle/employees/{employee.id}/history", headers=_auth(manager)
    )
    assert refused.status_code == 403


async def test_the_probation_policy_reads_as_unconfigured_rather_than_defaulted(
    client, hr_user
) -> None:
    response = await client.get("/api/v1/lifecycle/probation/policy", headers=_auth(hr_user))
    assert response.status_code == 200
    assert response.json()["configured"] is False
    assert response.json()["silence_policy"] is None


async def test_legal_entity_fixture_is_isolated(session, legal_entity: LegalEntity) -> None:
    assert await session.get(LegalEntity, legal_entity.id) is not None
