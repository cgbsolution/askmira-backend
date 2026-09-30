"""Asset management against a real database.

Three flows, end to end: the register (hand-over, acknowledgement, return),
requests (ask, decide, fulfil), and exit — where each asset the leaver holds
becomes a clearance item, and an unreturned one reaches the settlement at its
book value.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Employee, Tenant, UserRole
from app.models.assets import (
    Asset,
    AssetAssignment,
    AssetCondition,
    AssetRequestStatus,
    AssetStatus,
    AssignmentOutcome,
)
from app.models.audit import AuditLog
from app.models.exit_workflow import (
    ClearanceItem,
    ClearanceStatus,
    ExitType,
    SettlementLine,
)
from app.models.notifications import Notification
from app.services.approvals import service as approvals
from app.services.assets import service as assets
from app.services.exit_workflow import service as exit_service
from app.services.exit_workflow import settlement as settlement_engine
from tests.integration import platform_support
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, make_person

pytestmark = requires_db

client = platform_support.client


def _asset_payload(category_id, tag: str, **overrides) -> dict:
    base = {
        "category_id": category_id,
        "asset_tag": tag,
        "name": "ThinkPad T14",
        "serial_number": f"SN-{tag}",
        "make": "Lenovo",
        "model": "T14 Gen 4",
        "specs": None,
        "notes": None,
        "purchase_date": date(2025, 8, 31),
        "purchase_cost": Decimal("90000.00"),
        "vendor": "Acme Distributors",
        "invoice_number": "INV-1",
        "warranty_until": None,
        "location_id": None,
        "condition": AssetCondition.NEW,
    }
    base.update(overrides)
    return base


async def _notified(session: AsyncSession, user_id, event_key: str) -> bool:
    return (
        await session.scalar(
            select(Notification.id).where(
                Notification.recipient_user_id == user_id, Notification.event_key == event_key
            )
        )
    ) is not None


async def test_register_hand_over_acknowledge_and_return(
    session: AsyncSession, tenant: Tenant, client
) -> None:
    hr = await make_person(session, tenant, role=UserRole.HR_ADMIN, name="Asha Admin")
    worker = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Ravi Worker")

    category = await assets.create_category(
        session, hr, code="LAPTOP", name="Laptops", description=None,
        custodian_user_id=None, depreciation_months=36, requires_acknowledgement=True,
    )
    laptop = await assets.create_asset(session, hr, _asset_payload(category.id, "LT-001"))
    await session.commit()

    # The register is HR's.
    with pytest.raises(HTTPException) as refused:
        await assets.create_asset(session, worker, _asset_payload(category.id, "LT-002"))
    assert refused.value.status_code == 403
    response = await client.get("/api/v1/assets", headers=bearer(worker))
    assert response.status_code == 403

    # A duplicate tag is refused rather than silently merged.
    with pytest.raises(HTTPException) as duplicate:
        await assets.create_asset(session, hr, _asset_payload(category.id, "lt-001"))
    assert duplicate.value.status_code == 409

    due = assets.today() + timedelta(days=30)
    assignment = await assets.assign_asset(
        session, hr, laptop.id, employee_id=worker.employee_id, expected_return_date=due
    )
    await session.commit()
    assert laptop.status is AssetStatus.ASSIGNED
    assert await _notified(session, worker.id, assets.EVENT_ASSIGNED)

    # One holder at a time.
    with pytest.raises(HTTPException) as again:
        await assets.assign_asset(session, hr, laptop.id, employee_id=worker.employee_id)
    assert again.value.status_code == 409

    # Nobody acknowledges on the holder's behalf — HR included.
    with pytest.raises(HTTPException) as proxy:
        await assets.acknowledge(session, hr, assignment.id)
    assert proxy.value.status_code == 403
    response = await client.post(
        f"/api/v1/assets/assignments/{assignment.id}/acknowledge", headers=bearer(worker)
    )
    assert response.status_code == 200, response.text
    assert response.json()["acknowledged_at"] is not None

    response = await client.get("/api/v1/assets/me", headers=bearer(worker))
    assert response.status_code == 200
    mine = response.json()
    assert [row["asset_tag"] for row in mine["current"]] == ["LT-001"]
    assert mine["history"] == []

    figures = await assets.summary(session, hr)
    assert figures["assigned"] == 1 and figures["in_stock"] == 0
    assert figures["unacknowledged"] == 0
    assert figures["purchase_cost_total"] == Decimal("90000.00")

    # Nobody receives their own asset back.
    with pytest.raises(HTTPException) as own:
        await assets.return_asset(
            session, worker, assignment.id, condition_in=AssetCondition.GOOD
        )
    assert own.value.status_code == 403

    # A recovery belongs to a damaged return only.
    with pytest.raises(HTTPException) as undamaged:
        await assets.return_asset(
            session, hr, assignment.id, condition_in=AssetCondition.GOOD,
            recovery_amount=Decimal("100"),
        )
    assert undamaged.value.status_code == 422

    await assets.return_asset(
        session, hr, assignment.id, condition_in=AssetCondition.DAMAGED,
        notes="Hinge cracked", recovery_amount=Decimal("2500.00"),
        next_status=AssetStatus.IN_REPAIR,
    )
    await session.commit()
    await session.refresh(laptop)
    assert laptop.status is AssetStatus.IN_REPAIR
    assert laptop.condition is AssetCondition.DAMAGED

    detail = await assets.asset_detail(session, hr, laptop.id)
    assert detail["current_assignment"] is None
    (closed,) = detail["history"]
    assert closed["outcome"] is AssignmentOutcome.RETURNED
    assert closed["recovery_amount"] == Decimal("2500.00")
    assert closed["acknowledged_at"] is not None

    # Closed means closed: the next holder is a new row.
    with pytest.raises(HTTPException) as reopened:
        await assets.return_asset(
            session, hr, assignment.id, condition_in=AssetCondition.GOOD
        )
    assert reopened.value.status_code == 409

    # Back from repair, then retired with a reason; nothing is ever deleted.
    await assets.change_status(
        session, hr, laptop.id, target=AssetStatus.IN_STOCK, reason="Hinge replaced"
    )
    await assets.change_status(
        session, hr, laptop.id, target=AssetStatus.RETIRED, reason="End of life"
    )
    await session.commit()
    with pytest.raises(HTTPException) as dead:
        await assets.assign_asset(session, hr, laptop.id, employee_id=worker.employee_id)
    assert dead.value.status_code == 409

    trail = (
        await session.scalars(
            select(AuditLog.entity_type).where(AuditLog.tenant_id == tenant.id)
        )
    ).all()
    assert {"asset_category", "asset", "asset_assignment"} <= set(trail)


async def test_request_is_decided_by_the_line_and_fulfilled_by_hr(
    session: AsyncSession, tenant: Tenant
) -> None:
    boss = await make_person(session, tenant, role=UserRole.MANAGER, name="Kavya Boss")
    boss_emp = await session.get(Employee, boss.employee_id)
    report = await make_person(
        session, tenant, role=UserRole.EMPLOYEE, name="Ravi Report", manager=boss_emp
    )
    outsider = await make_person(session, tenant, role=UserRole.MANAGER, name="Omar Mgr")
    hr = await make_person(session, tenant, role=UserRole.HR_OPS, name="Hari Ops")

    laptops = await assets.create_category(
        session, hr, code="LAPTOP", name="Laptops", description=None,
        custodian_user_id=None, depreciation_months=36, requires_acknowledgement=True,
    )
    phones = await assets.create_category(
        session, hr, code="PHONE", name="Phones", description=None,
        custodian_user_id=None, depreciation_months=None, requires_acknowledgement=False,
    )
    laptop = await assets.create_asset(session, hr, _asset_payload(laptops.id, "LT-100"))
    phone = await assets.create_asset(
        session, hr, _asset_payload(phones.id, "PH-100", name="Pixel 8")
    )
    await session.commit()

    asked = await assets.create_request(
        session, report, category_id=laptops.id, reason="Joining the data team", needed_by=None
    )
    await session.commit()
    assert asked.status is AssetRequestStatus.PENDING
    assert await _notified(session, boss.id, assets.EVENT_REQUEST_SUBMITTED)

    # A second open request for the same kind of thing is refused.
    with pytest.raises(HTTPException) as twice:
        await assets.create_request(
            session, report, category_id=laptops.id, reason="Again", needed_by=None
        )
    assert twice.value.status_code == 409

    # The inbox shows it to the line manager, and to nobody outside the line.
    boss_inbox = await approvals.inbox(session, boss)
    assert [i.id for i in boss_inbox.items if i.source == "asset_request"] == [asked.id]
    outsider_inbox = await approvals.inbox(session, outsider)
    assert not [i for i in outsider_inbox.items if i.source == "asset_request"]
    assert not [
        i for i in (await approvals.inbox(session, report)).items if i.source == "asset_request"
    ]

    # Nobody decides their own request; a manager outside the line cannot either.
    for who in (report, outsider):
        with pytest.raises(HTTPException) as refused:
            await assets.decide_request(session, who, asked.id, approve=True, note=None)
        assert refused.value.status_code == 403

    # A rejection needs a reason; an approval does not.
    with pytest.raises(HTTPException) as bare:
        await assets.decide_request(session, boss, asked.id, approve=False, note=" ")
    assert bare.value.status_code == 422
    await assets.decide_request(session, boss, asked.id, approve=True, note="Go ahead")
    await session.commit()
    assert asked.status is AssetRequestStatus.APPROVED
    assert await _notified(session, report.id, assets.EVENT_REQUEST_DECIDED)

    review = await assets.list_requests(session, hr, scope="review")
    (row,) = review
    assert row["can_fulfil"] and not row["can_decide"]

    # The asset handed over has to be the kind that was asked for.
    with pytest.raises(HTTPException) as wrong:
        await assets.fulfil_request(session, hr, asked.id, asset_id=phone.id)
    assert wrong.value.status_code == 422

    await assets.fulfil_request(session, hr, asked.id, asset_id=laptop.id)
    await session.commit()
    assert asked.status is AssetRequestStatus.FULFILLED
    assert asked.fulfilled_asset_id == laptop.id
    assert laptop.status is AssetStatus.ASSIGNED
    mine = await assets.my_assets(session, report)
    assert [row["asset_tag"] for row in mine["current"]] == ["LT-100"]

    # A fulfilled request is closed: it cannot be withdrawn.
    with pytest.raises(HTTPException) as late:
        await assets.cancel_request(session, report, asked.id)
    assert late.value.status_code == 409

    # And one still open can be withdrawn only by the person who asked.
    phone_ask = await assets.create_request(
        session, report, category_id=phones.id, reason="On call", needed_by=None
    )
    await session.commit()
    with pytest.raises(HTTPException) as proxy:
        await assets.cancel_request(session, hr, phone_ask.id)
    assert proxy.value.status_code == 403
    await assets.cancel_request(session, report, phone_ask.id)
    await session.commit()
    assert phone_ask.status is AssetRequestStatus.CANCELLED


async def test_exit_clearance_closes_assignments_and_recovers_book_value(
    session: AsyncSession, tenant: Tenant
) -> None:
    hr = await make_person(session, tenant, role=UserRole.HR_ADMIN, name="Asha Admin")
    custodian = await make_person(session, tenant, role=UserRole.HR_OPS, name="Ishaan IT")
    leaver = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Leela Leaver")

    laptops = await assets.create_category(
        session, hr, code="LAPTOP", name="Laptops", description=None,
        custodian_user_id=custodian.id, depreciation_months=36, requires_acknowledgement=True,
    )
    cards = await assets.create_category(
        session, hr, code="CARD", name="Access cards", description=None,
        custodian_user_id=None, depreciation_months=None, requires_acknowledgement=False,
    )
    laptop = await assets.create_asset(session, hr, _asset_payload(laptops.id, "LT-900"))
    phone = await assets.create_asset(
        session, hr, _asset_payload(laptops.id, "LT-901", name="MacBook Air")
    )
    card = await assets.create_asset(
        session, hr,
        _asset_payload(cards.id, "AC-900", name="Access card", purchase_cost=Decimal("500")),
    )
    held = {}
    for asset in (laptop, phone, card):
        held[asset.asset_tag] = await assets.assign_asset(
            session, hr, asset.id, employee_id=leaver.employee_id
        )
    await session.commit()

    record = await exit_service.initiate_exit(
        session, hr,
        employee_id=leaver.employee_id,
        exit_type=ExitType.RESIGNATION,
        resignation_date=date(2026, 6, 1),
        approved_last_working_day=date(2026, 8, 31),
        notice_period_days=0,
    )
    await session.commit()

    items = {
        item.asset_assignment_id: item
        for item in (
            await session.scalars(
                select(ClearanceItem).where(ClearanceItem.exit_record_id == record.id)
            )
        ).all()
    }
    assert set(items) == {row.id for row in held.values()}
    laptop_item = items[held["LT-900"].id]
    assert laptop_item.description == "Return LT-900 ThinkPad T14"
    assert laptop_item.department == "IT/Admin"
    assert laptop_item.is_mandatory
    # The category's custodian collects it; a category without one falls to HR.
    assert laptop_item.owner_role == "named_user"
    assert laptop_item.owner_user_id == custodian.id
    assert items[held["AC-900"].id].owner_role == "hr"

    # Returned at clearance: the assignment closes and the asset is back in stock.
    await exit_service.clear_item(
        session, custodian, item_id=items[held["LT-901"].id].id, outcome=ClearanceStatus.CLEARED
    )
    # Not returned, no figure typed: the book value on the last working day is
    # recovered — 90,000 bought 31 Aug 2025 over 36 months, 12 gone by 31 Aug 2026.
    signed = await exit_service.clear_item(
        session, custodian, item_id=laptop_item.id,
        outcome=ClearanceStatus.CLEARED_WITH_RECOVERY,
    )
    assert signed.recovery_amount == Decimal("60000.00")
    # Returned through the register instead: the pending item signs itself off.
    await assets.return_asset(
        session, hr, held["AC-900"].id, condition_in=AssetCondition.GOOD
    )
    await session.commit()

    for tag, outcome, asset_status in (
        ("LT-901", AssignmentOutcome.RETURNED, AssetStatus.IN_STOCK),
        ("LT-900", AssignmentOutcome.LOST, AssetStatus.LOST),
        ("AC-900", AssignmentOutcome.RETURNED, AssetStatus.IN_STOCK),
    ):
        row = await session.get(AssetAssignment, held[tag].id)
        await session.refresh(row)
        assert row.closed_at is not None, tag
        assert row.outcome is outcome, tag
        assert row.exit_record_id == record.id, tag
        asset = await session.scalar(select(Asset).where(Asset.asset_tag == tag))
        await session.refresh(asset)
        assert asset.status is asset_status, tag
    assert (await session.get(AssetAssignment, held["LT-900"].id)).recovery_amount == Decimal(
        "60000.00"
    )
    card_item = await session.get(ClearanceItem, items[held["AC-900"].id].id)
    await session.refresh(card_item)
    assert card_item.status is ClearanceStatus.CLEARED

    # The settlement's existing asset line picks the recovery up.
    settlement = await exit_service.compute_settlement(
        session, hr, exit_id=record.id,
        inputs=settlement_engine.SettlementInput(
            final_month_salary=Decimal("45000"),
            final_month_basis="31 of 31 days worked in August.",
        ),
    )
    await session.commit()
    lines = {
        line.code: line
        for line in (
            await session.scalars(
                select(SettlementLine).where(SettlementLine.settlement_id == settlement.id)
            )
        ).all()
    }
    assert lines["ASSETS"].amount == Decimal("60000.00")
    assert "LT-900" in lines["ASSETS"].basis
