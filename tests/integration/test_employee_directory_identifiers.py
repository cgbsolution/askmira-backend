"""The directory never reads the encrypted identifiers it does not show.

A copy of the data whose encryption key differs (a developer's machine on the
shared database, a restored backup) cannot decrypt PAN, Aadhaar or bank
numbers. The directory list and its export must still work there: neither
shows those numbers, so neither should decrypt them.
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from sqlalchemy import text

from app.core.crypto import _FIELD_PREFIX
from app.models.user import UserRole
from tests.integration import platform_support
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, make_person

pytestmark = requires_db

client = platform_support.client

API = "/api/v1"


async def test_the_directory_loads_when_an_identifier_cannot_be_decrypted(client, session, tenant):
    hr = await make_person(session, tenant, role=UserRole.ADMIN, name="Priya Hr")
    other = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Ravi Kumar")
    headers, other_id = bearer(hr), other.employee_id
    # Written under another key: decrypting it raises.
    junk = _FIELD_PREFIX + Fernet(Fernet.generate_key()).encrypt(b"ABCPV1234F").decode()
    await session.execute(
        text(
            "update employees set pan = :junk, aadhaar_number = :junk, "
            "bank_account_number = :junk where id = :id"
        ),
        {"junk": junk, "id": other_id},
    )
    await session.commit()
    session.expire_all()

    page = await client.get(f"{API}/employees?page=1&page_size=25", headers=headers)
    assert page.status_code == 200, page.text
    codes = {row["employee_code"] for row in page.json()["items"]}
    assert len(codes) == 2

    found = await client.get(f"{API}/employees?search=Ravi", headers=headers)
    assert found.status_code == 200, found.text
    assert [row["first_name"] for row in found.json()["items"]] == ["Ravi"]

    csv = await client.get(f"{API}/employees/export", headers=headers)
    assert csv.status_code == 200, csv.text
    assert "Ravi" in csv.text
