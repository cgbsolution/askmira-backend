"""The platform console: only the super admin makes workspaces, and nobody signs up."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.attendance import Shift
from app.models.audit import AuditLog
from app.models.employee import Employee
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.auth import LoginRequest
from app.services import auth as auth_service
from app.services import platform
from tests.integration.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
async def client(session: AsyncSession) -> AsyncGenerator[httpx.AsyncClient, None]:
    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def super_admin(session: AsyncSession, tenant: Tenant) -> User:
    user = User(
        tenant_id=tenant.id,
        email="platform@acme.test",
        password_hash="not-a-real-hash",
        full_name="Platform",
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    return user


GSTIN_KA = "29AAACN1234F1ZT"
GSTIN_MH = "27AAACN1234F1ZX"


def _fields(email: str = "founder@newco-demo.com", **extra) -> dict:
    fields = {
        "company_name": "NewCo",
        "legal_name": "NewCo Technologies Private Limited",
        "company_type": "private_limited",
        "industry": "Software",
        "company_size": "51-200",
        "website": "newco-demo.com",
        "pan": "aaacn1234f",
        "tan": "BLRN12345C",
        "gstin": GSTIN_KA,
        "cin": "U72200KA2019PTC123456",
        "pf_registration_no": "KNBNG0012345000",
        "esi_registration_no": "53000123450001001",
        "address_line1": "12, 4th Cross, Indiranagar",
        "city": "Bengaluru",
        "state_code": "ka",
        "pincode": "560038",
        "admin_full_name": "Asha Rao",
        "admin_email": email,
        "admin_phone": "+91 98450 12345",
        "admin_designation": "HR Head",
    }
    fields.update(extra)
    return fields


def _new(email: str = "founder@newco-demo.com", **extra) -> platform.WorkspaceIn:
    return platform.WorkspaceIn(**_fields(email, **extra))


def _bearer(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
    )
    return {"Authorization": f"Bearer {token}"}


async def test_there_is_no_public_sign_up(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": "Anybody Ltd",
            "full_name": "Stranger",
            "email": "stranger@anybody.test",
            "password": "Str0ngPassword!",
        },
    )
    assert response.status_code in (404, 405)


async def test_only_the_super_admin_creates_a_workspace(
    session: AsyncSession, hr_user: User, super_admin: User, client: httpx.AsyncClient
) -> None:
    with pytest.raises(HTTPException) as refused:
        await platform.create(session, hr_user, _new(), request=None)
    assert refused.value.status_code == 403
    over_http = await client.get("/api/v1/platform/workspaces", headers=_bearer(hr_user))
    assert over_http.status_code == 403

    created = await platform.create(session, super_admin, _new(), request=None)
    temporary = created["admin"]["temporary_password"]

    tenant = await session.get(Tenant, uuid.UUID(created["workspace"]["id"]))
    admin = await session.scalar(select(User).where(User.tenant_id == tenant.id))
    assert admin.role is UserRole.ADMIN
    assert admin.must_change_password is True
    assert await session.scalar(select(Employee.id).where(Employee.tenant_id == tenant.id))
    assert await session.scalar(select(Shift.id).where(Shift.tenant_id == tenant.id))
    audit_row = await session.scalar(
        select(AuditLog).where(AuditLog.tenant_id == tenant.id, AuditLog.entity_type == "tenant")
    )
    assert audit_row.actor_type == "platform"

    # The new admin signs in with the temporary password, into their own workspace.
    signed_in = await auth_service.login(
        session, LoginRequest(email="founder@newco-demo.com", password=temporary)
    )
    assert str(signed_in.tenant.id) == created["workspace"]["id"]
    assert signed_in.user.must_change_password is True

    listed = await platform.list_workspaces(session, super_admin)
    names = {row["name"]: row for row in listed}
    assert names["NewCo"]["admins"][0]["email"] == "founder@newco-demo.com"
    assert names["NewCo"]["employees"] == 1
    assert names["NewCo"]["gstin"] == GSTIN_KA
    assert names["NewCo"]["city"] == "Bengaluru"


async def test_one_email_is_one_login_across_every_workspace(
    session: AsyncSession, tenant: Tenant, super_admin: User
) -> None:
    existing = User(
        tenant_id=tenant.id,
        email="priya@acme-demo.com",
        password_hash="not-a-real-hash",
        full_name="Priya",
        role=UserRole.ADMIN,
        is_active=True,
    )
    session.add(existing)
    await session.commit()
    # priya@acme-demo.com already signs in to the first workspace.
    with pytest.raises(HTTPException) as taken:
        await platform.create(session, super_admin, _new(email="PRIYA@acme-demo.com"), request=None)
    assert taken.value.status_code == 409

    created = await platform.create(session, super_admin, _new(), request=None)
    with pytest.raises(HTTPException) as again:
        await platform.add_admin(
            session,
            super_admin,
            uuid.UUID(created["workspace"]["id"]),
            platform.AdminIn(full_name="Dup", email="priya@acme-demo.com"),
            request=None,
        )
    assert again.value.status_code == 409


async def test_a_workspace_address_is_checked(session: AsyncSession, super_admin: User) -> None:
    with pytest.raises(HTTPException) as bad:
        await platform.create(session, super_admin, _new(workspace="Not A Slug!"), request=None)
    assert bad.value.status_code == 422
    await platform.create(session, super_admin, _new(workspace="newco"), request=None)
    with pytest.raises(HTTPException) as clash:
        await platform.create(
            session,
            super_admin,
            _new(email="other@newco-demo.com", workspace="newco"),
            request=None,
        )
    assert clash.value.status_code == 409


async def test_suspending_a_workspace_stops_it_on_the_next_request(
    session: AsyncSession, super_admin: User, client: httpx.AsyncClient
) -> None:
    created = await platform.create(session, super_admin, _new(), request=None)
    tenant_id = uuid.UUID(created["workspace"]["id"])
    admin = await session.scalar(select(User).where(User.tenant_id == tenant_id))

    before = await client.get("/api/v1/auth/me", headers=_bearer(admin))
    assert before.status_code == 200

    await platform.set_active(
        session, super_admin, tenant_id, active=False, reason="Contract ended", request=None
    )
    after = await client.get("/api/v1/auth/me", headers=_bearer(admin))
    assert after.status_code == 403
    assert "suspended" in after.json()["detail"]

    await platform.set_active(
        session, super_admin, tenant_id, active=True, reason="Renewed", request=None
    )
    back = await client.get("/api/v1/auth/me", headers=_bearer(admin))
    assert back.status_code == 200

    with pytest.raises(HTTPException) as own:
        await platform.set_active(
            session, super_admin, super_admin.tenant_id, active=False, reason="x" * 5, request=None
        )
    assert own.value.status_code == 422


async def test_a_locked_out_admin_gets_a_temporary_password(
    session: AsyncSession, super_admin: User
) -> None:
    created = await platform.create(session, super_admin, _new(), request=None)
    tenant_id = uuid.UUID(created["workspace"]["id"])
    admin = await session.scalar(select(User).where(User.tenant_id == tenant_id))
    reset = await platform.reset_admin_password(
        session, super_admin, tenant_id, admin.id, request=None
    )
    await session.refresh(admin)
    assert admin.must_change_password is True
    assert admin.tokens_valid_from is not None
    signed_in = await auth_service.login(
        session,
        LoginRequest(email=admin.email, password=reset["admin"]["temporary_password"]),
    )
    assert signed_in.user.email == admin.email


async def test_a_workspace_admin_cannot_reuse_an_email_from_another_workspace(
    session: AsyncSession, tenant: Tenant, other_hr_user: User
) -> None:
    from app.schemas.platform_admin import ManagedUserCreate
    from app.services.identity import users as user_admin

    session.add(
        User(
            tenant_id=tenant.id,
            email="ravi@acme-demo.com",
            password_hash="not-a-real-hash",
            full_name="Ravi",
            role=UserRole.EMPLOYEE,
            is_active=True,
        )
    )
    await session.commit()
    with pytest.raises(HTTPException) as refused:
        await user_admin.create_user(
            session,
            other_hr_user,
            ManagedUserCreate(email="ravi@acme-demo.com", full_name="Ravi again"),
        )
    assert refused.value.status_code == 400
    assert "one login" in refused.value.detail


async def test_the_company_s_legal_identity_and_office_are_set_up_with_it(
    session: AsyncSession, super_admin: User
) -> None:
    from app.models.org import LegalEntity, Location

    created = await platform.create(
        session, super_admin, _new(modules={"benefits": True}), request=None
    )
    tenant_id = uuid.UUID(created["workspace"]["id"])
    entity = await session.scalar(select(LegalEntity).where(LegalEntity.tenant_id == tenant_id))
    assert entity.name == "NewCo Technologies Private Limited"
    assert (entity.pan, entity.tan, entity.gstin) == ("AAACN1234F", "BLRN12345C", GSTIN_KA)
    assert entity.registered_address.endswith("Bengaluru, KA 560038")
    office = await session.scalar(select(Location).where(Location.tenant_id == tenant_id))
    assert (office.state_code, office.pincode) == ("KA", "560038")
    tenant = await session.get(Tenant, tenant_id)
    assert tenant.enabled_modules["benefits"] is True
    assert tenant.settings["profile"]["cin"] == "U72200KA2019PTC123456"
    admin = await session.scalar(select(User).where(User.tenant_id == tenant_id))
    assert admin.phone == "9845012345"
    employee = await session.get(Employee, admin.employee_id)
    assert (employee.legal_entity_id, employee.location_id) == (entity.id, office.id)
    assert employee.designation == "HR Head"


def test_a_gstin_must_belong_to_the_company_and_its_state() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="PAN"):
        platform.WorkspaceIn(**_fields(pan="AAACZ9999Z"))
    with pytest.raises(ValidationError, match="registered in"):
        platform.WorkspaceIn(**_fields(gstin=GSTIN_MH))
    with pytest.raises(ValidationError):
        platform.WorkspaceIn(**_fields(gstin="29AAACN1234F1ZA"))
    with pytest.raises(ValidationError, match="PIN"):
        platform.WorkspaceIn(**_fields(pincode="060038"))
    with pytest.raises(ValidationError, match="mobile"):
        platform.WorkspaceIn(**_fields(admin_phone="12345"))
    with pytest.raises(ValidationError, match="module"):
        platform.WorkspaceIn(**_fields(modules={"teleportation": True}))
    # No GSTIN at all is allowed — not every company is registered.
    assert platform.WorkspaceIn(**_fields(gstin=None)).gstin is None
