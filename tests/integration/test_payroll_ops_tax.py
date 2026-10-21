"""W3 tax gates against a real database.

Proof documents are stored and readable only by their owner and payroll; a
verifier never decides their own proof; taxable income from other modules
reaches the projection; window reminders are raised once; Form 12BB issues
through the letter engine.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditAction, AuditLog
from app.models.exit_benefits import ExitReason, GratuityPayout
from app.models.notifications import Notification
from app.models.tax import DeclarationWindow, ProofStatus, WindowType
from app.models.user import UserRole
from app.services.tax import reminders
from app.services.tax import service as tax_service
from tests.integration.conftest import requires_db
from tests.integration.test_payroll_ops_helpers import (
    PNG_BYTES,
    person,
    storage_root,  # noqa: F401 - fixture
    tds_rule_set,
)

pytestmark = requires_db

FY = "2026-27"


async def _setup(session, tenant, legal_entity, location):
    await tds_rule_set(session, tenant, effective=date(2026, 4, 1))
    session.add_all(
        [
            DeclarationWindow(
                tenant_id=tenant.id,
                financial_year=FY,
                window_type=WindowType.DECLARATION,
                opens_at=date(2026, 4, 1),
                closes_at=date(2027, 3, 31),
                allow_late_submission=True,
            ),
            DeclarationWindow(
                tenant_id=tenant.id,
                financial_year=FY,
                window_type=WindowType.PROOF,
                opens_at=date(2026, 4, 1),
                closes_at=date(2027, 3, 31),
                allow_late_submission=True,
            ),
        ]
    )
    await session.commit()
    owner, owner_user = await person(
        session, tenant, legal_entity, location, code="EMP1", role=UserRole.EMPLOYEE
    )
    colleague, colleague_user = await person(
        session, tenant, legal_entity, location, code="EMP2", role=UserRole.EMPLOYEE
    )
    payroll, payroll_user = await person(
        session, tenant, legal_entity, location, code="PAY1", role=UserRole.SUB_ADMIN
    )
    return owner, owner_user, colleague_user, payroll, payroll_user


async def _submitted(session, user, employee_id):
    declaration = await tax_service.upsert_declaration(
        session,
        user,
        employee_id=employee_id,
        financial_year=FY,
        section="80C",
        sub_category="PPF",
        declared_amount=Decimal("100000"),
    )
    await tax_service.submit_declaration(session, user, declaration.id)
    await session.commit()
    return declaration


async def test_a_proof_document_is_stored_and_readable_only_by_owner_and_payroll(
    session: AsyncSession,
    tenant,
    legal_entity,
    location,
    storage_root,  # noqa: F811
) -> None:
    owner, owner_user, colleague_user, _, payroll_user = await _setup(
        session, tenant, legal_entity, location
    )
    declaration = await _submitted(session, owner_user, owner.id)

    proof = await tax_service.submit_proof(
        session,
        owner_user,
        declaration_id=declaration.id,
        proof_amount=Decimal("60000"),
        data=PNG_BYTES,
        document_name="../../etc/passwd receipt.png",
    )
    await session.commit()
    assert proof.document_storage_key.startswith(f"{tenant.id}/tax-proofs/")
    assert "/" not in proof.document_name and ".." not in proof.document_name

    stored = (storage_root / proof.document_storage_key).read_bytes()
    assert PNG_BYTES not in stored, "stored encrypted, not as uploaded"

    data, mime, _ = await tax_service.proof_document(session, owner_user, proof.id)
    assert data == PNG_BYTES and mime == "image/png"

    with pytest.raises(HTTPException) as exc:
        await tax_service.proof_document(session, colleague_user, proof.id)
    assert exc.value.status_code == 403

    await tax_service.proof_document(session, payroll_user, proof.id)
    await session.commit()
    reads = await session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == AuditAction.SENSITIVE_READ, AuditLog.entity_id == proof.id)
    )
    assert reads == 1, "payroll opening someone's proof is recorded; the owner's own read is not"


async def test_an_upload_that_is_not_an_accepted_type_is_refused(
    session: AsyncSession,
    tenant,
    legal_entity,
    location,
    storage_root,  # noqa: F811
) -> None:
    owner, owner_user, *_ = await _setup(session, tenant, legal_entity, location)
    declaration = await _submitted(session, owner_user, owner.id)
    with pytest.raises(HTTPException) as exc:
        await tax_service.submit_proof(
            session,
            owner_user,
            declaration_id=declaration.id,
            proof_amount=Decimal("1"),
            data=b"<html>not a pdf</html>",
            document_name="x.pdf",
        )
    assert exc.value.status_code == 422


async def test_a_colleague_cannot_attach_a_proof_to_someone_elses_declaration(
    session: AsyncSession,
    tenant,
    legal_entity,
    location,
    storage_root,  # noqa: F811
) -> None:
    owner, owner_user, colleague_user, *_ = await _setup(session, tenant, legal_entity, location)
    declaration = await _submitted(session, owner_user, owner.id)
    with pytest.raises(HTTPException) as exc:
        await tax_service.submit_proof(
            session,
            colleague_user,
            declaration_id=declaration.id,
            proof_amount=Decimal("1"),
            data=PNG_BYTES,
            document_name="x.png",
        )
    assert exc.value.status_code == 403


async def test_nobody_verifies_a_proof_against_their_own_declaration(
    session: AsyncSession,
    tenant,
    legal_entity,
    location,
    storage_root,  # noqa: F811
) -> None:
    _, _, _, payroll, payroll_user = await _setup(session, tenant, legal_entity, location)
    declaration = await _submitted(session, payroll_user, payroll.id)
    proof = await tax_service.submit_proof(
        session,
        payroll_user,
        declaration_id=declaration.id,
        proof_amount=Decimal("50000"),
        data=PNG_BYTES,
        document_name="own.png",
    )
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await tax_service.verify_proof(
            session,
            payroll_user,
            proof.id,
            status_decision=ProofStatus.VERIFIED,
            accepted_amount=Decimal(0),
            remarks="",
        )
    assert exc.value.status_code == 403


async def test_the_queue_names_the_employee_and_the_section(
    session: AsyncSession,
    tenant,
    legal_entity,
    location,
    storage_root,  # noqa: F811
) -> None:
    owner, owner_user, _, _, payroll_user = await _setup(session, tenant, legal_entity, location)
    declaration = await _submitted(session, owner_user, owner.id)
    await tax_service.submit_proof(
        session,
        owner_user,
        declaration_id=declaration.id,
        proof_amount=Decimal("100"),
        data=PNG_BYTES,
        document_name="a.png",
    )
    await session.commit()
    rows = await tax_service.proof_queue(session, payroll_user, financial_year=FY)
    assert len(rows) == 1
    assert rows[0]["employee_code"] == "EMP1"
    assert rows[0]["section"] == "80C"
    assert rows[0]["has_document"] is True


async def test_taxable_gratuity_reaches_the_projection(
    session: AsyncSession, tenant, legal_entity, location
) -> None:
    """08 §4: the excess above the exemption provably reaches the TDS computation."""
    owner, owner_user, *_ = await _setup(session, tenant, legal_entity, location)
    before = await tax_service.live_preview(session, owner_user, owner.id, FY)

    session.add(
        GratuityPayout(
            tenant_id=tenant.id,
            employee_id=owner.id,
            exit_date=date(2026, 9, 30),
            exit_reason=ExitReason.RESIGNATION,
            service_days=3000,
            service_years=Decimal(8),
            monthly_wages=Decimal(50000),
            gross_amount=Decimal(3500000),
            exempt_amount=Decimal(2000000),
            taxable_amount=Decimal(1500000),
        )
    )
    await session.commit()

    after = await tax_service.live_preview(session, owner_user, owner.id, FY)
    assert after.gross_annual_income == before.gross_annual_income + Decimal(1500000)
    assert after.annual_tax_liability > before.annual_tax_liability
    assert [line["code"] for line in after.other_income] == ["GRATUITY_TAXABLE"]


async def test_window_reminders_are_raised_once_per_day_and_kind(
    session: AsyncSession, tenant, legal_entity, location
) -> None:
    today = date.today()
    await tds_rule_set(session, tenant, effective=date(2000, 4, 1))
    session.add(
        DeclarationWindow(
            tenant_id=tenant.id,
            financial_year="2099-00",
            window_type=WindowType.DECLARATION,
            opens_at=today - timedelta(days=10),
            closes_at=today + timedelta(days=7),
            reminder_days_before=[7, 1],
        )
    )
    await session.commit()
    await person(session, tenant, legal_entity, location, code="EMP9", role=UserRole.EMPLOYEE)

    first = await reminders.send_window_reminders(session, tenant.id, today=today)
    assert first.windows_checked == 1
    assert any("tax.window.opened" in event for event in first.events)
    assert any("tax.window.closing" in event for event in first.events)

    second = await reminders.send_window_reminders(session, tenant.id, today=today)
    assert second.events == [], "nothing is sent twice"

    rows = await session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.tenant_id == tenant.id,
            Notification.event_key.in_([reminders.EVENT_OPENED, reminders.EVENT_CLOSING]),
        )
    )
    assert rows == first.notifications_raised
    assert rows >= 2


async def test_form_12bb_issues_through_the_letter_engine(
    session: AsyncSession,
    tenant,
    legal_entity,
    location,
    storage_root,  # noqa: F811
) -> None:
    owner, owner_user, colleague_user, *_ = await _setup(session, tenant, legal_entity, location)
    await _submitted(session, owner_user, owner.id)

    letter = await tax_service.issue_form_12bb(
        session,
        owner_user,
        employee_id=owner.id,
        financial_year=FY,
        parent_name="R. Test",
        place="Bengaluru",
    )
    await session.commit()
    assert "80C (PPF): declared 1,00,000.00, accepted 0.00" in letter.merged_html
    assert "Not claimed" in letter.merged_html

    listed = await tax_service.list_form_12bb(session, owner_user, owner.id, FY)
    assert [row.id for row in listed] == [letter.id]

    data, name = await tax_service.form_12bb_pdf(session, owner_user, letter.id)
    assert data.startswith(b"%PDF") and name.endswith(".pdf")

    with pytest.raises(HTTPException) as exc:
        await tax_service.form_12bb_pdf(session, colleague_user, letter.id)
    assert exc.value.status_code == 403


async def test_form_12bb_refuses_without_a_pan(
    session: AsyncSession, tenant, legal_entity, location
) -> None:
    await _setup(session, tenant, legal_entity, location)
    nopan, nopan_user = await person(
        session, tenant, legal_entity, location, code="NOPAN", role=UserRole.EMPLOYEE, pan=None
    )
    with pytest.raises(tax_service.TaxWorkflowError):
        await tax_service.issue_form_12bb(
            session,
            nopan_user,
            employee_id=nopan.id,
            financial_year=FY,
            parent_name="X",
            place="Y",
        )


async def test_a_moved_window_is_audited(session: AsyncSession, tenant, hr_user) -> None:
    await tax_service.upsert_window(
        session,
        hr_user,
        financial_year=FY,
        window_type=WindowType.PROOF,
        opens_at=date(2027, 1, 1),
        closes_at=date(2027, 1, 31),
        allow_late_submission=False,
        reminder_days_before=[1, 7, 7],
    )
    await session.commit()
    window = await tax_service.window_for(session, hr_user, FY, WindowType.PROOF)
    assert window.reminder_days_before == [7, 1]
    entry = await session.scalar(
        select(AuditLog).where(AuditLog.entity_type == "declaration_window")
    )
    assert entry is not None and entry.action is AuditAction.RULE_CHANGE
    assert datetime.now(UTC) - entry.created_at < timedelta(minutes=5)
