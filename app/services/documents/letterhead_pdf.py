"""The letterhead a letter is printed on, drawn from the workspace's own choices.

A letterhead is usually a picture someone made once in Word: logo, address,
coloured bands, all baked into one image. That cannot be recoloured or given a
new phone number, and it cannot be handed to the next company. So it is drawn
here instead, as vectors, from a handful of settings: three colours, the logo,
and the contact lines. Each design is a page background; the letter's own
content is laid out by the HTML renderer inside the space the design leaves
free (`BodyArea`), and the two are stacked page by page.

Designs:

- ``wave`` — coloured bands across the top and the foot with a slanted edge, a
  dot pattern at the top right, the logo top left, thin rules under the header
  and above the footer, and the company's address, email and website in the
  footer. The layout of the letterhead HR already uses.
- ``classic`` — a thin colour bar, logo left, company details right, a rule
  under the header and a one-line footer. Quieter, for formal letters.
- ``none`` — nothing printed: for pre-printed stationery.

Pure functions of their inputs. Nothing here touches the database or storage,
so a letterhead can be drawn and checked in a test.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from reportlab.lib.colors import Color
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas

__all__ = [
    "DESIGNS",
    "BodyArea",
    "LetterheadArt",
    "body_area",
    "draw_letterhead",
    "mix",
]

PAGE_W, PAGE_H = A4
DESIGNS = ("wave", "classic", "none")

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


@dataclass(frozen=True, slots=True)
class LetterheadArt:
    """Everything a design draws. Colours are #rrggbb."""

    design: str = "wave"
    primary_colour: str = "#1B5A92"
    secondary_colour: str = "#4DAAF6"
    accent_colour: str = "#3A9E96"
    company_name: str = ""
    address: str = ""
    phone: str = ""
    email: str = ""
    website: str = ""
    logo: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class BodyArea:
    """Page margins, in points, inside which the letter's content is laid out.

    `footer_line` is the baseline area for the reference and page number,
    measured from the page's foot.
    """

    top: float
    right: float
    bottom: float
    left: float
    footer_line: float


def body_area(design: str) -> BodyArea:
    if design == "wave":
        return BodyArea(top=146, right=58, bottom=122, left=58, footer_line=100)
    if design == "classic":
        return BodyArea(top=128, right=62, bottom=92, left=62, footer_line=66)
    return BodyArea(top=72, right=62, bottom=72, left=62, footer_line=40)


# ── Colour helpers ──────────────────────────────────────────────────────────


def _rgb(hex_colour: str) -> tuple[float, float, float]:
    value = hex_colour if _HEX.match(hex_colour or "") else "#1B5A92"
    return tuple(int(value[i : i + 2], 16) / 255 for i in (1, 3, 5))  # type: ignore[return-value]


def mix(hex_colour: str, other: str, amount: float) -> str:
    """`hex_colour` moved `amount` (0-1) of the way towards `other`."""
    a, b = _rgb(hex_colour), _rgb(other)
    parts = [round((x + (y - x) * amount) * 255) for x, y in zip(a, b, strict=True)]
    return "#" + "".join(f"{p:02X}" for p in parts)


def _colour(hex_colour: str) -> Color:
    r, g, b = _rgb(hex_colour)
    return Color(r, g, b)


def _tint(hex_colour: str, amount: float) -> Color:
    return _colour(mix(hex_colour, "#FFFFFF", amount))


def _shade(hex_colour: str, amount: float) -> Color:
    return _colour(mix(hex_colour, "#000000", amount))


# ── Drawing primitives ──────────────────────────────────────────────────────


def _polygon(c: rl_canvas.Canvas, points: list[tuple[float, float]], fill: Color) -> None:
    path = c.beginPath()
    path.moveTo(*points[0])
    for point in points[1:]:
        path.lineTo(*point)
    path.close()
    c.setFillColor(fill)
    c.drawPath(path, stroke=0, fill=1)


def _gradient_polygon(
    c: rl_canvas.Canvas,
    points: list[tuple[float, float]],
    start: tuple[float, float],
    end: tuple[float, float],
    colours: tuple[Color, Color],
) -> None:
    c.saveState()
    path = c.beginPath()
    path.moveTo(*points[0])
    for point in points[1:]:
        path.lineTo(*point)
    path.close()
    c.clipPath(path, stroke=0, fill=0)
    c.linearGradient(start[0], start[1], end[0], end[1], colours, extend=True)
    c.restoreState()


def _logo(
    c: rl_canvas.Canvas, data: bytes | None, x: float, top: float, max_w: float, max_h: float
) -> bool:
    """Draw the logo with its top-left at (x, top), fitted inside the box."""
    if not data:
        return False
    try:
        image = ImageReader(io.BytesIO(data))
        width, height = image.getSize()
    except Exception:
        return False
    if not width or not height:
        return False
    scale = min(max_w / width, max_h / height)
    w, h = width * scale, height * scale
    c.drawImage(image, x, top - h, width=w, height=h, mask="auto")
    return True


def _wrap(c: rl_canvas.Canvas, text: str, font: str, size: float, width: float) -> list[str]:
    """Split `text` into lines no wider than `width`, keeping its own line breaks."""
    lines: list[str] = []
    for paragraph in (text or "").splitlines():
        words = paragraph.split()
        if not words:
            continue
        current = words[0]
        for word in words[1:]:
            trial = f"{current} {word}"
            if c.stringWidth(trial, font, size) <= width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _fit(c: rl_canvas.Canvas, text: str, font: str, size: float, width: float) -> float:
    """The largest size at most `size` at which `text` fits in `width`."""
    while size > 5 and c.stringWidth(text, font, size) > width:
        size -= 0.25
    return size


# ── Designs ─────────────────────────────────────────────────────────────────


def _draw_wave(c: rl_canvas.Canvas, art: LetterheadArt) -> None:
    w, h = PAGE_W, PAGE_H
    primary, secondary, accent = art.primary_colour, art.secondary_colour, art.accent_colour

    # Top band: a gradient from deep to light, with a slanted left end.
    _gradient_polygon(
        c,
        [(132.6, h), (w, h), (w, h - 27), (104.5, h - 27)],
        (132.6, h),
        (w, h),
        (_shade(primary, 0.12), _colour(secondary)),
    )
    # The light wedge at the top-left corner and the pale sliver beside it.
    _polygon(c, [(0, h), (132.6, h), (93.5, h - 37.5), (0, h - 37.5)], _tint(secondary, 0.08))
    _polygon(
        c,
        [(104.5, h - 27), (135.5, h - 27), (125, h - 37.5), (93.5, h - 37.5)],
        _tint(accent, 0.78),
    )

    # Dot pattern at the top right: larger to the right, paler lower down,
    # cut off along a diagonal so it reads as a fading corner.
    x0, y_top, y_bottom, step = 412.0, h - 30, h - 186, 8.9
    row = 0
    y = y_top
    while y >= y_bottom:
        fraction_down = (y_top - y) / (y_top - y_bottom)
        x_start = x0 + fraction_down * 60
        x = x_start
        while x <= w - 2:
            across = (x - x0) / (w - x0)
            radius = 0.55 + 1.75 * across
            c.setFillColor(_tint(secondary, 0.80 + 0.14 * fraction_down))
            c.circle(x, y, radius, stroke=0, fill=1)
            x += step
        row += 1
        y -= step

    # Logo, or the company's name where there is no logo.
    if not _logo(c, art.logo, 37, h - 58, 170, 52):
        name = art.company_name or ""
        c.setFillColor(_colour(primary))
        size = _fit(c, name, "Helvetica-Bold", 20, 330)
        c.setFont("Helvetica-Bold", size)
        c.drawString(37, h - 96, name)

    # Rule under the header.
    c.setStrokeColor(_colour(accent))
    c.setLineWidth(1.25)
    c.line(37, h - 128.4, 558, h - 128.4)

    # Footer: rule, then name and address left, email centre, website right.
    c.line(37, 93.5, 558, 93.5)
    c.setFillColor(_colour(accent))
    name = art.company_name or ""
    if name:
        c.setFont("Helvetica-Bold", _fit(c, name, "Helvetica-Bold", 9.5, 205))
        c.drawString(37, 76, name)
    c.setFont("Helvetica-Bold", 6.8)
    y = 66.5
    for line in _wrap(c, art.address, "Helvetica-Bold", 6.8, 205)[:4]:
        c.drawString(37, y, line)
        y -= 8.6
    middle = [part for part in (art.email, art.phone) if part]
    y = 77
    for part in middle:
        c.setFont("Helvetica-Bold", _fit(c, part, "Helvetica-Bold", 9, 150))
        c.drawString(258, y, part)
        y -= 11
    if art.website:
        c.setFont("Helvetica-Bold", _fit(c, art.website, "Helvetica-Bold", 9, 140))
        c.drawRightString(557, 77, art.website)

    # Foot band: deep on the left into the primary colour, with a light wedge
    # rising at the right and a pale sliver at its edge.
    _gradient_polygon(
        c,
        [(0, 0), (372, 0), (402, 30), (0, 30)],
        (0, 0),
        (402, 0),
        (_shade(primary, 0.55), _colour(primary)),
    )
    _polygon(c, [(294, 30), (311.5, 46.5), (404, 46.5), (386.5, 30)], _tint(accent, 0.75))
    _polygon(c, [(357, 0), (w, 0), (w, 46.5), (403, 46.5)], _tint(secondary, 0.05))


def _draw_classic(c: rl_canvas.Canvas, art: LetterheadArt) -> None:
    w, h = PAGE_W, PAGE_H
    primary, accent = art.primary_colour, art.accent_colour

    c.setFillColor(_colour(primary))
    c.rect(0, h - 8, w, 8, stroke=0, fill=1)

    has_logo = _logo(c, art.logo, 62, h - 34, 160, 48)
    c.setFillColor(_colour(primary))
    name = art.company_name or ""
    if has_logo:
        c.setFont("Helvetica-Bold", _fit(c, name, "Helvetica-Bold", 11, 250))
        c.drawRightString(w - 62, h - 44, name)
    else:
        c.setFont("Helvetica-Bold", _fit(c, name, "Helvetica-Bold", 18, 300))
        c.drawString(62, h - 62, name)
    c.setFillColor(_shade(primary, 0.2))
    c.setFont("Helvetica", 7.5)
    y = h - 56
    for line in _wrap(c, art.address, "Helvetica", 7.5, 230)[:3]:
        c.drawRightString(w - 62, y, line)
        y -= 9.5
    contact = "  ·  ".join(part for part in (art.phone, art.email) if part)
    if contact:
        c.drawRightString(w - 62, y, contact)

    c.setStrokeColor(_colour(accent))
    c.setLineWidth(1.25)
    c.line(62, h - 104, w - 62, h - 104)

    c.setLineWidth(0.6)
    c.line(62, 54, w - 62, 54)
    c.setFillColor(_colour(accent))
    footer = "  ·  ".join(
        part for part in (art.company_name, art.email, art.phone, art.website) if part
    )
    if footer:
        c.setFont("Helvetica-Bold", _fit(c, footer, "Helvetica-Bold", 7.5, w - 124))
        c.drawCentredString(w / 2, 40, footer)


def draw_letterhead(art: LetterheadArt) -> bytes:
    """One A4 page carrying only the letterhead, as PDF bytes."""
    import reportlab.rl_config as rl_config

    # Fixed creation date and document id: the same letterhead is the same bytes.
    rl_config.invariant = 1
    buffer = io.BytesIO()
    c = rl_canvas.Canvas(buffer, pagesize=A4, invariant=1)
    if art.design == "wave":
        _draw_wave(c, art)
    elif art.design == "classic":
        _draw_classic(c, art)
    c.showPage()
    c.save()
    return buffer.getvalue()
