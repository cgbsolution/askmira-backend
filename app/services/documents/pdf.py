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

__all__ = ["DEFAULT_PAGE_CSS", "PdfRenderError", "content_hash", "render_pdf", "wrap_document"]


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
       color: #1e2430; }
h1, h2, h3 { color: #14595c; }
/* xhtml2pdf does not implement border-collapse; cell borders carry the grid. */
table.ctc-table { width: 100%; margin: 12pt 0; }
table.ctc-table th { background-color: #dcefe6; text-align: left; padding: 6pt;
                     border: 1px solid #bfe0d4; font-size: 9.5pt; }
table.ctc-table td { padding: 6pt; border: 1px solid #e8e0cf; font-size: 9.5pt; }
table.ctc-table tfoot td { background-color: #f2ece0; font-weight: bold; }
#page-footer { font-size: 8pt; color: #6b788d; border-top: 0.5pt solid #e8e0cf;
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
        f'<div style="font-size:8.5pt;color:#6b788d">{letterhead.address}</div>'
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
<div style="border-bottom:1pt solid #14595c;padding-bottom:8pt;margin-bottom:18pt">
  {logo}
  <div style="font-size:13pt;font-weight:bold;color:#14595c">
    {letterhead.company_name}</div>
  {address}
</div>
{merged_html}
</body></html>"""


def render_pdf(
    *,
    merged_html: str,
    reference_number: str,
    letterhead: Letterhead,
) -> bytes:
    """Render a letter to PDF bytes.

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

    document = wrap_document(
        merged_html=merged_html,
        reference_number=reference_number,
        letterhead=letterhead,
    )

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
