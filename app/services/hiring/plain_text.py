"""Stored HTML, turned into plain paragraphs for a page that must not render HTML.

Job descriptions, the careers page "about" copy and the privacy notice are
stored as HTML because the letter and board integrations expect it. The public
pages render text only — no `dangerouslySetInnerHTML`, no sanitiser to keep up
to date — so the HTML is reduced to paragraphs here, by construction: the output
is a list of strings and contains no markup at all.

Pure. Standard library only.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

__all__ = ["html_to_paragraphs"]

_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "ul",
        "ol",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "tr",
        "section",
        "article",
        "header",
        "footer",
        "blockquote",
    }
)
_SKIP_TAGS = frozenset({"script", "style", "template", "noscript", "iframe", "object"})


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skipping += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
            if tag == "li":
                self.parts.append("• ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skipping = max(0, self._skipping - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_to_paragraphs(html: str | None, *, limit: int = 60_000) -> list[str]:
    """Paragraphs of text, with every tag, script and style removed."""
    if not html:
        return []
    collector = _Collector()
    try:
        collector.feed(html[:limit])
        collector.close()
    except Exception:
        return []
    text = "".join(collector.parts)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n")]
    paragraphs: list[str] = []
    for line in lines:
        if line:
            paragraphs.append(line)
    return paragraphs
