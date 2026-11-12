"""Birthday and work-anniversary greeting cards, drawn as a picture.

One square card (1080 x 1080, the size a social post uses) from a design and a
person: the company logo at the top, a heading, the person's photo in a ring,
their name on a ribbon, a title and a message. The same JPEG goes in the
person's email and on the company feed, so what HR previews is exactly what is
sent.

A design is data HR edits, never code: a layout, a background pattern, six
colours, the wording and a few switches. The wording takes placeholders, filled
per person when the card is drawn:

    {name} {first_name} {full_name} {years} {years_ordinal} {years_label}
    {company} {designation} {department}

Pure and blocking (Pillow, no database, no I/O): callers run `render_card` in a
worker thread.
"""

from __future__ import annotations

import io
import math
import random
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageFont, ImageOps
from pydantic import BaseModel, Field

__all__ = [
    "PLACEHOLDERS",
    "PRESETS",
    "CardDesign",
    "CardPerson",
    "Preset",
    "fill",
    "ordinal",
    "preset",
    "presets_for",
    "render_card",
]

Kind = Literal["birthday", "work_anniversary"]

SIZE = 1080
#: Drawn at twice the size and scaled down, so circles and ribbons are smooth.
_SCALE = 2
_FONTS = Path(__file__).parent / "card_fonts"
_HEX = r"^#[0-9a-fA-F]{6}$"

PLACEHOLDERS: dict[str, str] = {
    "{name}": "The name as the card shows it (first or full, per the design)",
    "{first_name}": "First name",
    "{full_name}": "Full name",
    "{years}": "Years completed, e.g. 3",
    "{years_ordinal}": "3rd, 5th, 10th",
    "{years_label}": "1 year, 3 years",
    "{company}": "The company's name",
    "{designation}": "Their designation",
    "{department}": "Their department",
}


class CardDesign(BaseModel):
    """What HR chooses for a card. Every field has a sensible default."""

    layout: Literal["classic", "minimal"] = "classic"
    pattern: Literal["stripes", "confetti", "dots", "none"] = "stripes"
    font: Literal["modern", "elegant"] = "modern"

    background_colour: str = Field(default="#F3F8FC", pattern=_HEX)
    pattern_colour: str = Field(default="#9FCBE5", pattern=_HEX)
    heading_colour: str = Field(default="#0B4F78", pattern=_HEX)
    text_colour: str = Field(default="#0B4F78", pattern=_HEX)
    accent_colour: str = Field(default="#B9D8EA", pattern=_HEX)
    ribbon_text_colour: str = Field(default="#0B4F78", pattern=_HEX)

    heading: str = Field(default="Congratulations", max_length=60)
    subheading: str = Field(default="On your achievement", max_length=80)
    title: str = Field(default="Happy {years_ordinal} work anniversary", max_length=70)
    message: str = Field(
        default=(
            "Happy work anniversary! Our journey together has been filled with shared "
            "goals, laughter, and growth. Here's to many more years of collaboration "
            "and success."
        ),
        max_length=400,
    )

    show_logo: bool = True
    #: A light plate behind the logo, for a dark logo on a dark card.
    logo_plate: bool = False
    show_photo: bool = True
    name_style: Literal["first", "full"] = "first"
    uppercase_name: bool = True
    uppercase_titles: bool = True


@dataclass(frozen=True, slots=True)
class CardPerson:
    """Who the card is for, and what it may show of them."""

    first_name: str
    full_name: str
    company: str
    years: int | None = None
    designation: str | None = None
    department: str | None = None
    #: The photo's bytes (PNG or JPEG), or None to show initials.
    photo: bytes | None = None
    #: The company logo's bytes, or None.
    logo: bytes | None = None


@dataclass(frozen=True, slots=True)
class Preset:
    key: str
    kind: Kind
    name: str
    design: CardDesign


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _values(design: CardDesign, person: CardPerson) -> dict[str, str]:
    years = person.years or 0
    first = (person.first_name or person.full_name or "").strip()
    full = (person.full_name or first).strip()
    return {
        "{name}": first if design.name_style == "first" else full,
        "{first_name}": first,
        "{full_name}": full,
        "{years}": str(years),
        "{years_ordinal}": ordinal(years) if years else "",
        "{years_label}": f"{years} year" if years == 1 else f"{years} years",
        "{company}": person.company or "the company",
        "{designation}": person.designation or "",
        "{department}": person.department or "",
    }


def fill(text: str, design: CardDesign, person: CardPerson) -> str:
    """The wording with this person's values in it. Unknown braces stay as typed."""
    out = text or ""
    for token, value in _values(design, person).items():
        out = out.replace(token, value)
    # "Happy  work anniversary" when {years_ordinal} is empty reads as a typo.
    return re.sub(r"[ \t]{2,}", " ", out).strip()


# ── Presets: the gallery HR starts from ─────────────────────────────────────

_ANNIVERSARY_MESSAGE = (
    "Happy work anniversary! Our journey together has been filled with shared goals, "
    "laughter, and growth. Here's to many more years of collaboration and success."
)
_BIRTHDAY_MESSAGE = (
    "Wishing you a wonderful birthday, {first_name}! May the year ahead bring you joy, "
    "good health and success. Warm wishes from all of us at {company}."
)

PRESETS: tuple[Preset, ...] = (
    Preset(
        "anniversary_ice",
        "work_anniversary",
        "Ice blue",
        CardDesign(message=_ANNIVERSARY_MESSAGE),
    ),
    Preset(
        "anniversary_maroon",
        "work_anniversary",
        "Maroon and marigold",
        CardDesign(
            pattern="dots",
            font="elegant",
            background_colour="#FEF8F4",
            pattern_colour="#F2B33D",
            heading_colour="#7D0712",
            text_colour="#3A1A16",
            accent_colour="#7D0712",
            ribbon_text_colour="#FFFFFF",
            title="{years_label} with us",
            message=_ANNIVERSARY_MESSAGE,
        ),
    ),
    Preset(
        "anniversary_midnight",
        "work_anniversary",
        "Midnight gold",
        CardDesign(
            layout="minimal",
            pattern="stripes",
            font="elegant",
            background_colour="#14213D",
            pattern_colour="#E5B84B",
            heading_colour="#E5B84B",
            text_colour="#F4F1EA",
            accent_colour="#E5B84B",
            ribbon_text_colour="#14213D",
            logo_plate=True,
            heading="Happy anniversary",
            subheading="{years_label} of great work",
            title="Thank you for everything",
            message=_ANNIVERSARY_MESSAGE,
        ),
    ),
    Preset(
        "birthday_confetti",
        "birthday",
        "Confetti",
        CardDesign(
            pattern="confetti",
            background_colour="#FFF8EE",
            pattern_colour="#F2B33D",
            heading_colour="#7D0712",
            text_colour="#3A1A16",
            accent_colour="#F2B33D",
            ribbon_text_colour="#3A1A16",
            heading="Happy birthday",
            subheading="Today is all about you",
            title="Have a fantastic year ahead",
            message=_BIRTHDAY_MESSAGE,
        ),
    ),
    Preset(
        "birthday_blush",
        "birthday",
        "Blush",
        CardDesign(
            pattern="dots",
            font="elegant",
            background_colour="#FDF0F2",
            pattern_colour="#E8A0AE",
            heading_colour="#8E1F3A",
            text_colour="#4A1E28",
            accent_colour="#E8A0AE",
            ribbon_text_colour="#4A1E28",
            heading="Happy birthday",
            subheading="Wishing you the very best",
            title="Many happy returns",
            message=_BIRTHDAY_MESSAGE,
        ),
    ),
    Preset(
        "birthday_ice",
        "birthday",
        "Ice blue",
        CardDesign(
            heading="Happy birthday",
            subheading="Wishing you a wonderful day",
            title="Cheers to you, {first_name}",
            message=_BIRTHDAY_MESSAGE,
        ),
    ),
    Preset(
        "birthday_midnight",
        "birthday",
        "Midnight gold",
        CardDesign(
            layout="minimal",
            pattern="confetti",
            font="elegant",
            background_colour="#14213D",
            pattern_colour="#E5B84B",
            heading_colour="#E5B84B",
            text_colour="#F4F1EA",
            accent_colour="#E5B84B",
            ribbon_text_colour="#14213D",
            logo_plate=True,
            heading="Happy birthday",
            subheading="",
            title="Here's to a brilliant year",
            message=_BIRTHDAY_MESSAGE,
        ),
    ),
)


def presets_for(kind: str) -> list[Preset]:
    return [p for p in PRESETS if p.kind == kind]


def preset(key: str) -> Preset | None:
    return next((p for p in PRESETS if p.key == key), None)


# ── Drawing ─────────────────────────────────────────────────────────────────


@cache
def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_FONTS / name), size)


def _faces(design: CardDesign) -> tuple[str, str, str]:
    """(display, body, label) font files."""
    if design.font == "elegant":
        return "Fraunces_700Bold.ttf", "Inter_400Regular.ttf", "Inter_600SemiBold.ttf"
    return "Inter_800ExtraBold.ttf", "Inter_400Regular.ttf", "Inter_600SemiBold.ttf"


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    value = hex_colour.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=True))  # type: ignore[return-value]


def _s(value: float) -> int:
    return round(value * _SCALE)


def _fit(
    text: str, face: str, size: int, max_width: int, *, minimum: int = 18
) -> ImageFont.FreeTypeFont:
    """The largest font no bigger than `size` in which `text` fits on one line."""
    while size > minimum:
        font = _font(face, _s(size))
        if font.getlength(text) <= _s(max_width):
            return font
        size -= 2
    return _font(face, _s(minimum))


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        line = words[0]
        for word in words[1:]:
            trial = f"{line} {word}"
            if font.getlength(trial) <= _s(max_width):
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


def _rotated_bar(
    cx: float, cy: float, length: float, width: float, angle: float
) -> list[tuple[float, float]]:
    """The corners of a bar of `length` by `width` centred on (cx, cy), turned `angle`°."""
    rad = math.radians(angle)
    dx, dy = math.cos(rad), math.sin(rad)
    nx, ny = -dy, dx
    hl, hw = length / 2, width / 2
    return [
        (_s(cx + dx * hl + nx * hw), _s(cy + dy * hl + ny * hw)),
        (_s(cx + dx * hl - nx * hw), _s(cy + dy * hl - ny * hw)),
        (_s(cx - dx * hl - nx * hw), _s(cy - dy * hl - ny * hw)),
        (_s(cx - dx * hl + nx * hw), _s(cy - dy * hl + ny * hw)),
    ]


def _sparkle(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, fill) -> None:
    """A four-pointed star."""
    inner = r * 0.28
    points = []
    for i in range(8):
        angle = math.radians(i * 45 - 90)
        radius = r if i % 2 == 0 else inner
        points.append((_s(x + math.cos(angle) * radius), _s(y + math.sin(angle) * radius)))
    draw.polygon(points, fill=fill)


def _pattern(canvas: Image.Image, design: CardDesign) -> None:
    if design.pattern == "none":
        return
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    colour = _rgb(design.pattern_colour)

    if design.pattern == "stripes":
        # Two corners of long diagonal bars, as on a printed card.
        bars = [
            (930, 70, 760, 90, 140),
            (1060, 250, 760, 120, 110),
            (820, -40, 620, 60, 90),
            (1100, 520, 560, 70, 70),
            (150, 1010, 760, 90, 140),
            (20, 830, 760, 120, 110),
            (260, 1120, 620, 60, 90),
            (-20, 560, 560, 70, 70),
        ]
        for cx, cy, length, width, alpha in bars:
            draw.polygon(_rotated_bar(cx, cy, length, width, 45), fill=(*colour, alpha))
        for x, y, r in [(78, 70, 16), (150, 48, 9), (210, 112, 12), (96, 172, 10), (40, 250, 7)]:
            _sparkle(draw, x, y, r, (*colour, 170))
    elif design.pattern == "dots":
        for y in range(28, SIZE, 34):
            for x in range(28, SIZE, 34):
                # Only in two opposite corners, fading towards the middle.
                corner = min(x + y, (SIZE - x) + (SIZE - y))
                if corner > 420:
                    continue
                alpha = round(150 * (1 - corner / 420))
                r = 4.5
                draw.ellipse([_s(x - r), _s(y - r), _s(x + r), _s(y + r)], fill=(*colour, alpha))
    elif design.pattern == "confetti":
        palette = [
            colour,
            _rgb(design.accent_colour),
            _rgb(design.heading_colour),
            _mix(colour, (255, 255, 255), 0.35),
        ]
        rng = random.Random(7)
        placed = 0
        while placed < 90:
            x, y = rng.uniform(0, SIZE), rng.uniform(0, SIZE)
            # Keep the middle clear for the photo and the words.
            if 80 < x < SIZE - 80 and 120 < y < SIZE - 30:
                continue
            fill = (*rng.choice(palette), rng.randint(130, 220))
            if rng.random() < 0.35:
                r = rng.uniform(5, 9)
                draw.ellipse([_s(x - r), _s(y - r), _s(x + r), _s(y + r)], fill=fill)
            else:
                draw.polygon(
                    _rotated_bar(
                        x, y, rng.uniform(16, 30), rng.uniform(6, 10), rng.uniform(0, 180)
                    ),
                    fill=fill,
                )
            placed += 1

    if _is_dark(design.background_colour) and design.pattern == "stripes":
        # Bright bars over a dark ground turn muddy at full strength.
        layer.putalpha(layer.getchannel("A").point(lambda a: round(a * 0.5)))
    canvas.alpha_composite(layer)


def _initials(person: CardPerson) -> str:
    parts = [p for p in (person.full_name or person.first_name or "?").split() if p]
    if not parts:
        return "?"
    letters = parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")
    return letters.upper()


def _open_image(data: bytes | None) -> Image.Image | None:
    if not data:
        return None
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        return ImageOps.exif_transpose(image)
    except Exception:
        # A photo or logo that will not open must not stop the wish.
        return None


def _paste_photo(
    canvas: Image.Image, design: CardDesign, person: CardPerson, cx: int, cy: int, r: int
) -> None:
    ring = _rgb(design.accent_colour)
    draw = ImageDraw.Draw(canvas)
    outer = r + 22
    draw.ellipse(
        [_s(cx - outer), _s(cy - outer), _s(cx + outer), _s(cy + outer)],
        fill=(*_mix(ring, (255, 255, 255), 0.45), 255),
    )
    inner = r + 8
    draw.ellipse(
        [_s(cx - inner), _s(cy - inner), _s(cx + inner), _s(cy + inner)],
        fill=(*ring, 255),
    )

    diameter = _s(r * 2)
    photo = _open_image(person.photo)
    if photo is not None:
        photo = photo.convert("RGB")
        side = min(photo.size)
        left = (photo.width - side) // 2
        # A portrait is usually a face near the top: crop a little above centre.
        top = max(0, min(photo.height - side, round((photo.height - side) * 0.3)))
        face = photo.crop((left, top, left + side, top + side)).resize(
            (diameter, diameter), Image.Resampling.LANCZOS
        )
    else:
        face = Image.new("RGB", (diameter, diameter), _mix(ring, _rgb(design.heading_colour), 0.55))
        letters = _initials(person)
        font = _font(_faces(design)[0], _s(r * 0.8))
        ImageDraw.Draw(face).text(
            (diameter / 2, diameter / 2), letters, font=font, fill=(255, 255, 255), anchor="mm"
        )
    mask = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, diameter - 1, diameter - 1], fill=255)
    canvas.paste(face, (_s(cx - r), _s(cy - r)), mask)


def _ribbon(canvas: Image.Image, design: CardDesign, text: str, cy: int) -> int:
    """The name on a ribbon with folded tails. Returns its bottom edge."""
    draw = ImageDraw.Draw(canvas)
    _display, _body, label = _faces(design)
    font = _fit(text, label, 40, 560, minimum=22)
    text_w = font.getlength(text) / _SCALE
    width = max(340, min(680, text_w + 120))
    height = 70
    left, right = SIZE / 2 - width / 2, SIZE / 2 + width / 2
    top, bottom = cy - height / 2, cy + height / 2
    colour = _rgb(design.accent_colour)
    tail = _mix(colour, (0, 0, 0), 0.18)
    drop, reach, notch = 20, 62, 22
    for side in (-1, 1):
        edge = left if side == -1 else right
        outer = edge + side * reach
        draw.polygon(
            [
                (_s(edge - side * 10), _s(top + drop)),
                (_s(outer), _s(top + drop)),
                (_s(outer - side * notch), _s(bottom + drop - height / 2)),
                (_s(outer), _s(bottom + drop)),
                (_s(edge - side * 10), _s(bottom + drop)),
            ],
            fill=(*tail, 255),
        )
    draw.rectangle([_s(left), _s(top), _s(right), _s(bottom)], fill=(*colour, 255))
    draw.text(
        (_s(SIZE / 2), _s(cy)),
        text,
        font=font,
        fill=(*_rgb(design.ribbon_text_colour), 255),
        anchor="mm",
    )
    return round(bottom + drop)


def _logo(canvas: Image.Image, data: bytes | None, top: int, *, plate: bool) -> int:
    """The logo centred at `top`. Returns where the next thing starts."""
    image = _open_image(data)
    if image is None:
        return top
    image = image.convert("RGBA")
    image.thumbnail((_s(340), _s(96)), Image.Resampling.LANCZOS)
    x = (canvas.width - image.width) // 2
    if plate:
        pad = _s(14)
        ImageDraw.Draw(canvas).rounded_rectangle(
            [x - pad, _s(top) - pad, x + image.width + pad, _s(top) + image.height + pad],
            radius=_s(14),
            fill=(255, 255, 255, 240),
        )
        top += 6
        x = (canvas.width - image.width) // 2
    canvas.alpha_composite(image, (x, _s(top)))
    return top + round(image.height / _SCALE) + (34 if plate else 24)


def _is_dark(hex_colour: str) -> bool:
    r, g, b = _rgb(hex_colour)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 < 0.35


def _line(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, colour: str, y: float
) -> float:
    """One centred line at `y`. Returns the y below it."""
    if not text:
        return y
    draw.text((_s(SIZE / 2), _s(y)), text, font=font, fill=(*_rgb(colour), 255), anchor="ma")
    ascent, descent = font.getmetrics()
    return y + (ascent + descent) / _SCALE


def render_card(design: CardDesign, person: CardPerson) -> bytes:
    """The card as a JPEG."""
    display, body, _label = _faces(design)
    canvas = Image.new("RGBA", (_s(SIZE), _s(SIZE)), (*_rgb(design.background_colour), 255))
    _pattern(canvas, design)
    draw = ImageDraw.Draw(canvas)

    def titled(text: str) -> str:
        filled = fill(text, design, person)
        return filled.upper() if design.uppercase_titles else filled

    y: float = _logo(canvas, person.logo, 52, plate=design.logo_plate) if design.show_logo else 70
    heading = titled(design.heading)
    if heading:
        y = _line(draw, heading, _fit(heading, display, 84, 940), design.heading_colour, y) + 4
    subheading = titled(design.subheading)
    if subheading:
        y = _line(draw, subheading, _fit(subheading, body, 38, 900), design.heading_colour, y)

    name = fill("{name}", design, person)
    if design.uppercase_name:
        name = name.upper()

    if design.show_photo:
        radius = 168 if design.layout == "classic" else 150
        centre = round(y + 40 + 22 + radius)
        _paste_photo(canvas, design, person, SIZE // 2, centre, radius)
        y = centre + radius + 22
    if design.layout == "classic":
        y = _ribbon(canvas, design, name, round(y + 18 + 35)) + 24
    else:
        y += 22
        y = _line(draw, name, _fit(name, display, 60, 900), design.heading_colour, y) + 6

    title = titled(design.title)
    if title:
        y = _line(draw, title, _fit(title, display, 50, 940), design.heading_colour, y) + 6

    message = fill(design.message, design, person)
    if message:
        bottom = SIZE - 40
        size = 32
        while True:
            font = _font(body, _s(size))
            lines = _wrap(message, font, 880)
            leading = size * 1.32
            if y + leading * len(lines) <= bottom or size <= 18:
                break
            size -= 2
        for line in lines:
            if y + leading > SIZE - 10:
                break
            draw.text(
                (_s(SIZE / 2), _s(y)),
                line,
                font=font,
                fill=(*_rgb(design.text_colour), 255),
                anchor="ma",
            )
            y += leading

    out = canvas.convert("RGB").resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    out.save(buffer, format="JPEG", quality=90, optimize=True, progressive=True)
    return buffer.getvalue()
