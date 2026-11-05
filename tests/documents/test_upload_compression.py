"""Every upload is compressed before it is stored, without changing what it is.

The rules under test (`app/services/storage/compress.py`, `app/services/uploads.py`):

* a photo is turned upright, scaled down and saved again, losing its hidden
  metadata (a phone's GPS position) on the way;
* a PNG keeps its transparency and stays a PNG;
* a scanned PDF keeps every page and its text, with its images shrunk;
* a signed PDF, a password-protected PDF, an unreadable file and a file the
  work would not shrink are kept exactly as they were;
* the workspace's upload limit is checked on the file as it arrived.
"""

from __future__ import annotations

import base64
import io
import pathlib
import re
import uuid
import zlib

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject

from app.core.crypto import EncryptionError, encrypt_secret
from app.services import uploads
from app.services.filings import support
from app.services.storage.compress import DOCUMENT, LOGO, PHOTO, compress
from app.services.storage.sniff import FileRejectedError, FileTooLargeError, size_label

JPEG, PNG, WEBP, PDF = "image/jpeg", "image/png", "image/webp", "application/pdf"


def _camera_frame(width: int, height: int) -> Image.Image:
    """Something a phone camera would produce: detail and sensor noise, which
    compress about as badly as a real photo of a document."""
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for y in range(0, height, 40):
        line = "Aadhaar 1234 5678 9012  Ramesh Kumar  DOB 01/01/1990  " * 6
        draw.text((40, y), line, fill="black")
    noise = Image.effect_noise((width, height), 30).convert("RGB")
    return Image.blend(image, noise, 0.15)


def _jpeg(image: Image.Image, quality: int = 95, **kwargs) -> bytes:
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality, **kwargs)
    return out.getvalue()


def _open(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


# ═══════════════════════════════════════════════════════════════════════════
# Photos
# ═══════════════════════════════════════════════════════════════════════════


def test_a_phone_photo_is_scaled_down_and_saved_much_smaller() -> None:
    original = _jpeg(_camera_frame(4000, 3000))
    smaller = compress(original, JPEG)
    image = _open(smaller)
    assert image.format == "JPEG"
    assert max(image.size) == DOCUMENT.max_side
    assert image.size == (2400, 1800), "the shape is kept"
    assert len(smaller) < len(original) / 3


def test_the_gps_position_and_camera_details_are_removed() -> None:
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"  # camera make
    exif[0x8825] = {1: "N", 2: (12.0, 58.0, 0.0), 3: "E", 4: (77.0, 35.0, 0.0)}  # GPS
    original = _jpeg(_camera_frame(3000, 2000), exif=exif.tobytes())
    assert 0x8825 in _open(original).getexif()

    kept = _open(compress(original, JPEG)).getexif()
    assert 0x8825 not in kept and 0x010F not in kept


def test_a_sideways_photo_is_turned_upright() -> None:
    exif = Image.Exif()
    exif[0x0112] = 6  # stored sideways: viewers rotate it 90 degrees to show it
    original = _jpeg(_camera_frame(3200, 2400), exif=exif.tobytes())

    image = _open(compress(original, JPEG))
    assert image.size == (1800, 2400), "upright, so it still shows the right way round"
    assert image.getexif().get(0x0112) in (None, 1)


def test_a_profile_photo_is_scaled_to_a_face() -> None:
    image = _open(compress(_jpeg(_camera_frame(3000, 3000)), JPEG, PHOTO))
    assert image.size == (1024, 1024)


def test_a_png_logo_keeps_its_transparency_and_stays_a_png() -> None:
    logo = Image.new("RGBA", (3200, 1200), (0, 0, 0, 0))
    ImageDraw.Draw(logo).rectangle([200, 200, 3000, 1000], fill=(125, 7, 18, 255))
    out = io.BytesIO()
    logo.save(out, "PNG", compress_level=0)
    original = out.getvalue()

    smaller = compress(original, PNG, LOGO)
    image = _open(smaller)
    assert image.format == "PNG" and image.mode == "RGBA"
    assert image.size == (1600, 600)
    assert image.getpixel((0, 0))[3] == 0, "the clear corner is still clear"
    assert image.getpixel((800, 300))[3] == 255
    assert len(smaller) < len(original)


def test_a_webp_receipt_stays_a_webp() -> None:
    out = io.BytesIO()
    _camera_frame(3600, 2400).save(out, "WEBP", quality=100)
    image = _open(compress(out.getvalue(), WEBP))
    assert image.format == "WEBP" and max(image.size) == 2400


def test_a_small_file_is_not_touched() -> None:
    small = _jpeg(Image.new("RGB", (64, 64), "red"))
    assert compress(small, JPEG) is small


def test_a_photo_already_small_and_compressed_is_kept_as_it_was() -> None:
    # Saving it again would cost detail for little or nothing.
    original = _jpeg(_camera_frame(1200, 900), quality=70, optimize=True)
    assert len(original) > 32 * 1024
    assert compress(original, JPEG) is original


def test_an_unreadable_image_is_kept_as_it_was() -> None:
    broken = b"\xff\xd8\xff\xe0" + b"not really a jpeg " * 4000
    assert compress(broken, JPEG) is broken


def test_a_cmyk_jpeg_is_kept_as_it_was() -> None:
    original = _jpeg(_camera_frame(2600, 2000).convert("CMYK"))
    assert compress(original, JPEG) is original


# ═══════════════════════════════════════════════════════════════════════════
# PDFs
# ═══════════════════════════════════════════════════════════════════════════


def _scanned_pdf(pages: int = 2, *, as_pixels: bool = False) -> bytes:
    """A scan: every page one full-page image at 300 dpi, plus a line of text.

    ReportLab embeds a JPEG file as a JPEG (wrapped in ASCII85) and a decoded
    image as Flate-compressed pixels: the two ways scans arrive.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    out = io.BytesIO()
    pdf = canvas.Canvas(out, pagesize=A4)
    for number in range(1, pages + 1):
        frame = _camera_frame(2480, 3508)
        source = frame if as_pixels else io.BytesIO(_jpeg(frame, quality=92))
        pdf.drawImage(ImageReader(source), 0, 0, *A4)
        pdf.drawString(72, 72, f"Experience certificate page {number}")
        pdf.showPage()
    pdf.save()
    return out.getvalue()


def _image_sizes(data: bytes) -> list[tuple[int, int]]:
    return [image.image.size for page in PdfReader(io.BytesIO(data)).pages for image in page.images]


def test_a_scanned_pdf_keeps_every_page_and_its_text_and_shrinks() -> None:
    original = _scanned_pdf(pages=2)
    smaller = compress(original, PDF)
    assert len(smaller) < len(original) / 2

    reader = PdfReader(io.BytesIO(smaller))
    assert len(reader.pages) == 2
    assert "Experience certificate page 2" in reader.pages[1].extract_text()
    assert _image_sizes(smaller) == [(1697, 2400), (1697, 2400)]


def test_a_scan_stored_as_raw_pixels_is_saved_as_jpeg_inside_the_pdf() -> None:
    original = _scanned_pdf(pages=1, as_pixels=True)
    smaller = compress(original, PDF)
    assert len(smaller) < len(original) / 4
    assert len(PdfReader(io.BytesIO(smaller)).pages) == 1


def test_a_digitally_signed_pdf_is_never_rewritten() -> None:
    # A TRACES Form 16 or an e-signed letter: rewriting it breaks the signature.
    signed = _scanned_pdf(pages=1) + b"\n% /ByteRange [0 840 960 240] /Contents <00>\n"
    assert compress(signed, PDF) is signed


def test_a_pdf_with_signature_fields_is_never_rewritten() -> None:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(_scanned_pdf(pages=1))))
    writer._root_object[NameObject("/AcroForm")] = DictionaryObject(
        {NameObject("/Fields"): ArrayObject(), NameObject("/SigFlags"): NumberObject(3)}
    )
    out = io.BytesIO()
    writer.write(out)
    original = out.getvalue()
    assert compress(original, PDF) is original


def test_a_password_protected_pdf_is_kept_as_it_was() -> None:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(_scanned_pdf(pages=1))))
    writer.encrypt("payslip-password")
    out = io.BytesIO()
    writer.write(out)
    protected = out.getvalue()
    assert compress(protected, PDF) is protected


def test_a_damaged_pdf_is_kept_as_it_was() -> None:
    # The reader would have to repair it; a repaired reading may have lost
    # something, so it is not rewritten from one.
    original = _scanned_pdf(pages=1)
    damaged = original[: original.rfind(b"xref")] + b"%%EOF\n"
    assert compress(damaged, PDF) is damaged


def test_garbage_after_a_pdf_header_is_kept_as_it_was() -> None:
    garbage = b"%PDF-1.4\n" + bytes(range(256)) * 400
    assert compress(garbage, PDF) is garbage


def test_a_generated_letter_is_stored_compressed() -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    from app.services.documents.pdf import _underlay

    def page(text: str) -> bytes:
        out = io.BytesIO()
        pdf = canvas.Canvas(out, pagesize=A4, pageCompression=0)
        for line in range(60):
            pdf.drawString(72, 800 - line * 12, f"{text} line {line} " * 4)
        pdf.showPage()
        pdf.save()
        return out.getvalue()

    document, letterhead = page("Offer letter"), page("Letterhead")
    assert b"(Offer letter line 1" in document, "the page as drawn is uncompressed"
    letter = _underlay(document, letterhead)
    assert b"(Offer letter line 1" not in letter, "the stored letter is compressed"
    assert len(letter) < (len(document) + len(letterhead)) / 2
    assert "Offer letter line 1" in PdfReader(io.BytesIO(letter)).pages[0].extract_text()
    assert _underlay(document, letterhead) == letter, "the same letter is the same bytes"


# ═══════════════════════════════════════════════════════════════════════════
# The upload limit
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (None, 10),
        ({}, 10),
        ({"max_upload_mb": 5}, 5),
        ({"max_upload_mb": 25}, 25),
        ({"max_upload_mb": 0}, 1),
        ({"max_upload_mb": 400}, 25),
        ({"max_upload_mb": "5"}, 10),
        ({"max_upload_mb": True}, 10),
        ({"max_upload_mb": 7.5}, 10),
    ],
)
def test_the_workspace_limit_is_read_safely(settings, expected) -> None:
    assert uploads.limit_mb(settings) == expected


@pytest.mark.parametrize(
    ("size", "up", "label"),
    [
        (10 * 1024 * 1024, False, "10 MB"),
        (10 * 1024 * 1024 + 1, True, "10.1 MB"),
        (13_002_342, True, "12.4 MB"),
        (600 * 1024, False, "600 KB"),
        (10, False, "1 KB"),
    ],
)
def test_sizes_read_as_people_say_them(size, up, label) -> None:
    assert size_label(size, up=up) == label


async def test_a_file_over_the_limit_is_refused_naming_both_sizes() -> None:
    data = b"%PDF-1.4\n" + b"\x00" * (5 * 1024 * 1024)
    with pytest.raises(FileTooLargeError) as caught:
        await uploads.accept(data, max_bytes=5 * 1024 * 1024)
    assert str(caught.value) == "The file is 5.1 MB. The upload limit is 5 MB."
    assert uploads.rejected(caught.value).status_code == 413


async def test_the_limit_is_on_the_file_as_uploaded_and_what_is_kept_is_compressed() -> None:
    original = _jpeg(_camera_frame(4000, 3000))
    accepted = await uploads.accept(original, max_bytes=len(original))
    assert accepted.original_size == len(original)
    assert accepted.size < len(original) / 3
    assert accepted.kind.mime_type == JPEG
    assert accepted.sizes == {"size_bytes": accepted.size, "uploaded_size_bytes": len(original)}


async def test_a_type_not_accepted_here_is_refused_with_the_screens_reason() -> None:
    with pytest.raises(FileRejectedError, match="has to be a PNG or JPEG") as caught:
        await uploads.accept(
            b"%PDF-1.4\n%%EOF\n",
            max_bytes=1024,
            types=(JPEG, PNG),
            wrong_type="A profile photo has to be a PNG or JPEG image.",
        )
    assert not isinstance(caught.value, FileTooLargeError)
    assert uploads.rejected(caught.value, reject_status=422).status_code == 422


def test_import_parsers_hold_files_to_the_highest_limit_an_admin_can_set() -> None:
    from app.services.accounts import service as accounts
    from app.services.attendance import csv_import
    from app.services.disbursement import responses
    from app.services.employee_import import sheet
    from app.services.gst import csv_io
    from app.services.payroll import history_import, salary_import

    for ceiling in (
        accounts.MAX_UPLOAD_BYTES,
        csv_import.MAX_BYTES,
        responses.MAX_RESPONSE_BYTES,
        sheet.MAX_BYTES,
        csv_io.MAX_BYTES,
        history_import.MAX_FILE_BYTES,
        salary_import.MAX_BYTES,
    ):
        assert ceiling == uploads.CEILING_BYTES == uploads.MAX_MB * 1024 * 1024


def test_every_upload_goes_through_the_shared_door() -> None:
    """A new upload path that checks a file itself would skip the workspace's
    limit and the compression: only the upload module may call the checks."""
    app = pathlib.Path(__file__).resolve().parents[2] / "app"
    allowed = {app / "services" / "uploads.py", app / "services" / "storage" / "sniff.py"}
    offenders = [
        str(path.relative_to(app))
        for path in app.rglob("*.py")
        if path not in allowed
        and re.search(r"\bvalidate_upload\(", path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


# ═══════════════════════════════════════════════════════════════════════════
# Sealed filing documents (tax proofs, bills, generated returns)
# ═══════════════════════════════════════════════════════════════════════════


def test_a_sealed_document_costs_only_a_few_bytes_more_than_it_holds() -> None:
    tenant = uuid.uuid4()
    pdf = _scanned_pdf(pages=1)
    sealed = support._seal(tenant, pdf)
    # Before: the base64 text of the file, encrypted as text, about 1.8 times as large.
    assert len(sealed) < len(pdf) + 200
    assert support._unseal(tenant, sealed) == pdf


def test_a_generated_text_file_is_compressed_before_it_is_encrypted() -> None:
    tenant = uuid.uuid4()
    text = ("EMP001,ACME,PF,1800.00,2026-10\n" * 5000).encode()
    sealed = support._seal(tenant, text)
    assert len(sealed) < len(text) / 10
    assert support._unseal(tenant, sealed) == text


def test_a_document_sealed_the_old_way_still_opens() -> None:
    tenant = uuid.uuid4()
    data = b"%PDF-1.4 a rent receipt"
    old = encrypt_secret(base64.b64encode(data).decode("ascii"), tenant).encode("ascii")
    assert support._unseal(tenant, old) == data


def test_another_tenants_key_cannot_open_a_sealed_document() -> None:
    sealed = support._seal(uuid.uuid4(), zlib.compress(b"x" * 100))
    with pytest.raises(EncryptionError):
        support._unseal(uuid.uuid4(), sealed)
