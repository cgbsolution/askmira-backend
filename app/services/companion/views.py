"""What the model may read of a tool's result.

A tool returns cards for the screen. The model reads a plain-text view of the
same cards, so it can reason over the figures and talk about them — with three
things taken out first, whatever the tool returned:

- columns that carry contact details, statutory identifiers, bank details or
  the free-text reasons people give (a leave reason can disclose health);
- anything in the remaining text that looks like an email address, a phone
  number, an Aadhaar number or a PAN;
- links and internal ids, except the refs and offer ids the model needs to
  name what it is acting on.

What the view carries is declared per tool (`Tool.reads`, DPDP categories) and
the turn's transfer row records the union. A workspace can switch reading off
(`CompanionConfig.ai_reads_results`); the model is then told only that the
result is on the person's screen.
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = ["HIDDEN", "MAX_VIEW_CHARS", "scrub", "view_of"]

#: A tool result longer than this is cut, and the model is told so.
MAX_VIEW_CHARS = 9000
MAX_ROWS = 40

HIDDEN = (
    "The result is shown to the person on their screen, under your reply. You may not read "
    "this kind of record here, so do not guess what it says: tell them it is below."
)

_DROP_COLUMN = re.compile(
    r"e-?mail|phone|mobile|contact|address|aadhaa?r|\bpan\b|\buan\b|esic|ifsc|bank|"
    r"account (?:no|number)|a/c|passport|reason|remark|comment|subject|date of birth|\bdob\b",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# A number ends where a digit (or a separator then a digit) does not follow, so
# "9876543210," in a sentence is caught and "6123456789.00" (an amount) is not.
_END = r"(?!\d|[.,]\d)"
_PHONE = re.compile(r"(?<![\d₹,.])(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}" + _END)
_AADHAAR = re.compile(
    r"(?<![\d,.])\d{4}[\s-]\d{4}[\s-]\d{4}" + _END + r"|(?<![\d,.₹])\d{12}" + _END
)
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")


def scrub(value: Any) -> str:
    text = "" if value is None else str(value)
    text = _EMAIL.sub("[email removed]", text)
    text = _PAN.sub("[PAN removed]", text)
    text = _AADHAAR.sub("[number removed]", text)
    return _PHONE.sub("[phone removed]", text)


def _keep(columns: list[Any]) -> list[int]:
    return [i for i, c in enumerate(columns) if not _DROP_COLUMN.search(str(c))]


def _table(columns: list[Any], rows: list[list[Any]], refs: list[Any] | None = None) -> list[str]:
    keep = _keep(columns)
    head = [scrub(columns[i]) for i in keep]
    if refs:
        head.append("ref")
    lines = [" | ".join(head)]
    for index, row in enumerate(rows[:MAX_ROWS]):
        cells = [scrub(row[i]) if i < len(row) else "" for i in keep]
        if refs:
            cells.append(str(refs[index]) if index < len(refs) and refs[index] else "")
        lines.append(" | ".join(cells))
    if len(rows) > MAX_ROWS:
        lines.append(f"(and {len(rows) - MAX_ROWS} more rows, not shown here)")
    return lines


def _card(card: dict[str, Any]) -> list[str]:
    kind = card.get("type")
    if kind == "notice":
        return [f"Notice: {scrub(card.get('text'))}"]
    if kind == "table":
        lines = [f"Table: {scrub(card.get('title'))} ({scrub(card.get('caption'))})"]
        if card.get("source"):
            lines[0] += f", from {card['source']}"
        return lines + _table(card.get("columns") or [], card.get("rows") or [], card.get("refs"))
    if kind == "record":
        lines = [f"Record: {scrub(card.get('title'))}"]
        for key in ("subtitle", "heading"):
            if card.get(key):
                lines.append(scrub(card[key]))
        for figure in card.get("figures") or []:
            if _DROP_COLUMN.search(str(figure.get("label"))):
                continue
            lines.append(f"- {scrub(figure.get('label'))}: {scrub(figure.get('value'))}")
        table = card.get("table") or {}
        if table.get("rows"):
            lines += _table(table.get("columns") or [], table["rows"], card.get("refs"))
        if card.get("note"):
            lines.append(f"Note: {scrub(card['note'])}")
        return lines
    if kind == "chart":
        title, caption = scrub(card.get("title")), scrub(card.get("caption"))
        lines = [f"Chart: {title} ({caption}), unit {card.get('unit')}"]
        x = card.get("x") or []
        for series in card.get("series") or []:
            pairs = ", ".join(
                f"{label} {value}"
                for label, value in zip(x, series.get("values") or [], strict=False)
            )
            lines.append(f"- {scrub(series.get('name'))}: {pairs}")
        return lines
    if kind == "policy":
        lines = [f"Policy answer ({card.get('outcome')}): {scrub(card.get('answer'))}"]
        cited = [str(c.get("label")) for c in card.get("citations") or []]
        if cited:
            lines.append("Cited: " + "; ".join(cited))
        return lines
    if kind == "report":
        lines = [f"Report read as: {scrub(card.get('interpretation'))}"]
        if card.get("problem"):
            lines.append(f"Problem: {scrub(card['problem'])}")
        result = card.get("result") or {}
        columns = [c.get("label") or c.get("key") for c in result.get("columns") or []]
        if columns:
            lines += _table(columns, result.get("rows") or [])
        return lines
    if kind == "own_record":
        return [f"From the person's own record: {scrub(card.get('text'))}"]
    if kind == "choices":
        options = "; ".join(
            f"{scrub(o.get('label'))}" + (f" ({scrub(o.get('detail'))})" if o.get("detail") else "")
            for o in card.get("options") or []
        )
        return [
            f"Choices shown: {scrub(card.get('title'))}. {options}. The person picks one by "
            "clicking it, or by naming it (an employee code settles it)."
        ]
    if kind == "launch":
        return [f"A button to open {card.get('name')} is shown."]
    if kind == "proposal":
        status = card.get("status")
        lines = [
            f"Offer {card.get('action_id')} ({card.get('kind')}), status {status}: "
            f"{scrub(card.get('title'))}"
        ]
        for figure in card.get("figures") or []:
            lines.append(f"- {scrub(figure.get('label'))}: {scrub(figure.get('value'))}")
        for note in card.get("notes") or []:
            lines.append(f"Note: {scrub(note)}")
        if status == "proposed":
            lines.append(
                "Nothing has been done yet. Tell the person plainly what this will do and ask "
                "them to confirm: they press the button on the offer, or reply yes."
            )
        result = card.get("result") or {}
        if result.get("text"):
            lines.append(f"Outcome: {scrub(result['text'])}")
        return lines
    return []


def view_of(cards: list[dict[str, Any]], *, allowed: bool) -> str:
    """The text the model reads for one tool call's cards."""
    if not allowed:
        offers = [c for c in cards if c.get("type") == "proposal"]
        if offers:
            ids = ", ".join(str(c.get("action_id")) for c in offers)
            return (
                f"An offer ({ids}) is shown to the person with a Confirm button. Ask them to "
                "check it and confirm. " + HIDDEN
            )
        return HIDDEN
    lines: list[str] = []
    for card in cards:
        lines += _card(card)
        lines.append("")
    text = "\n".join(lines).strip() or "The tool returned nothing."
    if len(text) > MAX_VIEW_CHARS:
        text = text[:MAX_VIEW_CHARS] + "\n(cut here; the person sees all of it on screen)"
    return text


def as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
