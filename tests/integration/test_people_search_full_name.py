"""A person is found by their full name, not only by one of its parts.

Names are stored as first and last name; a search for "Meera Nair" used to
match neither column and found nobody, in the directory, the people pickers
and Ask Mira alike.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee
from app.models.user import User
from app.services import employee as employee_service
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db


async def test_a_full_name_finds_the_person(
    session: AsyncSession, hr_user: User, employee: Employee
) -> None:
    hr = await as_caller(session, hr_user)
    for term in ("Anita Sharma", "anita sharma", "Anita", "Sharma"):
        rows, _ = await employee_service.list_employees(session, hr, search=term)
        names = [(row.first_name, row.last_name) for row in rows]
        assert ("Anita", "Sharma") in names, term
    nobody, total = await employee_service.list_employees(session, hr, search="Sharma Anita")
    assert nobody == [] and total == 0
