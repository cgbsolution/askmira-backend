"""The challan upload files, against the samples HR uploads today.

The layouts are copied from the user's own files: "CHALLAN UPLOAD.csv" (the
ECR upload sheet) and the ESIC portal's "MC_Template.xls". A change here is a
format change and has to be deliberate.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import xlrd

from app.services.filings.records import EcrLine, EcrReturn, EsicLine, EsicReturn
from app.services.filings.serialisers import (
    ESIC_MC_HEADINGS,
    serialise_ecr_upload_csv,
    serialise_esic_mc_xls,
)


def _ecr(*lines: EcrLine) -> EcrReturn:
    return EcrReturn(establishment_code="KNBNG0012345000", wage_month="102026", lines=list(lines))


def _line(**overrides) -> EcrLine:
    base = {
        "uan": "101838392339",
        "member_name": "Roja",
        "gross_wages": Decimal("29229.40"),
        "epf_wages": Decimal("16333"),
        "eps_wages": Decimal("16333"),
        "edli_wages": Decimal("16333"),
        "employee_share": Decimal("1960"),
        "employer_epf_share": Decimal("599"),
        "eps_contribution": Decimal("1361"),
        "ncp_days": Decimal("0"),
    }
    base.update(overrides)
    return EcrLine(**base)


def test_the_ecr_upload_csv_matches_the_challan_upload_sheet() -> None:
    content = serialise_ecr_upload_csv(_ecr(_line()))
    rows = content.split("\r\n")
    assert rows[0] == (
        "UAN,MEMBER_NAME,GROSS_WAGES,EPF_WAGES,EPS_WAGES,EDLI_WAGES,EPF_CONTRI_REMITTED,"
        "EPS_CONTRI_REMITTED,EPF_EPS_DIFF_REMITTED,NCP_DAYS,REFUND_OF_ADVANCES"
    )
    # The sample's first row, field for field: member share, then pension, then the difference.
    assert rows[1] == "101838392339,Roja,29229,16333,16333,16333,1960,1361,599,0,0"
    assert content.endswith("\r\n") and not content.startswith("﻿")


def test_the_ecr_upload_csv_rounds_ncp_days_up_and_quotes_a_comma() -> None:
    content = serialise_ecr_upload_csv(_ecr(_line(member_name="Rao, K", ncp_days=Decimal("1.5"))))
    row = content.split("\r\n")[1]
    assert row.startswith('101838392339,"Rao, K",')
    assert row.endswith(",2,0")


def test_an_empty_ecr_upload_is_just_the_header() -> None:
    assert serialise_ecr_upload_csv(_ecr()).count("\r\n") == 1


def _esic(*lines: EsicLine) -> EsicReturn:
    return EsicReturn(
        employer_code="53000123450001001",
        contribution_period="",
        wage_month="102026",
        lines=list(lines),
    )


def _ip(**overrides) -> EsicLine:
    base = {
        "ip_number": "5042408205",
        "ip_name": "Roja",
        "days_worked": Decimal("30"),
        "wages_paid": Decimal("16334.60"),
        "employee_contribution": Decimal("123"),
        "employer_contribution": Decimal("531"),
    }
    base.update(overrides)
    return EsicLine(**base)


def _sheets(content: bytes) -> xlrd.book.Book:
    return xlrd.open_workbook(file_contents=content, formatting_info=True)


def test_the_mc_template_is_an_excel_97_file_in_the_portals_layout() -> None:
    content = serialise_esic_mc_xls(
        _esic(_ip(), _ip(ip_number="0012345678", days_worked=Decimal("24.5")))
    )
    # OLE2 compound document: the portal takes .xls (Excel 97-2003) only.
    assert content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    book = _sheets(content)
    assert [sheet.name for sheet in book.sheets()] == ["Sheet1", "Instructions & Reason Codes"]
    sheet = book.sheet_by_index(0)
    assert tuple(sheet.row_values(0)) == ESIC_MC_HEADINGS
    assert sheet.row_values(0)[0] == "IP Number "  # the template's trailing space
    assert sheet.row_values(1) == ["5042408205", "Roja", 30.0, 16335.0, "", ""]
    # A leading zero survives (text), and half a day rounds up (rule 2).
    assert sheet.row_values(2)[0] == "0012345678"
    assert sheet.row_values(2)[2] == 25.0
    # Every cell is in Text format (rule 10).
    for column in range(6):
        cell = sheet.cell(1, column)
        if cell.value != "":
            fmt = book.format_map[book.xf_list[cell.xf_index].format_key].format_str
            assert fmt == "@"


def test_a_leaver_with_no_days_gets_reason_2_and_the_last_working_day() -> None:
    content = serialise_esic_mc_xls(
        _esic(
            _ip(
                days_worked=Decimal(0),
                wages_paid=Decimal(0),
                reason_code=2,
                last_working_day=date(2026, 9, 30),
            ),
            _ip(ip_number="5350522846", days_worked=Decimal(0), wages_paid=Decimal(0)),
        )
    )
    sheet = _sheets(content).sheet_by_index(0)
    assert sheet.row_values(1)[2:] == [0.0, 0.0, 2.0, "30/09/2026"]
    assert sheet.row_values(2)[2:] == [0.0, 0.0, 0.0, ""]


def test_someone_paid_for_part_of_the_month_has_no_reason_or_last_day() -> None:
    """Rule 6: worked at least a day and left mid-month → no last working day."""
    content = serialise_esic_mc_xls(
        _esic(_ip(days_worked=Decimal(12), reason_code=2, last_working_day=date(2026, 10, 12)))
    )
    assert _sheets(content).sheet_by_index(0).row_values(1)[4:] == ["", ""]


def test_the_mc_template_carries_the_reason_code_sheet() -> None:
    guide = _sheets(serialise_esic_mc_xls(_esic())).sheet_by_index(1)
    assert guide.row_values(1)[:3] == ["Reason", "Code", "Note"]
    assert guide.row_values(4)[:2] == ["Left Service", 2.0]
    assert guide.row_values(15)[:2] == ["Duplicate IP", 13.0]
