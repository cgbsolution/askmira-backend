"""Courses: assigned to an audience, answered without seeing the key, certified, reminded."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee, EmploymentStatus
from app.models.learning import CourseKind
from app.models.org import LegalEntity
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services import learning
from app.services.learning import AssignIn, CourseIn, ModuleIn, QuestionIn
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db


async def _person(
    session: AsyncSession, tenant: Tenant, entity: LegalEntity, code: str
) -> tuple[Employee, User]:
    person = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code.title(),
        work_email=f"{code.lower()}@acme-demo.com",
        legal_entity_id=entity.id,
        status=EmploymentStatus.ACTIVE,
    )
    session.add(person)
    await session.flush()
    login = User(
        tenant_id=tenant.id,
        email=f"{code.lower()}.lms@acme-demo.com",
        password_hash="x",
        full_name=code.title(),
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=person.id,
    )
    session.add(login)
    await session.commit()
    return person, await as_caller(session, login)


async def test_a_posh_course_end_to_end(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    asha, asha_login = await _person(session, tenant, legal_entity, "ASHA")
    ravi, ravi_login = await _person(session, tenant, legal_entity, "RAVI")
    course = await learning.create_course(
        session,
        hr_user,
        CourseIn(
            title="POSH awareness 2026",
            kind=CourseKind.POSH,
            validity_months=12,
            pass_mark=50,
            modules=[ModuleIn(title="Our policy", body="Written by the Internal Committee.")],
            quiz=[
                QuestionIn(question="Who do you report to?", options=["IC", "Nobody"], correct=0),
                QuestionIn(question="Is retaliation allowed?", options=["Yes", "No"], correct=1),
            ],
        ),
        request=None,
    )
    course_id = uuid.UUID(course["id"])
    due = date.today() - timedelta(days=3)
    result = await learning.assign(
        session, hr_user, course_id, AssignIn(everyone=True, due_on=due), request=None
    )
    assert result["assigned"] == 2

    mine = await learning.my_assignments(session, asha_login)
    assert mine[0]["overdue"] is True
    opened = await learning.open_course(session, asha_login, uuid.UUID(mine[0]["id"]))
    assert all("correct" not in q for q in opened["content"]["quiz"])
    with pytest.raises(HTTPException):
        await learning.open_course(session, ravi_login, uuid.UUID(mine[0]["id"]))

    failed = await learning.complete(session, asha_login, uuid.UUID(mine[0]["id"]), answers=[1, 0])
    assert failed["passed"] is False and failed["score"] == 0
    passed = await learning.complete(session, asha_login, uuid.UUID(mine[0]["id"]), answers=[0, 1])
    assert passed["passed"] is True and passed["certificate_number"]
    assert passed["expires_on"] is not None

    data, name = await learning.certificate_pdf(session, asha_login, uuid.UUID(mine[0]["id"]))
    assert data[:4] == b"%PDF" and name.endswith(".pdf")
    with pytest.raises(HTTPException):
        await learning.certificate_pdf(session, ravi_login, uuid.UUID(mine[0]["id"]))

    # Assigning again skips anyone already current.
    again = await learning.assign(
        session, hr_user, course_id, AssignIn(everyone=True, due_on=date.today()), request=None
    )
    assert again["assigned"] == 0 and again["already_current"] == 2

    report = await learning.course_report(session, hr_user, course_id)
    assert (report["completed"], report["overdue"]) == (1, 1)

    reminded = await learning.send_overdue_reminders(session, hr_user, today=date.today())
    assert reminded["reminded"] == 1
    twice = await learning.send_overdue_reminders(session, hr_user, today=date.today())
    assert twice["reminded"] == 0
