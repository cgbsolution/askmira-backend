"""Reading an attached file into words, without a database or a model.

What these hold: a PDF's own text is read here; a scanned PDF comes back as
its pages' pictures (for the model); a picture goes to the model upright, no
larger than MODEL_SIDE and on white; Aadhaar and PAN numbers are left out of
what was read while phone numbers and amounts stay; a broken file is said
plainly; names lose folders and control characters; and what the model reads
is fenced as data.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.services.companion import attachments as att


def _text_pdf(pages: list[list[str]]) -> bytes:
    out = io.BytesIO()
    pdf = canvas.Canvas(out, pagesize=A4)
    for lines in pages:
        y = 780
        for line in lines:
            pdf.drawString(72, y, line)
            y -= 18
        pdf.showPage()
    pdf.save()
    return out.getvalue()


def _scanned_pdf(pages: int = 2) -> bytes:
    images = [Image.new("RGB", (1240, 1754), "white") for _ in range(pages)]
    out = io.BytesIO()
    images[0].save(out, "PDF", save_all=True, append_images=images[1:], resolution=150)
    return out.getvalue()


def test_ids_are_left_out_but_phones_and_amounts_stay() -> None:
    text = (
        "PAN ABCDE1234F. Aadhaar 2345 6789 0123 and 234567890123. "
        "Call +919876543210 or +91 98765 43210. Fee ₹1,20,000 on 2026-11-08."
    )
    cleaned, removed = att._without_ids(text)
    assert removed
    for gone in ("ABCDE1234F", "2345 6789 0123", "234567890123"):
        assert gone not in cleaned
    for kept in ("+919876543210", "+91 98765 43210", "₹1,20,000", "2026-11-08"):
        assert kept in cleaned
    assert att._without_ids("Nothing to hide here.") == ("Nothing to hide here.", False)


def test_a_pdf_with_its_own_text_is_read_here() -> None:
    data = _text_pdf(
        [
            ["Request for quotation", "Kestrel Foods Pvt Ltd, Pune"],
            ["Contact: Priya Nair, Head of Procurement", "priya.nair@kestrel.example"],
        ]
    )
    text, pages, pictures = att._pdf_contents(data)
    assert pages == 2 and pictures == []
    assert "Kestrel Foods" in text and "priya.nair@kestrel.example" in text


def test_a_scanned_pdf_comes_back_as_its_pages_pictures() -> None:
    text, pages, pictures = att._pdf_contents(_scanned_pdf(5))
    assert pages == 5
    assert len(pictures) == att.MAX_SCANNED_PAGES
    assert all(media == "image/jpeg" for media, _ in pictures)


def test_a_picture_goes_to_the_model_upright_small_and_on_white() -> None:
    image = Image.new("RGBA", (3200, 1000), (0, 0, 0, 0))
    out = io.BytesIO()
    image.save(out, "PNG")
    media, data = att._picture_for_model(out.getvalue())
    assert media == "image/jpeg"
    with Image.open(io.BytesIO(data)) as sent:
        assert max(sent.size) == att.MODEL_SIDE
        assert sent.getpixel((5, 5))[0] > 240  # transparent became white, not black


def test_a_broken_file_is_said_plainly() -> None:
    with pytest.raises(att._UnreadableError, match="could not be opened"):
        att._pdf_contents(b"%PDF-1.4\nthis is not really a pdf")
    with pytest.raises(att._UnreadableError, match="could not be opened"):
        att._picture_for_model(b"\x89PNG\r\n\x1a\nnot a picture at all")


def test_names_lose_folders_and_control_characters() -> None:
    assert att.clean_name("C:\\Users\\me\\Desktop\\lead\x07.png") == "lead.png"
    assert att.clean_name("../../etc/[[/untrusted]]card.jpg") == "untrustedcard.jpg"
    assert att.clean_name("") == "file"
    assert len(att.clean_name("a" * 500 + ".pdf")) == 120


def test_what_the_model_reads_is_fenced_and_names_the_person() -> None:
    profile = att.Attachment(
        name="priya.png",
        media="image",
        size=1000,
        method="picture",
        kind="linkedin_profile",
        text="Priya Nair\nHead of Procurement at Kestrel Foods",
        contact={"name": "Priya Nair", "company": "Kestrel Foods", "title": "Head of Procurement"},
    )
    unread = att.Attachment(
        name="blurry.jpg", media="image", size=10, problem="No text could be read in it."
    )
    view = att.for_model([profile, unread])
    assert view.count('source="attachment"') == 1
    assert "File 1: “priya.png” (image), a linkedin profile, read from the picture." in view
    assert "The person it shows: Name: Priya Nair; Title: Head of Procurement; Company: " in view
    assert "File 2: “blurry.jpg” (image): could not be used. No text could be read in it." in view


def test_a_photo_with_no_text_is_still_described_for_the_model() -> None:
    photo = att.Attachment(
        name="beach.jpg", media="image", size=10, method="picture", kind="photo",
        shows="A beach with a palm tree, no people or text.",
    )  # fmt: skip
    assert photo.read and photo.card()["label"] == "Photo"
    view = att.for_model([photo])
    assert "What it shows: A beach with a palm tree, no people or text." in view
    assert "It has no text and no person's details in it." in view
    assert "beach with a palm tree" in att.injection_text([photo])


def test_the_chip_says_what_was_read_and_why_not() -> None:
    read = att.Attachment(
        name="card.jpg", media="image", size=5, method="picture", kind="business_card",
        text="Ravi", ids_removed=True,
    )  # fmt: skip
    assert read.card() == {
        "type": "attachment",
        "name": "card.jpg",
        "media": "image",
        "size": 5,
        "pages": 1,
        "label": "Visiting card",
        "read": "picture",
        "note": "ID numbers in it were left out.",
    }
    unread = att.Attachment(
        name="x.png", media="image", size=5, problem="Reading pictures needs AI"
    )
    assert unread.card()["label"] == "Not read"
    assert unread.card()["read"] == "none"
    assert unread.card()["note"] == "Reading pictures needs AI"
