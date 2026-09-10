"""The letter merge engine.

Two rules here are not stylistic:

1. **A required token that cannot be resolved blocks generation**, and the
   error names the token. A letter that goes out saying "Dear ," is a real
   incident, and the failure mode is silent — nobody notices until the
   candidate does.

2. **Template HTML is trusted; merged values are not.** The template is
   authored by a tenant admin — it is their own document design, and it
   contains real markup. The values merged into it come from employee records,
   which contain whatever someone typed. Every merged value is HTML-escaped.

Values resolve **as of a date**, not as of now. A relieving letter issued today
for an exit last month must show the salary that was in force at exit.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from app.services.documents.indian_numbers import amount_in_words, group_indian

__all__ = [
    "LetterMergeError",
    "MergeContext",
    "extract_tokens",
    "render_template",
]

# {{ employee.first_name }} — whitespace tolerated, token chars restricted.
TOKEN_PATTERN = re.compile(r"\{\{\s*([a-zA-Z][a-zA-Z0-9_.]*)\s*\}\}")


class LetterMergeError(ValueError):
    """A token could not be resolved, or a required value is missing."""


class SalaryLike(Protocol):
    ctc_annual: Any
    effective_from: date
    components: Any


@dataclass(slots=True)
class MergeContext:
    """Everything a template can draw on, resolved as of `as_of`.

    Built by the caller, which is what keeps this module free of database
    access and therefore testable without one.
    """

    as_of: date
    employee: dict[str, Any] = field(default_factory=dict)
    salary: dict[str, Any] = field(default_factory=dict)
    tenant: dict[str, Any] = field(default_factory=dict)
    manual: dict[str, Any] = field(default_factory=dict)
    # Resolved component rows for the CTC table, newest structure first.
    salary_components: list[dict[str, Any]] = field(default_factory=list)


def extract_tokens(*html_fragments: str | None) -> set[str]:
    """Every token referenced across a template's header, body and footer."""
    found: set[str] = set()
    for fragment in html_fragments:
        if fragment:
            found.update(match.group(1) for match in TOKEN_PATTERN.finditer(fragment))
    return found


# ── Computed handlers ───────────────────────────────────────────────────────
# The things that are not a single field. Each returns raw HTML and is
# responsible for escaping its own inputs.


def _ctc_table(context: MergeContext) -> str:
    """The salary breakdown table every offer and appointment letter carries."""
    if not context.salary_components:
        raise LetterMergeError(
            "computed.ctc_table needs a salary breakdown, but this employee has no "
            "salary assigned as of the letter's as-of date"
        )

    rows = []
    for component in context.salary_components:
        if not component.get("part_of_ctc", True):
            continue
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(component['name']))}</td>"
            f"<td style=\"text-align:right\">"
            f"{html.escape(group_indian(component['monthly_amount']))}</td>"
            f"<td style=\"text-align:right\">"
            f"{html.escape(group_indian(component['annual_amount']))}</td>"
            "</tr>"
        )

    total = context.salary.get("ctc_annual", 0)
    return (
        '<table class="ctc-table" cellspacing="0" cellpadding="6">'
        "<thead><tr><th>Component</th><th>Monthly (INR)</th>"
        "<th>Annual (INR)</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "<tfoot><tr><td><strong>Total CTC</strong></td><td></td>"
        f'<td style="text-align:right"><strong>'
        f"{html.escape(group_indian(total))}</strong></td></tr></tfoot>"
        "</table>"
    )


def _ctc_in_words(context: MergeContext) -> str:
    ctc = context.salary.get("ctc_annual")
    if ctc is None:
        raise LetterMergeError("computed.ctc_in_words needs a salary, but none is assigned")
    return html.escape(amount_in_words(ctc))


def _tenure(context: MergeContext) -> str:
    """Service length as years and months — what an experience letter states."""
    joined = context.employee.get("date_of_joining")
    if not joined:
        raise LetterMergeError("computed.tenure needs the employee's date of joining")

    end = context.employee.get("date_of_exit") or context.as_of
    months = (end.year - joined.year) * 12 + (end.month - joined.month)
    if end.day < joined.day:
        months -= 1
    months = max(0, months)
    years, remainder = divmod(months, 12)

    parts = []
    if years:
        parts.append(f"{years} year{'s' if years != 1 else ''}")
    if remainder:
        parts.append(f"{remainder} month{'s' if remainder != 1 else ''}")
    return html.escape(" and ".join(parts) or "less than a month")


def _employee_full_name(context: MergeContext) -> str:
    parts = [context.employee.get("first_name"), context.employee.get("last_name")]
    name = " ".join(p for p in parts if p)
    if not name:
        raise LetterMergeError("computed.full_name needs at least a first name")
    return html.escape(name)


def _today_long(context: MergeContext) -> str:
    return html.escape(context.as_of.strftime("%d %B %Y"))


COMPUTED_HANDLERS = {
    "ctc_table": _ctc_table,
    "ctc_in_words": _ctc_in_words,
    "tenure": _tenure,
    "full_name": _employee_full_name,
    "letter_date": _today_long,
}


# ── Resolution ──────────────────────────────────────────────────────────────


def _format_scalar(value: Any) -> str:
    """Render a value for a letter, then escape it.

    Money and dates get Indian conventions; everything else is stringified.
    Escaping happens here, once, so no caller can forget it.
    """
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return html.escape(group_indian(value))
    if isinstance(value, date):
        return html.escape(value.strftime("%d %B %Y"))
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return html.escape(str(value))


def _resolve(token: str, context: MergeContext) -> str:
    """Resolve one token, or raise naming it."""
    namespace, _, path = token.partition(".")

    if namespace == "computed":
        handler = COMPUTED_HANDLERS.get(path)
        if handler is None:
            raise LetterMergeError(
                f"'{{{{{token}}}}}' is not a known computed field. Available: "
                + ", ".join(f"computed.{name}" for name in sorted(COMPUTED_HANDLERS))
            )
        # A computed handler returns HTML it has escaped itself — a CTC table
        # is markup by design.
        return handler(context)

    sources = {
        "employee": context.employee,
        "salary": context.salary,
        "tenant": context.tenant,
        "manual": context.manual,
    }
    if namespace not in sources:
        raise LetterMergeError(
            f"'{{{{{token}}}}}' uses an unknown source '{namespace}'. Available: "
            + ", ".join(sorted([*sources, "computed"]))
        )

    source = sources[namespace]
    if not path:
        raise LetterMergeError(f"'{{{{{token}}}}}' names a source but no field")

    value: Any = source
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise LetterMergeError(
                f"'{{{{{token}}}}}' could not be resolved — "
                f"'{part}' is not available on {namespace}"
            )
        value = value[part]

    return _format_scalar(value)


def render_template(
    *,
    body_html: str,
    context: MergeContext,
    header_html: str | None = None,
    footer_html: str | None = None,
    required_tokens: set[str] | None = None,
) -> str:
    """Merge a template into final HTML.

    Args:
        body_html: the template body. Trusted markup.
        context: resolved sources, as of the letter's date.
        required_tokens: tokens the template declares as required. Any that
            resolve to an empty string block generation — that is the "Dear ,"
            guard.

    Raises:
        LetterMergeError: naming the offending token.
    """
    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    empty_required: list[str] = []

    for token in extract_tokens(header_html, body_html, footer_html):
        try:
            value = _resolve(token, context)
        except LetterMergeError as exc:
            unresolved.append(f"{token} ({exc})")
            continue

        if not value.strip() and (required_tokens is None or token in required_tokens):
            empty_required.append(token)
        resolved[token] = value

    if unresolved:
        raise LetterMergeError(
            "This letter cannot be generated — "
            + "; ".join(unresolved)
        )
    if empty_required:
        raise LetterMergeError(
            "This letter cannot be generated: required field(s) resolved to nothing — "
            + ", ".join(sorted(empty_required))
            + ". A letter must never go out with a blank where a value should be."
        )

    def substitute(fragment: str | None) -> str:
        if not fragment:
            return ""
        return TOKEN_PATTERN.sub(lambda m: resolved.get(m.group(1), ""), fragment)

    return "".join(
        part
        for part in (substitute(header_html), substitute(body_html), substitute(footer_html))
        if part
    )
