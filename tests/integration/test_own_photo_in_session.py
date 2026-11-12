"""The signed-in person's own photo travels with their session.

The portal's header, sidebar, home greeting and Ask Miraa chat draw the
signed-in person from `/auth/session`; without the photo there they showed
initials even after the person uploaded a picture on My profile.
"""

from __future__ import annotations

import uuid

from app.models.user import User, UserRole
from app.services import auth as auth_service


async def _login_for(session, employee) -> User:
    user = User(
        tenant_id=employee.tenant_id,
        email=f"{employee.employee_code.lower()}-{uuid.uuid4().hex[:4]}@acme.test",
        password_hash="not-a-real-hash",
        full_name=employee.full_name,
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def test_session_carries_the_persons_own_photo(session, employee):
    me = await _login_for(session, employee)
    assert (await auth_service.user_read(session, me)).photo_url is None

    employee.photo_url = f"/api/v1/employees/{employee.id}/photo?v=abcd1234"
    await session.commit()
    read = await auth_service.user_read(session, me)
    assert read.photo_url == employee.photo_url

    # A new upload gets a new address, so every avatar picks up the new picture.
    employee.photo_url = f"/api/v1/employees/{employee.id}/photo?v=ef567890"
    await session.commit()
    assert (await auth_service.user_read(session, me)).photo_url.endswith("v=ef567890")

    employee.photo_url = None
    await session.commit()
    assert (await auth_service.user_read(session, me)).photo_url is None


async def test_a_login_without_an_employee_record_has_no_photo(session, hr_user):
    hr_user.employee_id = None
    assert (await auth_service.user_read(session, hr_user)).photo_url is None
