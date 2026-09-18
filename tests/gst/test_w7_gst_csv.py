"""GSTR-2B CSV reading and register CSV writing (W7)."""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal

import pytest

from app.services.gst import csv_io

HEADER = "vendor_gstin,vendor_name,invoice_number,invoice_date,taxable_value,cgst,sgst,igst,cess\n"


def test_lines_are_read_as_decimals_and_dates() -> None:
    lines, errors = csv_io.parse_gstr2b_csv(
        HEADER + "27aapfu0939f1zv,Hotel,INV/1,14-10-2026,\"1,000.00\",90,90,,\n"
    )
    assert not errors
    line = lines[0]
    assert line["vendor_gstin"] == "27AAPFU0939F1ZV"
    assert line["invoice_date"] == date(2026, 10, 14)
    assert line["taxable_value"] == Decimal("1000.00")
    assert line["igst"] == Decimal("0")


def test_bad_rows_are_numbered() -> None:
    text = HEADER + (
        ",x,INV1,2026-10-01,1,0,0,0,0\n"
        "27AAPFU0939F1ZV,x,,2026-10-01,1,0,0,0,0\n"
        "27AAPFU0939F1ZV,x,INV3,someday,1,0,0,0,0\n"
        "27AAPFU0939F1ZV,x,INV4,2026-10-01,-5,0,0,0,0\n"
        "27AAPFU0939F1ZV,x,INV5,2026-10-01,1.234,0,0,0,0\n"
        "27AAPFU0939F1ZV,x,INV6,2026-10-01,10,0,0,0,0\n"
    )
    lines, errors = csv_io.parse_gstr2b_csv(text)
    assert [e["row"] for e in errors] == [1, 2, 3, 4, 5]
    assert [line["invoice_number"] for line in lines] == ["INV6"]


def test_a_file_without_the_required_header_is_refused() -> None:
    with pytest.raises(csv_io.GstCsvError):
        csv_io.parse_gstr2b_csv("gstin,number\n")


def test_binary_and_oversized_files_are_refused() -> None:
    with pytest.raises(csv_io.GstCsvError):
        csv_io.decode_csv(b"PK\x03\x04\x00")
    with pytest.raises(csv_io.GstCsvTooLargeError):
        csv_io.decode_csv(b"x" * 5, max_bytes=4)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("=HYPERLINK(\"x\")", "'=HYPERLINK(\"x\")"),
        ("+1+1", "'+1+1"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("-12.50", "-12.50"),
        ("-cmd", "'-cmd"),
        ("plain", "plain"),
        (None, ""),
    ],
)
def test_formulas_are_neutralised(value, expected) -> None:
    assert csv_io.neutralise(value) == expected


def test_the_register_csv_lists_both_sections() -> None:
    register = {
        "eligible": [{"vendor_gstin": "27AAPFU0939F1ZV", "invoice_number": "=1+1", "cgst": "9"}],
        "awaiting_review": [{"vendor_gstin": "29AAGCB7383J1Z4", "invoice_number": "B"}],
    }
    rows = list(csv.reader(io.StringIO(csv_io.register_to_csv(register))))
    assert rows[0][0] == "section"
    assert rows[1][0] == "eligible" and rows[1][2] == "'=1+1"
    assert rows[2][0] == "awaiting_review"
