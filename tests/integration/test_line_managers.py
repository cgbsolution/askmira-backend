"""Managing people is a place in the org chart, not a role.

An employee becomes a manager the moment somebody reports to them and stops
being one when their last report moves — on the next request, with no role
change and no job in between.
"""

from __future__ import annotations

from app.models import Employee, UserRole
from tests.integration import platform_support
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, make_person

pytestmark = requires_db

client = platform_support.client

SESSION = "/api/v1/auth/session"
# Gated on HR or a line manager; the tenant fixture has the leave module on.
TEAM_TODAY = "/api/v1/attendance/team/today"


async def _team(session, tenant):
    boss = await make_person(
        session, tenant, role=UserRole.EMPLOYEE, name="Kavya Boss", password="Correct-Horse-9"
    )
    boss_emp = await session.get(Employee, boss.employee_id)
    report = await make_person(
        session, tenant, role=UserRole.EMPLOYEE, name="Ravi Report", manager=boss_emp
    )
    return boss, report


async def test_somebody_with_a_report_is_a_manager_without_a_manager_role(
    client, session, tenant
):
    boss, report = await _team(session, tenant)

    mine = (await client.get(SESSION, headers=bearer(boss))).json()["user"]
    assert mine["role"] == "employee"
    assert mine["is_manager"] is True
    assert (await client.get(TEAM_TODAY, headers=bearer(boss))).status_code == 200

    theirs = (await client.get(SESSION, headers=bearer(report))).json()["user"]
    assert theirs["is_manager"] is False
    assert (await client.get(TEAM_TODAY, headers=bearer(report))).status_code == 403


async def test_signing_in_says_so_too(client, session, tenant):
    boss, _ = await _team(session, tenant)
    response = await client.post(
        "/api/v1/auth/login", json={"email": boss.email, "password": "Correct-Horse-9"}
    )
    assert response.status_code == 200
    assert response.json()["user"]["is_manager"] is True


async def test_moving_the_last_report_takes_manager_access_away_at_once(
    client, session, tenant
):
    boss, report = await _team(session, tenant)
    assert (await client.get(TEAM_TODAY, headers=bearer(boss))).status_code == 200

    moved = await session.get(Employee, report.employee_id)
    moved.manager_id = None
    await session.commit()

    assert (await client.get(TEAM_TODAY, headers=bearer(boss))).status_code == 403
    assert (await client.get(SESSION, headers=bearer(boss))).json()["user"]["is_manager"] is False


async def test_hr_reaches_team_pages_without_reports(client, session, tenant):
    sub_admin = await make_person(session, tenant, role=UserRole.SUB_ADMIN, name="Hari Ops")
    assert (await client.get(TEAM_TODAY, headers=bearer(sub_admin))).status_code == 200
    mine = (await client.get(SESSION, headers=bearer(sub_admin))).json()["user"]
    assert mine["role"] == "sub_admin"
    assert mine["is_manager"] is False
