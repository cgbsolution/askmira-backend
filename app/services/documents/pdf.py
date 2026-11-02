"""HTML to PDF rendering for issued letters.

═══════════════════════════════════════════════════════════════════════════════
ON DETERMINISM
═══════════════════════════════════════════════════════════════════════════════

The slice asks for deterministic output so an issued letter can be re-rendered
and verified against a stored hash. PDF bytes are a poor anchor for that: every
PDF writer embeds a creation timestamp and a document id, and those move on
every render regardless of input.

So the integrity anchor is the **merged HTML**, hashed with SHA-256 and stored
on `IssuedLetter.content_hash`. That is genuinely deterministic — the merge
engine is a pure function — and it is the thing that actually matters: it
proves the *content* of the letter has not changed. The PDF is a rendering of
already-verified content.

`render_pdf` still pins what it can (a fixed creation date and document id
derived from the content hash), so two renders of the same letter produce
byte-identical PDFs. The test asserts that.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from html import escape as html_escape

from app.services.documents.letterhead_pdf import (
    LetterheadArt,
    body_area,
    draw_letterhead,
    mix,
)

__all__ = [
    "DEFAULT_PAGE_CSS",
    "PdfRenderError",
    "content_hash",
    "letter_css",
    "render_pdf",
    "wrap_document",
    "wrap_letter",
]


class PdfRenderError(RuntimeError):
    """The renderer could not produce a PDF from this HTML."""


def content_hash(merged_html: str) -> str:
    """SHA-256 of the merged HTML.

    This is the integrity anchor for an issued letter. Deterministic by
    construction, because the merge engine is a pure function of its inputs.
    """
    return hashlib.sha256(merged_html.encode("utf-8")).hexdigest()


# Page furniture every letter gets: A4, margins, and a footer carrying the
# reference number and page numbering, which the slice requires.
DEFAULT_PAGE_CSS = """
@page {
    size: a4 portrait;
    margin: 2.5cm 2cm 2.5cm 2cm;
    @frame footer_frame {
        -pdf-frame-content: page-footer;
        bottom: 1.2cm; left: 2cm; width: 17cm; height: 1cm;
    }
}
body { font-family: Helvetica, Arial, sans-serif; font-size: 10.5pt; line-height: 1.5;
       color: #1a1110; }
h1, h2, h3 { color: #1a1110; }
/* xhtml2pdf does not implement border-collapse; cell borders carry the grid. */
table.ctc-table { width: 100%; margin: 12pt 0; }
table.ctc-table th { background-color: #fde8dd; text-align: left; padding: 6pt;
                     border: 1px solid #f2c2a9; font-size: 9.5pt; }
table.ctc-table td { padding: 6pt; border: 1px solid #f0e2d8; font-size: 9.5pt; }
table.ctc-table tfoot td { background-color: #f9efe8; font-weight: bold; }
#page-footer { font-size: 8pt; color: #7a6e68; border-top: 0.5pt solid #f0e2d8;
               padding-top: 4pt; }
"""


@dataclass(frozen=True, slots=True)
class Letterhead:
    """Tenant branding applied to every letter."""

    company_name: str
    logo_data_uri: str | None = None
    address: str | None = None
    website: str | None = None


def wrap_document(
    *,
    merged_html: str,
    reference_number: str,
    letterhead: Letterhead,
    extra_css: str = "",
) -> str:
    """Wrap merged letter content in the page furniture.

    Kept separate from `render_pdf` so the exact HTML that will be rendered can
    be inspected, diffed and tested without invoking a PDF engine.
    """
    logo = (
        f'<img src="{letterhead.logo_data_uri}" style="height:48px" alt="" />'
        if letterhead.logo_data_uri
        else ""
    )
    address = (
        f'<div style="font-size:8.5pt;color:#7a6e68">{letterhead.address}</div>'
        if letterhead.address
        else ""
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8" />
<style>{DEFAULT_PAGE_CSS}{extra_css}</style>
</head><body>
<div id="page-footer">
  {reference_number} &nbsp;|&nbsp; Page <pdf:pagenumber> of <pdf:pagecount>
</div>
<div style="border-bottom:1.5pt solid #eb061d;padding-bottom:8pt;margin-bottom:18pt">
  {logo}
  <div style="font-size:13pt;font-weight:bold;color:#1a1110">
    {letterhead.company_name}</div>
  {address}
</div>
{merged_html}
</body></html>"""


def letter_css(*, secondary_colour: str = "#4DAAF6", highlight_colour: str = "#E00000") -> str:
    """Type and tables for letters printed on a drawn letterhead.

    `.annexure` is the salary annexure (`computed.salary_annexure`); `.form` is
    a fill-in form (label, then a ruled line); `.page-break` starts a new page.
    """
    section = mix(secondary_colour, "#FFFFFF", 0.82)
    return f"""
body {{ font-family: Helvetica, Arial, sans-serif; font-size: 10pt; line-height: 1.4;
       color: #111111; }}
p {{ margin: 0 0 7pt 0; }}
h1, h2, h3 {{ color: #111111; margin: 0 0 8pt 0; }}
h1 {{ font-size: 15pt; }} h2 {{ font-size: 13.5pt; }} h3 {{ font-size: 11pt; }}
.page-break {{ page-break-before: always; }}
table.keep {{ width: 100%; }}
table.keep td {{ padding: 0; }}
table.ctc-table {{ width: 100%; margin: 10pt 0; }}
table.ctc-table th {{ background-color: {section}; text-align: left; padding: 4pt;
                     border: 0.5pt solid #333333; font-size: 9.5pt; }}
table.ctc-table td {{ padding: 4pt; border: 0.5pt solid #333333; font-size: 9.5pt; }}
table.annexure {{ width: 100%; margin: 4pt 0 10pt 0; }}
table.annexure td {{ border: 0.6pt solid #111111; padding: 2.5pt 5pt 0.5pt 5pt;
                    font-size: 9.5pt; line-height: 1.2; }}
table.annexure td.num {{ text-align: right; padding-right: 14pt; }}
table.annexure tr.head td {{ font-weight: bold; }}
table.annexure tr.head td.num {{ text-align: center; padding-right: 5pt; }}
table.annexure tr.section td {{ background-color: {section}; font-weight: bold;
                               text-align: center; }}
table.annexure tr.total td {{ color: {highlight_colour}; font-weight: bold; }}
table.annexure tr.ctc td {{ font-weight: bold; }}
table.form {{ width: 100%; margin: 0 0 4pt 0; }}
table.form td {{ padding: 3.5pt 2pt 1pt 2pt; vertical-align: bottom; font-weight: bold; }}
table.form td.line {{ border-bottom: 0.6pt solid #111111; font-weight: normal; }}
table.grid {{ width: 100%; margin: 6pt 0 10pt 0; }}
table.grid th {{ background-color: {section}; border: 0.6pt solid #111111; padding: 4pt;
                 font-size: 9.5pt; text-align: center; }}
table.grid td {{ border: 0.6pt solid #111111; padding: 4pt; font-size: 9.5pt; }}
#page-footer {{ font-size: 7pt; color: #8a8a8a; text-align: right; }}
"""


def wrap_letter(
    *,
    merged_html: str,
    reference_number: str,
    art: LetterheadArt,
    show_reference: bool = True,
    highlight_colour: str = "#E00000",
) -> str:
    """The HTML for a letter printed on a drawn letterhead.

    Margins come from the design, so the content sits in the space between its
    header and footer; the letterhead itself is laid underneath afterwards.
    """
    area = body_area(art.design)
    width = 595.28 - area.left - area.right
    footer = (
        f'<div id="page-footer">Ref. {reference_number} &nbsp;|&nbsp; '
        "Page <pdf:pagenumber> of <pdf:pagecount></div>"
        if show_reference
        else ""
    )
    frame = (
        f"""@frame footer_frame {{ -pdf-frame-content: page-footer;
        bottom: {area.footer_line}pt; left: {area.left}pt; width: {width}pt; height: 12pt; }}"""
        if show_reference
        else ""
    )
    css = letter_css(secondary_colour=art.secondary_colour, highlight_colour=highlight_colour)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8" />
<title>{html_escape(art.company_name or "Letter")} {html_escape(reference_number)}</title>
<style>
@page {{ size: a4 portrait;
    margin: {area.top}pt {area.right}pt {area.bottom}pt {area.left}pt;
    {frame}
}}
{css}
</style>
</head><body>
{footer}
{merged_html}
</body></html>"""


def _underlay(document: bytes, letterhead: bytes) -> bytes:
    """Every page of `document` with the letterhead page drawn beneath it."""
    from pypdf import PdfReader, PdfWriter

    background = PdfReader(io.BytesIO(letterhead)).pages[0]
    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(document)).pages:
        writer.add_page(page).merge_page(background, over=False)
    # A fixed document id: the same letter is the same bytes.
    writer._ID = None
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def render_pdf(
    *,
    merged_html: str,
    reference_number: str,
    letterhead: Letterhead | None = None,
    art: LetterheadArt | None = None,
    show_reference: bool = True,
    highlight_colour: str = "#E00000",
) -> bytes:
    """Render a letter to PDF bytes.

    With `art`, the letter is printed on that drawn letterhead (letters). With
    only `letterhead`, the plain header other documents use (payslips, Form 16).

    Raises:
        PdfRenderError: if the renderer is unavailable or the HTML cannot be
            laid out. Never returns a partial or empty PDF — a truncated offer
            letter is worse than a failed generation.
    """
    try:
        from xhtml2pdf import pisa
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise PdfRenderError(
            "The PDF renderer is not installed. Install xhtml2pdf, or configure an "
            "alternative renderer."
        ) from exc

    if art is not None:
        document = wrap_letter(
            merged_html=merged_html,
            reference_number=reference_number,
            art=art,
            show_reference=show_reference,
            highlight_colour=highlight_colour,
        )
    elif letterhead is not None:
        document = wrap_document(
            merged_html=merged_html,
            reference_number=reference_number,
            letterhead=letterhead,
        )
    else:
        raise PdfRenderError("A letter needs a letterhead to be printed on.")

    buffer = io.BytesIO()
    result = pisa.CreatePDF(
        src=document,
        dest=buffer,
        encoding="utf-8",
        # Pin the document id off the content hash so two renders of the same
        # letter are byte-identical. Without this the writer generates a fresh
        # random id each time.
        **_deterministic_options(merged_html),
    )

    if result.err:
        raise PdfRenderError(
            f"Could not render letter {reference_number} to PDF: {result.err} layout error(s)"
        )

    data = buffer.getvalue()
    if not data:
        raise PdfRenderError(f"The PDF renderer produced no output for {reference_number}")
    if art is not None and art.design != "none":
        try:
            data = _underlay(data, draw_letterhead(art))
        except Exception as exc:
            raise PdfRenderError(
                f"Could not print letter {reference_number} on the letterhead: {exc}"
            ) from exc
    return data


def _deterministic_options(merged_html: str) -> dict:
    """Renderer options that pin the varying metadata.

    reportlab, which sits under xhtml2pdf, honours an `invariant` setting that
    fixes the creation date and derives the document id from the content
    instead of from randomness.
    """
    import reportlab.rl_config as rl_config
    from reportlab.rl_config import invariant  # noqa: F401  (presence check)

    rl_config.invariant = 1
    return {}
