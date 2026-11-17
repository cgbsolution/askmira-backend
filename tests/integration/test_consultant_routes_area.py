"""Consultant hours, payouts and contractors follow a sub-admin's payroll rights."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from app.models.access import UserAreaRights
from app.models.consultant_hours import ConsultantHoursEntry
from app.models.employee import EmploymentType
from app.models.user import UserRole
from tests.integration import platform_support
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, make_person

pytestmark = requires_db

client = platform_support.client

_ID = uuid.uuid4()


async def test_sub_admin_without_payroll_rights_cannot_reach_consultant_routes(
    client, session, tenant, employee
):
    blind = await make_person(session, tenant, role=UserRole.SUB_ADMIN, name="Bea Blind")
    viewer = await make_person(session, tenant, role=UserRole.SUB_ADMIN, name="Vera Viewer")
    session.add_all(
        [
            UserAreaRights(
                tenant_id=tenant.id, user_id=blind.id, area="payroll", can_view=False
            ),
            UserAreaRights(
                tenant_id=tenant.id, user_id=viewer.id, area="payroll",
                can_view=True, can_approve=False,
            ),
        ]
    )  # fmt: skip
    await session.commit()

    employee.employment_type = EmploymentType.CONSULTANT
    entry = ConsultantHoursEntry(
        tenant_id=tenant.id,
        employee_id=employee.id,
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        total_billable_hours=Decimal("120.00"),
    )
    session.add(entry)
    await session.commit()

    # Hours has a non-HR view (a finance approver's own consultants), so a narrowed
    # sub-admin gets an empty list, not the tenant's entries.
    hours_url = "/api/v1/consultant-hours"
    admin = await make_person(session, tenant, role=UserRole.ADMIN, name="Ada Admin")
    assert len((await client.get(hours_url, headers=bearer(admin))).json()) == 1
    response = await client.get(hours_url, headers=bearer(blind))
    assert response.status_code == 200 and response.json() == []

    for path in (
        "/api/v1/consultant-payouts",
        "/api/v1/contractors/invoices",
        "/api/v1/contractors",
    ):
        response = await client.get(path, headers=bearer(blind))
        assert response.status_code == 403, path

    # Hours hides an entry from anyone who is not HR or its approver: a 404, not a 403. The
    # admin gets past that to "not waiting for a decision", so the 404 is the narrowing.
    decide = f"/api/v1/consultant-hours/{entry.id}/decide"
    body = {"approve": True}
    assert (await client.post(decide, json=body, headers=bearer(admin))).status_code == 409
    assert (await client.post(decide, json=body, headers=bearer(viewer))).status_code == 404

    approve = f"/api/v1/consultant-payouts/{_ID}/approve"
    assert (await client.post(approve, headers=bearer(viewer))).status_code == 403
