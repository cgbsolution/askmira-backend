"""Companies of a group as legal entities: logos, who sees which, moving people.

A group (CGB) runs several companies in one workspace: its own, a sister
company, a client it staffs. Each is a legal entity with its own logo; after
sign-in a person sees their own company's logo; HR moves people between
companies in one go; and a month one company's payroll already paid is never
paid again by another's.
"""

from __future__ import annotations

import struct
import uuid
import zlib
from datetime import UTC, date, datetime

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import hr_scope
from app.models.access import UserAreaRights
from app.models.audit import AuditAction, AuditLog
from app.models.employee import Employee, EmploymentStatus
from app.models.lifecycle import LifecycleEvent, LifecycleEventType, LifecycleStatus
from app.models.org import LegalEntity
from app.models.payroll import PayrollRun, PayrollRunStatus, PayrollRunType, Payslip
from app.models.tenant import Tenant
from app.models.user import UserRole
from app.schemas.employee import EmployeeUpdate
from app.services import employee as employee_service
from app.services.payroll import run as run_service
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, client, make_person  # noqa: F401
from tests.integration.test_payroll_run_flow import (  # noqa: F401  (fixture)
    _approved_rules,
    _lock,
    org,
)

pytestmark = requires_db

API = "/api/v1"


def _png() -> bytes:
    """A real one-pixel PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\x00\x00")
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", pixels)
        + chunk(b"IEND", b"")
    )


async def _entity(session: AsyncSession, tenant: Tenant, name: str, code: str, **extra):
    row = LegalEntity(tenant_id=tenant.id, name=name, code=code, **extra)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _employee(session: AsyncSession, tenant: Tenant, code: str, entity: LegalEntity | None):
    row = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code.title(),
        work_email=f"{code.lower()}-{uuid.uuid4().hex[:6]}@acme.test",
        legal_entity_id=entity.id if entity else None,
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2025, 1, 1),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


# ── Logos ───────────────────────────────────────────────────────────────────


async def test_a_company_logo_is_uploaded_shown_publicly_and_removed(
    session: AsyncSession,
    client: httpx.AsyncClient,  # noqa: F811
    tenant: Tenant,
    other_tenant: Tenant,
    legal_entity: LegalEntity,
) -> None:
    admin = await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    staff = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Esha Staff")
    outsider = await make_person(session, other_tenant, role=UserRole.ADMIN, name="Olu Other")
    url = f"{API}/org/legal-entities/{legal_entity.id}/logo"
    png = _png()

    # Only an admin of this workspace may set it.
    files = {"file": ("logo.png", png, "image/png")}
    assert (await client.post(url, files=files, headers=bearer(staff))).status_code == 403
    assert (await client.post(url, files=files, headers=bearer(outsider))).status_code == 404

    # Not an image, renamed: refused by its bytes.
    fake = {"file": ("logo.png", b"<svg onload=alert(1)>", "image/png")}
    assert (await client.post(url, files=fake, headers=bearer(admin))).status_code == 422

    response = await client.post(url, files=files, headers=bearer(admin))
    assert response.status_code == 200, response.text
    logo_url = response.json()["logo_url"]
    assert logo_url.startswith(f"{API}/public/workspaces/entities/{legal_entity.id}/logo?v=")

    listed = (await client.get(f"{API}/org/legal-entities", headers=bearer(staff))).json()
    assert listed[0]["logo_url"] == logo_url

    # Public, like the workspace logo: no sign-in in front of it.
    served = await client.get(logo_url)
    assert served.status_code == 200
    assert served.content == png
    assert served.headers["content-type"] == "image/png"

    # Audited in the same transaction.
    audited = await session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.entity_type == "legal_entity", AuditLog.entity_id == legal_entity.id)
    )
    assert audited == 1

    assert (await client.delete(url, headers=bearer(admin))).status_code == 204
    assert (await client.get(logo_url)).status_code == 404
    listed = (await client.get(f"{API}/org/legal-entities", headers=bearer(admin))).json()
    assert listed[0]["logo_url"] is None
    # Unknown id: nothing to show.
    assert (
        await client.get(f"{API}/public/workspaces/entities/{uuid.uuid4()}/logo")
    ).status_code == 404


async def test_a_suspended_workspace_serves_no_company_logo(
    session: AsyncSession,
    client: httpx.AsyncClient,  # noqa: F811
    tenant: Tenant,
) -> None:
    entity = await _entity(
        session, tenant, "Artbox Solutions", "ARTBOX", logo_key="t/branding/x.png"
    )
    tenant.is_active = False
    await session.commit()
    response = await client.get(f"{API}/public/workspaces/entities/{entity.id}/logo")
    assert response.status_code == 404


# ── Who sees which logo ─────────────────────────────────────────────────────


async def test_the_session_carries_the_persons_own_company(
    session: AsyncSession,
    client: httpx.AsyncClient,  # noqa: F811
    tenant: Tenant,
) -> None:
    tenant.logo_key = f"{tenant.id}/branding/group.png"
    await session.commit()
    artbox = await _entity(
        session, tenant, "Artbox Solutions", "ARTBOX", logo_key=f"{tenant.id}/branding/a.png"
    )
    dxc = await _entity(session, tenant, "DXC Technology (client)", "DXC")

    artbox_person = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Ankit A")
    dxc_person = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Divya D")
    no_company = await make_person(
        session, tenant, role=UserRole.ADMIN, name="Platform P", with_employee=False
    )
    for person, entity in ((artbox_person, artbox), (dxc_person, dxc)):
        record = await session.get(Employee, person.employee_id)
        record.legal_entity_id = entity.id
    await session.commit()

    async def company(user) -> dict:
        response = await client.get(f"{API}/auth/session", headers=bearer(user))
        assert response.status_code == 200, response.text
        return response.json()["company"]

    # Their own company's name and logo.
    seen = await company(artbox_person)
    assert seen["name"] == "Artbox Solutions"
    assert seen["legal_entity_id"] == str(artbox.id)
    assert f"/public/workspaces/entities/{artbox.id}/logo?v=" in seen["logo_url"]

    # A company with no logo of its own: its name, the group's logo.
    seen = await company(dxc_person)
    assert seen["name"] == "DXC Technology (client)"
    assert f"/public/workspaces/{tenant.slug}/logo?v=" in seen["logo_url"]

    # Nobody's employee: the workspace.
    seen = await company(no_company)
    assert seen == {
        "name": tenant.name,
        "logo_url": seen["logo_url"],
        "legal_entity_id": None,
    }
    assert f"/public/workspaces/{tenant.slug}/logo" in seen["logo_url"]


# ── Moving people between companies ─────────────────────────────────────────


async def test_hr_moves_people_to_another_company_in_one_go(
    session: AsyncSession,
    client: httpx.AsyncClient,  # noqa: F811
    tenant: Tenant,
    other_tenant: Tenant,
    legal_entity: LegalEntity,
) -> None:
    dxc = await _entity(session, tenant, "DXC Technology (client)", "DXC")
    hr = await make_person(session, tenant, role=UserRole.SUB_ADMIN, name="Hema HR")
    staff = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Sunil Staff")
    hr_record = await session.get(Employee, hr.employee_id)
    hr_record.legal_entity_id = legal_entity.id
    first = await _employee(session, tenant, "EXT001", legal_entity)
    second = await _employee(session, tenant, "EXT002", None)
    already = await _employee(session, tenant, "DXC001", dxc)
    foreign = await _employee(session, other_tenant, "GLB001", None)
    leaver = await _employee(session, tenant, "LEFT01", legal_entity)
    leaver.status = EmploymentStatus.EXITED
    await session.commit()

    body = {
        "employee_ids": [
            str(first.id),
            str(second.id),
            str(first.id),  # twice: counted once
            str(already.id),
            str(hr.employee_id),  # their own record
            str(foreign.id),  # another workspace's
            str(leaver.id),  # has left
        ],
        "legal_entity_id": str(dxc.id),
    }
    url = f"{API}/employees/bulk/legal-entity"
    assert (await client.patch(url, json=body, headers=bearer(staff))).status_code == 403

    response = await client.patch(url, json=body, headers=bearer(hr))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["moved"] == 2
    assert result["already_there"] == 1
    assert result["legal_entity_name"] == "DXC Technology (client)"
    reasons = {row["employee_id"]: row["reason"] for row in result["skipped"]}
    assert "own record" in reasons[str(hr.employee_id)]
    assert reasons[str(foreign.id)] == "Not found in this workspace."
    assert reasons[str(leaver.id)].startswith("Has left")

    await session.refresh(first)
    await session.refresh(second)
    await session.refresh(foreign)
    await session.refresh(leaver)
    assert first.legal_entity_id == dxc.id
    assert second.legal_entity_id == dxc.id
    assert foreign.legal_entity_id is None
    # A leaver keeps the company that paid them, and gets no transfer.
    assert leaver.legal_entity_id == legal_entity.id
    assert (
        await session.scalar(select(LifecycleEvent).where(LifecycleEvent.employee_id == leaver.id))
        is None
    )

    # In their history as a transfer, and audited.
    event = await session.scalar(
        select(LifecycleEvent).where(LifecycleEvent.employee_id == first.id)
    )
    assert event.event_type is LifecycleEventType.TRANSFER
    assert event.previous_values == {"legal_entity_id": str(legal_entity.id)}
    assert event.new_values == {"legal_entity_id": str(dxc.id)}
    audited = await session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.entity_type == "employee", AuditLog.entity_id.in_([first.id, second.id]))
    )
    assert audited == 2

    # The directory filters by company, and the list counts each company's people.
    listed = (
        await client.get(f"{API}/employees?legal_entity_id={dxc.id}", headers=bearer(staff))
    ).json()
    assert {row["employee_code"] for row in listed["items"]} == {"EXT001", "EXT002", "DXC001"}
    assert all(row["legal_entity_id"] == str(dxc.id) for row in listed["items"])
    entities = (await client.get(f"{API}/org/legal-entities", headers=bearer(staff))).json()
    counts = {row["code"]: row["headcount"] for row in entities}
    assert counts["DXC"] == 3
    assert counts["ACME"] == 1  # the HR person, who was not moved

    # People with no company at all: what no payroll run pays.
    nobody = await _employee(session, tenant, "NOCO01", None)
    loose = (
        await client.get(f"{API}/employees?without_legal_entity=true", headers=bearer(hr))
    ).json()
    assert str(nobody.id) in {row["id"] for row in loose["items"]}
    assert all(row["legal_entity_id"] is None for row in loose["items"])

    # The export names each person's company.
    csv = (await client.get(f"{API}/employees/export?legal_entity_id={dxc.id}", headers=bearer(hr)))
    assert csv.status_code == 200
    assert "Company" in csv.text.splitlines()[0]
    assert "DXC Technology (client)" in csv.text
    # Its audit row says which company was exported.
    exported = await session.scalar(
        select(AuditLog)
        .where(AuditLog.action == AuditAction.EXPORT)
        .order_by(AuditLog.created_at.desc())
    )
    assert exported.changes["legal_entity_id"] == str(dxc.id)
    assert exported.changes["without_legal_entity"] is False

    # Another workspace's company, or an inactive one, cannot be chosen.
    foreign_entity = await _entity(session, other_tenant, "Globex Pvt Ltd", "GLOBEX")
    body = {"employee_ids": [str(first.id)], "legal_entity_id": str(foreign_entity.id)}
    assert (await client.patch(url, json=body, headers=bearer(hr))).status_code == 404
    dxc.is_active = False
    await session.commit()
    body = {"employee_ids": [str(first.id)], "legal_entity_id": str(dxc.id)}
    response = await client.patch(url, json=body, headers=bearer(hr))
    assert response.status_code == 422
    assert "inactive" in response.json()["detail"]
    # Nothing at all to move is refused before anything happens.
    body = {"employee_ids": [], "legal_entity_id": str(dxc.id)}
    assert (await client.patch(url, json=body, headers=bearer(hr))).status_code == 422


async def test_moving_people_needs_the_edit_right_not_the_create_right(
    session: AsyncSession,
    client: httpx.AsyncClient,  # noqa: F811
    tenant: Tenant,
    legal_entity: LegalEntity,
) -> None:
    dxc = await _entity(session, tenant, "DXC Technology (client)", "DXC")
    person = await _employee(session, tenant, "EXT010", legal_entity)
    adder = await make_person(session, tenant, role=UserRole.SUB_ADMIN, name="Riya Adder")
    editor = await make_person(session, tenant, role=UserRole.SUB_ADMIN, name="Esha Editor")
    session.add_all(
        [
            UserAreaRights(
                tenant_id=tenant.id,
                user_id=adder.id,
                area="people",
                can_view=True,
                can_create=True,
                can_edit=False,
            ),
            UserAreaRights(
                tenant_id=tenant.id,
                user_id=editor.id,
                area="people",
                can_view=True,
                can_create=False,
                can_edit=True,
            ),
        ]
    )
    await session.commit()
    url = f"{API}/employees/bulk/legal-entity"
    body = {"employee_ids": [str(person.id)], "legal_entity_id": str(dxc.id)}

    # Create without Edit: refused, and nobody moves.
    assert (await client.patch(url, json=body, headers=bearer(adder))).status_code == 403
    await session.refresh(person)
    assert person.legal_entity_id == legal_entity.id

    # Edit without Create: allowed, as the same change one record at a time is.
    response = await client.patch(url, json=body, headers=bearer(editor))
    assert response.status_code == 200, response.text
    await session.refresh(person)
    assert person.legal_entity_id == dxc.id


async def test_a_company_changed_on_the_record_is_a_transfer_in_the_history(
    session: AsyncSession,
    client: httpx.AsyncClient,  # noqa: F811
    tenant: Tenant,
    legal_entity: LegalEntity,
) -> None:
    artbox = await _entity(session, tenant, "Artbox Solutions", "ARTBOX")
    hr = await make_person(session, tenant, role=UserRole.ADMIN, name="Hari HR")
    person = await _employee(session, tenant, "ART001", legal_entity)

    response = await client.patch(
        f"{API}/employees/{person.id}",
        json={"legal_entity_id": str(artbox.id)},
        headers=bearer(hr),
    )
    assert response.status_code == 200, response.text
    event = await session.scalar(
        select(LifecycleEvent).where(LifecycleEvent.employee_id == person.id)
    )
    assert event.event_type is LifecycleEventType.TRANSFER
    assert event.new_values == {"legal_entity_id": str(artbox.id)}


# ── A month is paid by one company ──────────────────────────────────────────


async def test_a_month_one_company_paid_is_not_paid_again_by_another(
    session: AsyncSession,
    org,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _approved_rules(session, org)
    session.add(_lock(org["tenant"], org["maker"], 2026, 4))
    await session.commit()
    dxc = await _entity(session, org["tenant"], "DXC Technology (client)", "DXC")
    meera = org["meera_emp"].id

    acme_april = await run_service.create_run(
        session, org["maker"], legal_entity_id=org["entity"].id, period_year=2026, period_month=4
    )
    await run_service.compute_run(session, org["maker"], acme_april.id)

    # While ACME's April run waits for approval, Meera's move to DXC is
    # back-dated to 15 April on her record. April is now DXC's month (she is
    # DXC's on its last day), so DXC's run holds her too.
    await employee_service.update_employee(
        session,
        org["maker"],
        meera,
        EmployeeUpdate(legal_entity_id=dxc.id, changes_effective_from=date(2026, 4, 15)),
    )
    dxc_april = await run_service.create_run(
        session, org["maker"], legal_entity_id=dxc.id, period_year=2026, period_month=4
    )
    computed = await run_service.compute_run(session, org["maker"], dxc_april.id)
    assert computed.employee_count == 1
    moved_in = [n["message"] for n in computed.input_notices if n.get("key") == "company_move"]
    assert any("moved here from Acme Technologies Pvt Ltd on 15 Apr 2026" in m for m in moved_in)

    # ACME approves first and pays her April.
    approved = await run_service.approve_run(session, org["checker"], acme_april.id)
    assert approved.status is PayrollRunStatus.APPROVED

    # DXC cannot approve paying the same April again — not even when the
    # approver's own scope leaves Meera out of what they can see.
    checker_record = org["checker"].employee_id
    hr_scope.set_scope(hr_scope.HrScope(employee_ids=(checker_record,)))
    try:
        with pytest.raises(HTTPException) as exc:
            await run_service.approve_run(session, org["checker"], dxc_april.id)
    finally:
        hr_scope.set_scope(None)
    assert exc.value.status_code == 409
    assert "already paid for this month" in exc.value.detail
    assert "MEERA" in exc.value.detail

    # Recomputed, DXC's run leaves her out, says why, and does not hand her to
    # the run's input sources (an arrear batch or a bonus would otherwise be
    # marked paid by a run that never paid her).
    seen: dict[str, set] = {}
    real_collect = run_service.collect_inputs

    async def spy(*args, **kwargs):
        seen["scope"] = set(args[2].employee_ids)
        return await real_collect(*args, **kwargs)

    monkeypatch.setattr(run_service, "collect_inputs", spy)
    recomputed = await run_service.compute_run(session, org["maker"], dxc_april.id)
    assert meera not in seen["scope"]
    slip = await session.scalar(
        select(Payslip).where(Payslip.run_id == dxc_april.id, Payslip.employee_id == meera)
    )
    assert slip is None
    assert recomputed.employee_count == 0
    notices = [n["message"] for n in recomputed.input_notices if n.get("key") == "company_move"]
    assert any(
        "Acme Technologies Pvt Ltd's approved run already paid them" in m and "MEERA" in m
        for m in notices
    )


async def test_a_month_is_paid_by_the_company_on_its_last_day(
    session: AsyncSession,
    org,  # noqa: F811
) -> None:
    """A transfer dated 1 May, already applied, leaves April with the old company."""
    await _approved_rules(session, org)
    for month in (4, 5):
        session.add(_lock(org["tenant"], org["maker"], 2026, month))
    await session.commit()
    acme = org["entity"]
    dxc = await _entity(session, org["tenant"], "DXC Technology (client)", "DXC")
    meera = await session.get(Employee, org["meera_emp"].id)

    # The transfer as the morning sweep leaves it: effected, record moved.
    now = datetime.now(UTC)
    session.add(
        LifecycleEvent(
            tenant_id=org["tenant"].id,
            employee_id=meera.id,
            event_type=LifecycleEventType.TRANSFER,
            effective_from=date(2026, 5, 1),
            previous_values={"legal_entity_id": str(acme.id)},
            new_values={"legal_entity_id": str(dxc.id)},
            reason="Moves to the DXC account from May.",
            status=LifecycleStatus.EFFECTED,
            raised_by_user_id=org["maker"].id,
            approved_by_user_id=org["checker"].id,
            approved_at=now,
            effected_at=now,
        )
    )
    meera.legal_entity_id = dxc.id
    await session.commit()

    async def payslip_people(entity_id, month):
        run = await run_service.create_run(
            session, org["maker"], legal_entity_id=entity_id, period_year=2026, period_month=month
        )
        await run_service.compute_run(session, org["maker"], run.id)
        rows = await session.scalars(select(Payslip.employee_id).where(Payslip.run_id == run.id))
        return run, set(rows.all())

    # April, run after the move: still ACME's, all of it.
    acme_april, paid = await payslip_people(acme.id, 4)
    assert meera.id in paid
    _, paid = await payslip_people(dxc.id, 4)
    assert meera.id not in paid
    # May is DXC's.
    _, paid = await payslip_people(dxc.id, 5)
    assert meera.id in paid
    _, paid = await payslip_people(acme.id, 5)
    assert meera.id not in paid

    # An off-cycle run for April follows the same rule: ACME's, not DXC's.
    await run_service.approve_run(session, org["checker"], acme_april.id)
    dxc_april = await session.scalar(
        select(PayrollRun).where(
            PayrollRun.legal_entity_id == dxc.id,
            PayrollRun.period_month == 4,
        )
    )
    await run_service.approve_run(session, org["checker"], dxc_april.id)
    off_cycle = await run_service.create_run(
        session,
        org["maker"],
        legal_entity_id=acme.id,
        period_year=2026,
        period_month=4,
        run_type=PayrollRunType.OFF_CYCLE,
        employee_ids=[meera.id],
        reason="April overtime approved late.",
    )
    assert off_cycle.legal_entity_id == acme.id
    with pytest.raises(HTTPException) as exc:
        await run_service.create_run(
            session,
            org["maker"],
            legal_entity_id=dxc.id,
            period_year=2026,
            period_month=4,
            run_type=PayrollRunType.OFF_CYCLE,
            employee_ids=[meera.id],
            reason="April overtime approved late.",
        )
    assert exc.value.status_code == 422
    assert "30 Apr 2026" in exc.value.detail


async def test_a_move_inside_the_month_is_explained_on_both_runs(
    session: AsyncSession,
    org,  # noqa: F811
) -> None:
    await _approved_rules(session, org)
    session.add(_lock(org["tenant"], org["maker"], 2026, 4))
    await session.commit()
    acme = org["entity"]
    dxc = await _entity(session, org["tenant"], "DXC Technology (client)", "DXC")
    await employee_service.update_employee(
        session,
        org["maker"],
        org["ravi_emp"].id,
        EmployeeUpdate(legal_entity_id=dxc.id, changes_effective_from=date(2026, 4, 20)),
    )

    acme_april = await run_service.create_run(
        session, org["maker"], legal_entity_id=acme.id, period_year=2026, period_month=4
    )
    acme_run = await run_service.compute_run(session, org["maker"], acme_april.id)
    left = [n["message"] for n in acme_run.input_notices if n.get("key") == "company_move"]
    assert any(
        "RAVI moved to DXC Technology (client) on 20 Apr 2026, so this run does not pay them" in m
        for m in left
    )

    dxc_april = await run_service.create_run(
        session, org["maker"], legal_entity_id=dxc.id, period_year=2026, period_month=4
    )
    dxc_run = await run_service.compute_run(session, org["maker"], dxc_april.id)
    assert dxc_run.employee_count == 1
    arrived = [n["message"] for n in dxc_run.input_notices if n.get("key") == "company_move"]
    assert any("This run pays all of April 2026" in m for m in arrived)
