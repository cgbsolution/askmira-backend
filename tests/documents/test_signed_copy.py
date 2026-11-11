"""The signature picture clean-up and printing it into a letter (no database)."""

from __future__ import annotations

import io
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from PIL import Image, ImageDraw

from app.services.documents.signed_copy import prepare_signature, stamp_letter

ACK = (
    "<p><strong>Employee Acknowledgement</strong></p>"
    "<p><strong>Employee Signature: __________________<br/>Date: __________________</strong></p>"
)


def _png(background=(255, 255, 255), ink=(10, 10, 10), size=(600, 200), draw=True) -> bytes:
    image = Image.new("RGB", size, background)
    if draw:
        pen = ImageDraw.Draw(image)
        pen.line([(40, 150), (120, 40), (200, 160), (300, 50), (420, 140)], fill=ink, width=6)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _jpeg_photo() -> bytes:
    """A phone photo: grey-ish paper, blue ink."""
    image = Image.new("RGB", (1200, 900), (196, 190, 182))
    ImageDraw.Draw(image).line([(200, 600), (500, 300), (800, 620)], fill=(30, 40, 110), width=12)
    out = io.BytesIO()
    image.save(out, format="JPEG")
    return out.getvalue()


def test_a_clean_signature_is_cropped_to_the_ink_on_a_transparent_ground() -> None:
    prepared = Image.open(io.BytesIO(prepare_signature(_png())))
    assert prepared.mode == "RGBA"
    assert prepared.width < 600 and prepared.height < 200  # cropped
    corner = prepared.getpixel((0, 0))
    assert corner[3] == 0  # paper is transparent


def test_a_phone_photo_on_grey_paper_works() -> None:
    prepared = Image.open(io.BytesIO(prepare_signature(_jpeg_photo())))
    assert prepared.getpixel((0, 0))[3] == 0


@pytest.mark.parametrize(
    ("data", "said"),
    [
        (b"", "Choose a picture"),
        (b"not an image at all", "not a picture"),
        (b"%PDF-1.4 a pdf", "not a picture"),
        (_png(draw=False), "No signature could be seen"),
        (_png(background=(0, 0, 0), draw=False), "photo, not a signature"),
        (b"x" * (5 * 1024 * 1024 + 1), "larger than 5 MB"),
    ],
    ids=["empty", "text", "pdf", "blank-paper", "all-black", "too-large"],
)
def test_anything_that_is_not_a_signature_is_refused(data: bytes, said: str) -> None:
    with pytest.raises(HTTPException) as caught:
        prepare_signature(data)
    assert caught.value.status_code == 422
    assert said in caught.value.detail


def test_a_dark_photo_is_refused_as_not_a_signature() -> None:
    image = Image.new("RGB", (400, 300), (250, 250, 250))
    ImageDraw.Draw(image).rectangle([(20, 20), (380, 280)], fill=(20, 20, 20))
    out = io.BytesIO()
    image.save(out, format="PNG")
    with pytest.raises(HTTPException) as caught:
        prepare_signature(out.getvalue())
    assert "photo, not a signature" in caught.value.detail


def test_the_signature_and_date_go_into_the_letters_own_lines() -> None:
    signature = prepare_signature(_png())
    stamped = stamp_letter(
        "<p>Body</p>" + ACK,
        typed_name="Anita Sharma",
        accepted_at=datetime(2026, 11, 11, 6, 0, tzinfo=UTC),
        content_hash="a" * 64,
        signature_png=signature,
    )
    assert 'Employee Signature: <img src="data:image/png;base64,' in stamped
    assert "Date: 11 November 2026" in stamped
    assert "____" not in stamped
    assert "Accepted electronically by Anita Sharma on 11 Nov 2026, 11:30 AM IST" in stamped
    assert "aaaaaaaaaaaaaaaa" in stamped


def test_a_letter_without_a_signature_line_gets_an_acknowledgement_block() -> None:
    stamped = stamp_letter(
        "<p>Dear Anita, you are confirmed.</p>",
        typed_name="Anita <b>Sharma</b>",
        accepted_at=datetime(2026, 1, 5, 6, 0, tzinfo=UTC),
        content_hash="b" * 64,
        signature_png=None,
    )
    assert stamped.startswith("<p>Dear Anita, you are confirmed.</p>")
    assert "Employee Acknowledgement" in stamped
    assert "Date:</strong> 5 January 2026" in stamped
    # The typed name stands in for the signature, escaped.
    assert "Anita &lt;b&gt;Sharma&lt;/b&gt;" in stamped and "<b>Sharma" not in stamped
