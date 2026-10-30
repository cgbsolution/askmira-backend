"""factoHR register parsing, one synthetic workbook per layout family. No database.

Gates:
  * Columns are found by header, never by letter: every family maps to the same keys.
  * The first earnings block is the rate, the second what was earned; a register
    with one block (Sep-2023) has earned only.
  * The month's CTC column wins over "Master CTC" where the register has one.
  * Data rows stop at the first row without an employee code; strays below are noted.
  * Hidden rows are read. F&F sheets are read, with the days divisor from the formula.
  * PAN, Aadhaar, UAN, ESIC, bank and e-mail are never kept, not even inside a remark.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from openpyxl.utils import get_column_letter

from app.services.payroll.history_parse import (
    REMOVED,
    month_from_text,
    parse_register,
    parse_registers,
)
from tests.payroll.payroll_history_books import (
    APR24,
    APR26,
    DEC23,
    FEB25,
    FNF,
    IDENTIFIERS,
    MAR26,
    MAY24,
    OCT23,
    SEP23,
    Sheet,
    book,
    person,
    totals,
)

D = Decimal


def _group(layout, labels: dict[str, str]) -> list:
    """A group row: a label above the column holding `key`."""
    row = [None] * len(layout)
    keys = [k for _, k in layout]
    for key, label in labels.items():
        row[keys.index(key)] = label
    return row


def _letter(layout, key: str) -> str:
    return get_column_letter([k for _, k in layout].index(key) + 1)


def _assert_no_identifiers(rows) -> None:
    for row in rows:
        blob = " ".join([*row.cells.keys(), *row.cells.values(), *row.raw.values()])
        for value in IDENTIFIERS.values():
            assert value not in blob, f"{value} kept on row {row.row}"
        for header in ("UAN", "PAN", "Aadhar", "Adhar", "IFSC", "Account", "Bank Name", "ESI No"):
            assert not any(k.startswith(header) for k in row.cells), header


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("CGB Payroll Sep 23.xlsx", (2023, 9)),
        ("Payroll sheet Oct-23 (1).xlsx", (2023, 10)),
        ("FINAL Consolidated Salary Register May-241.xlsx", (2024, 5)),
        ("Final Consolidated Salary Register June-26.xlsx", (2026, 6)),
        ("Final Consolidated Salary Register July-24.xlsx", (2024, 7)),
        ("Consolidated Salary Register Jan-2025.xlsx", (2025, 1)),
        ("Wages Register  April- 2025", (2025, 4)),
        ("Salary Register Mar - 24", (2024, 3)),
        ("Salary Register September-2026", (2026, 9)),
        ("Salary Register Sept-25", (2025, 9)),
        ("no month here 2024", None),
    ],
)
def test_the_month_comes_from_the_file_name_or_title(name, expected) -> None:
    assert month_from_text(name) == expected


def test_sep23_has_one_block_which_is_earned_and_the_month_ctc_wins() -> None:
    full = person("CBH23-246", "Abhay", date(2023, 5, 18), ctc=58333, basic=28192, hra=14096,
                  special=14095, master_ctc=58333.333333333336, note="OFFER LETTER")
    part = person("CBL23-328", "Bina", date(2023, 9, 1), ctc=30000, basic=12000, hra=6000,
                  special=12000, worked=24, master_ctc=28824)
    sheet = Sheet("Sep", SEP23, [full, part],
                  above=[_group(SEP23, {"workable": "Working Days", "basic": "Earnings"})])
    parsed = parse_register("CGB Payroll Sep 23.xlsx", book([sheet]))

    assert (parsed.year, parsed.month) == (2023, 9)
    assert not [i for i in parsed.issues if i.level == "error"]
    main = parsed.main
    assert main.header_row == 2 and not main.has_actual and not main.has_rounded
    assert main.columns["ctc_monthly"] == ("H", "Sep CTC")
    assert main.columns["earned_basic"][0] == "M"
    assert main.columns["pf_wage"][0] == "R" and main.columns["esi_wage"][0] == "S"
    first, second = parsed.rows
    assert first.actual is None
    assert first.earned["basic"] == D("28192") and first.earned["gross"] == D("56383")
    assert first.ctc_monthly == D("58333")
    assert first.status is None
    assert first.cells["Master CTC (G)"].startswith("58333.33333")
    assert first.cells["(F)"] == "OFFER LETTER"
    assert second.days_worked == D("24") and second.lop_days == D("6")
    assert second.earned["basic"] == D("9600")
    assert any("Master CTC (G)" in note for note in parsed.notes)
    _assert_no_identifiers(parsed.rows)


def test_oct23_takes_the_oct_ctc_and_lists_the_sheets_it_does_not_import() -> None:
    row = person("CAP22-049", "Yogeeswari V", date(2022, 4, 1), ctc=62666.66667, basic=29167,
                 hra=14584, special=16966, workable=31, master_ctc=62666.666666666664,
                 prev_ctc=62667, diff=-0.33333)
    main = Sheet("Sep", OCT23, [row], above=[
        [None, None, None, date(2023, 10, 31)] + [None] * (len(OCT23) - 4)
    ])
    copy = Sheet("Sheet2", [("Emp Code", "code"), ("Name", "name")], [{"code": "CAP22-049",
                                                                         "name": "x"}])
    hold = Sheet("HOLD", [("Emp Code", "code")], [{"code": "CMU23-444"}], above=[None])
    parsed = parse_register("Payroll sheet Oct-23 (1).xlsx", book([main, copy, hold]))

    assert (parsed.year, parsed.month) == (2023, 10)
    sheet = parsed.main
    assert sheet.name == "Sep" and sheet.has_actual
    assert sheet.columns["ctc_monthly"] == ("H", "Oct CTC")
    r = parsed.rows[0]
    assert r.ctc_monthly == D("62666.66667")
    assert r.actual["basic"] == D("29167") and r.earned["basic"] == D("29167")
    assert "Master CTC (G)" in r.cells and "Sep CTC (I)" in r.cells
    assert [name for name, _ in parsed.other_sheets] == ["Sheet2", "HOLD"]
    assert any("Sheet2" in note and "not imported" in note for note in parsed.notes)
    assert len(parsed.rows) == 1


def test_dec23_stops_at_the_blank_row_and_notes_the_stray_duplicate() -> None:
    a = person("CBL22-056", "Ann", date(2022, 1, 1), ctc=40000, basic=16000, hra=6400,
               special=15000)
    b = person("CBL22-057", "Ben", date(2022, 1, 1), ctc=56666.67, basic=22667, hra=9067,
               special=21000)
    stray = dict(b, ctc=333333)
    sheet = Sheet("Sheet1", DEC23, [a, b, None, stray],
                  above=[_group(DEC23, {"workable": "DAYS", "a_basic": "Earning Actual"})])
    parsed = parse_register("FINAL Salary Register Dec-23.xlsx", book([sheet]))

    assert [r.code for r in parsed.rows] == ["CBL22-056", "CBL22-057"]
    assert parsed.main.last_row == 4
    assert parsed.main.register_totals == {} and parsed.main.totals_row is None
    assert any("row 6 (CBL22-057)" in note for note in parsed.notes)
    assert parsed.rows[1].ctc_monthly == D("56666.67")


def test_jan24_has_its_header_in_row_one_and_keeps_both_rows_of_a_person() -> None:
    old = person("CRJ23-098", "Amit", date(2023, 1, 1), ctc=24501.81333, basic=11404,
                 hra=5702, special=5913.29333, workable=31, worked=15, lop=0)
    new = person("CRJ23-098", "Amit", date(2023, 1, 1), ctc=50000, basic=20000, hra=8000,
                 special=17422, bonus=1666, workable=31, worked=16, lop=0)
    sheet = Sheet("Sheet1", DEC23, [new, old])
    parsed = parse_register("FINAL Consolidate Salary Register Jan-24.xlsx", book([sheet]))

    assert parsed.main.header_row == 1 and parsed.title is None
    assert [r.row for r in parsed.rows] == [2, 3]
    assert {r.ctc_monthly for r in parsed.rows} == {D("50000"), D("24501.81333")}


def test_feb24_reads_the_group_row_layout_and_the_fnf_sheet_with_its_divisor() -> None:
    hold = person("CMU20-004", "Vijay", date(2020, 12, 1), ctc=81812.32692, basic=32725.2,
                  hra=13090.08, special=32474, workable=29, worked=0, lop=29, pf=0,
                  status="Hold", bank=0)
    main = Sheet("Salary Register", DEC23, [hold], above=[
        None, _group(DEC23, {"workable": "DAYS", "a_basic": "Actual Salary",
                             "basic": "Earning Salary", "pf": "Deduction"}),
    ])
    ff = person("CMU20-004", "Vijay", date(2020, 12, 1), ctc=81812, basic=32725.2, hra=13090.08,
                special=32474, workable=29, worked=23, pt=0, dol=date(2024, 2, 23),
                pf=1335.483870967742, pf_rounded=1335)
    fnf = Sheet("F&F Payment", FNF, [ff], above=[None],
                formulas={"basic": "={a_basic_col}{r}/29*{worked_col}{r}"})
    parsed = parse_register("Consolidate Salary Register Feb-24.xlsx", book([main, fnf]))

    assert [s.role for s in parsed.sheets] == ["main", "fnf"]
    assert parsed.main.header_row == 3
    main_row, ff_row = parsed.rows
    assert main_row.status == "Hold" and main_row.bank_transfer == D("0")
    assert main_row.net_salary == D("-439")
    assert ff_row.kind == "fnf" and ff_row.sheet == "F&F Payment"
    assert ff_row.workable_days == D("29")
    assert abs(ff_row.earned["basic"] - D("25954.4689655")) < D("0.0000001")
    assert ff_row.bank_transfer == ff_row.net_salary
    assert ff_row.adjustment == D("0")
    expected = sum(
        (v for v in (ff_row.pf_employee, ff_row.esi_employee, ff_row.medical_insurance,
                     ff_row.professional_tax, ff_row.tds, ff_row.other_deduction) if v),
        D(0),
    )
    assert ff_row.total_deduction == expected
    assert ff_row.dol == "2024-02-23"
    # The second PF (the challan's, rounded) stays a cell.
    assert ff_row.cells[f"PF ({_letter(FNF, 'pf_rounded')})"] == "1335"
    assert ff_row.pf_employee == D("1335.483870967742")
    _assert_no_identifiers(parsed.rows)


def test_fnf_without_a_formula_falls_back_to_paid_days_with_a_note() -> None:
    ff = person("CBL24-508", "Yuthika", date(2024, 2, 1), ctc=18000, basic=7200, hra=2880,
                special=5510, bonus=600, worked=10)
    main = Sheet("Salay Register", APR24, [person("CHD23-001", "x", date(2023, 1, 1), ctc=1000,
                                                     basic=500, hra=200, special=100)],
                 above=[["Salary Register Apr - 24"], None])
    parsed = parse_register("Final Consolidated Salary Register Apr-24.xlsx",
                            book([main, Sheet("F&F", FNF, [ff], above=[None])]))
    ff_row = parsed.rows[-1]
    assert ff_row.workable_days is None
    assert any(i.category == "fnf_days" for i in parsed.issues)


def test_apr24_reads_the_fnf_sheet_once_not_the_copy_below_the_totals() -> None:
    rows = [
        person("CHD23-201", "Ravi", date(2023, 3, 1), ctc=30000, basic=12000, hra=4800,
               special=10000, client="BOB", project="HelpDesk"),
        person("CHD23-202", "Sita", date(2023, 3, 1), ctc=40000, basic=16000, hra=6400,
               special=13000, client="CGB Internal", project="Internal"),
    ]
    main = Sheet("Salay Register", APR24, rows, above=[["Salary Register Apr - 24"],
                                                         _group(APR24, {"a_basic": "Actual"})])
    copy_header = ["EmpId", "Name"]
    main.after = [
        totals(main, ["ctc", "gross", "net", "bank", "total_ded"]),
        None,
        copy_header,
        ["CBL24-508", "Yuthika"],
    ]
    ff = person("CBL24-508", "Yuthika", date(2024, 2, 1), ctc=18000, basic=7200, hra=2880,
                special=5510, bonus=600, worked=10)
    fnf = Sheet("F&F", FNF, [ff], above=[None],
                formulas={"basic": "={a_basic_col}{r}/30*{worked_col}{r}"})
    parsed = parse_register("Final Consolidated Salary Register Apr-24.xlsx", book([main, fnf]))

    assert parsed.title == "Salary Register Apr - 24"
    assert [r.code for r in parsed.rows] == ["CHD23-201", "CHD23-202", "CBL24-508"]
    assert parsed.main.totals_row == 6
    assert D(parsed.main.register_totals["bank_transfer"]) == sum(
        (r.bank_transfer for r in parsed.rows[:2]), D(0)
    )
    assert any("(EmpId)" in note and "(CBL24-508)" in note for note in parsed.notes)
    assert parsed.rows[1].client == "CGB Internal" and parsed.rows[1].account == "CGB Internal"
    assert parsed.rows[2].workable_days == D("30")


def test_may24_has_shifted_columns_and_a_rounded_column() -> None:
    row = person("CMU20-001", "Prasad", date(2020, 1, 13), ctc=31704.39291, basic=12682.12,
                 hra=5072.85, special=10634.2850063077, bonus=1056.420596, workable=31,
                 worked=31, whole=True, tds=8260.99, helper="check")
    sheet = Sheet("Sheet1", MAY24, [row], above=[["Salary Register May - 24"], None])
    parsed = parse_register("FINAL Consolidated Salary Register May-241.xlsx", book([sheet]))

    assert (parsed.year, parsed.month) == (2024, 5)
    main = parsed.main
    assert main.has_rounded
    assert main.columns["workable_days"][0] == "K"
    assert main.columns["actual_basic"][0] == "N" and main.columns["earned_basic"][0] == "S"
    assert main.columns["bank_transfer_rounded"][0] == _letter(MAY24, "rounded")
    r = parsed.rows[0]
    assert r.cells["(J)"] == "check"
    assert r.tds == D("8260.99")
    assert r.bank_transfer_rounded == D(str(round(r.bank_transfer)))


def test_feb25_on_the_project_column_names_the_account() -> None:
    row = person("CHD24-546", "Suneetha", date(2024, 4, 1), ctc=70833.33333333333, basic=28333,
                 hra=11334, special=27853, workable=28, worked=28, whole=True,
                 client="DXC", project="BOB", domain="RRB", payment_status=date(2025, 3, 1))
    sheet = Sheet("External", FEB25, [row], above=[["Salary Register  FEB- 2025"], None])
    parsed = parse_register("Final Consolidated Salary Register Feb-2025.xlsx", book([sheet]))

    r = parsed.rows[0]
    assert r.has_domain and r.account == "BOB"
    assert (r.client, r.project, r.domain) == ("DXC", "BOB", "RRB")
    assert r.payment_status == "2025-03-01"


def test_mar26_renamed_headers_map_to_the_same_keys() -> None:
    row = person("CBL22-084", "Roja", date(2022, 11, 2), ctc=32670, basic=16335, hra=6534,
                 special=5173, bonus=1361, workable=28, whole=True, client="HPE",
                 project="HPE", domain="HPE", remarks="Fine", name_length=4,
                 payment_status=date(2026, 4, 1))
    sheet = Sheet("Salary Register", MAR26, [row],
                  above=[["Salary Register March-2026"], None])
    parsed = parse_register("Final Consolidated Salary Register Mar-26.xlsx", book([sheet]))

    cols = {k: v[0] for k, v in parsed.main.columns.items()}
    assert cols["ctc_monthly"] == "I" and cols["ctc_earned"] == "J"
    assert cols["actual_basic"] == "O" and cols["earned_gross"] == "X"
    assert cols["medical_insurance"] == "AA" and cols["adjustment"] == "AF"
    assert cols["bank_transfer"] == "AG" and cols["bank_transfer_rounded"] == "AH"
    assert cols["payment_status"] == "AX" and cols["status"] == "C"
    r = parsed.rows[0]
    assert r.actual["basic"] == D("16335") and r.medical_insurance == D("239")
    assert r.cells["Name Len (AW)"] == "4"
    _assert_no_identifiers(parsed.rows)


def test_apr26_bank_transfer_amount_is_the_bank_figure() -> None:
    row = person("CBL22-084", "Roja", date(2022, 11, 2), ctc=32670, basic=16335, hra=6534,
                 special=5173, bonus=1361, workable=31, whole=True, adj=100)
    sheet = Sheet("Salary Register", APR26, [row], above=[["Salary Register April-2026"], None])
    parsed = parse_register("Final Consolidated Salary Register Apr-26.xlsx", book([sheet]))
    r = parsed.rows[0]
    assert not parsed.main.has_rounded
    assert parsed.main.columns["bank_transfer"][1] == "Bank Transfer Amount"
    assert r.bank_transfer == r.net_salary + 100


def test_a_hidden_row_is_read_and_noted() -> None:
    rows = [
        person("CHD24-540", "A", date(2024, 1, 1), ctc=30000, basic=12000, hra=4800,
               special=10000, workable=28, whole=True),
        person("CHD24-546", "B", date(2024, 4, 1), ctc=70833, basic=28333, hra=11334,
               special=27853, workable=28, worked=3, whole=True, status="F&F Paid"),
    ]
    sheet = Sheet("Salary Register", FEB25, rows, above=[["Salary Register  Mar- 2025"], None],
                  hidden={1})
    sheet.after = [totals(sheet, ["bank", "net"], skip_hidden=True)]
    parsed = parse_register("Final Consolidated Salary Register Mar-25.xlsx", book([sheet]))

    assert [r.hidden for r in parsed.rows] == [False, True]
    assert any("hidden row(s) 5" in note for note in parsed.notes)
    register_bank = D(parsed.main.register_totals["bank_transfer"])
    assert register_bank == parsed.rows[0].bank_transfer  # the SUBTOTAL leaves row 5 out


def test_identifiers_are_never_kept_even_inside_a_remark() -> None:
    row = person("CHD24-600", "C", date(2024, 1, 1), ctc=30000, basic=12000, hra=4800,
                 special=10000, workable=28, whole=True,
                 remarks="KOTAK MAHINDRA BANK\nKKBK0005297\n6346262973(15 days F&F paid)",
                 check="ABCDE1234F")
    sheet = Sheet("Salary Register", FEB25, [row], above=[["Salary Register  Mar- 2025"], None])
    parsed = parse_register("Final Consolidated Salary Register Mar-25.xlsx", book([sheet]))

    r = parsed.rows[0]
    assert "KKBK0005297" not in r.remarks and "6346262973" not in r.remarks
    assert r.remarks.startswith("KOTAK MAHINDRA BANK") and REMOVED in r.remarks
    assert "15 days F&F paid" in r.remarks
    assert "ABCDE1234F" not in " ".join(r.cells.values())
    _assert_no_identifiers(parsed.rows)
    assert any("replaced by" in note for note in parsed.notes)


def test_text_in_a_money_column_is_read_as_empty_with_a_warning() -> None:
    row = person("CHD23-440", "D", date(2023, 1, 1), ctc=30000, basic=12000, hra=4800,
                 special=10000, a_bonus="-", net="#N/A")
    sheet = Sheet("Sheet1", DEC23, [row], above=[None])
    parsed = parse_register("FINAL Salary Register Dec-23.xlsx", book([sheet]))
    r = parsed.rows[0]
    assert r.net_salary is None and r.raw["net_salary"] == "#N/A"
    assert r.actual["bonus"] is None
    warnings = [i for i in parsed.issues if i.category == "text_in_figure"]
    assert {i.level for i in warnings} == {"warning"} and len(warnings) == 2


def test_a_title_naming_another_month_is_an_error() -> None:
    row = person("CHD23-001", "x", date(2023, 1, 1), ctc=1000, basic=500, hra=200, special=100)
    sheet = Sheet("Sheet1", APR24, [row], above=[["Salary Register Mar - 24"], None])
    parsed = parse_register("Final Consolidated Salary Register Apr-24.xlsx", book([sheet]))
    assert any(i.level == "error" and i.category == "month" for i in parsed.issues)


def test_two_files_for_one_month_and_unreadable_files_are_errors() -> None:
    row = person("CHD23-001", "x", date(2023, 1, 1), ctc=1000, basic=500, hra=200, special=100)
    data = book([Sheet("Sheet1", DEC23, [row])])
    months = parse_registers(
        [
            ("Register Jan-24.xlsx", data),
            ("Register Jan-24 (copy).xlsx", data),
            ("Register Feb-24.xlsx", b"not a workbook"),
            ("Register.xlsx", data),
        ]
    )
    jan = next(m for m in months if (m.year, m.month) == (2024, 1))
    assert any(i.category == "duplicate_month" for i in jan.issues)
    feb = next(m for m in months if (m.year, m.month) == (2024, 2))
    assert any(i.category == "unreadable" for f in feb.files for i in f.issues)
    unplaced = next(m for m in months if m.year == 0)
    assert any(i.category == "month" for f in unplaced.files for i in f.issues)


def test_a_workbook_without_an_emp_code_header_is_an_error() -> None:
    data = book([Sheet("Sheet1", [("Name", "name")], [{"name": "x"}])])
    parsed = parse_register("Register Jan-24.xlsx", data)
    assert any(i.category == "no_register" for i in parsed.issues)
