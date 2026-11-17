"""The payout statement PDF sets the ₹ sign in the bundled rupee font."""

from __future__ import annotations

import io
import uuid
from datetime import date

from pypdf import PdfReader

from app.services.consultants import statement
from app.services.documents import pdf
from tests.integration.conftest import requires_db
from tests.integration.test_consultant_fixed_monthly import OCT_1, OCT_31, _fixed, _run
from tests.integration.test_consultant_payout_run import (
    CUT_OFF,
    PAY_DATE,
    _consultant,
    _hours,
    _line,
    _setup,
)

pytestmark = requires_db


def _fonts(data: bytes) -> set[str]:
    """The base font names used on the PDF's pages."""
    names: set[str] = set()
    for page in PdfReader(io.BytesIO(data)).pages:
        for font in (page["/Resources"].get("/Font") or {}).values():
            names.add(str(font.get_object()["/BaseFont"]))
    return names


def _capture(monkeypatch) -> list[str]:
    seen: list[str] = []
    real = pdf.render_pdf

    def capture(*, merged_html, **kwargs):
        seen.append(merged_html)
        return real(merged_html=merged_html, **kwargs)

    monkeypatch.setattr(pdf, "render_pdf", capture)
    return seen


async def test_fixed_monthly_statement_sets_the_rupee_in_its_font(
    session, tenant, hr_user, legal_entity, monkeypatch
) -> None:
    await _setup(session, tenant, hr_user)
    person = await _fixed(session, tenant, legal_entity, "FIX1", (date(2026, 1, 1), "100000"))
    await _hours(session, tenant, person, monday=OCT_1, period_end=OCT_31, total="160")
    line = _line(await _run(session, hr_user), person)
    seen = _capture(monkeypatch)

    _, data = await statement.payout_statement_pdf(session, hr_user, uuid.UUID(line["id"]))

    assert f"Fixed monthly {statement.RUPEE}100,000.00 × 31/31 days" in seen[0]  # noqa: RUF001
    # Every ₹ in the statement is inside the rupee span.
    assert seen[0].count("₹") == seen[0].count(statement.RUPEE)
    # The rupee font is embedded under the bundled glyph file's own name (a subset).
    assert any(name.endswith("+DejaVuSans") for name in _fonts(data))
    assert "₹" in PdfReader(io.BytesIO(data)).pages[0].extract_text()


async def test_hourly_statement_has_no_bare_rupee_sign(
    session, tenant, hr_user, legal_entity, monkeypatch
) -> None:
    await _setup(session, tenant, hr_user)
    person = await _consultant(session, tenant, legal_entity, "CON1")
    await _hours(session, tenant, person)
    line = _line(await _run(session, hr_user, cut_off=CUT_OFF, pay_date=PAY_DATE), person)
    seen = _capture(monkeypatch)

    _, data = await statement.payout_statement_pdf(session, hr_user, uuid.UUID(line["id"]))

    # Hourly rates and amounts are plain figures: no ₹ the base fonts could drop.
    assert data.startswith(b"%PDF")
    assert "₹" not in seen[0]
