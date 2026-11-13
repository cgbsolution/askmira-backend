"""The saved-accounts cookie: each account once, the newest token, at most six."""

from __future__ import annotations

import uuid

from app.core import cookies
from app.core.security import create_access_token, create_refresh_token


def _token(user: uuid.UUID) -> str:
    return create_refresh_token(user_id=str(user), tenant_id=str(uuid.uuid4()))


def test_a_new_account_is_added_last_and_a_known_one_keeps_its_place() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    first_a, first_b = _token(a), _token(b)
    saved = cookies.merge_account(cookies.merge_account([], first_a), first_b)
    assert saved == [first_a, first_b]

    renewed_a = _token(a)
    assert cookies.merge_account(saved, renewed_a) == [renewed_a, first_b]


def test_never_more_than_six_and_the_one_just_signed_in_stays() -> None:
    users = [uuid.uuid4() for _ in range(7)]
    saved: list[str] = []
    for user in users:
        saved = cookies.merge_account(saved, _token(user))
    assert len(saved) == cookies.MAX_SAVED_ACCOUNTS
    owners = [cookies.refresh_subject(t) for t in saved]
    assert owners == [str(u) for u in users[1:]]  # the oldest went

    # Renewing the oldest kept account must not push itself out.
    renewed = _token(users[1])
    assert cookies.merge_account(saved, renewed)[0] == renewed


def test_dead_and_foreign_tokens_fall_out() -> None:
    a = uuid.uuid4()
    access = create_access_token(user_id=str(a), tenant_id=None, role="admin")
    saved = cookies.merge_account(["garbage", access], _token(a))
    assert len(saved) == 1
    assert cookies.refresh_subject(saved[0]) == str(a)
    assert cookies.refresh_subject(access) is None


def test_without_account_removes_only_that_one() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    saved = [_token(a), _token(b)]
    assert cookies.without_account(saved, str(a)) == [saved[1]]


def test_six_saved_tokens_fit_in_one_cookie() -> None:
    saved = [_token(uuid.uuid4()) for _ in range(cookies.MAX_SAVED_ACCOUNTS)]
    assert len("~".join(saved)) < 3500
