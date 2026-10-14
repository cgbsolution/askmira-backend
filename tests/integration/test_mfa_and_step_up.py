"""Two-step sign-in, and fresh proof of identity before money moves.

What these hold, over HTTP:

- with two-step sign-in on, a right password alone returns no session — only
  a short-lived token for the second step, which a code completes;
- a code cannot be replayed, and a recovery code works exactly once;
- a guarded action refuses with 428 until the person re-proves who they are
  (password and, with two-step on, a code); the refusal for a person who may
  not act at all comes first, as a 403, without asking for anything;
- an admin can reset somebody else's two-step sign-in, not their own.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import totp
from app.core.security import create_access_token, hash_password
from app.db.session import get_session
from app.main import app
from app.models.user import User, UserRole
from tests.integration.conftest import requires_db

pytestmark = requires_db

PASSWORD = "Correct-Horse-42"
PF_RULES = {
    "wage_ceiling": 15000, "employee_rate": 12, "employer_rate": 12,
    "eps_wage_ceiling": 15000, "edli_wage_ceiling": 15000, "eps_rate": 8.33,
}


@pytest.fixture
async def client(session: AsyncSession):
    async def override():
        yield session

    app.dependency_overrides[get_session] = override
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


async def _user(session, tenant, email: str, role: UserRole) -> User:
    user = User(
        tenant_id=tenant.id, email=email, password_hash=hash_password(PASSWORD),
        full_name=email.split("@")[0].title(), role=role, is_active=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


def _auth(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
    )
    return {"Authorization": f"Bearer {token}"}


async def _turn_on(client, user) -> tuple[str, list[str]]:
    setup = await client.post("/api/v1/auth/mfa/setup", headers=_auth(user))
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    assert setup.json()["otpauth_uri"].startswith("otpauth://totp/")
    wrong = await client.post(
        "/api/v1/auth/mfa/enable", json={"code": "000000"}, headers=_auth(user)
    )
    assert wrong.status_code == 422
    enabled = await client.post(
        "/api/v1/auth/mfa/enable",
        json={"code": totp.code_at(secret, datetime.now(UTC))},
        headers=_auth(user),
    )
    assert enabled.status_code == 200, enabled.text
    return secret, enabled.json()["recovery_codes"]


async def test_sign_in_needs_the_code_once_it_is_on(client, session, tenant) -> None:
    user = await _user(session, tenant, "meera@acme-demo.com", UserRole.ADMIN)

    plain = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
    )
    assert plain.status_code == 200 and plain.json()["access_token"]

    secret, recovery = await _turn_on(client, user)
    assert len(recovery) == 10
    status = (await client.get("/api/v1/auth/mfa", headers=_auth(user))).json()
    assert status["enabled"] is True and status["recovery_codes_left"] == 10

    first = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
    )
    assert first.status_code == 200
    body = first.json()
    assert body["mfa_required"] is True and "access_token" not in body
    mfa_token = body["mfa_token"]

    # The step token is not a session.
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {mfa_token}"})
    ).status_code == 401

    wrong = await client.post(
        "/api/v1/auth/login/mfa", json={"mfa_token": mfa_token, "code": "123456"}
    )
    assert wrong.status_code == 401

    # The enable step used the current code; the next step's is still in the window.
    next_code = totp.code_at(secret, datetime.now(UTC) + timedelta(seconds=30))
    done = await client.post(
        "/api/v1/auth/login/mfa", json={"mfa_token": mfa_token, "code": next_code}
    )
    assert done.status_code == 200, done.text
    assert done.json()["access_token"]

    replay = await client.post(
        "/api/v1/auth/login/mfa", json={"mfa_token": mfa_token, "code": next_code}
    )
    assert replay.status_code == 401, "a code is good once"

    with_recovery = await client.post(
        "/api/v1/auth/login/mfa", json={"mfa_token": mfa_token, "code": recovery[0].lower()}
    )
    assert with_recovery.status_code == 200
    again = await client.post(
        "/api/v1/auth/login/mfa", json={"mfa_token": mfa_token, "code": recovery[0]}
    )
    assert again.status_code == 401, "a recovery code is good once"


async def test_a_rule_change_waits_for_fresh_proof(client, session, tenant) -> None:
    maker = await _user(session, tenant, "priya@acme-demo.com", UserRole.ADMIN)
    checker = await _user(session, tenant, "ananya@acme-demo.com", UserRole.ADMIN)
    secret, _codes = await _turn_on(client, checker)

    created = await client.post(
        "/api/v1/payroll/rule-sets",
        json={
            "rule_type": "pf", "jurisdiction": "IN", "effective_from": str(date(2026, 4, 1)),
            "rules": PF_RULES, "source_reference": "EPF Scheme 1952, para 29",
        },
        headers=_auth(maker),
    )
    assert created.status_code in (200, 201), created.text
    url = f"/api/v1/payroll/rule-sets/{created.json()['id']}/approve"

    # The author is refused for who they are, before any password is asked for.
    own = await client.post(url, json={"attest": True}, headers=_auth(maker))
    assert own.status_code == 403

    asked = await client.post(url, json={"attest": True}, headers=_auth(checker))
    assert asked.status_code == 428
    assert asked.headers["X-Step-Up-Action"] == "payroll.rule_change"

    challenge = (
        await client.post(
            "/api/v1/opsec/step-up", json={"action_key": "payroll.rule_change"},
            headers=_auth(checker),
        )
    ).json()
    assert challenge["required"] is True and challenge["satisfied"] is False

    # The password alone is not enough with two-step sign-in on.
    password_only = await client.post(
        f"/api/v1/opsec/step-up/{challenge['challenge_id']}",
        json={"password": PASSWORD}, headers=_auth(checker),
    )
    assert password_only.json()["satisfied"] is False
    still = await client.post(url, json={"attest": True}, headers=_auth(checker))
    assert still.status_code == 428

    proved = await client.post(
        f"/api/v1/opsec/step-up/{challenge['challenge_id']}",
        json={
            "password": PASSWORD,
            "code": totp.code_at(secret, datetime.now(UTC) + timedelta(seconds=30)),
        },
        headers=_auth(checker),
    )
    assert proved.json()["satisfied"] is True
    assert proved.json()["checked"] == ["password", "authenticator code"]

    approved = await client.post(url, json={"attest": True}, headers=_auth(checker))
    assert approved.status_code == 200, approved.text
    assert approved.json()["is_approved"] is True


async def test_an_admin_resets_others_but_not_themselves(client, session, tenant) -> None:
    admin = await _user(session, tenant, "kavya@acme-demo.com", UserRole.ADMIN)
    staff = await _user(session, tenant, "rohan@acme-demo.com", UserRole.SUB_ADMIN)
    await _turn_on(client, staff)
    await _turn_on(client, admin)

    own = await client.post(f"/api/v1/auth/mfa/reset/{admin.id}", headers=_auth(admin))
    assert own.status_code == 403
    other = await client.post(f"/api/v1/auth/mfa/reset/{staff.id}", headers=_auth(admin))
    assert other.status_code == 204
    status = (await client.get("/api/v1/auth/mfa", headers=_auth(staff))).json()
    assert status["enabled"] is False
    # A sub-admin cannot reset anybody's.
    denied = await client.post(f"/api/v1/auth/mfa/reset/{admin.id}", headers=_auth(staff))
    assert denied.status_code == 403
