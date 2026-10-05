"""Slice 04 gates that need a real database.

The one that matters most is SCIM de-provisioning: revoke access, keep the
employment record. That is the question every security reviewer asks, and it
cannot be asserted without actually writing payroll history and then
de-provisioning against it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import AttendanceDay, AttendancePunch, AttendanceStatus
from app.models.audit import AuditLog
from app.models.employee import Employee
from app.models.identity import (
    DeviceIntegration,
    DevicePunchException,
    DeviceStatus,
    DeviceVendor,
    IdentityProvider,
    IdpType,
    ScimToken,
)
from app.models.payroll import (
    CalculationType,
    ComponentType,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
)
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.payroll import EmployeeSalaryCreate
from app.services.identity import devices as device_service
from app.services.identity import scim as scim_service
from app.services.identity import sso as sso_service
from app.services.payroll import service as salary_service
from tests.integration.conftest import requires_db

pytestmark = requires_db


# ═══════════════════════════════════════════════════════════════════════════
# SCIM de-provisioning — the headline gate
# ═══════════════════════════════════════════════════════════════════════════


async def test_scim_deactivate_revokes_sessions_but_preserves_payroll_history(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate.

    Build real payroll and attendance history, de-provision through SCIM, then
    assert the access is gone and the records are not.
    """
    # ── Real history to protect ─────────────────────────────────────────────
    basic = SalaryComponent(
        tenant_id=tenant.id, code="BASIC", name="Basic",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.PERCENTAGE_OF,
        base_component_code="CTC", value=Decimal(40), counts_toward_pf=True,
        display_order=1,
    )
    special = SalaryComponent(
        tenant_id=tenant.id, code="SPECIAL", name="Special allowance",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.BALANCE, display_order=9,
    )
    session.add_all([basic, special])
    await session.flush()

    structure = SalaryStructure(
        tenant_id=tenant.id, name="Standard", effective_from=date(2026, 1, 1),
        applicable_grades=[], applicable_locations=[],
    )
    for index, component in enumerate((basic, special)):
        structure.components.append(
            SalaryStructureComponent(
                tenant_id=tenant.id, component_id=component.id,
                component=component, display_order=index,
            )
        )
    session.add(structure)
    await session.commit()

    await salary_service.assign_salary(
        session, hr_user, employee.id,
        EmployeeSalaryCreate(
            structure_id=structure.id, ctc_annual=Decimal("1200000"),
            effective_from=date(2026, 4, 1),
        ),
    )

    session.add(
        AttendanceDay(
            tenant_id=tenant.id, employee_id=employee.id,
            attendance_date=date(2026, 4, 6), status=AttendanceStatus.PRESENT,
            payable_fraction=Decimal(1), computed_at=datetime.now(UTC),
        )
    )

    # The user being de-provisioned, linked to that employee.
    leaver = User(
        tenant_id=tenant.id, email="leaver@acme.test", password_hash="!scim",
        full_name="Anita Sharma", role=UserRole.EMPLOYEE, is_active=True,
        employee_id=employee.id,
    )
    session.add(leaver)
    await session.commit()

    salary_before = len(await salary_service.salary_history(session, hr_user, employee.id))
    attendance_before = await session.scalar(
        select(func.count(AttendanceDay.id)).where(AttendanceDay.employee_id == employee.id)
    )
    assert salary_before == 1
    assert attendance_before == 1

    # ── De-provision ────────────────────────────────────────────────────────
    before = datetime.now(UTC)
    updated = await scim_service.deactivate_user(session, tenant.id, leaver.id)

    # Access is gone.
    assert updated.is_active is False
    assert updated.tokens_valid_from is not None
    assert updated.tokens_valid_from >= before, "every issued token must be invalidated"

    # History is not.
    assert (
        await session.scalar(
            select(func.count(Employee.id)).where(Employee.id == employee.id)
        )
        == 1
    ), "the employee record must survive a directory de-provision"
    assert (
        len(await salary_service.salary_history(session, hr_user, employee.id))
        == salary_before
    ), "payroll history must survive"
    assert (
        await session.scalar(
            select(func.count(AttendanceDay.id)).where(
                AttendanceDay.employee_id == employee.id
            )
        )
        == attendance_before
    ), "attendance history must survive"

    entry = await session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "user", AuditLog.entity_id == leaver.id
        )
    )
    assert entry is not None
    assert entry.changes["employee_record"] == "retained"
    assert entry.changes["payroll_history"] == "retained"


async def test_a_scim_patch_setting_active_false_also_revokes(
    session: AsyncSession, tenant: Tenant
) -> None:
    """Azure AD de-provisions with PATCH, not DELETE.

    A service that only implements DELETE silently never de-provisions anyone.
    """
    user = User(
        tenant_id=tenant.id, email="patchme@acme.test", password_hash="!scim",
        full_name="Patch Me", role=UserRole.EMPLOYEE, is_active=True,
    )
    session.add(user)
    await session.commit()

    updated = await scim_service.apply_patch(
        session,
        tenant.id,
        user.id,
        {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "active", "value": False}],
        },
    )

    assert updated.is_active is False
    assert updated.tokens_valid_from is not None


async def test_a_pathless_patch_carrying_a_partial_resource_works(
    session: AsyncSession, tenant: Tenant
) -> None:
    """Okta sends `{"op": "replace", "value": {"active": false}}` with no path."""
    user = User(
        tenant_id=tenant.id, email="okta@acme.test", password_hash="!scim",
        full_name="Okta User", role=UserRole.EMPLOYEE, is_active=True,
    )
    session.add(user)
    await session.commit()

    updated = await scim_service.apply_patch(
        session, tenant.id, user.id,
        {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "value": {"active": False}}],
        },
    )
    assert updated.is_active is False


async def test_a_scim_payload_claiming_admin_does_not_grant_it(
    session: AsyncSession, tenant: Tenant
) -> None:
    """A corporate directory does not decide who administers AskMira."""
    user = await scim_service.provision_user(
        session,
        tenant.id,
        {
            "schemas": [scim_service.SCIM_USER_SCHEMA],
            "userName": "wannabe@acme.test",
            "displayName": "Wannabe Admin",
            "roles": [{"value": "admin", "primary": True}],
            "groups": [{"value": "Administrators"}],
        },
    )
    assert user.role is UserRole.EMPLOYEE


async def test_a_scim_patch_cannot_change_a_role(
    session: AsyncSession, tenant: Tenant
) -> None:
    user = User(
        tenant_id=tenant.id, email="escalate@acme.test", password_hash="!scim",
        full_name="Escalate Me", role=UserRole.EMPLOYEE, is_active=True,
    )
    session.add(user)
    await session.commit()

    updated = await scim_service.apply_patch(
        session, tenant.id, user.id,
        {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {"op": "replace", "path": "roles", "value": [{"value": "admin"}]},
                {"op": "add", "path": "groups", "value": [{"value": "Administrators"}]},
            ],
        },
    )
    assert updated.role is UserRole.EMPLOYEE


async def test_scim_token_auth_resolves_the_tenant(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    record, plaintext = await scim_service.create_scim_token(session, hr_user, "Azure AD")

    assert plaintext not in (record.token_hash, record.token_hint)
    assert record.token_hash != plaintext, "only the hash is stored"

    resolved = await scim_service.authenticate_token(session, f"Bearer {plaintext}")
    assert resolved.tenant_id == tenant.id

    with pytest.raises(scim_service.ScimError):
        await scim_service.authenticate_token(session, "Bearer not-a-real-token")
    with pytest.raises(scim_service.ScimError):
        await scim_service.authenticate_token(session, None)


async def test_scim_list_is_paginated_and_one_indexed(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    for index in range(5):
        session.add(
            User(
                tenant_id=tenant.id, email=f"u{index}@acme.test", password_hash="!x",
                full_name=f"User {index}", role=UserRole.EMPLOYEE,
            )
        )
    await session.commit()

    page = await scim_service.list_users(session, tenant.id, start_index=1, count=2)
    assert page["startIndex"] == 1
    assert page["itemsPerPage"] == 2
    assert page["totalResults"] >= 6  # 5 plus the hr_user fixture


async def test_scim_supports_the_username_filter_directories_actually_send(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    page = await scim_service.list_users(
        session, tenant.id, filter_expression='userName eq "hr@acme.test"'
    )
    assert page["totalResults"] == 1
    assert page["Resources"][0]["userName"] == "hr@acme.test"


async def test_an_unsupported_scim_filter_returns_a_scim_shaped_error(
    session: AsyncSession, tenant: Tenant
) -> None:
    with pytest.raises(scim_service.ScimError) as exc:
        await scim_service.list_users(
            session, tenant.id, filter_expression='title co "Manager"'
        )
    assert exc.value.scim_type == "invalidFilter"


async def test_scim_cannot_reach_another_tenants_users(
    session: AsyncSession, tenant: Tenant, other_tenant: Tenant, hr_user: User
) -> None:
    with pytest.raises(scim_service.ScimError) as exc:
        await scim_service.deactivate_user(session, other_tenant.id, hr_user.id)
    assert exc.value.status == 404


# ═══════════════════════════════════════════════════════════════════════════
# SSO policy
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_replayed_assertion_id_is_refused(
    session: AsyncSession, tenant: Tenant
) -> None:
    """The checklist gate. Valid assertion, used twice."""
    expiry = datetime.now(UTC) + timedelta(minutes=5)

    await sso_service.consume_assertion_id(
        session, tenant_id=tenant.id, assertion_id="_assertion-1", not_on_or_after=expiry
    )
    await session.commit()

    with pytest.raises(sso_service.SsoError, match="already been used"):
        await sso_service.consume_assertion_id(
            session, tenant_id=tenant.id, assertion_id="_assertion-1",
            not_on_or_after=expiry,
        )


async def test_expired_assertion_ids_are_purged(
    session: AsyncSession, tenant: Tenant
) -> None:
    await sso_service.consume_assertion_id(
        session, tenant_id=tenant.id, assertion_id="_old",
        not_on_or_after=datetime.now(UTC) - timedelta(hours=1),
    )
    await sso_service.consume_assertion_id(
        session, tenant_id=tenant.id, assertion_id="_current",
        not_on_or_after=datetime.now(UTC) + timedelta(hours=1),
    )
    await session.commit()

    purged = await sso_service.purge_expired_assertions(session)
    assert purged == 1

    # The still-valid one must remain protected.
    with pytest.raises(sso_service.SsoError):
        await sso_service.consume_assertion_id(
            session, tenant_id=tenant.id, assertion_id="_current",
            not_on_or_after=datetime.now(UTC) + timedelta(hours=1),
        )


async def test_enforce_sso_blocks_password_login_but_permits_break_glass(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate: break-glass works and audits at high severity."""
    session.add(
        IdentityProvider(
            tenant_id=tenant.id, name="Entra ID", idp_type=IdpType.SAML,
            enabled=True, enforce_sso=True, allowed_email_domains=["acme.test"],
        )
    )
    ordinary = User(
        tenant_id=tenant.id, email="someone@acme.test", password_hash="!x",
        full_name="Someone", role=UserRole.EMPLOYEE, is_active=True,
    )
    session.add(ordinary)
    tenant.settings = {**(tenant.settings or {}), "break_glass_email": hr_user.email}
    await session.commit()

    # An ordinary user is refused.
    with pytest.raises(sso_service.SsoError, match="requires single sign-on"):
        await sso_service.assert_password_login_allowed(session, ordinary)

    # The break-glass account is not.
    await sso_service.assert_password_login_allowed(session, hr_user)
    await session.commit()

    entry = await session.scalar(
        select(AuditLog).where(
            AuditLog.entity_id == hr_user.id, AuditLog.summary.like("BREAK-GLASS%")
        )
    )
    assert entry is not None
    assert entry.changes["severity"] == "high"
    assert entry.changes["reason"] == "break_glass"


async def test_password_login_is_unaffected_when_sso_is_not_enforced(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    session.add(
        IdentityProvider(
            tenant_id=tenant.id, name="Optional SSO", idp_type=IdpType.OIDC,
            enabled=True, enforce_sso=False, allowed_email_domains=["acme.test"],
        )
    )
    await session.commit()

    await sso_service.assert_password_login_allowed(session, hr_user)


async def test_email_domain_routes_to_the_right_provider(
    session: AsyncSession, tenant: Tenant, other_tenant: Tenant
) -> None:
    session.add_all(
        [
            IdentityProvider(
                tenant_id=tenant.id, name="Acme Entra", idp_type=IdpType.SAML,
                enabled=True, allowed_email_domains=["acme.test"],
            ),
            IdentityProvider(
                tenant_id=other_tenant.id, name="Globex Okta", idp_type=IdpType.OIDC,
                enabled=True, allowed_email_domains=["globex.test"],
            ),
        ]
    )
    await session.commit()

    acme = await sso_service.provider_for_email(session, "anyone@acme.test")
    globex = await sso_service.provider_for_email(session, "anyone@globex.test")

    assert acme is not None and acme[0].name == "Acme Entra"
    assert globex is not None and globex[0].name == "Globex Okta"
    assert await sso_service.provider_for_email(session, "nobody@unknown.test") is None


async def test_jit_provisioning_creates_a_user_but_never_an_employee(
    session: AsyncSession, tenant: Tenant
) -> None:
    """A user with no employee record is a valid state for a contractor."""
    provider = IdentityProvider(
        tenant_id=tenant.id, name="Entra", idp_type=IdpType.SAML, enabled=True,
        allowed_email_domains=["acme.test"], jit_provisioning=True,
    )
    session.add(provider)
    await session.commit()

    employees_before = await session.scalar(select(func.count(Employee.id)))

    user = await sso_service.provision_user(
        session, tenant_id=tenant.id, provider=provider,
        email="contractor@acme.test", first_name="Ravi", last_name="Kumar",
        external_employee_code=None,
    )
    await session.commit()

    assert user.role is UserRole.EMPLOYEE
    assert user.employee_id is None
    assert await session.scalar(select(func.count(Employee.id))) == employees_before


async def test_a_disallowed_email_domain_is_refused(
    session: AsyncSession, tenant: Tenant
) -> None:
    provider = IdentityProvider(
        tenant_id=tenant.id, name="Entra", idp_type=IdpType.SAML, enabled=True,
        allowed_email_domains=["acme.test"],
    )
    session.add(provider)
    await session.commit()

    with pytest.raises(sso_service.SsoError, match="not permitted"):
        await sso_service.provision_user(
            session, tenant_id=tenant.id, provider=provider,
            email="attacker@evil.test", first_name="X", last_name="Y",
            external_employee_code=None,
        )


async def test_sso_login_does_not_change_an_existing_users_role(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """A subsequent SSO login must leave the assigned role exactly as it is."""
    provider = IdentityProvider(
        tenant_id=tenant.id, name="Entra", idp_type=IdpType.SAML, enabled=True,
        allowed_email_domains=["acme.test"],
    )
    session.add(provider)
    await session.commit()

    returned = await sso_service.provision_user(
        session, tenant_id=tenant.id, provider=provider, email=hr_user.email,
        first_name="Priya", last_name="Ramesh", external_employee_code=None,
    )
    await session.commit()

    assert returned.id == hr_user.id
    assert returned.role is UserRole.ADMIN


async def test_an_unmatched_employee_code_leaves_the_user_unlinked(
    session: AsyncSession, tenant: Tenant
) -> None:
    """A near-miss is not a match. Guessing attaches the wrong payslips."""
    provider = IdentityProvider(
        tenant_id=tenant.id, name="Entra", idp_type=IdpType.SAML, enabled=True,
        allowed_email_domains=["acme.test"], employee_id_attribute="employeeId",
    )
    session.add(provider)
    await session.commit()

    user = await sso_service.provision_user(
        session, tenant_id=tenant.id, provider=provider, email="ghost@acme.test",
        first_name="Ghost", last_name="User", external_employee_code="NOPE999",
    )
    await session.commit()

    assert user.employee_id is None

    entry = await session.scalar(
        select(AuditLog).where(AuditLog.summary.like("%could not be linked%"))
    )
    assert entry is not None
    assert entry.changes["unmatched_employee_code"] == "NOPE999"


async def test_a_matching_employee_code_links_the_user(
    session: AsyncSession, tenant: Tenant, employee: Employee
) -> None:
    provider = IdentityProvider(
        tenant_id=tenant.id, name="Entra", idp_type=IdpType.SAML, enabled=True,
        allowed_email_domains=["acme.test"], employee_id_attribute="employeeId",
    )
    session.add(provider)
    await session.commit()

    user = await sso_service.provision_user(
        session, tenant_id=tenant.id, provider=provider, email="anita@acme.test",
        first_name="Anita", last_name="Sharma",
        external_employee_code=employee.employee_code,
    )
    await session.commit()

    assert user.employee_id == employee.id


# ═══════════════════════════════════════════════════════════════════════════
# Devices
# ═══════════════════════════════════════════════════════════════════════════


async def _device(session: AsyncSession, tenant: Tenant, hr_user: User):
    return await device_service.create_device(
        session, hr_user, device_id="DEV-BLR-01", name="Bengaluru main gate",
        vendor=DeviceVendor.ESSL,
    )


async def test_duplicate_punches_deduplicate_to_one(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate: devices re-send routinely."""
    device, _ = await _device(session, tenant, hr_user)

    batch = [
        {"UserID": employee.employee_code, "LogTime": "2026-04-06 09:00:00", "Status": "0"},
        {"UserID": employee.employee_code, "LogTime": "2026-04-06 18:00:00", "Status": "1"},
    ]

    first = await device_service.ingest_punches(session, device, batch)
    assert first.accepted == 2
    assert first.duplicates == 0

    # The operator presses sync again.
    second = await device_service.ingest_punches(session, device, batch)
    assert second.accepted == 0
    assert second.duplicates == 2

    stored = await session.scalar(
        select(func.count(AttendancePunch.id)).where(
            AttendancePunch.employee_id == employee.id
        )
    )
    assert stored == 2


async def test_duplicates_within_a_single_batch_are_collapsed(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    device, _ = await _device(session, tenant, hr_user)
    row = {"UserID": employee.employee_code, "LogTime": "2026-04-06 09:00:00", "Status": "0"}

    result = await device_service.ingest_punches(session, device, [row, row, row])

    assert result.accepted == 1
    assert result.duplicates == 2


async def test_an_unknown_employee_code_does_not_fail_the_batch(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate.

    One unmapped person must never take out an entire site's attendance.
    """
    device, _ = await _device(session, tenant, hr_user)

    result = await device_service.ingest_punches(
        session,
        device,
        [
            {"UserID": employee.employee_code, "LogTime": "2026-04-06 09:00:00", "Status": "0"},
            {"UserID": "GHOST-999", "LogTime": "2026-04-06 09:01:00", "Status": "0"},
            {"UserID": employee.employee_code, "LogTime": "2026-04-06 18:00:00", "Status": "1"},
        ],
    )

    assert result.accepted == 2, "the known employee's punches still landed"
    assert result.exceptions == 1
    assert result.unmapped_codes == ["GHOST-999"]

    orphan = await session.scalar(
        select(DevicePunchException).where(DevicePunchException.employee_code == "GHOST-999")
    )
    assert orphan is not None
    assert "GHOST-999" in orphan.reason


async def test_an_unreadable_payload_becomes_an_exception_not_a_failure(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    device, _ = await _device(session, tenant, hr_user)

    result = await device_service.ingest_punches(
        session,
        device,
        [
            {"UserID": employee.employee_code, "LogTime": "2026-04-06 09:00:00", "Status": "0"},
            {"garbage": True},
        ],
    )
    assert result.accepted == 1
    assert result.exceptions == 1


async def test_a_device_silent_past_its_threshold_is_flagged(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate.

    Silent device failure is how a month of attendance goes missing.
    """
    device, _ = await _device(session, tenant, hr_user)
    device.silence_threshold_minutes = 60
    device.last_seen_at = datetime.now(UTC) - timedelta(hours=5)
    await session.commit()

    silent = await device_service.silent_devices(session, tenant.id)

    assert [d.id for d in silent] == [device.id]
    await session.refresh(device)
    assert device.status is DeviceStatus.SILENT


async def test_a_recently_seen_device_is_not_flagged(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    device, _ = await _device(session, tenant, hr_user)
    device.last_seen_at = datetime.now(UTC)
    await session.commit()

    assert await device_service.silent_devices(session, tenant.id) == []


async def test_ingesting_clears_a_silent_flag(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    device, _ = await _device(session, tenant, hr_user)
    device.status = DeviceStatus.SILENT
    await session.commit()

    await device_service.ingest_punches(
        session,
        device,
        [{"UserID": employee.employee_code, "LogTime": "2026-04-06 09:00:00", "Status": "0"}],
    )
    await session.refresh(device)
    assert device.status is DeviceStatus.ACTIVE


async def test_a_device_token_authenticates_and_establishes_the_tenant(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    device, token = await _device(session, tenant, hr_user)

    assert device.push_token_hash != token, "only the hash is stored"

    resolved = await device_service.authenticate_device(session, token)
    assert resolved.id == device.id
    assert resolved.tenant_id == tenant.id

    with pytest.raises(device_service.DeviceError):
        await device_service.authenticate_device(session, "dev_not-a-real-token")


async def test_a_disabled_device_cannot_authenticate(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    device, token = await _device(session, tenant, hr_user)
    device.status = DeviceStatus.DISABLED
    await session.commit()

    with pytest.raises(device_service.DeviceError):
        await device_service.authenticate_device(session, token)


# ═══════════════════════════════════════════════════════════════════════════
# Secrets at rest
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_stored_client_secret_is_never_plaintext_in_the_database(
    session: AsyncSession, tenant: Tenant
) -> None:
    """The checklist gate: grep for plaintext columns."""
    from app.core.crypto import decrypt_secret, encrypt_secret

    plaintext = "the-real-oidc-client-secret"
    provider = IdentityProvider(
        tenant_id=tenant.id, name="Entra", idp_type=IdpType.OIDC, enabled=True,
        client_id="abc", client_secret_encrypted=encrypt_secret(plaintext, tenant.id),
    )
    session.add(provider)
    await session.commit()

    # Read the raw column, bypassing the ORM entirely.
    raw = await session.scalar(
        select(IdentityProvider.client_secret_encrypted).where(
            IdentityProvider.id == provider.id
        )
    )
    assert plaintext not in raw
    assert raw.startswith("enc:v1:")
    assert decrypt_secret(raw, tenant.id) == plaintext


async def test_a_scim_token_is_stored_only_as_a_hash(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    _, plaintext = await scim_service.create_scim_token(session, hr_user, "Azure AD")

    rows = (await session.scalars(select(ScimToken))).all()
    for row in rows:
        assert plaintext not in row.token_hash
        assert plaintext not in (row.token_hint or "")


async def test_a_device_push_token_is_stored_only_as_a_hash(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    _, token = await _device(session, tenant, hr_user)

    rows = (await session.scalars(select(DeviceIntegration))).all()
    for row in rows:
        assert token not in (row.push_token_hash or "")
        assert token not in (row.push_token_hint or "")
