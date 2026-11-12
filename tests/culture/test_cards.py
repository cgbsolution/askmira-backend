"""The greeting card: drawn the same for any wording, photo or none."""

from __future__ import annotations

import io

import pytest
from PIL import Image
from pydantic import ValidationError

from app.services.culture.cards import (
    PRESETS,
    CardDesign,
    CardPerson,
    fill,
    ordinal,
    presets_for,
    render_card,
)


def _person(**overrides) -> CardPerson:
    values = {
        "first_name": "Ritica",
        "full_name": "Ritica Sharma",
        "company": "CGB Solutions",
        "years": 3,
        "designation": "Analyst",
        "department": "Finance",
    }
    values.update(overrides)
    return CardPerson(**values)


def _photo() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (400, 600), (200, 150, 120)).save(buffer, "JPEG")
    return buffer.getvalue()


@pytest.mark.parametrize(
    "n,expected",
    [
        (1, "1st"),
        (2, "2nd"),
        (3, "3rd"),
        (4, "4th"),
        (11, "11th"),
        (12, "12th"),
        (13, "13th"),
        (21, "21st"),
        (22, "22nd"),
        (101, "101st"),
        (111, "111th"),
    ],
)
def test_ordinals(n: int, expected: str) -> None:
    assert ordinal(n) == expected


def test_placeholders_are_filled_per_person() -> None:
    design = CardDesign(name_style="full")
    text = "{name} / {first_name} / {years_ordinal} / {years_label} / {company} / {department}"
    assert fill(text, design, _person()) == (
        "Ritica Sharma / Ritica / 3rd / 3 years / CGB Solutions / Finance"
    )
    assert fill("{years_label}", design, _person(years=1)) == "1 year"


def test_an_unknown_placeholder_is_left_as_typed() -> None:
    assert fill("Hi {nickname}", CardDesign(), _person()) == "Hi {nickname}"


def test_an_empty_ordinal_leaves_no_double_space() -> None:
    assert fill("Happy {years_ordinal} birthday", CardDesign(), _person(years=None)) == (
        "Happy birthday"
    )


def test_every_preset_draws_a_square_jpeg() -> None:
    for preset in PRESETS:
        data = render_card(preset.design, _person(photo=_photo()))
        image = Image.open(io.BytesIO(data))
        assert image.format == "JPEG"
        assert image.size == (1080, 1080), preset.key


def test_both_occasions_have_presets() -> None:
    assert presets_for("birthday") and presets_for("work_anniversary")
    assert all(p.kind in ("birthday", "work_anniversary") for p in PRESETS)


def test_a_very_long_message_and_name_still_draw() -> None:
    design = CardDesign(message="Thank you " * 40, heading="W" * 60, title="A" * 70)
    data = render_card(design, _person(full_name="A" * 120, first_name="B" * 120))
    assert Image.open(io.BytesIO(data)).size == (1080, 1080)


def test_a_broken_photo_or_logo_does_not_stop_the_card() -> None:
    data = render_card(CardDesign(), _person(photo=b"not an image", logo=b"\x89PNG broken"))
    assert Image.open(io.BytesIO(data)).size == (1080, 1080)


def test_with_and_without_a_photo_differ() -> None:
    design = CardDesign()
    assert render_card(design, _person(photo=_photo())) != render_card(design, _person())


def test_the_card_is_drawn_the_same_every_time() -> None:
    """Confetti is seeded: a re-render is the same picture, so previews do not flicker."""
    design = presets_for("birthday")[0].design
    assert render_card(design, _person()) == render_card(design, _person())


@pytest.mark.parametrize("colour", ["red", "#FFF", "#GGGGGG", "", "#12345678", "javascript:"])
def test_colours_must_be_six_digit_hex(colour: str) -> None:
    with pytest.raises(ValidationError):
        CardDesign(background_colour=colour)


def test_wording_has_a_length_limit() -> None:
    with pytest.raises(ValidationError):
        CardDesign(message="x" * 401)
    with pytest.raises(ValidationError):
        CardDesign(layout="poster")
