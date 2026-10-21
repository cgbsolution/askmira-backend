"""The branded email every message is sent in.

A template is wording: a subject and a few sentences with `{placeholders}`.
This module is the envelope around it — the company's logo and colour at the
top, the wording laid out as paragraphs and lists, a button into the portal,
the company's own footer, and a small "Powered by AskMira" line that no tenant
setting removes.

Pure functions, no database: the caller resolves the brand and passes it in,
so the layout is testable on its own and the same for every sending path
(notifications, digests, candidate messages, the relay test).

What a template author can write is deliberately small, because the author is
an HR admin and the output lands in inboxes:

- a blank line starts a new paragraph;
- lines starting with "- " or "* " become a bulleted list;
- `**words**` become bold;
- an http(s) address becomes a link.

Everything else is escaped. No raw HTML from a template ever reaches the email.
"""

from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass

from app.services.notifications.adapters import InlineImage

__all__ = [
    "DEFAULT_ACCENT",
    "LOGO_CID",
    "EmailAction",
    "EmailBrand",
    "RenderedEmail",
    "body_to_html",
    "render_email",
]

#: AskMira's Deep Teal, used until a workspace picks its own colour.
DEFAULT_ACCENT = "#14595C"
LOGO_CID = "company-logo"

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_URL = re.compile(r"https?://[^\s<>\"')\]]+")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_BULLET = re.compile(r"^\s*[-*•]\s+")


@dataclass(frozen=True, slots=True)
class EmailBrand:
    """How one workspace's email looks."""

    company_name: str
    accent_colour: str = DEFAULT_ACCENT
    sign_off: str = ""
    footer_text: str = ""
    logo: bytes | None = None
    logo_mime: str = "image/png"


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    subject: str
    text: str
    html: str
    images: tuple[InlineImage, ...] = ()


def _accent(brand: EmailBrand) -> str:
    return brand.accent_colour if _HEX.match(brand.accent_colour or "") else DEFAULT_ACCENT


def _inline(line: str, accent: str) -> str:
    """One line of wording: escaped, then bold and links put back."""
    out: list[str] = []
    last = 0
    for match in _URL.finditer(line):
        out.append(_BOLD.sub(r"<strong>\1</strong>", html.escape(line[last : match.start()])))
        url = match.group(0).rstrip(".,;:")
        tail = match.group(0)[len(url) :]
        safe = html.escape(url, quote=True)
        out.append(
            f'<a href="{safe}" style="color:{accent};text-decoration:underline;'
            f'word-break:break-all;">{safe}</a>{html.escape(tail)}'
        )
        last = match.end()
    out.append(_BOLD.sub(r"<strong>\1</strong>", html.escape(line[last:])))
    return "".join(out)


def body_to_html(body: str, accent: str = DEFAULT_ACCENT) -> str:
    """Paragraphs and bulleted lists from plain wording."""
    blocks: list[str] = []
    paragraph: list[str] = []
    bullets: list[str] = []
    p_style = "margin:0 0 16px 0;font-size:15px;line-height:24px;color:#1E2430;"

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(f'<p style="{p_style}">' + "<br>".join(paragraph) + "</p>")
            paragraph.clear()

    def flush_bullets() -> None:
        if bullets:
            items = "".join(
                f'<li style="margin:0 0 6px 0;font-size:15px;line-height:22px;'
                f'color:#1E2430;">{item}</li>'
                for item in bullets
            )
            blocks.append(f'<ul style="margin:0 0 16px 0;padding-left:22px;">{items}</ul>')
            bullets.clear()

    for raw in (body or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            flush_paragraph()
            flush_bullets()
        elif _BULLET.match(line):
            flush_paragraph()
            bullets.append(_inline(_BULLET.sub("", line, count=1), accent))
        else:
            flush_bullets()
            paragraph.append(_inline(line.strip(), accent))
    flush_paragraph()
    flush_bullets()
    return "".join(blocks)


def _body_to_text(body: str) -> str:
    return _BOLD.sub(r"\1", (body or "").replace("\r\n", "\n")).strip()


@dataclass(frozen=True, slots=True)
class EmailAction:
    """A button in an email. `kind` picks its look: the main action, or a quieter one."""

    label: str
    url: str
    kind: str = "primary"  # primary | secondary | danger


_BUTTON_COLOURS = {"secondary": ("#FFFFFF", "#1E2430"), "danger": ("#B42318", "#FFFFFF")}

_PAGE = "margin:0;padding:0;background:#F4F1EA;"
_OUTER = 'role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"'
_INNER = 'role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"'
_INNER_STYLE = "width:100%;max-width:600px;font-family:Segoe UI,Helvetica,Arial,sans-serif;"
_CARD = "background:#FFFFFF;border-radius:12px;padding:32px 32px 24px 32px;"
_H1 = "margin:0 0 20px 0;font-size:21px;line-height:29px;font-weight:700;color:#1E2430;"
_POWERED = "margin:12px 0 0 0;font-size:11px;line-height:16px;color:#8A919C;"
_ASKMIRA = '<span style="font-weight:600;color:#14595C;">AskMira</span>'


def _buttons(actions: tuple[EmailAction, ...], accent: str) -> str:
    if not actions:
        return ""
    cells = []
    for action in actions:
        background, colour = _BUTTON_COLOURS.get(action.kind, (accent, "#FFFFFF"))
        border = "#D9DDD8" if action.kind == "secondary" else background
        safe = html.escape(action.url, quote=True)
        cells.append(
            f'<td style="padding:0 8px 8px 0;"><a href="{safe}" '
            f'style="display:inline-block;padding:12px 22px;font-size:15px;font-weight:600;'
            f"color:{colour};background:{background};border:1px solid {border};"
            f'text-decoration:none;border-radius:8px;">{html.escape(action.label)}</a></td>'
        )
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin:8px 0 16px 0;"><tr>{"".join(cells)}</tr></table>'
    )


def render_email(
    brand: EmailBrand,
    *,
    subject: str,
    body: str,
    action_url: str | None = None,
    action_label: str = "Open AskMira",
    actions: tuple[EmailAction, ...] = (),
    reason: str | None = None,
    embed_logo: bool = True,
) -> RenderedEmail:
    """Lay a message out in the workspace's branded email.

    `actions` are buttons (Approve / Reject on an approval email). Without
    them, `action_url` adds one button into the portal — unless the wording
    already carries a link of its own (a password reset, an interview
    booking): two calls to action in one short email is one too many.
    `reason` is the footer line saying why the reader got this. `embed_logo`
    attaches the logo inline (cid:) for sending; off, it is a data: URI, for
    a preview in the browser.
    """
    accent = _accent(brand)
    company = html.escape(brand.company_name or "Your company")
    title = html.escape(subject or "")
    if not actions and action_url and not _URL.search(body or ""):
        actions = (EmailAction(label=action_label, url=action_url),)

    images: tuple[InlineImage, ...] = ()
    if brand.logo:
        if embed_logo:
            src = f"cid:{LOGO_CID}"
            images = (InlineImage(cid=LOGO_CID, data=brand.logo, mime_type=brand.logo_mime),)
        else:
            src = f"data:{brand.logo_mime};base64,{base64.b64encode(brand.logo).decode()}"
        header = (
            f'<img src="{src}" alt="{company}" height="44" '
            f'style="display:block;height:44px;max-height:44px;width:auto;max-width:220px;'
            f'border:0;outline:none;text-decoration:none;">'
        )
    else:
        header = (
            f'<span style="font-size:20px;line-height:28px;font-weight:700;'
            f'color:{accent};">{company}</span>'
        )

    button = _buttons(actions, accent)

    sign_off = ""
    if brand.sign_off.strip():
        lines = "<br>".join(html.escape(line) for line in brand.sign_off.strip().splitlines())
        sign_off = (
            f'<p style="margin:8px 0 0 0;font-size:15px;line-height:24px;'
            f'color:#1E2430;">{lines}</p>'
        )

    footer_bits: list[str] = []
    if brand.footer_text.strip():
        footer_bits.append(
            "<br>".join(html.escape(line) for line in brand.footer_text.strip().splitlines())
        )
    if reason:
        footer_bits.append(html.escape(reason))
    footer = "".join(
        f'<p style="margin:0 0 8px 0;font-size:12px;line-height:18px;color:#5B6472;">{bit}</p>'
        for bit in footer_bits
    )

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>{title}</title>
</head>
<body style="{_PAGE}">
<div style="display:none;max-height:0;overflow:hidden;">{title}</div>
<table {_OUTER} style="background:#F4F1EA;">
<tr><td align="center" style="padding:32px 12px;">
<table {_INNER} style="{_INNER_STYLE}">
<tr><td style="padding:0 4px 16px 4px;">{header}</td></tr>
<tr><td style="{_CARD}border-top:4px solid {accent};">
<h1 style="{_H1}">{title}</h1>
{body_to_html(body, accent)}
{button}
{sign_off}
</td></tr>
<tr><td style="padding:20px 8px 0 8px;">
{footer}
<p style="{_POWERED}">Sent by {company} &middot; Powered by {_ASKMIRA}</p>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""

    text_parts = [subject or "", "", _body_to_text(body)]
    for action in actions:
        text_parts += ["", f"{action.label}: {action.url}"]
    if brand.sign_off.strip():
        text_parts += ["", brand.sign_off.strip()]
    text_parts += ["", "--"]
    if brand.footer_text.strip():
        text_parts.append(brand.footer_text.strip())
    if reason:
        text_parts.append(reason)
    text_parts.append(f"Sent by {brand.company_name or 'your company'} · Powered by AskMira")
    return RenderedEmail(
        subject=subject or "",
        text="\n".join(text_parts).strip() + "\n",
        html=html_doc,
        images=images,
    )
