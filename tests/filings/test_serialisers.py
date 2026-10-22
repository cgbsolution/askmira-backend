"""Slice 10 gate: golden-file tests.

A generated file either matches a known-good sample byte for byte or it does
not. There is no "looks right" — the portal is not reading it charitably, and a
trailing newline or an LF where a CRLF belongs is a rejection.

The golden strings are written inline with explicit escapes rather than stored
as fixture files, so a diff shows exactly which byte moved. `\\r\\n` in these
tests is the format speaking, not a stylistic choice.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.filings.records import (
    EcrLine,
    EcrReturn,
    EsicLine,
    EsicReturn,
    PtChallan,
    PtLine,
    Tds24QReturn,
    TdsChallanLine,
    TdsDeducteeLine,
    TdsSalaryDetail,
)
from app.services.filings.serialisers import (
    ECR_COLUMNS,
    ECR_DELIMITER,
    SerialiserError,
    content_hash,
    serialise_ecr,
    serialise_esic,
    serialise_pt_challan,
    serialise_tds_24q,
)


def D(value: str | int) -> Decimal:  # noqa: N802 - a Decimal alias, used throughout
    return Decimal(str(value))


# ═══════════════════════════════════════════════════════════════════════════
# PF ECR
# ═══════════════════════════════════════════════════════════════════════════


def ecr_line(**overrides) -> EcrLine:
    base = {
        "uan": "100123456789",
        "member_name": "ANITA SHARMA",
        "gross_wages": D(50_000),
        "epf_wages": D(15_000),
        "eps_wages": D(15_000),
        "edli_wages": D(15_000),
        "employee_share": D(1_800),
        "employer_epf_share": D(550),
        "eps_contribution": D(1_250),
        "ncp_days": D(0),
    }
    base.update(overrides)
    return EcrLine(**base)


def test_the_ecr_matches_the_golden_file_byte_for_byte() -> None:
    """The gate. One member, every column, exact bytes."""
    ecr = EcrReturn(
        establishment_code="KNBNG0012345000",
        wage_month="062026",
        lines=[ecr_line()],
    )
    # EE share, then the EPS contribution, then the EPF-EPS difference.
    expected = (
        "100123456789#~#ANITA SHARMA#~#50000#~#15000#~#15000#~#15000#~#1800#~#1250#~#550"
        "#~#0#~#0\r\n"
    )
    assert serialise_ecr(ecr) == expected


def test_a_two_member_ecr_matches_the_golden_file() -> None:
    ecr = EcrReturn(
        establishment_code="KNBNG0012345000",
        wage_month="062026",
        lines=[
            ecr_line(),
            ecr_line(
                uan="100987654321",
                member_name="RAHUL IYER",
                gross_wages=D(28_000),
                epf_wages=D(14_000),
                eps_wages=D(14_000),
                edli_wages=D(14_000),
                employee_share=D(1_680),
                employer_epf_share=D(513),
                eps_contribution=D(1_167),
                ncp_days=D(2),
            ),
        ],
    )
    expected = (
        "100123456789#~#ANITA SHARMA#~#50000#~#15000#~#15000#~#15000#~#1800#~#1250#~#550"
        "#~#0#~#0\r\n"
        "100987654321#~#RAHUL IYER#~#28000#~#14000#~#14000#~#14000#~#1680#~#1167#~#513"
        "#~#2#~#0\r\n"
    )
    assert serialise_ecr(ecr) == expected


def test_the_ecr_uses_crlf_not_lf() -> None:
    """The EPFO uploader is a Windows tool; an LF file reads as one long line."""
    output = serialise_ecr(EcrReturn("EST", "062026", [ecr_line()]))
    assert "\r\n" in output
    assert output.count("\n") == output.count("\r\n"), "no bare LF anywhere"


def test_the_ecr_has_no_header_row() -> None:
    """The uploader treats a header as a member record and fails on it."""
    output = serialise_ecr(EcrReturn("EST", "062026", [ecr_line()]))
    assert output.startswith("100123456789")


def test_every_ecr_line_has_the_prescribed_field_count() -> None:
    output = serialise_ecr(EcrReturn("EST", "062026", [ecr_line(), ecr_line()]))
    for line in output.strip().split("\r\n"):
        assert len(line.split(ECR_DELIMITER)) == len(ECR_COLUMNS)


def test_ecr_wages_are_whole_rupees() -> None:
    """Paise in the ECR is a format rejection."""
    output = serialise_ecr(
        EcrReturn("EST", "062026", [ecr_line(gross_wages=D("50000.49"))])
    )
    assert "#~#50000#~#" in output


def test_ecr_wages_round_rather_than_truncate() -> None:
    """Truncating loses a rupee per employee per month across an establishment."""
    output = serialise_ecr(
        EcrReturn("EST", "062026", [ecr_line(gross_wages=D("50000.50"))])
    )
    assert "#~#50001#~#" in output


def test_ncp_days_round_up_rather_than_down() -> None:
    """Half a day of loss of pay is a day of non-contribution.

    Rounding down would understate NCP, which overstates contributory service —
    the error that favours the employer.
    """
    output = serialise_ecr(EcrReturn("EST", "062026", [ecr_line(ncp_days=D("0.5"))]))
    assert output.split(ECR_DELIMITER)[9] == "1"


def test_the_field_separator_in_a_member_name_is_refused() -> None:
    """It is the delimiter; the file would be unparseable and the portal would
    say only that row 4 is malformed."""
    with pytest.raises(SerialiserError, match="the field separator"):
        serialise_ecr(EcrReturn("EST", "062026", [ecr_line(member_name="A#~#B")]))


def test_the_employers_shares_are_in_the_portals_order() -> None:
    """Field 8 is the EPS (pension) contribution, field 9 the EPF-EPS difference."""
    fields = serialise_ecr(EcrReturn("EST", "062026", [ecr_line()])).strip().split(ECR_DELIMITER)
    assert ECR_COLUMNS[7:9] == ("eps_contribution", "employer_epf_share")
    assert (fields[7], fields[8]) == ("1250", "550")


def test_an_empty_ecr_produces_an_empty_file_not_a_stray_newline() -> None:
    assert serialise_ecr(EcrReturn("EST", "062026", [])) == ""


def test_the_ecr_serialiser_is_deterministic() -> None:
    ecr = EcrReturn("EST", "062026", [ecr_line(), ecr_line(uan="100987654321")])
    assert serialise_ecr(ecr) == serialise_ecr(ecr)
    assert content_hash(serialise_ecr(ecr)) == content_hash(serialise_ecr(ecr))


# ═══════════════════════════════════════════════════════════════════════════
# ESIC
# ═══════════════════════════════════════════════════════════════════════════


def test_the_esic_return_matches_the_golden_file() -> None:
    esic = EsicReturn(
        employer_code="31000123450001099",
        contribution_period="APR-SEP",
        wage_month="062026",
        lines=[
            EsicLine(
                ip_number="3100123456",
                ip_name="Anita Sharma",
                days_worked=D(30),
                wages_paid=D(18_000),
                employee_contribution=D(135),
                employer_contribution=D(585),
            )
        ],
    )
    expected = (
        "IP Number,IP Name,No of Days for which wages paid/payable during the month,"
        "Total Monthly Wages,Reason Code for Zero workings days,Last Working Day\r\n"
        "3100123456,Anita Sharma,30,18000,0,\r\n"
    )
    assert serialise_esic(esic) == expected


def test_the_esic_return_carries_a_header_unlike_the_ecr() -> None:
    """The portal matches on the header text, so it is the portal's wording."""
    output = serialise_esic(EsicReturn("EMP", "APR-SEP", "062026", []))
    assert output.startswith("IP Number,IP Name,")


def test_a_comma_in_a_name_is_quoted_rather_than_shifting_every_column() -> None:
    esic = EsicReturn(
        "EMP", "APR-SEP", "062026",
        [
            EsicLine(
                ip_number="3100123456",
                ip_name="Sharma, Anita",
                days_worked=D(30),
                wages_paid=D(18_000),
                employee_contribution=D(135),
                employer_contribution=D(585),
            )
        ],
    )
    output = serialise_esic(esic)
    assert '"Sharma, Anita"' in output
    body = output.strip().split("\r\n")[1]
    # Six columns still, despite the comma inside one of them.
    import csv
    import io

    assert len(next(csv.reader(io.StringIO(body)))) == 6


def test_a_last_working_day_is_formatted_for_the_portal() -> None:
    esic = EsicReturn(
        "EMP", "APR-SEP", "062026",
        [
            EsicLine(
                ip_number="3100123456",
                ip_name="Anita Sharma",
                days_worked=D(12),
                wages_paid=D(7_200),
                employee_contribution=D(54),
                employer_contribution=D(234),
                reason_code=2,
                last_working_day=date(2026, 6, 12),
            )
        ],
    )
    assert "12/06/2026" in serialise_esic(esic)


# ═══════════════════════════════════════════════════════════════════════════
# Professional Tax
# ═══════════════════════════════════════════════════════════════════════════


def test_the_pt_challan_matches_the_golden_file() -> None:
    challan = PtChallan(
        state_code="KA",
        registration_number="PT/BLR/12345",
        period="2026-06",
        lines=[
            PtLine("ACM001", "Anita Sharma", D(50_000), D(200)),
            PtLine("ACM002", "Rahul Iyer", D(18_000), D(150)),
        ],
        slab_counts={"above 20000": 1, "up to 20000": 1},
    )
    expected = (
        "Professional Tax — KA\r\n"
        "Registration number: PT/BLR/12345\r\n"
        "Period: 2026-06\r\n"
        "Employees: 2\r\n"
        "Total tax: 350.00\r\n"
        "\r\n"
        "Employee count by slab:\r\n"
        "  above 20000: 1\r\n"
        "  up to 20000: 1\r\n"
        "\r\n"
        "Employee code,Name,Gross wages,Tax deducted\r\n"
        "ACM001,Anita Sharma,50000.00,200.00\r\n"
        "ACM002,Rahul Iyer,18000.00,150.00\r\n"
    )
    assert serialise_pt_challan(challan) == expected


def test_a_missing_pt_registration_is_visible_on_the_challan() -> None:
    """Rather than an empty line an HR user might not notice."""
    challan = PtChallan("KA", None, "2026-06", [])
    assert "NOT ON RECORD" in serialise_pt_challan(challan)


def test_the_pt_challan_omits_the_slab_block_when_the_state_wants_none() -> None:
    challan = PtChallan("KA", "PT/1", "2026-06", [PtLine("A", "B", D(1), D(1))])
    assert "Employee count by slab" not in serialise_pt_challan(challan)


# ═══════════════════════════════════════════════════════════════════════════
# TDS 24Q
# ═══════════════════════════════════════════════════════════════════════════


def challan_line(**overrides) -> TdsChallanLine:
    base = {
        "serial": 1,
        "bsr_code": "0510308",
        "deposit_date": date(2026, 7, 7),
        "challan_serial": "00123",
        "tds_amount": D(45_000),
        "surcharge": D(0),
        "education_cess": D(1_800),
        "interest": D(0),
        "late_fee": D(0),
        "total_amount": D(46_800),
    }
    base.update(overrides)
    return TdsChallanLine(**base)


def deductee_line(**overrides) -> TdsDeducteeLine:
    base = {
        "challan_serial_in_file": 1,
        "serial": 1,
        "employee_code": "ACM001",
        "pan": "ABCDE1234F",
        "name": "Anita Sharma",
        "amount_paid": D(300_000),
        "tds_deducted": D(45_000),
        "tds_deposited": D(45_000),
        "deduction_date": date(2026, 6, 30),
        "rate": D(15),
    }
    base.update(overrides)
    return TdsDeducteeLine(**base)


def test_the_q1_24q_matches_the_golden_file() -> None:
    return_ = Tds24QReturn(
        tan="BLRA12345B",
        pan="AAACA1234A",
        financial_year="2026-2027",
        quarter=1,
        deductor_name="Acme Technologies Pvt Ltd",
        challans=[challan_line()],
        deductees=[deductee_line()],
    )
    expected = (
        "FH^24Q^BLRA12345B^2026-2027^Q1\r\n"
        "BH^Acme Technologies Pvt Ltd^AAACA1234A^1^1\r\n"
        "CD^1^0510308^07072026^00123^45000.00^0.00^1800.00^0.00^0.00^46800.00^N\r\n"
        "DD^1^1^ABCDE1234F^Anita Sharma^300000.00^45000.00^45000.00^30062026^15.00\r\n"
        "FT^5^45000.00\r\n"
    )
    assert serialise_tds_24q(return_) == expected


def test_annexure_ii_appears_in_q4_and_only_in_q4() -> None:
    """The checklist gate.

    Filing Annexure II in Q1 would be filing a year's figures before the year
    has happened.
    """
    detail = TdsSalaryDetail(
        employee_code="ACM001",
        pan="ABCDE1234F",
        name="Anita Sharma",
        regime="new",
        gross_salary=D(1_200_000),
        exempt_allowances=D(0),
        standard_deduction=D(75_000),
        income_from_salary=D(1_200_000),
        other_income=D(0),
        gross_total_income=D(1_200_000),
        chapter_via_deductions=D(0),
        taxable_income=D(1_125_000),
        tax_payable=D(90_000),
        relief=D(0),
        total_tds_deducted=D(90_000),
    )

    q4 = Tds24QReturn(
        tan="BLRA12345B", pan="AAACA1234A", financial_year="2026-2027", quarter=4,
        deductor_name="Acme", challans=[challan_line()], deductees=[deductee_line()],
        salary_details=[detail],
    )
    output = serialise_tds_24q(q4)
    assert "\r\nSD^ABCDE1234F^Anita Sharma^new^" in output

    for quarter in (1, 2, 3):
        without = Tds24QReturn(
            tan="BLRA12345B", pan="AAACA1234A", financial_year="2026-2027",
            quarter=quarter, deductor_name="Acme", challans=[challan_line()],
            deductees=[deductee_line()],
        )
        assert "SD^" not in serialise_tds_24q(without), f"Q{quarter}"
        assert without.includes_annexure_ii is False

    assert q4.includes_annexure_ii is True


def test_supplying_annexure_ii_for_q1_is_refused_rather_than_ignored() -> None:
    """Silently dropping it would hide a caller bug that matters."""
    detail = TdsSalaryDetail(
        "ACM001", "ABCDE1234F", "Anita", "new", D(1), D(0), D(0), D(1), D(0), D(1),
        D(0), D(1), D(0), D(0), D(0),
    )
    return_ = Tds24QReturn(
        tan="T", pan="P", financial_year="2026-2027", quarter=1, deductor_name="Acme",
        salary_details=[detail],
    )
    with pytest.raises(SerialiserError, match="Q4 return only"):
        serialise_tds_24q(return_)


def test_every_deductee_sits_under_its_own_challan() -> None:
    """Two challans, one deductee each, and the DD rows follow their CD row."""
    return_ = Tds24QReturn(
        tan="T", pan="P", financial_year="2026-2027", quarter=2, deductor_name="Acme",
        challans=[challan_line(serial=1), challan_line(serial=2, challan_serial="00124")],
        deductees=[
            deductee_line(challan_serial_in_file=2, serial=2, pan="ZYXWV9876Z"),
            deductee_line(challan_serial_in_file=1, serial=1),
        ],
    )
    rows = serialise_tds_24q(return_).strip().split("\r\n")
    kinds = [row.split("^")[0] for row in rows]
    assert kinds == ["FH", "BH", "CD", "DD", "CD", "DD", "FT"]

    # The deductee under challan 2 is the one that follows the second CD row.
    assert rows[5].split("^")[3] == "ZYXWV9876Z"


def test_a_quarter_outside_one_to_four_is_refused() -> None:
    return_ = Tds24QReturn(
        tan="T", pan="P", financial_year="2026-2027", quarter=5, deductor_name="Acme"
    )
    with pytest.raises(SerialiserError, match="not a quarter"):
        serialise_tds_24q(return_)


def test_the_24q_trailer_counts_every_row_before_it() -> None:
    return_ = Tds24QReturn(
        tan="T", pan="P", financial_year="2026-2027", quarter=1, deductor_name="Acme",
        challans=[challan_line()], deductees=[deductee_line()],
    )
    rows = serialise_tds_24q(return_).strip().split("\r\n")
    assert rows[-1].split("^")[1] == str(len(rows))


def test_24q_amounts_carry_paise_unlike_the_ecr() -> None:
    """Different portals, different conventions. Neither is guessed."""
    return_ = Tds24QReturn(
        tan="T", pan="P", financial_year="2026-2027", quarter=1, deductor_name="Acme",
        challans=[challan_line(tds_amount=D("45000.55"), total_amount=D("46800.55"))],
    )
    assert "45000.55" in serialise_tds_24q(return_)


# ═══════════════════════════════════════════════════════════════════════════
# The content hash
# ═══════════════════════════════════════════════════════════════════════════


def test_the_content_hash_changes_when_one_byte_changes() -> None:
    """It is what proves the file held later is the file uploaded then."""
    one = serialise_ecr(EcrReturn("EST", "062026", [ecr_line()]))
    two = serialise_ecr(EcrReturn("EST", "062026", [ecr_line(employee_share=D(1_801))]))
    assert content_hash(one) != content_hash(two)
    assert len(content_hash(one)) == 64
