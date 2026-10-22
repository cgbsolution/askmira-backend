"""Reading employee spreadsheets: factoHR's export as it comes, and our template.

No database: the file is read, its headers matched and its values parsed.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime

import pytest
from openpyxl import load_workbook

from app.services.employee_import import fields as f
from app.services.employee_import.sheet import SheetError, read_table
from app.services.employee_import.template import build_template
from tests.people import factohr_sheet

PERSON = {
    "Emp Code": "CAP22-001",
    "Full Name": "Asha Verma",
    "First Name": "Asha",
    "Last Name": "Verma",
    "Birth Date": datetime(1994, 3, 15),
    "Joining Date": datetime(2022, 4, 1),
    "Gender": "Female",
    "Status": "Active",
    "Employment Type": "Permanent",
    "Email": "asha.verma@example.com",
    "Local Address > Address": "12, Lake View Road, Indiranagar",
    "Local Address > City": "Bangalore",
    "Local Address > State": "Karnataka",
    "Local Address > Pincode": "560038",
    "Permanent Address > Address": "4-12, Temple Street, Kothur (V), Tirupati, Andhra Pradesh",
    "Permanent Address > Pincode": "517503",
    "Companyy": "Example Solutions Pvt Ltd",
    "PF Detail > PF No": "KRBNG00123450000012345",
    "PF Detail > Joining Date": datetime(2023, 1, 1),
}


def _table(people=None, **kwargs):
    data = factohr_sheet.build(people or [PERSON], **kwargs)
    return read_table(data, "Employee Detail.xlsx")


def _value(table, key, row=0):
    return table.rows[row][1][table.mapping[key]]


def test_the_factohr_export_is_found_under_its_title_and_group_rows():
    table = _table()
    assert table.source == "factoHR Employee Detail report"
    assert table.header_row == 6
    assert len(table.rows) == 1
    # The first, ungrouped Joining Date, not PF's or ESIC's.
    assert f.read_date(_value(table, "date_of_joining")) == date(2022, 4, 1)
    assert _value(table, "employee_code") == "CAP22-001"


def test_repeated_headers_are_told_apart_by_their_group():
    table = _table()
    by_index = {c.index: c for c in table.columns}
    assert by_index[table.mapping["current_line1"]].header == "Local Address > Address"
    assert by_index[table.mapping["permanent_line1"]].header == "Permanent Address > Address"
    # Past the merged heading, still the permanent address's.
    assert by_index[table.mapping["permanent_pincode"]].header == "Permanent Address > Pincode"
    assert by_index[table.mapping["current_city"]].header == "Local Address > City"
    assert by_index[table.mapping["pf_number"]].name == "PF No"
    assert by_index[table.mapping["legal_entity"]].name == "Companyy"
    # A name that does not repeat is shown plain.
    assert by_index[table.mapping["notice_period_days"]].header == "Notice Period For Employee"
    ignored = {c.header for c in table.unused}
    assert {"Title", "Approving Manager", "PF Detail > Joining Date"} <= ignored


def test_dates_read_as_excel_serials_text_and_dates():
    assert f.read_date(34043) == date(1993, 3, 15)
    assert f.read_date("34043") == date(1993, 3, 15)
    assert f.read_date("15-03-1993") == date(1993, 3, 15)
    assert f.read_date("15/03/1993") == date(1993, 3, 15)
    assert f.read_date("15-Mar-1993") == date(1993, 3, 15)
    assert f.read_date("1993-03-15") == date(1993, 3, 15)
    assert f.read_date("15-03-1993 00:00:00") == date(1993, 3, 15)
    assert f.read_date(datetime(1993, 3, 15, 9, 30)) == date(1993, 3, 15)
    assert f.read_date("") is None
    assert f.read_date("NA") is None
    with pytest.raises(ValueError, match="not a date"):
        f.read_date("someday")
    table = _table(dates_as_serials=False)
    assert f.read_date(_value(table, "date_of_birth")) == date(1994, 3, 15)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Active", "active"),
        ("Inactive", "left"),
        ("Withdrawn", "left"),
        ("Suspended", "left"),
        ("On Notice", "on_notice"),
        (None, None),
    ],
)
def test_statuses_as_factohr_words_them(raw, expected):
    assert f.read_status(raw) == expected


def test_choices_read_whatever_their_spelling():
    assert f.read_employment_type("Permanent") == "full_time"
    assert f.read_employment_type("Consultant") == "consultant"
    assert f.read_employment_type("Intern") == "intern"
    assert f.read_gender("F") == "female"
    assert f.read_marital("Unmarried") == "single"
    assert f.read_blood_group("O positive") == "O+"
    assert f.read_blood_group("AB -ve") == "AB-"
    assert f.read_yes("YES") is True
    assert f.manager_code("CBL22-044 - Shobha M") == "CBL22-044"
    with pytest.raises(ValueError, match="status"):
        f.read_status("Floating")


def test_numbers_keep_their_digits_and_zero_is_empty():
    assert f.digits(132708827006) == "132708827006"
    assert f.digits(132708827006.0) == "132708827006"
    assert f.digits("3787 7286 1496") == "378772861496"
    assert f.digits("0") is None
    assert f.read_phone("98765 43210 ") == "9876543210"
    assert f.read_phone("0") is None


def test_places_and_one_line_addresses():
    assert f.state_of_place("Bangalore office") == "KA"
    assert f.state_of_place("Banagalore") == "KA"
    assert f.state_of_place("Uttar pradesh") == "UP"
    assert f.state_of_place("Dubai") is None
    assert f.state_code("CHHATISHGARH") == "CG"
    line = "#2-7, Kothur (V), Padipeta (Po), Tirupati ,Andhra Pradesh, Pin : 517503."
    assert f.pincode_in(line) == "517503"
    assert f.state_in(line) == "AP"
    assert f.city_in(line, "AP") == "Tirupati"
    assert f.city_in("14, Main Road, Hosur, Tamil Nadu, 635109", "TN") == "Hosur"
    assert f.state_of_pin("560102") == "KA"
    assert f.state_of_pin("403001") == "GA"
    # A street is not a city.
    assert f.city_in("14 MG Road Sector 21 Near Bus Stand 999999", None) is None


def test_the_template_reads_back_as_the_template():
    content = build_template([])
    book = load_workbook(io.BytesIO(content))
    assert book.sheetnames == ["Employees", "Instructions"]
    headers = [cell.value for cell in book["Employees"][1]]
    assert headers[:4] == ["Emp Code", "First Name", "Middle Name", "Last Name"]
    assert "Expected Confirmation Date" not in headers
    # No example row: an untouched template creates nobody.
    assert book["Employees"].max_row == 1

    table = read_table(content, "template.xlsx")
    assert table.source == "AskMira template"
    template_fields = {field.key for field in f.FIELDS if field.template}
    assert template_fields <= set(table.mapping)
    assert table.unused == []


def test_a_csv_with_our_headers_reads_too():
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Emp Code", "First Name", "Email", "Joining Date", "Local City"])
    writer.writerow(["E1", "Ravi", "ravi@example.com", "01-04-2024", "Pune"])
    table = read_table(buffer.getvalue().encode("utf-8"), "people.csv")
    assert _value(table, "work_email") == "ravi@example.com"
    assert _value(table, "current_city") == "Pune"


def test_files_that_are_not_employee_lists_are_refused_plainly():
    with pytest.raises(SheetError, match="empty"):
        read_table(b"", "a.xlsx")
    with pytest.raises(SheetError, match=r"\.xls files"):
        read_table(b"\xd0\xcf\x11\xe0", "old.xls")
    with pytest.raises(SheetError, match="Emp Code"):
        read_table(b"Name,Email\nRavi,ravi@example.com\n", "people.csv")
    with pytest.raises(SheetError, match="Excel workbook"):
        read_table(b"PK\x03\x04 not really a zip", "broken.xlsx")
