"""Receipt validation, report status and the expense input rules (W7)."""

from __future__ import annotations

import io

import pytest
from PIL import Image
from pydantic import ValidationError

from app.models.expenses import ExpenseClaimStatusEnum as C
from app.models.expenses import ExpenseReportStatusEnum as R
from app.schemas.expenses import ClaimIn, DecisionIn
from app.services.expenses import receipts
from app.services.expenses.service import derive_report_status


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_the_type_comes_from_the_bytes() -> None:
    verdict = receipts.validate_receipt(_png())
    assert verdict.content_type == "image/png"
    assert verdict.extension == ".png"
    assert len(verdict.sha256) == 64


@pytest.mark.parametrize(
    ("data", "status"),
    [
        (b"", 400),
        (b"<html>not a receipt</html>", 415),
        (b"\x89PNG\r\n\x1a\ntruncated", 415),
        (b"%PDF-1.7 /Launch /Win", 415),
    ],
)
def test_bad_uploads_are_refused(data: bytes, status: int) -> None:
    with pytest.raises(receipts.ReceiptError) as exc:
        receipts.validate_receipt(data)
    assert exc.value.status_code == status


def test_an_oversized_upload_is_refused() -> None:
    with pytest.raises(receipts.ReceiptError) as exc:
        receipts.validate_receipt(b"%PDF-" + b"0" * 20, max_bytes=10)
    assert exc.value.status_code == 413


def test_a_plain_pdf_passes() -> None:
    assert receipts.validate_receipt(b"%PDF-1.4\n1 0 obj\n").content_type == "application/pdf"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("../../etc/passwd", "passwd.pdf"),
        ("C:\\bills\\taxi <script>.exe", "taxi _script.pdf"),
        (None, "receipt.pdf"),
        ("...", "receipt.pdf"),
    ],
)
def test_the_display_name_is_never_a_path(name, expected) -> None:
    assert receipts.safe_display_name(name, ".pdf") == expected


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], None),
        ([C.CANCELLED], R.CANCELLED),
        ([C.SUBMITTED, C.APPROVED], R.SUBMITTED),
        ([C.APPROVED, C.REIMBURSED, C.CANCELLED], R.APPROVED),
        ([C.REJECTED, C.REJECTED], R.REJECTED),
        ([C.APPROVED, C.REJECTED], R.PARTIALLY_APPROVED),
    ],
)
def test_report_status_is_derived_from_its_lines(statuses, expected) -> None:
    assert derive_report_status(statuses) is expected


def test_a_rejection_needs_a_reason() -> None:
    with pytest.raises(ValidationError):
        DecisionIn(approve=False, note=" ")
    assert DecisionIn(approve=True).approve


def test_a_claim_amount_is_a_decimal_and_a_currency_is_three_letters() -> None:
    claim = ClaimIn(category_key="taxi", city_tier="a", amount="0.10", incurred_on="2026-09-07")
    assert str(claim.amount) == "0.10"
    assert claim.employee_id is None
    with pytest.raises(ValidationError):
        ClaimIn(category_key="taxi", city_tier="a", amount="1", incurred_on="2026-09-07",
                currency="E1")
