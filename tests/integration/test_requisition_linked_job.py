"""An approved requisition opens its job in the linked interview tool (AnytimeHire).

* The last approval opens the job there, signed use=act, keyed by the
  requisition so a second try gives back the same job and never a duplicate.
* The approval stands even when AnytimeHire refuses: the refusal is kept on the
  requisition, audited, and HR can try again once the tool has room.
* An earlier step, a rejection, or a workspace without AnytimeHire opens nothing.
* Another workspace cannot open someone else's requisition.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.hiring import Requisition, RequisitionStatus
from app.models.offer import ApprovalDecision, RequisitionApproval
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from tests.integration.conftest import requires_db
from tests.integration.test_sales_procurement_link import (  # noqa: F401  (fixture)
    FakeProducts,
    _link,
    fake,
)

pytestmark = requires_db


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


async def _admin(session: AsyncSession, tenant: Tenant, tag: str) -> User:
    user = User(
        tenant_id=tenant.id,
        email=f"{tag}@acme.test",
        password_hash="x",
        full_name=tag.title(),
        role=UserRole.ADMIN,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _waiting(
    session: AsyncSession, tenant: Tenant, *, steps: int = 1, code: str = "REQ-101"
) -> Requisition:
    """A requisition waiting on `steps` admin signatures."""
    modules = dict(tenant.enabled_modules or {})
    modules["hiring"] = True
    tenant.enabled_modules = modules
    requisition = Requisition(
        tenant_id=tenant.id,
        code=code,
        title="Account Executive",
        openings=2,
        employment_type="full_time",
        experience_min_years=2,
        experience_max_years=5,
        budget_ctc_min=Decimal("800000"),
        budget_ctc_max=Decimal("1100000"),
        justification="Q4 pipeline is 28% above plan.",
        description="Own the West region accounts\nRun demos with the solutions team",
        skills=["B2B sales", "CRM"],
        status=RequisitionStatus.PENDING_APPROVAL,
    )
    session.add(requisition)
    await session.flush()
    for sequence in range(1, steps + 1):
        session.add(
            RequisitionApproval(
                tenant_id=tenant.id,
                requisition_id=requisition.id,
                sequence=sequence,
                approver_role="admin",
                decision=ApprovalDecision.PENDING,
            )
        )
    await session.commit()
    await session.refresh(requisition)
    return requisition


async def _decide(client, user: User, requisition: Requisition, approve: bool = True):
    return await client.post(
        f"/api/v1/hiring/requisitions/{requisition.id}/decide",
        json={"approve": approve, "note": None if approve else "Not this quarter."},
        headers=_auth(user),
    )


async def test_the_last_approval_opens_the_job_in_anytimehire(
    client, session: AsyncSession, tenant: Tenant, fake: FakeProducts
) -> None:
    await _link(session, tenant, "interview", None)
    first = await _admin(session, tenant, "asha")
    second = await _admin(session, tenant, "bala")
    requisition = await _waiting(session, tenant, steps=2)

    one = await _decide(client, first, requisition)
    assert one.status_code == 200, one.text
    assert one.json()["status"] == "pending_approval"
    assert "linked_job" not in one.json()
    assert fake.acts("interview", "job.open") == []

    two = await _decide(client, second, requisition)
    assert two.status_code == 200, two.text
    assert two.json()["status"] == "open"
    assert two.json()["linked_job"] == {"opened": True, "ref": "j1", "existing": False}

    [sent] = fake.acts("interview", "job.open")
    params = sent["params"]
    assert params["key"] == f"askmira-req:{requisition.id}"
    assert params["title"] == "Account Executive"
    assert params["openings"] == 2
    assert (params["experience_min"], params["experience_max"]) == (2, 5)
    assert params["skills"] == [{"name": "B2B sales"}, {"name": "CRM"}]
    assert params["responsibilities"] == [
        "Own the West region accounts",
        "Run demos with the solutions team",
    ]
    assert sent["actor"]["email"] == second.email
    # No budget figures leave AskMira with the job.
    assert "800000" not in str(params) and "1100000" not in str(params)

    await session.refresh(requisition)
    assert requisition.linked_job_ref == "j1"
    assert requisition.linked_job_error is None
    detail = await client.get(
        f"/api/v1/hiring/requisitions/{requisition.id}", headers=_auth(second)
    )
    assert detail.json()["linked_job_ref"] == "j1"

    # Again: the same job, never a second one.
    again = await client.post(
        f"/api/v1/hiring/requisitions/{requisition.id}/open-linked-job", headers=_auth(second)
    )
    assert again.json() == {"opened": True, "ref": "j1", "existing": True}
    assert len(fake.jobs) == 1


async def test_a_refusal_keeps_the_approval_and_can_be_tried_again(
    client, session: AsyncSession, tenant: Tenant, fake: FakeProducts
) -> None:
    await _link(session, tenant, "interview", None)
    admin = await _admin(session, tenant, "asha")
    requisition = await _waiting(session, tenant)
    fake.job_quota = 0

    result = await _decide(client, admin, requisition)
    assert result.status_code == 200
    assert result.json()["status"] == "open"
    linked = result.json()["linked_job"]
    assert linked["opened"] is False
    assert "all 6 are used" in linked["error"]

    await session.refresh(requisition)
    assert requisition.status is RequisitionStatus.OPEN
    assert "all 6 are used" in requisition.linked_job_error
    summaries = (
        await session.scalars(
            select(AuditLog.summary).where(
                AuditLog.entity_type == "requisition", AuditLog.entity_id == requisition.id
            )
        )
    ).all()
    assert any("could not be opened in AnytimeHire" in s for s in summaries)

    fake.job_quota = 10
    retry = await client.post(
        f"/api/v1/hiring/requisitions/{requisition.id}/open-linked-job", headers=_auth(admin)
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["opened"] is True
    await session.refresh(requisition)
    assert requisition.linked_job_ref and requisition.linked_job_error is None


async def test_nothing_opens_without_anytimehire_or_on_a_rejection(
    client, session: AsyncSession, tenant: Tenant, fake: FakeProducts
) -> None:
    admin = await _admin(session, tenant, "asha")
    plain = await _waiting(session, tenant, code="REQ-201")
    result = await _decide(client, admin, plain)
    assert result.json()["status"] == "open"
    assert result.json()["linked_job"] is None
    refused = await client.post(
        f"/api/v1/hiring/requisitions/{plain.id}/open-linked-job", headers=_auth(admin)
    )
    assert refused.status_code == 409

    await _link(session, tenant, "interview", None)
    rejected = await _waiting(session, tenant, code="REQ-202")
    result = await _decide(client, admin, rejected, approve=False)
    assert result.json()["status"] == "rejected"
    assert fake.acts("interview", "job.open") == []


async def test_another_workspace_cannot_open_it(
    client, session: AsyncSession, tenant: Tenant, fake: FakeProducts
) -> None:
    await _link(session, tenant, "interview", None)
    admin = await _admin(session, tenant, "asha")
    requisition = await _waiting(session, tenant)
    await _decide(client, admin, requisition)

    other = Tenant(
        name="Other Co",
        slug=f"other-{uuid.uuid4().hex[:8]}",
        enabled_modules={"core_hr": True, "hiring": True},
        settings={"linked_products": {"interview": True}},
    )
    session.add(other)
    await session.commit()
    stranger = await _admin(session, other, "olu")
    response = await client.post(
        f"/api/v1/hiring/requisitions/{requisition.id}/open-linked-job", headers=_auth(stranger)
    )
    assert response.status_code == 404


async def test_requisitions_work_with_anytimehire_even_with_hiring_switched_off(
    client, session: AsyncSession, tenant: Tenant, fake: FakeProducts
) -> None:
    admin = await _admin(session, tenant, "asha")
    requisition = await _waiting(session, tenant)
    modules = dict(tenant.enabled_modules or {})
    modules["hiring"] = False
    tenant.enabled_modules = modules
    await session.commit()

    off = await _decide(client, admin, requisition)
    assert off.status_code == 403

    await _link(session, tenant, "interview", None)
    linked = await _decide(client, admin, requisition)
    assert linked.status_code == 200, linked.text
    assert linked.json()["linked_job"]["opened"] is True
