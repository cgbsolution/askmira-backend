"""Who works at the demo company.

Twenty-two people across leadership, engineering, product, sales, finance,
operations and HR, in Bengaluru (Karnataka) and Mumbai (Maharashtra) so that
state levies differ. Each person is here because some flow needs them:

- managers at two levels (Vikram has reports; so does Arjun, who reports to him),
- salaries on both sides of the ESI threshold and one above the surcharge band,
- a joiner still on probation and in onboarding (Fatima),
- someone serving notice (Neha) and someone who has left (Ravi),
- an administrator who looks after assets (Manoj).

The first five codes and the logins of the first six people come from
`seed_demo.py`; this file only adds what those records lack.
"""

from __future__ import annotations

import hashlib

from app.services.employee_identifiers import verhoeff_valid

from .api import Person

DOMAIN = "askmira-demo.com"


def _p(key: str, code: str | None, first: str, last: str, **kw) -> Person:
    return Person(key=key, code=code, first_name=first, last_name=last,
                  email=kw.pop("email", f"{key}@{DOMAIN}"), **kw)


CAST: list[Person] = [
    # ── From seed_demo: the role logins ─────────────────────────────────────
    _p("ananya", "DEMO001", "Ananya", "Rao", email=f"admin@{DOMAIN}", role="admin",
       department="LDR", designation="Chief Executive Officer", grade="E1",
       gender="female", date_of_birth="1982-03-14", date_of_joining="2021-06-01",
       ctc=6_000_000, phone="+91 98450 11001"),
    _p("priya", "DEMO002", "Priya", "Ramesh", email=f"hr.admin@{DOMAIN}", role="admin",
       department="HR", designation="Head of People", grade="M4", manager="ananya",
       gender="female", date_of_birth="1986-07-22", date_of_joining="2022-01-10",
       ctc=3_200_000, phone="+91 98450 11002"),
    _p("karthik", "DEMO003", "Karthik", "Iyer", email=f"hr.ops@{DOMAIN}", role="sub_admin",
       department="HR", designation="HR Operations Executive", grade="P2", manager="priya",
       gender="male", date_of_birth="1995-11-03", date_of_joining="2023-07-17",
       ctc=720_000, phone="+91 98450 11003"),
    _p("vikram", "DEMO004", "Vikram", "Shah", email=f"manager@{DOMAIN}",
       department="ENG", designation="Engineering Manager", grade="M3", manager="ananya",
       gender="male", date_of_birth="1988-10-09", date_of_joining="2022-04-04",
       ctc=3_600_000, phone="+91 98450 11004"),
    _p("meera", "DEMO005", "Meera", "Nair", email=f"employee@{DOMAIN}",
       department="ENG", designation="Software Engineer", grade="P3", manager="vikram",
       gender="female", date_of_birth="1996-10-12", date_of_joining="2024-04-01",
       ctc=1_800_000, phone="+91 98450 11005"),
    # ── Added by the people flow, through the API ───────────────────────────
    _p("arjun", "DEMO006", "Arjun", "Mehta",
       department="ENG", designation="Senior Software Engineer", grade="P4", manager="vikram",
       gender="male", date_of_birth="1991-02-18", date_of_joining="2022-08-01",
       ctc=2_600_000, phone="+91 98450 11006"),
    _p("sneha", "DEMO007", "Sneha", "Kulkarni",
       department="ENG", designation="Software Engineer", grade="P3", manager="vikram",
       gender="female", date_of_birth="1997-05-27", date_of_joining="2023-10-09",
       ctc=1_500_000, phone="+91 98450 11007"),
    _p("rohan", "DEMO008", "Rohan", "Das",
       department="ENG", designation="QA Engineer", grade="P2", manager="arjun",
       gender="male", date_of_birth="1998-12-01", date_of_joining="2024-01-15",
       ctc=900_000, phone="+91 98450 11008"),
    _p("fatima", "DEMO009", "Fatima", "Sheikh",
       department="ENG", designation="Software Engineer", grade="P2", manager="vikram",
       gender="female", date_of_birth="1999-08-19", date_of_joining="2026-09-21",
       ctc=1_200_000, phone="+91 98450 11009"),
    _p("aditya", "DEMO010", "Aditya", "Verma",
       department="ENG", designation="DevOps Engineer", grade="P3", manager="vikram",
       gender="male", date_of_birth="1993-04-25", date_of_joining="2023-03-01",
       ctc=1_700_000, phone="+91 98450 11010"),
    _p("kavya", "DEMO011", "Kavya", "Menon",
       department="PRD", designation="Product Manager", grade="M2", manager="ananya",
       gender="female", date_of_birth="1990-09-30", date_of_joining="2022-11-14",
       ctc=2_800_000, phone="+91 98450 11011"),
    _p("nikhil", "DEMO012", "Nikhil", "Joshi",
       department="PRD", designation="Product Designer", grade="P3", manager="kavya",
       gender="male", date_of_birth="1994-06-11", date_of_joining="2024-06-03",
       ctc=1_400_000, phone="+91 98450 11012"),
    _p("rahul", "DEMO013", "Rahul", "Kapoor", location="MUM",
       department="SAL", designation="Head of Sales", grade="M3", manager="ananya",
       gender="male", date_of_birth="1985-01-20", date_of_joining="2022-02-01",
       ctc=3_000_000, phone="+91 98200 11013"),
    _p("pooja", "DEMO014", "Pooja", "Desai", location="MUM",
       department="SAL", designation="Account Executive", grade="P2", manager="rahul",
       gender="female", date_of_birth="1996-03-08", date_of_joining="2024-09-02",
       ctc=850_000, phone="+91 98200 11014"),
    _p("imran", "DEMO015", "Imran", "Khan", location="MUM",
       department="SAL", designation="Sales Development Representative", grade="P1",
       manager="rahul", gender="male", date_of_birth="2000-07-15",
       date_of_joining="2025-11-03", ctc=240_000, phone="+91 98200 11015"),
    _p("neha", "DEMO016", "Neha", "Gupta", location="MUM",
       department="SAL", designation="Account Manager", grade="P3", manager="rahul",
       gender="female", date_of_birth="1993-10-21", date_of_joining="2023-05-15",
       ctc=1_100_000, phone="+91 98200 11016"),
    _p("suresh", "DEMO017", "Suresh", "Pillai",
       department="FIN", designation="Finance Manager", grade="M2", manager="ananya",
       gender="male", date_of_birth="1984-12-05", date_of_joining="2021-09-01",
       ctc=2_400_000, phone="+91 98450 11017"),
    _p("divya", "DEMO018", "Divya", "Reddy",
       department="FIN", designation="Accounts Executive", grade="P2", manager="suresh",
       gender="female", date_of_birth="1997-01-29", date_of_joining="2024-02-12",
       ctc=600_000, phone="+91 98450 11018"),
    _p("manoj", "DEMO019", "Manoj", "Kumar",
       department="OPS", designation="Office & IT Administrator", grade="P2", manager="priya",
       gender="male", date_of_birth="1990-08-08", date_of_joining="2022-06-20",
       ctc=480_000, phone="+91 98450 11019"),
    _p("lakshmi", "DEMO020", "Lakshmi", "Narayan",
       department="OPS", designation="Customer Support Executive", grade="P1", manager="manoj",
       gender="female", date_of_birth="1999-11-11", date_of_joining="2025-07-01",
       ctc=228_000, phone="+91 98450 11020"),
    _p("anjali", "DEMO021", "Anjali", "Singh",
       department="HR", designation="Talent Acquisition Specialist", grade="P3",
       manager="priya", gender="female", date_of_birth="1994-02-14",
       date_of_joining="2023-01-09", ctc=1_000_000, phone="+91 98450 11021"),
    _p("ravi", "DEMO022", "Ravi", "Teja",
       department="ENG", designation="Software Engineer", grade="P3", manager="vikram",
       gender="male", date_of_birth="1995-09-17", date_of_joining="2023-06-01",
       ctc=1_600_000, phone="+91 98450 11022"),
]

#: The people seed_demo creates directly; everyone else goes through the API.
BASE_KEYS = ("ananya", "priya", "karthik", "vikram", "meera")

DEPARTMENTS = (
    ("LDR", "Leadership", "ananya"),
    ("ENG", "Engineering", "vikram"),
    ("PRD", "Product & Design", "kavya"),
    ("SAL", "Sales", "rahul"),
    ("FIN", "Finance", "suresh"),
    ("OPS", "Operations", "manoj"),
    ("HR", "People", "priya"),
)


# ── Identifiers that pass the same validation real ones do ─────────────────


def _digits(seed: str, n: int) -> str:
    raw = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return str(raw)[-n:].rjust(n, "1")


def pan_for(person: Person) -> str:
    """Five letters, four digits, a letter; fourth P for a person, fifth the surname."""
    h = hashlib.sha256(person.email.encode()).hexdigest().upper()
    letters = [c for c in h if c.isalpha()] + list("ABCDE")
    return (
        f"{letters[0]}{letters[1]}{letters[2]}P{person.last_name[0].upper()}"
        f"{_digits(person.email + 'pan', 4)}{letters[3]}"
    )


def aadhaar_for(person: Person) -> str:
    """Twelve digits, not starting 0 or 1, with a valid Verhoeff check digit."""
    body = "8" + _digits(person.email + "uid", 10)
    for check in "0123456789":
        if verhoeff_valid(body + check):
            return body + check
    raise AssertionError("no Verhoeff digit")  # unreachable: one always exists


def uan_for(person: Person) -> str:
    return "10" + _digits(person.email + "uan", 10)


def esi_for(person: Person) -> str:
    return "31" + _digits(person.email + "esi", 8)


def account_for(person: Person) -> str:
    return "5010" + _digits(person.email + "acct", 10)


BANKS = (
    ("HDFC Bank", "HDFC0000523"),
    ("ICICI Bank", "ICIC0000104"),
    ("State Bank of India", "SBIN0040277"),
    ("Axis Bank", "UTIB0000009"),
    ("Kotak Mahindra Bank", "KKBK0000431"),
)


def bank_for(person: Person) -> tuple[str, str]:
    index = int(_digits(person.email + "bank", 3)) % len(BANKS)
    return BANKS[index]
