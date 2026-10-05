"""Approving a proposal applies it to the record; reverting restores the record."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import HTTPException

from app.models import AttendanceRegularisation, AttendanceStatus, UserRole
from app.models.attendance import RegularisationStatus
from app.services.autonomy import service as autonomy
from tests.integration.conftest import requires_db
from tests.integration.platform_support import make_person

pytestmark = requires_db


async def _regularisation(session, tenant, person):
    row = AttendanceRegularisation(
        tenant_id=tenant.id,
        employee_id=person.employee_id,
        attendance_date=date(2026, 9, 1),
        requested_status=AttendanceStatus.PRESENT,
        reason="Missed punch",
    )
    session.add(row)
    await session.commit()
    return row


async def _propose(session, hr, row, change):
    return await autonomy.propose(
        session,
        hr,
        action_key="attendance.regularise_missing_punch",
        subject_type="attendance_regularisation",
        subject_id=row.id,
        proposed_change=change,
        previous_state={"status": "pending"},
        reasoning="Badge log shows the person on site all day.",
        citations=("attendance_punches:p1",),
        confidence=0.9,
    )


async def test_revert_restores_the_record(session, tenant):
    hr = await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    person = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Meera Nair")
    row = await _regularisation(session, tenant, person)

    proposal = await _propose(session, hr, row, {"status": "approved"})
    if proposal.status.value == "pending":
        await autonomy.decide_proposal(session, hr, proposal_id=proposal.id, approve=True)
    await session.refresh(row)
    await session.refresh(proposal)
    assert row.status is RegularisationStatus.APPROVED
    assert proposal.previous_state == {"status": "pending"}

    outcome: dict = {}
    await autonomy.revert(
        session, hr, proposal_id=proposal.id, reason="Wrong day", outcome=outcome
    )
    await session.refresh(row)
    assert outcome["restored_to_record"] is True
    assert row.status is RegularisationStatus.PENDING


async def test_a_proposal_cannot_touch_an_unlisted_column(session, tenant):
    hr = await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    person = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Meera Nair")
    row = await _regularisation(session, tenant, person)
    with pytest.raises(HTTPException) as exc:
        proposal = await _propose(session, hr, row, {"employee_id": str(hr.employee_id)})
        await autonomy.decide_proposal(session, hr, proposal_id=proposal.id, approve=True)
    assert exc.value.status_code == 400
    await session.rollback()
    await session.refresh(row)
    assert row.employee_id == person.employee_id
