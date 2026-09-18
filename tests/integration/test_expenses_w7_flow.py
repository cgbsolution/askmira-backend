"""W7 expenses against a real database.

A claim with a policy violation → justification → finance decision; reports
with line items; receipts validated by their bytes; nobody decides their own
claim, trip, advance or settlement; another tenant gets a 404.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import AsyncGenerator
from datetime import date
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_session
from app.main import app
from app.models.employee import Employee, EmploymentStatus
from app.models.expenses import (
    CityTierEnum,
    ExpenseClaimStatusEnum,
    ExpenseReportStatusEnum,
    LimitTypeEnum,
    ViolationActionEnum,
)
from app.models.user import User, UserRole
from app.services.expenses import reads, receipt_store
from app.services.expenses import service as expenses
from app.services.gst import service as gst
from tests.integration.conftest import requires_db

pytestmark = requires_db

D = Decimal
TODAY = date(2026, 9, 7)


def png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 120, 110)).save(buffer, format="PNG")
    return buffer.getvalue()


async def _person(session, tenant, entity, location, *, code, role) -> tuple[Employee, User]:
    employee = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code.title(),
        work_email=f"{code.lower()}@acme.test",
        legal_entity_id=entity.id,
        location_id=location.id,
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2020, 1, 1),
    )
    session.add(employee)
    await session.flush()
    user = User(
        tenant_id=tenant.id,
        email=f"{code.lower()}@acme.test",
        password_hash="not-a-real-hash",
        full_name=code.title(),
        role=role,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(user)
    await session.commit()
    await session.refresh(employee)
    await session.refresh(user)
    return employee, user


@pytest.fixture
async def people(session, tenant, legal_entity, location):
    staff, staff_user = await _person(
        session, tenant, legal_entity, location, code="STAFF1", role=UserRole.EMPLOYEE
    )
    other, other_user = await _person(
        session, tenant, legal_entity, location, code="STAFF2", role=UserRole.EMPLOYEE
    )
    finance, finance_user = await _person(
        session, tenant, legal_entity, location, code="FIN1", role=UserRole.HR_OPS
    )
    return {
        "staff": staff, "staff_user": staff_user,
        "other": other, "other_user": other_user,
        "finance": finance, "finance_user": finance_user,
    }


def rule(category: str, action: ViolationActionEnum, limit: str = "8000") -> dict:
    return {
        "category_key": category,
        "city_tier": CityTierEnum.A,
        "limit_type": LimitTypeEnum.PER_CLAIM,
        "limit_amount": D(limit),
        "violation_action": action,
        "requires_receipt": True,
        "receipt_threshold": D("0"),
    }


async def _policy(session, hr_user):
    await expenses.create_policy(
        session, hr_user, name="Travel", effective_from=date(2026, 4, 1),
        rules=[
            rule("hotel", ViolationActionEnum.REQUIRE_JUSTIFICATION),
            rule("minibar", ViolationActionEnum.BLOCK, "500"),
            rule("taxi", ViolationActionEnum.FLAG, "2000"),
        ],
    )


async def _receipt(session, user, employee):
    return await receipt_store.store_receipt(
        session, user, employee_id=employee.id, data=png_bytes(), filename="../../hotel bill.png"
    )


async def test_violation_justify_and_finance_decision(session, hr_user, people):
    staff, staff_user = people["staff"], people["staff_user"]
    await _policy(session, hr_user)
    receipt = await _receipt(session, staff_user, staff)
    assert receipt.content_type == "image/png"
    assert "/" not in receipt.display_name

    out = await expenses.capture_claim(
        session, staff_user, employee_id=staff.id, category_key="hotel",
        city_tier=CityTierEnum.A, amount=D("9500"), incurred_on=TODAY,
        receipt_document_id=receipt.id,
    )
    assert out["verdict"]["kind"] == "needs_justification"
    assert out["may_submit"] is False
    claim_id = uuid.UUID(out["claim_id"])
    claim = await reads.get_claim(session, staff_user, claim_id)
    assert claim.has_receipt is True

    with pytest.raises(HTTPException) as exc:
        await expenses.submit_claim(session, staff_user, claim_id=claim_id)
    assert exc.value.status_code == 400
    submitted = await expenses.submit_claim(
        session, staff_user, claim_id=claim_id, justification="Only hotel near the client"
    )
    assert submitted.status is ExpenseClaimStatusEnum.SUBMITTED
    assert submitted.flagged_for_approver is True

    queue = await reads.review_queue(session, people["finance_user"], today=TODAY)
    assert claim_id in {c.id for c in queue.claims}

    decided = await expenses.decide_claim(
        session, people["finance_user"], claim_id=claim_id, approve=True, note="OK"
    )
    assert decided.status is ExpenseClaimStatusEnum.APPROVED

    # One receipt, one claim.
    with pytest.raises(HTTPException) as exc:
        await expenses.capture_claim(
            session, staff_user, employee_id=staff.id, category_key="taxi",
            city_tier=CityTierEnum.A, amount=D("100"), incurred_on=TODAY,
            receipt_document_id=receipt.id,
        )
    assert exc.value.status_code == 400


async def test_nobody_decides_their_own(session, hr_user, people):
    finance, finance_user = people["finance"], people["finance_user"]
    await _policy(session, hr_user)
    receipt = await _receipt(session, finance_user, finance)
    out = await expenses.capture_claim(
        session, finance_user, employee_id=finance.id, category_key="taxi",
        city_tier=CityTierEnum.A, amount=D("500"), incurred_on=TODAY,
        receipt_document_id=receipt.id,
    )
    claim_id = uuid.UUID(out["claim_id"])
    await expenses.submit_claim(session, finance_user, claim_id=claim_id)

    queue = await reads.review_queue(session, finance_user, today=TODAY)
    assert claim_id not in {c.id for c in queue.claims}
    assert queue.excluded_own == 1
    with pytest.raises(HTTPException) as exc:
        await expenses.decide_claim(session, finance_user, claim_id=claim_id, approve=True)
    assert exc.value.status_code == 403

    trip = await expenses.create_travel_request(
        session, finance_user, employee_id=finance.id, purpose="Audit",
        from_date=TODAY, to_date=TODAY, advance_requested=D("5000"),
    )
    with pytest.raises(HTTPException) as exc:
        await expenses.decide_travel_request(
            session, finance_user, travel_request_id=trip.id, approve=True
        )
    assert exc.value.status_code == 403
    await expenses.decide_travel_request(session, hr_user, travel_request_id=trip.id, approve=True)
    with pytest.raises(HTTPException) as exc:
        await expenses.issue_advance(
            session, finance_user, travel_request_id=trip.id, amount=D("5000"), issued_on=TODAY
        )
    assert exc.value.status_code == 403
    advance = await expenses.issue_advance(
        session, hr_user, travel_request_id=trip.id, amount=D("5000"), issued_on=TODAY
    )
    with pytest.raises(HTTPException) as exc:
        await expenses.settle_advance(
            session, finance_user, advance_id=advance.id, settled_on=TODAY
        )
    assert exc.value.status_code == 403

    status = await reads.block_status(session, finance_user, employee_id=finance.id, today=TODAY)
    assert status.permitted is True
    advances = await reads.list_advances(session, hr_user, employee_id=finance.id, today=TODAY)
    assert advances[0].outstanding == D("5000.00")


async def test_a_report_submits_whole_and_decides_line_by_line(session, hr_user, people):
    staff, staff_user = people["staff"], people["staff_user"]
    await _policy(session, hr_user)
    report = await expenses.create_report(
        session, staff_user, employee_id=staff.id, title="Pune trip"
    )
    lines = []
    for category, amount in (("taxi", "800"), ("minibar", "900"), ("hotel", "4000")):
        receipt = await _receipt(session, staff_user, staff)
        out = await expenses.capture_claim(
            session, staff_user, employee_id=staff.id, category_key=category,
            city_tier=CityTierEnum.A, amount=D(amount), incurred_on=TODAY,
            receipt_document_id=receipt.id, report_id=report.id,
        )
        lines.append(uuid.UUID(out["claim_id"]))

    with pytest.raises(HTTPException) as exc:
        await expenses.submit_report(session, staff_user, report_id=report.id)
    assert exc.value.status_code == 400
    assert "minibar" in exc.value.detail

    await expenses.cancel_claim(session, staff_user, claim_id=lines[1])
    submitted = await expenses.submit_report(session, staff_user, report_id=report.id)
    assert submitted.status is ExpenseReportStatusEnum.SUBMITTED

    await expenses.decide_claim(session, hr_user, claim_id=lines[0], approve=True)
    await expenses.decide_claim(
        session, hr_user, claim_id=lines[2], approve=False, note="Book through travel desk"
    )
    read = await reads.get_report(session, staff_user, report.id)
    assert read.status is ExpenseReportStatusEnum.PARTIALLY_APPROVED
    assert read.line_count == 2
    assert read.total_amount == D("4800.00")

    with pytest.raises(HTTPException) as exc:
        await reads.get_report(session, people["other_user"], report.id)
    assert exc.value.status_code == 403


async def test_receipts_are_checked_by_their_bytes_and_owner(session, people):
    staff, staff_user = people["staff"], people["staff_user"]
    with pytest.raises(HTTPException) as exc:
        await receipt_store.store_receipt(
            session, staff_user, employee_id=staff.id,
            data=b"<html><script>alert(1)</script></html>", filename="bill.pdf",
        )
    assert exc.value.status_code == 415
    with pytest.raises(HTTPException) as exc:
        await receipt_store.store_receipt(
            session, staff_user, employee_id=staff.id,
            data=b"%PDF-1.4 /OpenAction << /JavaScript (x) >>", filename="bill.pdf",
        )
    assert exc.value.status_code == 415

    receipt = await _receipt(session, staff_user, staff)
    with pytest.raises(HTTPException) as exc:
        await receipt_store.receipt_for_download(session, people["other_user"], receipt.id)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        await expenses.capture_claim(
            session, people["other_user"], employee_id=people["other"].id,
            category_key="taxi", city_tier=CityTierEnum.A, amount=D("100"),
            incurred_on=TODAY, receipt_document_id=receipt.id,
        )
    assert exc.value.status_code == 400


async def test_gst_credits_on_a_claim_are_the_owners(session, hr_user, people):
    staff, staff_user = people["staff"], people["staff_user"]
    out = await expenses.capture_claim(
        session, staff_user, employee_id=staff.id, category_key="hotel",
        city_tier=CityTierEnum.A, amount=D("100"), incurred_on=TODAY,
    )
    claim_id = uuid.UUID(out["claim_id"])
    await gst.capture_credit(
        session, staff_user, vendor_gstin="27AAPFU0939F1ZV", invoice_number="INV-1",
        invoice_date=TODAY, category_key="hotel", taxable_value=D("1000"),
        expense_claim_id=claim_id, employee_id=staff.id, cgst=D("60"), sgst=D("60"),
    )
    assert len(await gst.claim_credits(session, staff_user, claim_id)) == 1
    assert len(await gst.claim_credits(session, hr_user, claim_id)) == 1
    with pytest.raises(HTTPException) as exc:
        await gst.claim_credits(session, people["other_user"], claim_id)
    assert exc.value.status_code == 403


async def test_another_tenant_gets_404(session, other_hr_user, people):
    out = await expenses.capture_claim(
        session, people["staff_user"], employee_id=people["staff"].id, category_key="taxi",
        city_tier=CityTierEnum.A, amount=D("100"), incurred_on=TODAY,
    )
    claim_id = uuid.UUID(out["claim_id"])
    for call in (
        reads.get_claim(session, other_hr_user, claim_id),
        expenses.decide_claim(session, other_hr_user, claim_id=claim_id, approve=True),
        gst.claim_credits(session, other_hr_user, claim_id),
    ):
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 404


# ── Through HTTP ────────────────────────────────────────────────────────────


@pytest.fixture
async def as_user(session: AsyncSession) -> AsyncGenerator:
    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_session] = override_session
    transport = httpx.ASGITransport(app=app)

    def client_for(user: User) -> httpx.AsyncClient:
        app.dependency_overrides[get_current_user] = lambda: user
        return httpx.AsyncClient(transport=transport, base_url="http://testserver")

    try:
        yield client_for
    finally:
        app.dependency_overrides.clear()


async def test_http_gates_and_upload(session, hr_user, people, as_user):
    staff_user = people["staff_user"]
    await _policy(session, hr_user)
    async with as_user(staff_user) as client:
        upload = await client.post(
            "/api/v1/expenses/receipts",
            files={"file": ("bill.pdf", png_bytes(), "application/pdf")},
        )
        assert upload.status_code == 201, upload.text
        assert upload.json()["content_type"] == "image/png"
        receipt_id = upload.json()["id"]

        download = await client.get(f"/api/v1/expenses/receipts/{receipt_id}")
        assert download.status_code == 200
        assert download.headers["content-disposition"].startswith("attachment")

        captured = await client.post(
            "/api/v1/expenses/claims",
            json={"category_key": "taxi", "city_tier": "a", "amount": "2500",
                  "incurred_on": "2026-09-07", "receipt_document_id": receipt_id,
                  "has_receipt": True},
        )
        assert captured.status_code == 201, captured.text
        body = captured.json()
        assert body["verdict"]["kind"] == "flagged" and body["may_submit"] is True
        claim_id = body["claim_id"]

        assert (await client.get("/api/v1/expenses/claims")).status_code == 200
        assert (await client.get("/api/v1/expenses/claims?scope=all")).status_code == 403
        assert (
            await client.post(
                f"/api/v1/expenses/claims/{claim_id}/decision", json={"approve": True}
            )
        ).status_code == 403
        meta = await client.get("/api/v1/expenses/meta")
        assert meta.status_code == 200 and "hotel" in meta.json()["categories"]
        assert (await client.get("/api/v1/expenses/review-queue")).status_code == 403

    async with as_user(people["other_user"]) as client:
        assert (await client.get(f"/api/v1/expenses/claims/{claim_id}")).status_code == 403
        assert (await client.get(f"/api/v1/expenses/receipts/{receipt_id}")).status_code == 403

    async with as_user(hr_user) as client:
        reject = await client.post(
            f"/api/v1/expenses/claims/{claim_id}/decision", json={"approve": False}
        )
        assert reject.status_code == 422
        csv_resp = await client.get(
            "/api/v1/gst/register.csv",
            params={"period_start": "2026-09-01", "period_end": "2026-09-30"},
        )
        assert csv_resp.status_code == 200
        assert csv_resp.text.startswith("section,vendor_gstin")
        bad = await client.post(
            "/api/v1/gst/gstr2b-imports/csv",
            data={"period_label": "2026-09", "imported_on": "2026-10-14"},
            files={"file": ("2b.csv", b"vendor_gstin,invoice_number,invoice_date,taxable_value\n"
                                      b"27AAPFU0939F1ZV,INV-1,notadate,100\n", "text/csv")},
        )
        assert bad.status_code == 200 and bad.json()["imported"] is None
        assert bad.json()["errors"][0]["row"] == 1
