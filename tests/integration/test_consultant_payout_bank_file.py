"""A bank file for an approved consultant payout run, built and controlled like a salary file."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.crypto import decrypt_secret, encrypt_secret
from app.models.audit import AuditLog
from app.models.consultant_payouts import ConsultantPayoutRun, PayoutRunStatus
from app.models.disbursement import (
    BankFileFormat,
    DisbursementLine,
    DisbursementStatus,
    EmployeeBankAccount,
)
from app.services.consultants import payouts
from app.services.disbursement import consultant as consultant_disbursement
from app.services.disbursement import service as disbursement_service
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, client  # noqa: F401  (fixture)
from tests.integration.test_consultant_payout_run import (
    CUT_OFF,
    PAY_DATE,
    _confirmed,
    _consultant,
    _second_hr,
    _setup,
    _week,
)
from tests.integration.test_disbursement_flow import _profile

pytestmark = requires_db


async def _approved_run(session, tenant, hr_user, legal_entity, *, codes=("CON1", "CON2")):
    """An approved run paying one consultant per code, each on their own account."""
    await _setup(session, tenant, hr_user)
    people = []
    for index, code in enumerate(codes):
        person = await _consultant(session, tenant, legal_entity, code, bank=False)
        session.add(
            EmployeeBankAccount(
                tenant_id=tenant.id,
                employee_id=person.id,
                account_number=encrypt_secret(f"5010012345678{index}", tenant.id),
                ifsc="HDFC0001234",
                account_holder_name=person.full_name,
                is_primary=True,
            )
        )
        await session.commit()
        await _week(session, tenant, person)
        people.append(person)
    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    checker = await _second_hr(session, tenant)
    return run, people, checker


async def _approve(session, checker, run) -> dict:
    return await payouts.approve_run(session, checker, uuid.UUID(run["id"]), confirm=_confirmed)


async def test_bank_file_for_approved_payout_run(session, tenant, hr_user, legal_entity) -> None:
    run, people, checker = await _approved_run(session, tenant, hr_user, legal_entity)
    await _approve(session, checker, run)
    # ICICI's header carries the batch reference.
    profile = await _profile(session, tenant, legal_entity, file_format=BankFileFormat.ICICI)

    batch = await consultant_disbursement.prepare_consultant_batch(
        session,
        hr_user,
        payout_run_id=uuid.UUID(run["id"]),
        bank_profile_id=profile.id,
        value_date=PAY_DATE,
    )

    assert batch.consultant_payout_run_id == uuid.UUID(run["id"])
    assert batch.payroll_run_id is None
    assert batch.status is DisbursementStatus.PENDING_APPROVAL
    assert batch.employee_count == 2
    nets = {line["employee_id"]: Decimal(line["net_payable"]) for line in run["lines"]}
    assert batch.total_amount == sum(nets.values())
    assert batch.file_name == f"ICICI_CON20261005_{uuid.UUID(run['id']).hex[:8]}.txt"
    content = decrypt_secret(batch.encrypted_file, tenant.id) or ""
    assert "CON20261005" in content

    lines = (
        await session.scalars(select(DisbursementLine).where(DisbursementLine.batch_id == batch.id))
    ).all()
    assert {str(line.employee_id): Decimal(str(line.amount)) for line in lines} == nets
    by_person = {line["employee_id"]: uuid.UUID(line["id"]) for line in run["lines"]}
    for line in lines:
        assert line.consultant_payout_line_id == by_person[str(line.employee_id)]
        assert line.payslip_id is None


async def test_draft_run_has_no_bank_file(session, tenant, hr_user, legal_entity) -> None:
    run, _, _ = await _approved_run(session, tenant, hr_user, legal_entity)
    profile = await _profile(session, tenant, legal_entity)

    with pytest.raises(HTTPException) as refused:
        await consultant_disbursement.prepare_consultant_batch(
            session,
            hr_user,
            payout_run_id=uuid.UUID(run["id"]),
            bank_profile_id=profile.id,
            value_date=PAY_DATE,
        )
    assert refused.value.status_code == 409


async def test_second_standing_file_refused(session, tenant, hr_user, legal_entity) -> None:
    run, _, checker = await _approved_run(session, tenant, hr_user, legal_entity)
    await _approve(session, checker, run)
    profile = await _profile(session, tenant, legal_entity)
    kwargs = {
        "payout_run_id": uuid.UUID(run["id"]),
        "bank_profile_id": profile.id,
        "value_date": PAY_DATE,
    }
    await consultant_disbursement.prepare_consultant_batch(session, hr_user, **kwargs)

    with pytest.raises(HTTPException) as refused:
        await consultant_disbursement.prepare_consultant_batch(session, hr_user, **kwargs)
    assert refused.value.status_code == 409
    assert "second file would pay everyone again" in refused.value.detail


async def test_a_line_left_out_is_not_paid(session, tenant, hr_user, legal_entity) -> None:
    run, people, checker = await _approved_run(session, tenant, hr_user, legal_entity)
    left_out = next(line for line in run["lines"] if line["employee_id"] == str(people[1].id))
    await payouts.exclude_line(
        session,
        hr_user,
        uuid.UUID(run["id"]),
        uuid.UUID(left_out["id"]),
        reason="Paid by invoice this month",
    )
    await _approve(session, checker, run)
    profile = await _profile(session, tenant, legal_entity)

    batch = await consultant_disbursement.prepare_consultant_batch(
        session,
        hr_user,
        payout_run_id=uuid.UUID(run["id"]),
        bank_profile_id=profile.id,
        value_date=PAY_DATE,
    )

    lines = (
        await session.scalars(select(DisbursementLine).where(DisbursementLine.batch_id == batch.id))
    ).all()
    assert [line.employee_id for line in lines] == [people[0].id]


async def test_a_consultant_without_a_usable_account_blocks_the_file(
    session, tenant, hr_user, legal_entity
) -> None:
    run, people, checker = await _approved_run(session, tenant, hr_user, legal_entity)
    await _approve(session, checker, run)
    # The account was closed after the run was approved.
    account = await session.scalar(
        select(EmployeeBankAccount).where(EmployeeBankAccount.employee_id == people[0].id)
    )
    account.active = False
    await session.commit()
    profile = await _profile(session, tenant, legal_entity)

    with pytest.raises(disbursement_service.BatchBlockedError) as blocked:
        await consultant_disbursement.prepare_consultant_batch(
            session,
            hr_user,
            payout_run_id=uuid.UUID(run["id"]),
            bank_profile_id=profile.id,
            value_date=PAY_DATE,
        )
    assert blocked.value.batch.status is DisbursementStatus.FAILED
    assert blocked.value.batch.encrypted_file is None


async def test_a_consultant_file_is_approved_released_and_downloaded_like_a_salary_file(
    session, tenant, hr_user, legal_entity
) -> None:
    run, _, checker = await _approved_run(session, tenant, hr_user, legal_entity)
    await _approve(session, checker, run)
    profile = await _profile(session, tenant, legal_entity)
    batch = await consultant_disbursement.prepare_consultant_batch(
        session,
        hr_user,
        payout_run_id=uuid.UUID(run["id"]),
        bank_profile_id=profile.id,
        value_date=PAY_DATE,
    )
    await session.commit()

    # Maker-checker holds for consultants too.
    with pytest.raises(HTTPException) as own:
        await disbursement_service.approve_batch(
            session, hr_user, batch_id=batch.id, file_hash=batch.file_hash
        )
    assert own.value.status_code == 403

    await disbursement_service.approve_batch(
        session, checker, batch_id=batch.id, file_hash=batch.file_hash
    )
    content, _ = await disbursement_service.download_file(session, checker, batch_id=batch.id)
    assert content
    released = await disbursement_service.release_batch(session, checker, batch_id=batch.id)
    assert released.status is DisbursementStatus.RELEASED

    listed = await disbursement_service.list_batches(
        session, hr_user, consultant_payout_run_id=uuid.UUID(run["id"])
    )
    assert [row[0].id for row in listed] == [batch.id]
    assert (
        await disbursement_service.list_batches(session, hr_user, payroll_run_id=uuid.uuid4()) == []
    )
    fetched, _, _ = await disbursement_service.get_batch(session, hr_user, batch.id)
    assert fetched.consultant_payout_run_id == uuid.UUID(run["id"])
    assert len(await disbursement_service.batch_lines(session, hr_user, batch.id)) == 2


async def test_salary_only_operations_refuse_a_consultant_file(
    session, tenant, hr_user, legal_entity
) -> None:
    run, _, checker = await _approved_run(session, tenant, hr_user, legal_entity)
    await _approve(session, checker, run)
    profile = await _profile(session, tenant, legal_entity)
    batch = await consultant_disbursement.prepare_consultant_batch(
        session,
        hr_user,
        payout_run_id=uuid.UUID(run["id"]),
        bank_profile_id=profile.id,
        value_date=PAY_DATE,
    )
    await session.commit()

    attempts = [
        disbursement_service.supplementary_batch(
            session, hr_user, batch_id=batch.id, value_date=PAY_DATE
        ),
        disbursement_service.pay_held(session, hr_user, batch_id=batch.id, value_date=PAY_DATE),
        disbursement_service.reconcile(
            session,
            hr_user,
            batch_id=batch.id,
            responses=[{"account_number": "x", "status": "success"}],
        ),
    ]
    for attempt in attempts:
        with pytest.raises(HTTPException) as refused:
            await attempt
        assert refused.value.status_code == 409
        assert refused.value.detail == "Not available for consultant payouts yet."


async def test_bank_file_api(client, session, tenant, hr_user, legal_entity) -> None:  # noqa: F811
    run, _, checker = await _approved_run(session, tenant, hr_user, legal_entity)
    await _approve(session, checker, run)
    profile = await _profile(session, tenant, legal_entity)

    response = await client.post(
        f"/api/v1/consultant-payouts/{run['id']}/bank-file",
        json={"bank_profile_id": str(profile.id), "value_date": PAY_DATE.isoformat()},
        headers=bearer(hr_user),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["consultant_payout_run_id"] == run["id"]
    assert body["payroll_run_id"] is None
    assert body["employee_count"] == 2

    listed = await client.get(
        "/api/v1/disbursement/batches",
        params={"consultant_payout_run_id": run["id"]},
        headers=bearer(hr_user),
    )
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()] == [body["id"]]


async def _prepared(session, tenant, hr_user, legal_entity):
    run, _, checker = await _approved_run(session, tenant, hr_user, legal_entity)
    await _approve(session, checker, run)
    profile = await _profile(session, tenant, legal_entity)
    batch = await consultant_disbursement.prepare_consultant_batch(
        session,
        hr_user,
        payout_run_id=uuid.UUID(run["id"]),
        bank_profile_id=profile.id,
        value_date=PAY_DATE,
    )
    await session.commit()
    return run, checker, batch


async def test_unsent_consultant_file_can_be_regenerated(
    session, tenant, hr_user, legal_entity
) -> None:
    run, checker, batch = await _prepared(session, tenant, hr_user, legal_entity)
    await disbursement_service.approve_batch(
        session, checker, batch_id=batch.id, file_hash=batch.file_hash
    )
    await session.commit()

    rebuilt = await disbursement_service.regenerate_batch(session, hr_user, batch_id=batch.id)
    await session.commit()

    assert rebuilt.id != batch.id
    assert rebuilt.consultant_payout_run_id == uuid.UUID(run["id"])
    assert rebuilt.payroll_run_id is None
    assert rebuilt.status is DisbursementStatus.PENDING_APPROVAL
    assert rebuilt.approved_by_user_id is None
    assert rebuilt.employee_count == 2
    assert batch.status is DisbursementStatus.FAILED
    assert str(rebuilt.id) in (batch.notes or "")
    entries = (await session.scalars(select(AuditLog).where(AuditLog.entity_id == batch.id))).all()
    assert any("Approval voided" in (e.summary or "") for e in entries)
    # The rebuilt file is the one standing file now.
    with pytest.raises(HTTPException) as again:
        await consultant_disbursement.prepare_consultant_batch(
            session,
            hr_user,
            payout_run_id=uuid.UUID(run["id"]),
            bank_profile_id=batch.bank_profile_id,
            value_date=PAY_DATE,
        )
    assert again.value.status_code == 409


async def test_released_consultant_file_cannot_be_regenerated(
    session, tenant, hr_user, legal_entity
) -> None:
    _, checker, batch = await _prepared(session, tenant, hr_user, legal_entity)
    await disbursement_service.approve_batch(
        session, checker, batch_id=batch.id, file_hash=batch.file_hash
    )
    await disbursement_service.release_batch(session, checker, batch_id=batch.id)
    await session.commit()

    with pytest.raises(HTTPException) as refused:
        await disbursement_service.regenerate_batch(session, hr_user, batch_id=batch.id)
    assert refused.value.status_code == 409
    assert "sent to the bank" in refused.value.detail


async def test_paid_run_gets_no_bank_file(session, tenant, hr_user, legal_entity) -> None:
    run, _, checker = await _approved_run(session, tenant, hr_user, legal_entity)
    await _approve(session, checker, run)
    await payouts.mark_paid(
        session, hr_user, uuid.UUID(run["id"]), paid_on=PAY_DATE, reference="UTR123"
    )
    profile = await _profile(session, tenant, legal_entity)

    with pytest.raises(HTTPException) as refused:
        await consultant_disbursement.prepare_consultant_batch(
            session,
            hr_user,
            payout_run_id=uuid.UUID(run["id"]),
            bank_profile_id=profile.id,
            value_date=PAY_DATE,
        )
    assert refused.value.status_code == 409
    assert refused.value.detail == (
        "This payout run is marked paid. A bank file now would pay everyone again."
    )


async def test_a_file_made_before_the_run_was_marked_paid_stays_readable(
    session, tenant, hr_user, legal_entity
) -> None:
    run, checker, batch = await _prepared(session, tenant, hr_user, legal_entity)
    await disbursement_service.approve_batch(
        session, checker, batch_id=batch.id, file_hash=batch.file_hash
    )
    await session.commit()
    # Marked paid before a standing file stopped that (mark_paid now refuses).
    paid_run = await session.get(ConsultantPayoutRun, uuid.UUID(run["id"]))
    paid_run.status = PayoutRunStatus.PAID
    await session.commit()

    listed = await disbursement_service.list_batches(
        session, hr_user, consultant_payout_run_id=uuid.UUID(run["id"])
    )
    assert [row[0].id for row in listed] == [batch.id]
    fetched, _, _ = await disbursement_service.get_batch(session, hr_user, batch.id)
    assert fetched.id == batch.id
    content, _ = await disbursement_service.download_file(session, checker, batch_id=batch.id)
    assert content
    # Regenerating it would build a fresh file for a paid run, so it is refused too.
    with pytest.raises(HTTPException) as refused:
        await disbursement_service.regenerate_batch(session, hr_user, batch_id=batch.id)
    assert refused.value.status_code == 409


async def test_a_run_with_an_unsent_file_cannot_be_marked_paid(
    session, tenant, hr_user, legal_entity
) -> None:
    run, checker, batch = await _prepared(session, tenant, hr_user, legal_entity)
    run_id = uuid.UUID(run["id"])
    with pytest.raises(HTTPException) as waiting:
        await payouts.mark_paid(session, hr_user, run_id, paid_on=PAY_DATE, reference="UTR1")
    assert waiting.value.status_code == 409
    assert waiting.value.detail == (
        "This run has a bank file that hasn't been sent. Release it or cancel it before "
        "marking the run paid."
    )
    await disbursement_service.approve_batch(
        session, checker, batch_id=batch.id, file_hash=batch.file_hash
    )
    await session.commit()
    with pytest.raises(HTTPException) as approved:
        await payouts.mark_paid(session, hr_user, run_id, paid_on=PAY_DATE, reference="UTR1")
    assert approved.value.status_code == 409

    # Once the file is sent, the run is marked paid with the bank's reference.
    await disbursement_service.release_batch(session, checker, batch_id=batch.id)
    await session.commit()
    paid = await payouts.mark_paid(session, hr_user, run_id, paid_on=PAY_DATE, reference="UTR1")
    assert paid["status"] == "paid"


async def test_a_file_for_a_run_no_longer_approved_is_not_approved_or_released(
    session, tenant, hr_user, legal_entity
) -> None:
    run, checker, batch = await _prepared(session, tenant, hr_user, legal_entity)
    payout_run = await session.get(ConsultantPayoutRun, uuid.UUID(run["id"]))
    payout_run.status = PayoutRunStatus.PAID
    await session.commit()
    with pytest.raises(HTTPException) as approving:
        await disbursement_service.approve_batch(
            session, checker, batch_id=batch.id, file_hash=batch.file_hash
        )
    assert approving.value.status_code == 409

    payout_run.status = PayoutRunStatus.APPROVED
    await session.commit()
    await disbursement_service.approve_batch(
        session, checker, batch_id=batch.id, file_hash=batch.file_hash
    )
    await session.commit()
    payout_run.status = PayoutRunStatus.PAID
    await session.commit()
    with pytest.raises(HTTPException) as releasing:
        await disbursement_service.release_batch(session, checker, batch_id=batch.id)
    assert releasing.value.status_code == 409
    await session.refresh(batch)
    assert batch.status is DisbursementStatus.APPROVED
