"""Reading a bank response CSV (slice 11 §5)."""

from __future__ import annotations

import pytest

from app.services.disbursement.responses import (
    MAX_RESPONSE_BYTES,
    ResponseFileError,
    parse_response_csv,
)


def test_a_well_formed_file_reads_every_row() -> None:
    data = (
        b"account_number,status,reason,reference\r\n"
        b"50100123456789,SUCCESS,,UTR1\r\n"
        b"50100999999999,FAILED,Account closed,UTR2\r\n"
    )
    parsed = parse_response_csv(data)
    assert parsed.problems == []
    assert parsed.rows == [
        {"account_number": "50100123456789", "status": "SUCCESS", "reason": None,
         "reference": "UTR1"},
        {"account_number": "50100999999999", "status": "FAILED", "reason": "Account closed",
         "reference": "UTR2"},
    ]


def test_header_aliases_and_a_byte_order_mark_are_accepted() -> None:
    data = "﻿Account No,Status,Remarks,UTR\n5010 0123,success,,X\n".encode()
    parsed = parse_response_csv(data)
    assert parsed.rows[0]["account_number"] == "50100123"
    assert parsed.rows[0]["reference"] == "X"


def test_a_bad_row_is_reported_and_the_rest_still_read() -> None:
    data = b"account_number,status\nabc,FAILED\n50100123456789,\n50100123456789,SUCCESS\n"
    parsed = parse_response_csv(data)
    assert len(parsed.rows) == 1
    assert parsed.problems == [
        "Line 2: the account number is not digits.",
        "Line 3: no status.",
    ]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "empty"),
        (b"name,amount\nx,1\n", "missing account_number, status"),
        (b"%PDF-1.7\x00\x00", "not a text file"),
        (b"\xff\xfe\xfa", "not UTF-8"),
        (b"account_number,status\n", "no rows"),
        (b"a" * (MAX_RESPONSE_BYTES + 1), "larger"),
    ],
    ids=["empty", "no-header", "binary", "not-utf8", "no-rows", "too-large"],
)
def test_an_unreadable_file_is_refused_with_a_reason(data: bytes, message: str) -> None:
    with pytest.raises(ResponseFileError) as exc:
        parse_response_csv(data)
    assert message in str(exc.value)
