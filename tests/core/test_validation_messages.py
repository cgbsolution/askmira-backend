"""The 422 the frontend shows in a toast reads as a sentence."""

from __future__ import annotations

from app.main import _problem


def test_a_field_error_names_the_field() -> None:
    error = {"loc": ("body", "reason"), "msg": "Field required", "type": "missing"}
    assert _problem(error) == "reason: Field required"


def test_a_missing_body_is_a_sentence_not_a_bare_colon() -> None:
    error = {"loc": ("body",), "msg": "Field required", "type": "missing"}
    assert _problem(error) == "This request needs a body, and none was sent."


def test_an_error_without_a_field_keeps_its_message() -> None:
    error = {"loc": ("body",), "msg": "Value error, the dates are reversed", "type": "value_error"}
    assert _problem(error) == "Value error, the dates are reversed"
