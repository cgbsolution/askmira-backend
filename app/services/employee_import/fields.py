"""What each column of an employee spreadsheet means, and how its values are read.

One catalogue serves three jobs: matching a sheet's headers — the AskMira
template's, or another HR system's export as it comes (factoHR's Employee
Detail report is the one this was built against) — reading each value into
AskMira's shape, and writing the template. Headers match whatever their case
and spacing. Where an export repeats a header under different groups
("Address" under Local Address and again under Permanent Address), the alias
names the group too: `"local address > address"`.

Pure: no database, no request. A reader returns the value or raises
`ValueError` with a sentence HR can act on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

GROUP_SEP = " > "

#: The sections a field belongs to, in the template's column order.
SECTIONS = ("Basic details", "Job details", "Personal", "Address", "Statutory", "Bank")


@dataclass(frozen=True)
class Field:
    key: str
    #: The header in AskMira's template. factoHR's own wording wherever there is one.
    label: str
    section: str
    #: Normalised headers that mean this field, most specific first.
    aliases: tuple[str, ...]
    required: bool = False
    #: What to enter, for the template's instructions sheet.
    hint: str = ""
    example: str = ""
    #: Fixed choices offered as a drop-down in the template.
    choices: tuple[str, ...] = ()
    #: A column of the template. Off for what only other systems' exports carry
    #: (factoHR's Expected Confirmation Date, say): still read, never asked for.
    template: bool = True


def norm(text: object) -> str:
    """A header as matched: lower case, single spaces, no trailing * : or ."""
    value = str(text or "").replace("\n", " ").replace("\u2019", "'").strip().lower()
    value = re.sub(r"\s+", " ", value)
    return re.sub(r"[\s*:.]+$", "", value)


def qualified(group: str, name: str) -> str:
    return f"{norm(group)}{GROUP_SEP}{norm(name)}"


FIELDS: tuple[Field, ...] = (
    # ── Basic details ───────────────────────────────────────────────────────
    Field(
        "employee_code",
        "Emp Code",
        "Basic details",
        ("emp code", "employee code", "employee id", "emp id", "empcode", "employee no", "emp no"),
        required=True,
        hint="Their code, unique in the company. Letters, digits, - _ and / only.",
        example="EMP0042",
    ),
    Field("first_name", "First Name", "Basic details", ("first name", "firstname", "given name"),
          required=True, example="Kavya"),
    Field("middle_name", "Middle Name", "Basic details", ("middle name",),
          hint="Kept with the last name.", example=""),
    Field("last_name", "Last Name", "Basic details", ("last name", "lastname", "surname"),
          example="Menon"),
    Field("full_name", "Full Name", "Basic details", ("full name", "employee name"),
          hint="Used only when it differs from first and last name, as the name shown.",
          template=False),
    Field("gender", "Gender", "Basic details", ("gender", "sex"),
          choices=("Female", "Male", "Other", "Prefer not to say"), example="Female"),
    Field("date_of_birth", "Birth Date", "Basic details",
          ("birth date", "date of birth", "dob", "birthdate"),
          hint="DD-MM-YYYY.", example="15-03-1993"),
    Field("work_email", "Email", "Basic details",
          ("email", "work email", "official email", "office email", "company email",
           "company mail id", "official email id", "email id"),
          required=True, hint="Their work email. Their login is sent here.",
          example="kavya.menon@yourcompany.com"),
    Field("personal_email", "Personal Email", "Basic details",
          ("personal email", "personal email id", "personal mail id"),
          example="kavya.m@gmail.com"),
    Field("phone", "Mobile No", "Basic details",
          ("mobile no", "mobile", "mobile number", "phone", "phone number", "contact number",
           "contact no"),
          example="9876543210"),
    # ── Job details ─────────────────────────────────────────────────────────
    Field("date_of_joining", "Joining Date", "Job details",
          ("joining date", "date of joining", "doj", "join date"),
          required=True, hint="DD-MM-YYYY.", example="01-04-2024"),
    Field("employment_type", "Employment Type", "Job details",
          ("employment type", "employee type", "emp type", "type of employment"),
          choices=("Permanent", "Part time", "Contract", "Intern", "Consultant"),
          hint="Permanent (full time), Part time, Contract, Intern or Consultant.",
          example="Permanent"),
    Field("status", "Status", "Job details",
          ("status", "employee status", "employment status"),
          choices=("Active", "On notice", "Inactive"),
          hint="Active, or Inactive for somebody who has left (give their Leaving Date).",
          example="Active"),
    Field("confirmation_date", "Confirmation Date", "Job details",
          ("confirmation date", "date of confirmation", "probation end date"),
          hint="When probation ended or ends. DD-MM-YYYY."),
    Field("expected_confirmation_date", "Expected Confirmation Date", "Job details",
          ("expected confirmation date",),
          hint="Used when there is no Confirmation Date.", template=False),
    Field("probation_days", "Probation Period In Days", "Job details",
          ("probation period in days", "probation period", "probation days",
           "probation period (in days)"),
          hint="Used when neither confirmation date is given."),
    Field("notice_period_days", "Notice Period For Employee", "Job details",
          ("notice period for employee", "notice period", "notice period days",
           "notice period (days)"),
          hint="In days."),
    Field("leaving_date", "Leaving Date", "Job details",
          ("leaving date", "date of leaving", "exit date", "date of exit", "relieving date"),
          hint="Only for somebody who has left. DD-MM-YYYY."),
    Field("last_working_date", "Employee Last working date", "Job details",
          ("employee last working date", "last working date", "last working day", "lwd"),
          hint="Used instead of Leaving Date when both are given.", template=False),
    Field("designation", "Designation", "Job details", ("designation", "job title", "title of job"),
          example="Software Engineer"),
    Field("department", "Department", "Job details", ("department", "department name", "dept"),
          hint="Matched by name; a new one is created when you are an admin.",
          example="Engineering"),
    Field("project", "Project", "Job details",
          ("project", "project name", "client project", "client"),
          hint="The client's project they work on, or Internal. Matched by name or code; a new "
               "one is created when you are an admin.",
          example="Internal"),
    Field("location", "Location", "Job details",
          ("location", "work location", "office location", "branch"),
          hint="Matched by name; a new one is created when its state can be told.",
          example="Bangalore"),
    Field("legal_entity", "Company", "Job details",
          ("company", "companyy", "legal entity", "company name", "entity"),
          hint="The legal entity. Left blank, the company's only one is used.",
          example="Your Company Pvt Ltd"),
    Field("grade", "Grade", "Job details", ("grade", "band", "level")),
    Field("manager", "Reporting Manager", "Job details",
          ("reporting manager", "manager", "reports to", "reporting to", "manager code",
           "reporting manager code"),
          hint="Their manager's Emp Code (\"EMP0007\" or \"EMP0007 - Name\").",
          example="EMP0007"),
    # ── Personal ────────────────────────────────────────────────────────────
    Field("marital_status", "Marital Status", "Personal", ("marital status",),
          choices=("Unmarried", "Married", "Separated", "Divorced", "Widowed"),
          example="Unmarried"),
    Field("blood_group", "Blood Group", "Personal", ("blood group",),
          choices=("A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"), example="O+"),
    Field("nationality", "Nationality", "Personal", ("nationality",), example="Indian"),
    Field("father_name", "Father Name", "Personal",
          ("father name", "father's name", "fathers name")),
    Field("mother_name", "Mother Name", "Personal",
          ("mother name", "mother's name", "mothers name")),
    Field("spouse_name", "Spouse Name", "Personal", ("spouse name", "spouse's name", "husband name",
                                                     "wife name")),
    Field("alternate_phone", "Mobile No2", "Personal",
          ("mobile no2", "mobile no 2", "alternate mobile", "alternate phone",
           "alternate mobile no", "alternate number")),
    Field("emergency_name", "Emergency Contact Name", "Personal",
          ("emergency contact detail > name", "emergency contact name", "emergency contact")),
    Field("emergency_relationship", "Emergency Contact Relationship", "Personal",
          ("emergency contact detail > relationship", "emergency contact relationship",
           "emergency relationship"),
          choices=("Spouse", "Father", "Mother", "Sibling", "Child", "Friend", "Other")),
    Field("emergency_phone", "Emergency Contact Mobile", "Personal",
          ("emergency contact detail > mobile 1", "emergency contact mobile",
           "emergency contact number", "emergency mobile")),
    # ── Address ─────────────────────────────────────────────────────────────
    Field("current_line1", "Local Address", "Address",
          ("local address > address", "local address", "current address", "present address",
           "correspondence address", "address")),
    Field("current_line2", "Local Address 2", "Address",
          ("local address > address 2", "local address 2", "current address 2", "address 2"),
          template=False),

    Field("current_line3", "Local Address 3", "Address",
          ("local address > address 3", "local address 3", "current address 3", "address 3"),
          template=False),

    Field("current_city", "Local City", "Address",
          ("local address > city", "local city", "current city", "city")),
    Field("current_state", "Local State", "Address",
          ("local address > state", "local state", "current state", "state"),
          hint="State name or code (Karnataka or KA)."),
    Field("current_pincode", "Local Pincode", "Address",
          ("local address > pincode", "local pincode", "current pincode", "pincode", "pin code",
           "pin")),
    Field("permanent_same", "Permanent Same As Local Address", "Address",
          ("permanent same as local address", "permanent same as current address",
           "same as current address"),
          choices=("YES", "NO")),
    Field("permanent_line1", "Permanent Address", "Address",
          ("permanent address > address", "permanent address")),
    Field("permanent_line2", "Permanent Address 2", "Address",
          ("permanent address > address 2", "permanent address 2"), template=False),

    Field("permanent_line3", "Permanent Address 3", "Address",
          ("permanent address > address 3", "permanent address 3"), template=False),

    Field("permanent_city", "Permanent City", "Address",
          ("permanent address > city", "permanent city")),
    Field("permanent_state", "Permanent State", "Address",
          ("permanent address > state", "permanent state")),
    Field("permanent_pincode", "Permanent Pincode", "Address",
          ("permanent address > pincode", "permanent pincode")),
    # ── Statutory ───────────────────────────────────────────────────────────
    Field("pan", "Pan No", "Statutory",
          ("pan no", "pan", "pan number", "permanent account number details > pan no"),
          example="ABCPE1234F"),
    Field("aadhaar", "Aadhaar No", "Statutory",
          ("aadhaar card number details > aadhaar no", "aadhaar no", "aadhaar", "aadhar no",
           "aadhar", "aadhaar number", "aadhar number"),
          hint="Twelve digits. Only the last four are kept."),
    Field("uan", "UA No", "Statutory",
          ("pf detail > ua no", "ua no", "uan", "uan no", "uan number")),
    Field("pf_number", "PF No", "Statutory",
          ("pf detail > pf no", "pf no", "pf number", "pf account number", "pf account no")),
    Field("esi_number", "ESIC No", "Statutory",
          ("esic detail > esic no", "esic no", "esi no", "esic number", "esi number", "ip number")),
    # ── Bank ────────────────────────────────────────────────────────────────
    Field("bank_name", "Bank Name", "Bank", ("bank detail > bank name", "bank name", "bank")),
    Field("account_number", "Account No", "Bank",
          ("bank detail > account no", "account no", "account number", "bank account no",
           "bank account number", "a/c no", "salary account no")),
    Field("ifsc", "IFSC Code", "Bank", ("bank detail > ifsc code", "ifsc code", "ifsc")),
    Field("account_holder_name", "Name As Per Bank Records", "Bank",
          ("bank detail > name as per bank records", "name as per bank records",
           "account holder name", "name as per bank", "beneficiary name")),
)

FIELD_BY_KEY: dict[str, Field] = {field.key: field for field in FIELDS}


# ═══════════════════════════════════════════════════════════════════════════
# Reading values
# ═══════════════════════════════════════════════════════════════════════════

#: Cells that mean "nothing here" in exports.
_BLANKS = frozenset({"", "-", "--", "na", "n/a", "nil", "none", "null", "not available"})

#: Excel counts days from 30 Dec 1899 (its 1900 leap-year bug folded in).
_EXCEL_EPOCH = date(1899, 12, 30)

_DATE_FORMATS = (
    "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y",
    "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%y", "%d/%m/%y", "%d-%b-%y", "%b %d, %Y",
)


def text(raw: Any) -> str | None:
    """A cell as trimmed text; None for an empty or placeholder cell."""
    if raw is None:
        return None
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    value = re.sub(r"\s+", " ", str(raw)).strip()
    return None if value.lower() in _BLANKS else value


def digits(raw: Any) -> str | None:
    """A number kept as its digits: account numbers, Aadhaar, phone. `0` is empty."""
    value = text(raw)
    if value is None:
        return None
    compact = re.sub(r"[\s\-]", "", value)
    return None if compact.strip("0") == "" else compact


def read_date(raw: Any) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        if 1 <= raw < 2_958_466:
            return _EXCEL_EPOCH + timedelta(days=int(raw))
        raise ValueError(f"'{raw}' is not a date")
    value = text(raw)
    if value is None or value == "0":
        return None
    if re.fullmatch(r"\d{4,5}(\.0+)?", value):
        return _EXCEL_EPOCH + timedelta(days=int(float(value)))
    # "15-03-1993 00:00:00": the time carries nothing.
    candidate = value.split(" ")[0] if re.match(r"^\S+ \d{1,2}:\d{2}", value) else value
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"'{value}' is not a date (use DD-MM-YYYY)")


def read_int(raw: Any) -> int | None:
    value = text(raw)
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"'{value}' is not a number") from exc
    if not number.is_integer():
        raise ValueError(f"'{value}' is not a whole number")
    return int(number)


def read_yes(raw: Any) -> bool | None:
    value = text(raw)
    if value is None:
        return None
    lowered = value.lower()
    if lowered in {"yes", "y", "true", "1"}:
        return True
    if lowered in {"no", "n", "false", "0"}:
        return False
    raise ValueError(f"'{value}' is not Yes or No")


def _lookup(raw: Any, table: dict[str, str], what: str, allowed: str) -> str | None:
    value = text(raw)
    if value is None:
        return None
    key = re.sub(r"[\s_\-]+", " ", value.lower()).strip()
    if key in table:
        return table[key]
    raise ValueError(f"'{value}' is not a {what} AskMira knows ({allowed})")


_GENDER = {
    "female": "female", "f": "female", "woman": "female",
    "male": "male", "m": "male", "man": "male",
    "other": "non_binary", "others": "non_binary", "transgender": "non_binary",
    "non binary": "non_binary", "nonbinary": "non_binary", "third gender": "non_binary",
    "prefer not to say": "undisclosed", "undisclosed": "undisclosed",
    "not disclosed": "undisclosed",
}


def read_gender(raw: Any) -> str | None:
    return _lookup(raw, _GENDER, "gender", "Female, Male, Other or Prefer not to say")


_EMPLOYMENT_TYPE = {
    "permanent": "full_time", "full time": "full_time", "fulltime": "full_time",
    "regular": "full_time", "confirmed": "full_time", "probation": "full_time",
    "probationer": "full_time", "on probation": "full_time", "employee": "full_time",
    "part time": "part_time", "parttime": "part_time",
    "contract": "contract", "contractual": "contract", "fixed term": "contract",
    "temporary": "contract", "temp": "contract", "contract employee": "contract",
    "intern": "intern", "internship": "intern", "trainee": "intern", "apprentice": "intern",
    "consultant": "consultant", "freelancer": "consultant", "freelance": "consultant",
    "retainer": "consultant", "advisor": "consultant", "adviser": "consultant",
}


def read_employment_type(raw: Any) -> str | None:
    return _lookup(
        raw, _EMPLOYMENT_TYPE, "type of employment",
        "Permanent, Part time, Contract, Intern or Consultant",
    )


#: Where a person stands, as an export words it. "left" means they have gone
#: and need a leaving date; AskMira has no "suspended", so a suspended person
#: who has a leaving date is recorded as having left.
_STATUS = {
    "active": "active", "working": "active", "confirmed": "active", "probation": "active",
    "on probation": "active", "new joinee": "active", "new joiner": "active", "current": "active",
    "on notice": "on_notice", "notice period": "on_notice", "serving notice": "on_notice",
    "notice": "on_notice",
    "on leave": "on_leave", "long leave": "on_leave", "sabbatical": "on_leave",
    "inactive": "left", "in active": "left", "withdrawn": "left", "resigned": "left",
    "terminated": "left", "absconded": "left", "absconding": "left", "left": "left",
    "exited": "left", "exit": "left", "relieved": "left", "retired": "left", "deceased": "left",
    "separated": "left", "suspended": "left", "dismissed": "left", "inactive employee": "left",
}


def read_status(raw: Any) -> str | None:
    return _lookup(raw, _STATUS, "status", "Active, On notice, On leave or Inactive")


_MARITAL = {
    "single": "single", "unmarried": "single", "never married": "single",
    "married": "married", "separated": "separated", "divorced": "divorced",
    "widowed": "widowed", "widow": "widowed", "widower": "widowed",
    "prefer not to say": "undisclosed",
}


def read_marital(raw: Any) -> str | None:
    return _lookup(
        raw, _MARITAL, "marital status", "Unmarried, Married, Separated, Divorced or Widowed"
    )


_RELATIONSHIP = {
    "spouse": "spouse", "wife": "spouse", "husband": "spouse",
    "father": "father", "dad": "father", "mother": "mother", "mom": "mother", "mum": "mother",
    "brother": "sibling", "sister": "sibling", "sibling": "sibling",
    "son": "child", "daughter": "child", "child": "child",
    "father in law": "father_in_law", "mother in law": "mother_in_law",
    "grandfather": "grandparent", "grandmother": "grandparent", "grandparent": "grandparent",
    "friend": "friend", "other": "other", "relative": "other", "guardian": "other",
}


def read_relationship(raw: Any) -> str | None:
    return _lookup(raw, _RELATIONSHIP, "relationship", "Spouse, Father, Mother, Sibling, Child…")


def read_blood_group(raw: Any) -> str | None:
    value = text(raw)
    if value is None:
        return None
    match = re.fullmatch(
        r"(ab|a|b|o)\s*(\+|-|\+ve|-ve|positive|negative|pos|neg|ve\+|ve-)",
        value.lower().replace("(", "").replace(")", ""),
    )
    if not match:
        raise ValueError(f"'{value}' is not a blood group (A+, O-, …)")
    sign = "+" if match.group(2) in {"+", "+ve", "positive", "pos", "ve+"} else "-"
    return f"{match.group(1).upper()}{sign}"


def read_phone(raw: Any) -> str | None:
    value = text(raw)
    if value is None or value.strip("0 ") == "":
        return None
    compact = re.sub(r"\s+", "", value)
    if not re.fullmatch(r"[0-9+\-()]{6,32}", compact):
        raise ValueError(f"'{value}' is not a phone number")
    return compact


def manager_code(raw: Any) -> str | None:
    """The code out of "EMP0007 - Priya Sharma", or the cell itself."""
    value = text(raw)
    if value is None:
        return None
    return value.split(" - ", 1)[0].strip() or None


# ═══════════════════════════════════════════════════════════════════════════
# States
# ═══════════════════════════════════════════════════════════════════════════

#: State and union-territory names, with the misspellings exports carry,
#: to the codes AskMira stores.
STATE_NAMES: dict[str, str] = {
    "andaman and nicobar islands": "AN", "andaman and nicobar": "AN", "andaman & nicobar": "AN",
    "andhra pradesh": "AP", "arunachal pradesh": "AR", "assam": "AS", "bihar": "BR",
    "chandigarh": "CH", "chhattisgarh": "CG", "chhatishgarh": "CG", "chattisgarh": "CG",
    "chhatisgarh": "CG",
    "dadra and nagar haveli and daman and diu": "DH", "dadra and nagar haveli": "DH",
    "daman and diu": "DH", "delhi": "DL", "new delhi": "DL", "nct of delhi": "DL", "goa": "GA",
    "gujarat": "GJ", "himachal pradesh": "HP", "haryana": "HR", "jharkhand": "JH",
    "jammu and kashmir": "JK", "jammu & kashmir": "JK", "karnataka": "KA", "kerala": "KL",
    "ladakh": "LA", "lakshadweep": "LD", "maharashtra": "MH", "meghalaya": "ML", "manipur": "MN",
    "madhya pradesh": "MP", "mizoram": "MZ", "nagaland": "NL", "odisha": "OD", "orissa": "OD",
    "punjab": "PB", "puducherry": "PY", "pondicherry": "PY", "rajasthan": "RJ", "sikkim": "SK",
    "telangana": "TG", "tamil nadu": "TN", "tamilnadu": "TN", "tripura": "TR",
    "uttarakhand": "UK", "uttaranchal": "UK", "uttar pradesh": "UP", "west bengal": "WB",
}

#: Cities a work location is commonly named after, to their state.
CITY_STATES: dict[str, str] = {
    "bangalore": "KA", "bengaluru": "KA", "mysore": "KA", "mysuru": "KA", "mangalore": "KA",
    "hubli": "KA", "mumbai": "MH", "bombay": "MH", "pune": "MH", "nagpur": "MH", "nashik": "MH",
    "thane": "MH", "navi mumbai": "MH", "chennai": "TN", "madras": "TN", "coimbatore": "TN",
    "madurai": "TN", "hyderabad": "TG", "secunderabad": "TG", "warangal": "TG",
    "visakhapatnam": "AP", "vizag": "AP", "vijayawada": "AP", "tirupati": "AP", "guntur": "AP",
    "kolkata": "WB", "calcutta": "WB", "gurugram": "HR", "gurgaon": "HR", "faridabad": "HR",
    "noida": "UP", "greater noida": "UP", "ghaziabad": "UP", "lucknow": "UP", "kanpur": "UP",
    "agra": "UP", "varanasi": "UP", "ahmedabad": "GJ", "surat": "GJ", "vadodara": "GJ",
    "jaipur": "RJ", "udaipur": "RJ", "jodhpur": "RJ", "bhopal": "MP", "indore": "MP",
    "gwalior": "MP", "jabalpur": "MP", "kochi": "KL", "cochin": "KL", "trivandrum": "KL",
    "thiruvananthapuram": "KL", "kozhikode": "KL", "chandigarh": "CH", "mohali": "PB",
    "ludhiana": "PB", "amritsar": "PB", "bhubaneswar": "OD", "cuttack": "OD", "patna": "BR",
    "ranchi": "JH", "jamshedpur": "JH", "raipur": "CG", "durg": "CG", "bhilai": "CG",
    "dehradun": "UK", "guwahati": "AS", "panaji": "GA", "shimla": "HP", "srinagar": "JK",
    "jammu": "JK", "puducherry": "PY", "delhi": "DL", "new delhi": "DL",
}


def state_code(raw: Any) -> str | None:
    """A state's code from its name or code; None when it cannot be told."""
    from app.schemas.org import INDIAN_STATE_CODES

    value = text(raw)
    if value is None:
        return None
    upper = value.upper()
    if upper in INDIAN_STATE_CODES:
        return upper
    return STATE_NAMES.get(re.sub(r"\s+", " ", value.lower()).strip())


#: Spellings of city names common enough in HR data to be worth knowing.
_CITY_SPELLINGS = {
    "banglore": "bangalore", "banagalore": "bangalore", "bengalore": "bangalore",
    "bangaluru": "bangalore", "blr": "bangalore", "hyd": "hyderabad", "gurgoan": "gurugram",
    "gurgram": "gurugram", "mumbia": "mumbai", "chenai": "chennai", "dilli": "delhi",
}

#: Words that name an office rather than a place: "Bangalore office", "Pune HQ".
_OFFICE_WORDS = re.compile(r"\b(office|branch|hq|head office|campus|unit|site|centre|center)\b")


def state_of_place(name: str) -> str | None:
    """The state a location name points at: a state's own name, or a known city
    ("Bangalore office" and "Banglore" included)."""
    key = re.sub(r"\s+", " ", _OFFICE_WORDS.sub(" ", name.lower())).strip(" -,.")
    key = _CITY_SPELLINGS.get(key, key)
    return STATE_NAMES.get(key) or CITY_STATES.get(key) or state_code(key)


#: The state of a PIN code by its first two digits, where India Post's circle
#: puts all of them in one state; prefixes shared between states are left out
#: rather than guessed. Three-digit exceptions inside a circle come first.
_PIN_STATES = {
    "11": "DL", "12": "HR", "13": "HR", "14": "PB", "15": "PB", "17": "HP",
    "30": "RJ", "31": "RJ", "32": "RJ", "33": "RJ", "34": "RJ",
    "36": "GJ", "37": "GJ", "38": "GJ", "39": "GJ",
    "40": "MH", "41": "MH", "42": "MH", "43": "MH", "44": "MH",
    "45": "MP", "46": "MP", "47": "MP", "48": "MP", "49": "CG",
    "50": "TG", "51": "AP", "52": "AP", "53": "AP",
    "56": "KA", "57": "KA", "58": "KA", "59": "KA",
    "60": "TN", "61": "TN", "62": "TN", "63": "TN", "64": "TN",
    "67": "KL", "68": "KL", "69": "KL",
    "70": "WB", "71": "WB", "72": "WB", "73": "WB", "74": "WB",
    "75": "OD", "76": "OD", "77": "OD", "78": "AS",
}
_PIN_EXCEPTIONS = {"403": "GA", "737": "SK", "744": "AN"}


def state_of_pin(pincode: str | None) -> str | None:
    if not pincode or len(pincode) != 6:
        return None
    return _PIN_EXCEPTIONS.get(pincode[:3]) or _PIN_STATES.get(pincode[:2])


def pincode_in(textual: str) -> str | None:
    """The last six-digit PIN code in an address written as one line."""
    found = re.findall(r"(?<!\d)([1-9]\d{2})\s?(\d{3})(?!\d)", textual)
    return "".join(found[-1]) if found else None


def state_in(textual: str) -> str | None:
    """The state an address written as one line names, by state or city."""
    lowered = f" {re.sub(r'[^a-z&]+', ' ', textual.lower())} "
    for name, code in sorted(STATE_NAMES.items(), key=lambda item: -len(item[0])):
        if f" {name} " in lowered:
            return code
    for name, code in sorted(CITY_STATES.items(), key=lambda item: -len(item[0])):
        if f" {name} " in lowered:
            return code
    return None


#: Postal words around a place name in an Indian address: "Kothur (V)", "Dist.", "Pin :".
_POSTAL_WORDS = re.compile(
    r"\(.*?\)|\b(dist|district|post|po|taluk|taluka|mandal|tehsil|pin|pincode|pin code|city|"
    r"state|india|near|opp|via)\b|[0-9#:./\-]+",
    re.IGNORECASE,
)


def _place(part: str) -> str | None:
    """A comma-separated part as a place name, if that is all it is: one to three words."""
    words = _POSTAL_WORDS.sub(" ", part).split()
    if not 1 <= len(words) <= 3 or not all(word.isalpha() for word in words):
        return None
    return " ".join(word.capitalize() for word in words)


def city_in(textual: str, code: str | None) -> str | None:
    """The city in an address written as one line.

    A known city if it is named; otherwise the place just before the state
    (or the PIN code) — "…, Padipeta (Po), Tirupati, Andhra Pradesh, Pin 517503"
    gives Tirupati. None rather than a guess when that part is a street or a
    building.
    """
    lowered = f" {re.sub(r'[^a-z]+', ' ', textual.lower())} "
    for spelling, name in _CITY_SPELLINGS.items():
        if f" {spelling} " in lowered:
            return name.title()
    for name in sorted(CITY_STATES, key=len, reverse=True):
        if f" {name} " in lowered:
            return name.title()
    parts = [part.strip() for part in re.split(r"[,\n]", textual) if part.strip(" .")]
    states = {name for name, value in STATE_NAMES.items() if value == code}
    for index, part in enumerate(parts):
        bare = re.sub(r"[^a-z ]+", " ", part.lower())
        bare = re.sub(r"\s+", " ", bare).strip()
        holds_state = any(f" {name} " in f" {bare} " for name in states)
        holds_pin = pincode_in(part) is not None
        if not (holds_state or holds_pin):
            continue
        # "Bangalore Karnataka 560102": the place shares the part with its state.
        rest = bare
        for name in states:
            rest = re.sub(rf"\b{re.escape(name)}\b", " ", rest)
        same_part = _place(rest)
        if same_part:
            return same_part
        if index > 0:
            before = _place(parts[index - 1])
            if before and state_code(before) is None:
                return before
    return None
