"""The signed copy of an accepted letter, and letters shown as page pictures.

When a person accepts a letter they may upload a picture of their signature.
That picture is cleaned up (paper made transparent, cropped to the ink),
printed into the letter's "Employee Signature" line together with the date,
and the letter is rendered to PDF on its letterhead. That PDF is the signed
copy and is kept; the signature picture is not — it exists only for the
length of the request that prints it.

A letter without an "Employee Signature" line gets an acknowledgement block
added at the end instead, so every signed copy carries the signature and date.

`page_images` turns a PDF into one picture per page. A phone's browser does
not show a PDF inside a page, so the letter is shown as pictures of its pages:
exactly what prints, on any screen.
"""

from __future__ import annotations

import base64
import io
import re
from datetime import datetime, timedelta, timezone
from html import escape

from fastapi import HTTPException, status

__all__ = [
    "MAX_SIGNATURE_BYTES",
    "page_count",
    "page_image",
    "prepare_signature",
    "stamp_letter",
]

#: A signature picture larger than this is refused before it is opened.
MAX_SIGNATURE_BYTES = 5 * 1024 * 1024
_ACCEPTED_FORMATS = {"PNG", "JPEG", "WEBP", "GIF", "BMP"}
#: Printed size of the signature, in PDF points.
_SIGNATURE_HEIGHT_PT = 34
_SIGNATURE_MAX_WIDTH_PT = 150
_IST = timezone(timedelta(hours=5, minutes=30), "IST")


def _unusable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def prepare_signature(data: bytes) -> bytes:
    """A signature picture, ready to print: ink only, on a transparent ground.

    Accepts a scan or a phone photo of a signature on paper as well as a clean
    PNG. Near-white is made transparent, the picture is cropped to the ink and
    scaled down. Raises 422 for anything that is not a picture of a signature.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not data:
        raise _unusable("Choose a picture of your signature.")
    if len(data) > MAX_SIGNATURE_BYTES:
        raise _unusable("That picture is larger than 5 MB. Choose a smaller one.")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            kind = probe.format
            probe.verify()
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise _unusable(
            "That file is not a picture. Upload a PNG or JPG of your signature."
        ) from exc
    if kind not in _ACCEPTED_FORMATS:
        raise _unusable("Upload a PNG or JPG of your signature.")

    image = ImageOps.exif_transpose(image)
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    # Large phone photos: work on a sensible size.
    image.thumbnail((1600, 1600))

    # Ink is what is clearly darker than the paper. Paper (and anything already
    # transparent) becomes transparent; ink keeps its colour.
    grey = ImageOps.grayscale(image)
    alpha = image.getchannel("A")
    paper = _paper_level(grey)
    cutoff = max(60, paper - 70)
    ink = grey.point(lambda v: 255 if v < cutoff else 0)
    opaque = alpha.point(lambda a: 255 if a > 40 else 0)
    mask = Image.composite(ink, Image.new("L", image.size, 0), opaque)
    box = mask.getbbox()
    if box is None:
        raise _unusable(
            "No signature could be seen in that picture. Sign in dark ink on white paper "
            "and take the photo in good light."
        )
    inked = sum(mask.crop(box).histogram()[255:])
    area = (box[2] - box[0]) * (box[3] - box[1])
    if area and inked / area > 0.6:
        raise _unusable(
            "That picture looks like a photo, not a signature. Upload your signature on a "
            "plain white background."
        )

    pad = 8
    box = (
        max(0, box[0] - pad),
        max(0, box[1] - pad),
        min(image.width, box[2] + pad),
        min(image.height, box[3] + pad),
    )
    rgb = image.convert("RGB").crop(box)
    cut = mask.crop(box)
    # Ink is printed in a dark ink colour, not the photo's grey-brown cast.
    ink_colour = Image.new("RGB", rgb.size, (20, 24, 64))
    shaded = Image.blend(rgb, ink_colour, 0.55)
    out = Image.new("RGBA", rgb.size, (255, 255, 255, 0))
    out.paste(shaded, (0, 0), cut)
    out.thumbnail((900, 300))
    buffer = io.BytesIO()
    out.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _paper_level(grey) -> int:
    """The brightness of the paper: the level most of the picture is at or above."""
    histogram = grey.histogram()
    total = sum(histogram)
    seen = 0
    # The 60th percentile from the dark end: a signature covers far less than
    # 40 % of the picture, so this lands on the paper.
    for level, count in enumerate(histogram):
        seen += count
        if seen >= total * 0.6:
            return level
    return 255


def _signature_html(signature_png: bytes | None, typed_name: str) -> str:
    if signature_png is None:
        return (
            f'<i style="font-family:Times-Roman,serif;font-size:13pt;color:#141840">'
            f"{escape(typed_name)}</i>"
        )
    from PIL import Image

    with Image.open(io.BytesIO(signature_png)) as image:
        width, height = image.size
    printed_h = _SIGNATURE_HEIGHT_PT
    printed_w = round(width * printed_h / max(height, 1))
    if printed_w > _SIGNATURE_MAX_WIDTH_PT:
        printed_w = _SIGNATURE_MAX_WIDTH_PT
        printed_h = round(height * printed_w / max(width, 1))
    data = base64.b64encode(signature_png).decode("ascii")
    return (
        f'<img src="data:image/png;base64,{data}" width="{printed_w}" height="{printed_h}" '
        'alt="Signature" />'
    )


_SIGNATURE_LINE = re.compile(r"(Employee\s+Signature\s*:?\s*)(?:<[^>]+>\s*)*_{3,}", re.IGNORECASE)
_DATE_LINE = re.compile(r"(Date\s*:?\s*)(?:<[^>]+>\s*)*_{3,}", re.IGNORECASE)


def stamp_letter(
    merged_html: str,
    *,
    typed_name: str,
    accepted_at: datetime,
    content_hash: str,
    signature_png: bytes | None,
) -> str:
    """The letter's HTML with the signature, date and acceptance record printed in.

    The first "Employee Signature: ____" line gets the signature and the next
    "Date: ____" after it the date. A letter with no such line gets an
    acknowledgement block at the end.
    """
    on = accepted_at.astimezone(_IST)
    date_text = escape(on.strftime("%d %B %Y").lstrip("0"))
    signature = _signature_html(signature_png, typed_name)
    record = (
        '<p style="font-size:7pt;color:#666666;margin-top:6pt">'
        f"Accepted electronically by {escape(typed_name)} on "
        f"{escape(on.strftime('%d %b %Y, %I:%M %p IST').lstrip('0'))}. "
        f"Letter fingerprint {escape(content_hash[:16])}.</p>"
    )

    found = _SIGNATURE_LINE.search(merged_html)
    if found is None:
        return (
            merged_html
            + '<p>&nbsp;</p><table class="keep" cellspacing="0" cellpadding="0"><tr><td>'
            "<p><strong>Employee Acknowledgement</strong></p>"
            "<p>I hereby acknowledge receipt of this letter and confirm my acceptance of the "
            "terms and conditions mentioned herein.</p>"
            f"<p><strong>Employee Signature:</strong> {signature}<br/>"
            f"<strong>Date:</strong> {date_text}</p>"
            f"{record}</td></tr></table>"
        )

    stamped = merged_html[: found.start()] + found.group(1) + signature + merged_html[found.end() :]
    tail_from = found.start() + len(found.group(1)) + len(signature)
    date = _DATE_LINE.search(stamped, tail_from)
    if date is not None:
        stamped = stamped[: date.start()] + date.group(1) + date_text + stamped[date.end() :]
        # The record sits after the paragraph that holds the date.
        close = stamped.find("</p>", date.start())
        at = close + len("</p>") if close != -1 else len(stamped)
        cell = stamped.find("</td>", date.start())
        if cell != -1 and (close == -1 or cell < close):
            at = cell
        return stamped[:at] + record + stamped[at:]
    return stamped + record


# ── Pages as pictures ──────────────────────────────────────────────────────


def page_count(pdf: bytes) -> int:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf)
    try:
        return len(document)
    finally:
        document.close()


def page_image(pdf: bytes, number: int, *, width_px: int = 1100) -> bytes:
    """Page `number` (1-based) as a PNG about `width_px` wide. 404 past the end."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf)
    try:
        if number < 1 or number > len(document):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such page")
        page = document[number - 1]
        try:
            scale = width_px / page.get_width()
            bitmap = page.render(scale=scale)
            picture = bitmap.to_pil()
        finally:
            page.close()
    finally:
        document.close()
    buffer = io.BytesIO()
    picture.convert("RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
