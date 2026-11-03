"""Ask Mira without a model: the person's words matched to one tool, by rule.

Used whenever the AI planner cannot be: the server has no AI provider, the
workspace switched AI off, or the person has not agreed to it. Nothing leaves
AskMira in this mode: the message is matched here, the tool runs here as the
person (tools.run), and the result is shown as cards, as with the model.

Only the tools that need no model of their own are offered here (tools.offered
with `ai=False`): the policy and report questions are answered by a model, so
they wait for AI to be on.

The matching is deliberately plain: the words are first tidied (common typos,
Hindi-English words such as "meri", "chutti", "is mahine"), then read for what
is asked (keywords), for when (dates, weeks, months, quarters, years) and for
whom (a name or an employee code). When nothing matches, the reply says what
can be asked, with examples drawn from the tools this person actually has.

Who may ask what: an admin or sub-admin may ask about anybody ("what is Deepak's
salary", "how many employees are on payroll", "who joined this month").
Everybody else is answered about their own record only; a question about another
person or the whole company is refused here, before any tool runs.

The bank of questions this must answer lives in tests/companion/question_bank.py;
a phrasing that goes wrong belongs there before it is fixed here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.services.companion import product_tools
from app.services.companion.tools import Tool
from app.services.products.catalogue import PRODUCTS

__all__ = ["DirectPlan", "dates", "examples", "normalise", "plan"]

_HIRE = PRODUCTS["interview"].brand
_EXPENSE = PRODUCTS["expense"].brand
_HELPDESK = PRODUCTS["helpdesk"].brand
_SALES = PRODUCTS["saleinx"].brand
_PROCURE = PRODUCTS["procurement"].brand


@dataclass(frozen=True, slots=True)
class DirectPlan:
    reply: str
    tool: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)


# ── Tidying the words ────────────────────────────────────────────────────────

#: Misspellings and short forms people type, word for word.
_TYPOS: dict[str, str] = {
    # salary and pay
    "salery": "salary",
    "sallary": "salary",
    "salry": "salary",
    "slary": "salary",
    "salaray": "salary",
    "salarey": "salary",
    "saalary": "salary",
    "salries": "salaries",
    "saleries": "salaries",
    "sal": "salary",
    "paysilp": "payslip",
    "payslp": "payslip",
    "pay-slip": "payslip",
    "payslips": "payslips",
    "payrol": "payroll",
    "payrolll": "payroll",
    "pyroll": "payroll",
    "payrool": "payroll",
    "pay-roll": "payroll",
    # leave and attendance
    "leav": "leave",
    "leaev": "leave",
    "lave": "leave",
    "leaves": "leaves",
    "leavs": "leaves",
    "leves": "leaves",
    "attendence": "attendance",
    "attendace": "attendance",
    "atendance": "attendance",
    "attandance": "attendance",
    "attendnce": "attendance",
    "attendanc": "attendance",
    "attedance": "attendance",
    "absentees": "absent",
    "absentee": "absent",
    "latecomers": "late",
    "wfh": "work from home",
    # people
    "employe": "employee",
    "empolyee": "employee",
    "emplyee": "employee",
    "employes": "employees",
    "employess": "employees",
    "empolyees": "employees",
    "emps": "employees",
    "headcout": "headcount",
    "hedcount": "headcount",
    "head-count": "headcount",
    "manger": "manager",
    "maneger": "manager",
    "departmnet": "department",
    "deparment": "department",
    "departmnt": "department",
    "depts": "departments",
    "joinee": "joiner",
    "joinees": "joiners",
    "joinings": "joiners",
    "birthdy": "birthday",
    "bday": "birthday",
    "bdays": "birthdays",
    "b'day": "birthday",
    "b'days": "birthdays",
    "birthdate": "birthday",
    "aniversary": "anniversary",
    "anniversery": "anniversary",
    "aniversaries": "anniversaries",
    "probabtion": "probation",
    "probaton": "probation",
    "resignations": "resignations",
    "resigation": "resignation",
    # approvals
    "approvel": "approval",
    "aproval": "approval",
    "apporval": "approval",
    "approvl": "approval",
    "approvels": "approvals",
    "aprovals": "approvals",
    "aprove": "approve",
    # expenses
    "reimbursment": "reimbursement",
    "reimbursemnt": "reimbursement",
    "reimbusement": "reimbursement",
    "reimbersement": "reimbursement",
    "reimbursment's": "reimbursement",
    "reimbursments": "reimbursements",
    "expence": "expense",
    "expences": "expenses",
    "expens": "expense",
    # tickets and assets
    "tikcet": "ticket",
    "tiket": "ticket",
    "tickt": "ticket",
    "tikets": "tickets",
    "tickts": "tickets",
    "tikcets": "tickets",
    "laptops": "laptops",
    "lappy": "laptop",
    # hiring
    "intervew": "interview",
    "interveiw": "interview",
    "intreview": "interview",
    "interviw": "interview",
    "intervews": "interviews",
    "interveiws": "interviews",
    "candiate": "candidate",
    "candidat": "candidate",
    "canditate": "candidate",
    "candiates": "candidates",
    "candidtes": "candidates",
    "hirring": "hiring",
    "recruitement": "recruitment",
    "vacancies": "vacancies",
    # words around them
    "wat": "what",
    "wht": "what",
    "whats": "what is",
    "hw": "how",
    "mny": "many",
    "u": "you",
    "ur": "your",
    "pls": "please",
    "plz": "please",
    "plzz": "please",
    "thx": "thanks",
    "todays": "today",
    "2day": "today",
    "tmrw": "tomorrow",
    "tmr": "tomorrow",
    "tommorow": "tomorrow",
    "tomorow": "tomorrow",
    "tommorrow": "tomorrow",
    "yday": "yesterday",
    "yesterdays": "yesterday",
    "wk": "week",
    "mnth": "month",
    "mth": "month",
    "avg": "average",
    "ppl": "people",
    "nos": "numbers",
    "approvls": "approvals",
}

#: Hindi-English phrases, read as their English meaning.
_PHRASES: tuple[tuple[str, str], ...] = (
    (r"\bis (mahine|mahene|maheene)\b", "this month"),
    (r"\b(pichle|pichhle|pichhla|pichla) (mahine|mahina|month)\b", "last month"),
    (r"\b(agle|agla) (mahine|mahina|month)\b", "next month"),
    (r"\bis (hafte|hafta)\b", "this week"),
    (r"\b(pichle|pichhle|pichla) (hafte|hafta|week)\b", "last week"),
    (r"\b(agle|agla) (hafte|hafta|week)\b", "next week"),
    (r"\bis saal\b", "this year"),
    (r"\b(pichle|pichhle) saal\b", "last year"),
    (r"\baaj\b", "today"),
    (r"\b(chhutti|chutti|chuttiyan|chhuttiyan|chhutiyan|chhuttiya) (par|pe|pr)\b", "on leave"),
    (r"\b(leave) (par|pe|pr)\b", "on leave"),
    (r"\b(chhutti|chutti|chuttiyan|chhuttiyan|chhutiyan|chhuttiya)\b", "leave"),
    (r"\b(tankhwah|tankhwa|tankha|tanakhwah|vetan|pagar)\b", "salary"),
    (r"\b(hajiri|haziri|upasthiti)\b", "attendance"),
    (r"\b(kharcha|kharche|kharchon)\b", "expenses"),
    (r"\b(janamdin|janmdin)\b", "birthday"),
    (r"\b(meri|mera|mere|mujhe|apni|apna|apne)\b", "my"),
    (r"\bkaun\b", "who"),
    (r"\b(kitne|kitni|kitna) log\b", "how many people"),
    (r"\b(kitne|kitni|kitna) (employees?|staff|karmchari)\b", "how many employees"),
    (r"\b(kitne|kitni|kitna)\b", "how much"),
    (r"\b(baki|bakki|baaki|bachi|bache|bacha)\b", "left"),
    (r"\b(naye|naya|nae|nye) (log|joiners?|employees?|joinee)\b", "new joiners"),
    (r"\b(chhod|chod) (diya|gaye|gaya|di|gayi)\b", "left"),
    (r"\b(kab)\b", "when"),
    (r"\bjoin (hue|hua|hui|kiye|kiya|huye)\b", "joined"),
)


def normalise(text: str) -> str:
    """The message lowercased and tidied: typos fixed, Hindi-English read as English."""
    words = text.lower().replace("\u2019", "'").replace("\u2018", "'")
    words = re.sub(r"[,;!?]+", " ", words)
    words = " ".join(words.split())
    words = re.sub(r"\bwho's\b", "who is", words)
    words = re.sub(r"\bwhat's\b", "what is", words)
    words = re.sub(r"\bhow's\b", "how is", words)
    words = re.sub(r"\b(hey|hi|hello|dear|ok|okay) (mira|askmira|ask mira)\b", " ", words)
    words = " ".join(_TYPOS.get(w, w) for w in words.split())
    for pattern, meaning in _PHRASES:
        words = re.sub(pattern, meaning, words)
    return " ".join(words.split()).strip(" .")


#: How people start a question; nothing in it says what or whom they ask about.
_LEAD_IN = re.compile(
    r"\b(i want to know|i would like to know|i'd like to know|i want to see|i need to know|"
    r"i want to check|i wanted to know|i want|i need|i would like|i'd like|i wanna|"
    r"can you please tell me|can you tell me|could you tell me|could you please tell me|"
    r"please tell me|tell me|show me|give me|send me|get me|fetch me|"
    r"let me know|let me see|help me with|help me find|can you please|could you please|"
    r"can you|could you|would you|will you|please check|could you check|can you check|"
    r"do you know|any idea|quickly)\b"
)
_GREETING_LEAD = re.compile(
    r"^(hi+|hello|hey|hlo|namaste|namaskar|good (morning|afternoon|evening)|thanks|"
    r"thank you|ok|okay|dear|mira)( mira)?\.?\s+"
)


def _asked(words: str) -> str:
    """The question without its greeting and lead-in: "hi, can you tell me X" is X."""
    for _ in range(2):
        words = _GREETING_LEAD.sub("", words)
    stripped = " ".join(_LEAD_IN.sub(" ", words).split()).strip(" .")
    return stripped or words


# ── Reading the words ────────────────────────────────────────────────────────


def _has(text: str, *patterns: str) -> bool:
    return any(re.search(rf"\b{pattern}", text) for pattern in patterns)


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _month(day: date) -> tuple[date, date]:
    first = day.replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    return first, following - timedelta(days=1)


_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))
#: A month by name. "may" alone is too common a word, so it needs "in"/"for"/a year.
_MONTH = re.compile(
    rf"\b(?:(in|for|of|during|month of|salary of|payroll of)\s+)?({_MONTH_NAMES})\b"
    r"(?:\s*[-' ]?\s*(\d{4}|\d{2})\b)?"
)


def _named_month(text: str, today: date) -> tuple[date, date] | None:
    for match in _MONTH.finditer(text):
        lead, name, year = match.group(1), match.group(2), match.group(3)
        if name == "may" and not (lead or year):
            continue
        short = name in (
            "mar",
            "jan",
            "jun",
            "jul",
            "dec",
            "oct",
            "nov",
            "sep",
            "aug",
            "apr",
            "feb",
        )
        # Short forms only next to a lead word or a year: "sep payroll", "oct 2026".
        if (
            short
            and not (lead or year)
            and not re.search(rf"\b{name}\s+(payroll|salary|salaries|pay|run|month)\b", text)
        ):
            continue
        number = _MONTHS[name]
        if year:
            value = int(year)
            value = value + 2000 if value < 100 else value
        else:
            value = today.year
        return _month(date(value, number, 1))
    return None


def _quarter(day: date) -> tuple[date, date]:
    first_month = 3 * ((day.month - 1) // 3) + 1
    start = date(day.year, first_month, 1)
    end = _month(date(day.year, first_month + 2, 1))[1]
    return start, end


def _financial_year(day: date) -> tuple[date, date]:
    start_year = day.year if day.month >= 4 else day.year - 1
    return date(start_year, 4, 1), date(start_year + 1, 3, 31)


def dates(text: str, today: date) -> tuple[str, str]:
    """The range of days the words name, as ISO dates; ("", "") when they name none."""
    # "Every month", "per year", "monthly" say how often, not when.
    text = re.sub(r"\b(every|per|each|a|an) (day|week|month|year|quarter)\b", " ", text)
    found = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if found:
        return found[0], found[1] if len(found) > 1 else found[0]

    span = re.search(r"\b(next|last|past|coming|previous)\s+(\d{1,3})\s+days?\b", text)
    if span:
        count = max(1, min(int(span.group(2)), 366))
        if span.group(1) in ("next", "coming"):
            return today.isoformat(), (today + timedelta(days=count - 1)).isoformat()
        return (today - timedelta(days=count - 1)).isoformat(), today.isoformat()

    start: date | None = None
    end: date | None = None
    month = _named_month(text, today)
    if month:
        start, end = month
    elif _has(text, r"today\b", r"right now\b", r"now\b"):
        start = end = today
    elif _has(text, r"day after tomorrow\b"):
        start = end = today + timedelta(days=2)
    elif _has(text, r"tomorrow\b"):
        start = end = today + timedelta(days=1)
    elif _has(text, r"yesterday\b"):
        start = end = today - timedelta(days=1)
    elif _has(text, r"next week\b", r"coming week\b", r"upcoming week\b"):
        start = _monday(today) + timedelta(days=7)
        end = start + timedelta(days=6)
    elif _has(text, r"last week\b", r"previous week\b", r"past week\b"):
        start = _monday(today) - timedelta(days=7)
        end = start + timedelta(days=6)
    elif _has(text, r"this week\b", r"week\b"):
        start = _monday(today)
        end = start + timedelta(days=6)
    elif _has(text, r"next month\b", r"coming month\b", r"upcoming month\b"):
        start, end = _month(_month(today)[1] + timedelta(days=1))
    elif _has(text, r"last month\b", r"previous month\b", r"past month\b"):
        start, end = _month(today.replace(day=1) - timedelta(days=1))
    elif _has(text, r"next quarter\b"):
        start, end = _quarter(_quarter(today)[1] + timedelta(days=1))
    elif _has(text, r"last quarter\b", r"previous quarter\b"):
        start, end = _quarter(_quarter(today)[0] - timedelta(days=1))
    elif _has(text, r"this quarter\b", r"quarter\b"):
        start, end = _quarter(today)
    elif _has(
        text,
        r"last (financial|fiscal) year\b",
        r"last fy\b",
        r"previous (financial|fiscal) year\b",
    ):
        start, end = _financial_year(_financial_year(today)[0] - timedelta(days=1))
    elif _has(text, r"(this |current )?(financial|fiscal) year\b", r"this fy\b", r"fy\b"):
        start, end = _financial_year(today)
    elif _has(text, r"last year\b", r"previous year\b"):
        start, end = date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    elif _has(text, r"this year\b", r"year to date\b", r"ytd\b", r"so far this year\b"):
        start, end = date(today.year, 1, 1), date(today.year, 12, 31)
    elif _has(text, r"this month\b", r"current month\b", r"month\b"):
        start, end = _month(today)
    if start is None or end is None:
        return "", ""
    return start.isoformat(), end.isoformat()


def _span(inputs: dict[str, Any], fallback: str) -> str:
    start, end = inputs.get("from") or "", inputs.get("to") or ""
    if not start:
        return fallback
    return f"on {start}" if start == end else f"from {start} to {end}"


def _capped(when: dict[str, str], days: int) -> dict[str, str]:
    """A range no longer than `days`: a longer one keeps its last `days` days."""
    if not when["from"]:
        return when
    start, end = date.fromisoformat(when["from"]), date.fromisoformat(when["to"])
    if (end - start).days < days:
        return when
    return {"from": (end - timedelta(days=days - 1)).isoformat(), "to": end.isoformat()}


_STOP = {
    "a",
    "an",
    "the",
    "of",
    "for",
    "in",
    "on",
    "at",
    "to",
    "me",
    "my",
    "all",
    "any",
    "please",
    "show",
    "list",
    "find",
    "search",
    "get",
    "give",
    "tell",
    "who",
    "whos",
    "is",
    "are",
    "was",
    "were",
    "what",
    "which",
    "with",
    "and",
    "or",
    "about",
    "from",
    "do",
    "does",
    "did",
    "we",
    "our",
    "us",
    "there",
    "can",
    "could",
    "would",
    "you",
    "your",
    "i",
    "named",
    "called",
    "matching",
    "team",
    "department",
    "dept",
    "people",
    "person",
    "employee",
    "employees",
    "colleague",
    "colleagues",
    "staff",
    "directory",
    "job",
    "jobs",
    "role",
    "roles",
    "position",
    "positions",
    "opening",
    "openings",
    "candidate",
    "candidates",
    "applicant",
    "applicants",
    "interview",
    "interviews",
    "scheduled",
    "hiring",
    "claim",
    "claims",
    "expense",
    "expenses",
    "reimbursement",
    "reimbursements",
    "details",
    "contact",
    "email",
    "this",
    "next",
    "last",
    "week",
    "month",
    "today",
    "tomorrow",
    "yesterday",
    "how",
    "many",
    "much",
    "have",
    "has",
    "had",
    "be",
    "been",
    "it",
    "that",
    "these",
    "those",
    "status",
    "currently",
    "now",
    "working",
    "works",
    "work",
    "here",
    "everyone",
    "whole",
    "company",
    "everybody",
    "mine",
    "where",
    "anytimehire",
    "anytime",
    "hire",
    "expensegenie",
    "genie",
    "ticket",
    "tickets",
    "helpdesk",
    "help",
    "desk",
    "support",
    "asset",
    "assets",
    "raised",
    "assigned",
    "queue",
    "unassigned",
    "register",
    "hold",
    "progress",
    "cgb",
    "askmira",
    "mira",
    "hey",
    "hi",
    "hello",
    "dear",
    "kindly",
    "check",
    "know",
    "want",
    "need",
    "like",
    "see",
    "view",
    "let",
    "just",
    "quickly",
    "quick",
    "also",
    "again",
    "hai",
    "hain",
    "ka",
    "ki",
    "ke",
    "ko",
    "kya",
    "batao",
    "bataiye",
    "bata",
    "dikhao",
    "dikha",
    "dena",
    "de",
    "se",
    "mein",
    "par",
    "pe",
    "hua",
    "hue",
    "hui",
    "tha",
    "thi",
    "kaam",
    "karte",
    "karta",
    "karti",
    "log",
    "good",
    "morning",
    "afternoon",
    "evening",
    "thanks",
    "thank",
    "namaste",
    "ok",
    "okay",
    "look",
    "up",
    "am",
}


#: Words that say what is asked about a person, or are filler around a name.
_PERSON_FILLER = frozenset(
    {
        "salary",
        "salaries",
        "salary's",
        "ctc",
        "package",
        "compensation",
        "pay",
        "paid",
        "payslip",
        "payslips",
        "slip",
        "slips",
        "gross",
        "net",
        "take",
        "home",
        "in-hand",
        "inhand",
        "hand",
        "wage",
        "wages",
        "earn",
        "earns",
        "earning",
        "earnings",
        "increment",
        "monthly",
        "annual",
        "annually",
        "yearly",
        "per",
        "year",
        "years",
        "day",
        "days",
        "structure",
        "breakup",
        "break",
        "up",
        "breakdown",
        "current",
        "present",
        "latest",
        "total",
        "leave",
        "leaves",
        "balance",
        "balances",
        "left",
        "remaining",
        "available",
        "pending",
        "attendance",
        "lop",
        "loss",
        "manager",
        "managers",
        "boss",
        "lead",
        "reporting",
        "reports",
        "report",
        "profile",
        "detail",
        "info",
        "information",
        "joining",
        "joined",
        "join",
        "date",
        "doj",
        "designation",
        "title",
        "code",
        "id",
        "emp",
        "empid",
        "no",
        "number",
        "get",
        "gets",
        "getting",
        "make",
        "makes",
        "check",
        "know",
        "want",
        "see",
        "view",
        "need",
        "would",
        "like",
        "kindly",
        "sir",
        "mam",
        "maam",
        "the",
        "his",
        "her",
        "their",
        "them",
        "him",
        "he",
        "she",
        "they",
        "does",
        "did",
        "will",
        "should",
        "could",
        "may",
        "much",
        "so",
        "far",
        "till",
        "until",
        "upto",
        "as",
        "by",
        "when",
        "whom",
        "whose",
        "kitna",
        "kitni",
        "currently",
        "drawing",
        "draws",
        "draw",
        "download",
        "send",
        "share",
        "print",
        "generate",
        "fetch",
        "pull",
        "provide",
        "recent",
        "previous",
        "month's",
        "summary",
        "sheet",
        "register",
        "overview",
        "list",
        "status",
        "everyone",
        "all",
        "company",
        "staff",
        "absent",
        "late",
        "times",
        "marks",
        "count",
        "was",
        "were",
        "am",
        "made",
        "takes",
        "earned",
        "salary?",
        "monthly?",
        "stand",
        "standing",
        "much?",
        "anymore",
        "right",
        "whats",
        "taken",
        "took",
        "used",
        "availed",
        "finalists",
        "finalist",
        "marked",
        "approves",
        "approve",
        "approver",
        "holidays",
        "holiday",
        "name",
        "names",
        "bhejo",
        "bhej",
        "chahiye",
        "aa",
        "gayi",
        "gaya",
        "aayi",
        "aaya",
        "mili",
        "mila",
        "kya",
        "role",
    }
)

_SELF_WORDS = frozenset({"my", "mine", "myself", "me", "i", "i'm", "im", "own", "i've"})


def _person_topic(text: str) -> str | None:
    """What is asked about a person: salary, payslip, leave, attendance, manager or profile."""
    if _has(
        text,
        r"who (approves|will approve|is approving|should approve) (my|his|her|their)",
        r"approver\b",
    ):
        return "manager"
    if re.search(r"\bwho is (the|our) [a-z]+( [a-z]+)? (manager|head|lead)\b", text):
        return None  # a role, found in the directory
    if _has(text, "payslip", r"pay ?slip", "salary slip"):
        return "payslip"
    if _has(
        text,
        "salar",
        r"ctc\b",
        r"package\b",
        "compensation",
        r"pay\b",
        r"gross\b",
        r"take home\b",
        r"in-?hand\b",
        r"wages?\b",
        r"earns?\b",
        "earning",
        "increment",
        r"drawing\b",
        r"paid\b(?! leaves?| holidays?| off)",
        r"how much (does|do|did|is|am|are|will) [a-z0-9 ]{1,40}\b(make|makes|get|gets|take|"
        r"takes|earn)\b",
    ):
        return "salary"
    if _has(
        text,
        "leave balance",
        r"balance",
        r"leaves? (left|remaining|available|pending)",
        "remaining leave",
        r"how (many|much) (casual |sick |earned |privilege )?leaves?\b",
        r"leaves? (do|does|did) (i|he|she|they)\b",
        r"leaves? (i|he|she) (have|has)",
        r"my leaves?\b",
        r"leaves? of\b",
        r"(casual|sick|earned|privilege) leaves?\b",
        r"[a-z]+'s leaves?\b",
        r"leaves? how (much|many)\b",
        r"leaves? [a-z ]{0,15}\bleft\b",
        r"leaves? status\b",
        r"how many (cl|sl|el|pl)s?\b",
        r"(cl|sl|el|pl) (balance|left|remaining)\b",
        r"leaves? (taken|availed|used)\b",
        r"holidays? [a-z ]{0,15}\bleft\b",
        r"balance leaves?\b",
    ):
        return "leave_balance"
    if _has(
        text,
        "attendance",
        r"lop\b",
        "loss of pay",
        r"(days|times) [a-z0-9 ]{0,25}\b(present|absent|late)\b",
        r"late marks?\b",
        r"(present|absent) days\b",
        r"(am|was|were|is) (i|he|she) (marked )?(absent|present|late)\b",
    ):
        return "attendance_this_month"
    if _has(
        text,
        "manager",
        r"reports? to",
        "reporting to",
        r"boss\b",
        r"reporting (head|line|person|authority|officer)\b",
        r"team lead\b",
    ):
        return "manager"
    if _has(
        text,
        "profile",
        r"details?\b",
        "joining",
        r"joined\b",
        r"join\b",
        r"doj\b",
        "designation",
        r"employee (code|id|number|no)\b",
        r"emp ?(code|id|no)\b",
        r"work email\b",
        r"[a-z]+'s role\b",
        r"role of\b",
    ):
        return "profile"
    return None


def _person_query(text: str) -> tuple[str, bool]:
    """(the name or employee code asked about, whether the asker asks about themselves)."""
    code = re.search(r"\(([a-z0-9][a-z0-9/_.-]{0,31})\)", text)
    if code:
        return code.group(1), False
    quoted = re.search(r"[\"“](.+?)[\"”]", text)
    if quoted:
        return quoted.group(1).strip()[:100], False
    cleaned = re.sub(r"\d{4}-\d{2}-\d{2}", " ", text)
    cleaned = re.sub(r"\b(next|last|past|coming)\s+\d{1,3}\s+days?\b", " ", cleaned)
    cleaned = re.sub(rf"\b({_MONTH_NAMES})\b(\s+\d{{4}})?", " ", cleaned)
    tokens = re.findall(r"[a-z0-9][a-z0-9.@+'-]*", cleaned)
    about_me = any(t in _SELF_WORDS for t in tokens)
    kept: list[str] = []
    for token in tokens:
        token = token.removesuffix("'s").rstrip("'")
        if token in _STOP or token in _PERSON_FILLER or token in _SELF_WORDS:
            continue
        if token in _LEAVE_KINDS:
            continue
        # A lone letter, or a small number ("my last 3 payslips"), names nobody.
        if len(token) < 2 or (token.isdigit() and len(token) < 3):
            continue
        kept.append(token)
    return " ".join(kept)[:100], about_me and not kept


_LEAVE_KINDS = frozenset({"casual", "sick", "earned", "privilege", "cl", "sl", "el", "pl"})

_PERSON_LABEL = {
    "salary": "salary",
    "payslip": "payslip",
    "leave_balance": "leave balance",
    "attendance_this_month": "attendance",
    "manager": "manager",
    "profile": "profile",
}

_OWN_LOOKUP = {
    "salary": "my_salary",
    "payslip": "latest_payslip",
    "leave_balance": "leave_balance",
    "attendance_this_month": "attendance_this_month",
    "manager": "my_manager",
    "profile": "my_profile",
}

_OWN_REPLY = {
    "salary": "Here is your current salary.",
    "payslip": "Here is your latest payslip.",
    "leave_balance": "Here is your leave balance.",
    "attendance_this_month": "Here is your attendance this month.",
    "manager": "Here is who your manager is.",
    "profile": "Here is your profile.",
}

#: Tools about other people or the company: admins and sub-admins only.
_HR_TOOLS = frozenset(
    {
        "search_people",
        "who_is_out",
        "attendance_summary",
        "list_departments",
        "list_locations",
        "employee_record",
        "headcount",
        "report_question",
        "payroll_summary",
        "salary_totals",
        "people_changes",
        "people_dates",
        "payroll_trend",
        "headcount_trend",
        "account_pnl",
        "account_forecast",
    }
)


def _own_only(lead: str = "") -> DirectPlan:
    return DirectPlan(
        reply=(f"{lead} " if lead else "")
        + "I can only answer about your own record: your salary, payslip, leave balance, "
        "attendance, manager or profile. Other people's details and company-wide figures "
        "are for admins and sub-admins."
    )


def _search_text(text: str, drop: tuple[str, ...] = (), keep: tuple[str, ...] = ()) -> str:
    """What is left to search by once commands, dates, statuses and filler are taken out."""
    quoted = re.search(r"[\"“](.+?)[\"”]", text)
    if quoted:
        return quoted.group(1).strip()[:100]
    cleaned = re.sub(r"\d{4}-\d{2}-\d{2}", " ", text)
    cleaned = re.sub(r"\b(next|last|past|coming)\s+\d{1,3}\s+days?\b", " ", cleaned)
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9.@+'-]*", cleaned) if w.strip("'")]
    # "rohan's phone" searches for rohan.
    words = [w.removesuffix("'s") for w in words]
    kept = [w for w in words if w in keep or (w not in _STOP and w not in drop)]
    return " ".join(kept)[:100]


def _ticket_status(text: str) -> str:
    if _has(text, "in[ -]?progress", "being worked", "working on"):
        return "in_progress"
    if _has(text, "on hold", "hold\\b", "paused", "waiting on me", "waiting for me"):
        return "hold"
    if _has(text, "closed", "resolved", "done\\b", "fixed", "solved", "completed"):
        return "closed"
    if _has(text, "new\\b", "not picked", "unacknowledged"):
        return "new"
    if _has(text, "open\\b", "pending", "unresolved", "active", "not (yet )?resolved", "still"):
        return "open"
    return "none"


def _asset_status(text: str) -> str:
    if _has(text, "in use", "in-use", "allocated"):
        return "in_use"
    if _has(text, "spare", "unused", "free\\b", "available", "in stock"):
        return "spare"
    if _has(text, "retired", "scrapped", "disposed"):
        return "retired"
    return "none"


def _asset_search(words: str) -> str:
    """Asset words to search by, singular: "laptops" finds "Laptop"."""
    text = _search_text(
        words,
        (
            "in",
            "use",
            "spare",
            "retired",
            "unused",
            "available",
            "free",
            "allocated",
            "held",
            "hold",
            "stock",
            "inventory",
            "how",
            "many",
            "count",
        ),
    )
    return " ".join(w[:-1] if len(w) > 4 and w.endswith("s") else w for w in text.split())


def _status(text: str, allowed: tuple[str, ...]) -> str:
    synonyms = {
        "closed": "finished",
        "filled": "finished",
        "active": "open",
        "paused": "inactive",
        "shortlisted": "selected",
        "offer": "offered",
        "offers": "offered",
        "interviewing": "interview",
        "rejections": "rejected",
        "approve": "approved",
        "unapproved": "pending",
    }
    for word in re.findall(r"[a-z]+", text):
        word = synonyms.get(word, word)
        if word in allowed:
            return word
    return "none"


# ── Choosing ─────────────────────────────────────────────────────────────────


_GREETING = re.compile(
    r"^(hi+|hello|hey|hlo|helo|namaste|namaskar|good (morning|afternoon|evening)|thanks|"
    r"thank you|thank u|thanks a lot|ok|okay|cool|great|help|menu|start|"
    r"what can you do|what can i ask|what do you do|who are you|how can you help( me)?)"
    r"( so much| very much| a lot)?( mira)?( you)?$"
)


def examples(tools: list[Tool]) -> list[str]:
    """Questions this person can ask here, one per tool they have."""
    keys = {tool.key for tool in tools}
    sample = [
        ("employee_record", "What is Rohan's salary?"),
        ("headcount", "How many employees are on payroll?"),
        ("my_approvals", "What is waiting for my approval?"),
        ("payroll_trend", "Payroll cost trend for the last 6 months"),
        ("payroll_summary", "Payroll cost last month"),
        ("people_changes", "Who joined this month?"),
        ("my_record", "What is my salary?"),
        ("who_is_out", "Who is on leave this week?"),
        ("people_dates", "Birthdays this week"),
        ("query_data", "Leave days by month this year"),
        ("account_pnl", "P&L of each account"),
        ("my_record", "How many leaves do I have left?"),
        ("salary_totals", "Total salary bill"),
        ("search_people", "Find Rohan"),
        ("attendance_summary", "Attendance this month"),
        ("list_departments", "List departments"),
        ("hiring_overview", "Hiring overview"),
        ("hiring_jobs", "Show open jobs"),
        ("hiring_candidates", "Candidates offered"),
        ("hiring_interviews", "Interviews this week"),
        ("expense_claims", "My pending expense claims"),
        ("expense_summary", "Expense total this month"),
        ("helpdesk_tickets", "My open tickets"),
        ("hr_tickets", "My HR tickets"),
        ("helpdesk_assets", "What assets do I have?"),
        ("helpdesk_overview", "Helpdesk overview"),
        ("product_connections", f"Is {_HIRE} connected?"),
        ("open_product", f"Open {_HIRE}"),
        ("sales_deals", f"Won deals waiting for approval in {_SALES}"),
        ("sales_pipeline", "My sales pipeline"),
        ("procurement_approvals", f"What is waiting for my approval in {_PROCURE}?"),
        ("procurement_documents", "Invoices awaiting approval"),
        ("procurement_budgets", "Budgets by cost centre"),
        ("procurement_overview", f"{_PROCURE} overview"),
        ("account_forecast", "Prospective P&L of each account"),
    ]
    return [text for key, text in sample if key in keys]


def _help(tools: list[Tool], name: str, *, prefix: str = "") -> DirectPlan:
    if not tools:
        return DirectPlan(reply=f"{prefix} No lookups are switched on for you yet.".strip())
    tried = "\n".join(f"• {text}" for text in examples(tools)[:8])
    return DirectPlan(reply=f"{prefix} Here is what you can ask {name}:\n{tried}".strip())


def _unavailable(product: str) -> DirectPlan:
    return DirectPlan(
        reply=(
            f"Reading {product} is not available to you here: it is not linked to this "
            "workspace, you have no role in it, or an admin switched it off for Ask Mira."
        )
    )


# What a message is about. Each is a list of patterns read by _has.

_APPROVAL = (
    r"(pending|waiting|awaiting|due) (for |on |with )?(my|me)\b",
    r"(pending|waiting|awaiting) (for |on )?(my )?(approval|action|decision|sign-?off)",
    r"(for|need|needs|needing|require|requires|requiring) (my )?(approval|action|decision)",
    r"my approvals?\b",
    r"approvals? (pending|waiting|due|queue|inbox|for me|to do)",
    r"pending approvals?\b",
    r"(to|should|must|can) (i )?approve\b",
    r"to be approved\b",
    r"approval (inbox|requests?|list)\b",
    r"(anything|something|what|items?|requests?) (do i have )?to approve",
    r"what (do|should|must) i (need to )?approve",
    r"(my|approval) inbox\b",
    r"(pending|open) (leave |expense |travel |attendance |regulari[sz]ation |timesheet |"
    r"overtime |resignation |profile |document |loan |offer |requisition )?requests\b",
    r"(leave|expense|travel|attendance|regulari[sz]ation|timesheet|overtime|resignation|"
    r"comp.?off|shift swap|loan|offer|requisition|profile change) "
    r"(requests?|applications?|approvals?) (pending|waiting|to approve|for approval)",
    r"(leave|expense|travel|attendance|regulari[sz]ation|timesheet|overtime|resignation|"
    r"comp.?off|shift swap|loan|requisition) (requests?|applications?|approvals?)\b"
    r"(?! (status|policy))",
    r"who (has )?(applied|asked) for leave\b",
    r"pending (leaves?|regulari[sz]ations?|timesheets?|claims?|resignations?|requisitions?|"
    r"offers?|loans?|comp.?offs?|overtime|approvals?)\b",
    r"requests? (sent |raised )?to me\b",
    r"awaiting (my )?(approval|action)",
    r"approve (leave|expense|claims?|timesheets?|requests?|regulari)",
    r"^approvals?$",
    r"pending actions?\b",
    r"actions? (pending|waiting)\b",
)

_APPROVAL_KIND: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("leave", (r"leaves?\b", r"time ?off", "comp.?off", r"chutti")),
    (
        "attendance",
        ("attendance", "regulari[sz]", "timesheet", "overtime", "shift swap", r"punch"),
    ),
    ("expense", ("expense", "claim", "reimburs", "travel", r"bills?\b")),
    ("payroll", ("payroll", "salary", r"loans?\b", "adjustment", "tax proof", "settlement")),
    ("hiring", ("requisition", r"offers?\b", "hiring")),
    ("exit", ("resign", "clearance", r"exits?\b")),
    ("people", ("profile", "document", "lifecycle", "asset request")),
)

_PAYROLL = (
    r"payroll (cost|costs|total|totals|summary|status|run|runs|amount|figures?|register|"
    r"report|expense|bill|outgo|processed|done|liability|numbers?|details?|breakup|breakdown)",
    r"(total|company|monthly|month's|this month's|last month's|overall) payroll\b",
    r"payroll (for|of|in) (this|last|the|previous|next|current|[a-z]+ \d{4}|"
    rf"{_MONTH_NAMES})\b",
    rf"({_MONTH_NAMES}) (payroll|salaries|salary run|pay run)\b",
    r"(has|is|was|did) (the )?payroll (been |be )?(run|processed|done|paid|approved|completed)",
    r"(run|ran|process|processed) (the )?payroll\b",
    r"payroll\b$",
    r"^payroll\b",
    r"how much (did|do|have) we (pay|paid|spend|spent|disburse)[a-z ]{0,25}"
    r"(salar|wages|payroll|pf|esi|tds)",
    r"how much (pf|epf|esi|esic|tds|professional tax|pt|gratuity|bonus) (do|did|does|have|has|"
    r"is|was|will) (we|the company|company)",
    r"(pf|epf|esi|esic|tds|professional tax|gratuity) (paid|payable|deposited|deducted)",
    r"(total|company|all) (salar(y|ies)) (paid|disbursed|credited|transferred|payout)",
    r"salar(y|ies) (paid|disbursed|credited|transferred|payout|outgo|processed|released|run)\b",
    r"(did|have|has) (we|the company) (paid|pay|disbursed|credited|released) (the )?salar",
    rf"salar(y|ies) (for|of|in) (the )?(this month|last month|month|{_MONTH_NAMES})\b",
    r"(monthly )?salary outgo\b",
    rf"(salary|salaries) (cost|expense) (for|of|in) (this|last|the month|{_MONTH_NAMES})",
    r"(salary|salaries) (cost|expense) (this|last) month",
    r"salar(y|ies) (went|gone|go|going) out\b",
    r"(net|gross) pay (of|for) (the )?(company|month|everyone|all)",
    r"(total|company|overall) (net|gross) (pay|salary|salaries)\b",
    r"(total|company|overall|monthly) (pf|epf|esi|esic|tds|professional tax|pt|lwf)\b",
    r"(pf|epf|esi|esic|tds|professional tax|lwf) (summary|report|details|figures|"
    r"register|amount|contribution|challan|liability|"
    r"payable|dues?|deducted|deduction|total|for (this|last|the))",
    r"statutory (dues|deductions|payments|liability|contributions?)\b",
    r"pay ?runs?\b",
    r"cost to company (this|last|next|for|in|of)",
    r"(employer|employer's) (cost|contribution)",
)

_SALARY_TOTALS = (
    r"(total|company|overall|whole|entire|combined|aggregate) (annual |monthly |yearly )?"
    r"(salary|salaries|ctc|compensation|wage)",
    r"(salary|wage|ctc|compensation|payroll) bill\b",
    r"salary (cost|budget|spend|outlay)\b",
    r"(all|everyone's|everybody's|all employees'?|employees'|staff) "
    r"(salaries|salary|ctc|compensation)",
    r"(salaries|salary|ctc) of (all|everyone|everybody|the company|the team|all employees|"
    r"each|every)",
    r"average (salary|ctc|pay|package|compensation)",
    r"(department|dept|team)[- ]?wise (salary|ctc|cost)",
    r"(salary|ctc|cost) (by|per) (department|dept|team)",
    r"(sum|total) of (all )?(salaries|ctc)",
    r"how much (salary|ctc) (do|does) (we|the company) (pay|give)",
    r"company'?s? (ctc|salary)",
    r"(spend|spending|spent|paying) on salar(y|ies)",
)

_JOINERS = (
    r"(new )?joiners?\b",
    r"new (hires|employees|people|members|staff|recruits)\b",
    r"(who|people|employees|staff|persons|anyone|anybody) (all )?(has |have )?(joined|is joining|"
    r"are joining|will join|joining)",
    r"(joined|joining|join) (this|last|next|in|on|during|since|today|yesterday|tomorrow|between)",
    r"how many (people |employees |staff )?(joined|have joined|were hired|got hired|hired)",
    r"(onboarded|hired) (this|last|in|during|today)",
    r"(who|people|employees) (did we|have we|we) (hire|hired|onboard)",
    r"we hired\b",
    r"joinings?\b",
)

_LEAVERS = (
    r"exits?\b",
    r"exited\b",
    r"resign(ed|ations?|ing)?\b",
    r"attrition",
    r"turnover\b",
    r"separations?\b",
    r"separated\b",
    r"(on|serving) (their )?notice\b",
    r"notice period (now|currently|people|employees)",
    r"(who|people|employees|staff|persons|anyone|anybody) (all )?(has |have |had )?"
    r"(left|quit|is leaving|are leaving|will leave|leaving)",
    r"left (the company|the organi[sz]ation|us|this|last|in|during)",
    r"leaving (the company|us|this|next|in)",
    r"(quit|relieved|offboarded) (this|last|in|during)",
    r"how many (people |employees |staff )?(left|quit|exited|resigned)",
    r"offboarding\b",
    r"put (down |in )?(their|his|her|the) papers",
    r"papers? down\b",
    r"reliev(ing|ed)\b",
)

_DATES_KIND: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("birthdays", (r"birthdays?", r"born\b")),
    ("anniversaries", (r"anniversar", r"work ?versary", r"completing \d+ years?")),
    (
        "probation",
        (r"probation", r"confirmations? (due|dates?|pending)", r"due for confirmation"),
    ),
    ("last_working_day", (r"last working days?", r"lwd\b", r"last days?\b")),
)

_HR_TICKETS = (
    r"hr (tickets?|help ?desk|queries|query|requests?|issues?|complaints?|cases?|queue)",
    r"grievances?",
    r"tickets? (to|with|for|raised to|raised with|sent to) hr\b",
    r"(ask|asked) hr tickets?",
    r"(salary|payroll|letter|id card) (queries|query|tickets?|requests?)\b",
    r"quer(y|ies) (to|with|for) hr\b",
    r"(employee|staff|employees') (queries|query|tickets|grievances|complaints)\b",
    r"hr (reply|replied|respond|responded|answer|answered)",
    r"(reply|response|answer) from hr\b",
)

_IT_TICKETS = (
    r"tickets?\b",
    r"help ?desk",
    r"it support",
    r"it (issues?|problems?|requests?|complaints?)",
    r"support requests?",
)

_ASSETS = (
    r"assets?\b",
    r"laptops?\b",
    r"devices?\b",
    r"equipment",
    r"hardware",
    r"monitors?\b",
    r"(mobile|sim|phone) (assigned|allocated|given|issued)",
    r"(desktops?|computers?|machines?|mouse|keyboards?|headsets?|chargers?|dongles?|"
    r"data cards?)\b",
)

_HEADCOUNT = (
    r"head ?counts?\b",
    r"how many (people|employees|emp|staff|members|persons|workers|team members|folks|"
    r"active employees|employees are there|people work)\b",
    # Singular "employee number" is a person's code, not a count.
    r"(employees|staff|people|emp|workforce|team) (count|strength|numbers?|total|size)\b",
    r"(employee|headcount) (count|strength|total|size)\b",
    r"(working|work) (with|for) us\b",
    r"(number|no|count|total|strength|size) of (employees|people|staff|emp|workforce)\b",
    r"total (employees|people|staff|strength|workforce|headcount)\b",
    r"(on|in) (the )?payroll\b",
    r"(company|team|org|organi[sz]ation) (size|strength)\b",
    r"strength\b",
    r"how big is (the |our )?(company|team|org)",
)

_POLICY = (
    r"polic",
    r"rules?\b",
    r"entitle",
    r"handbook",
    r"holiday list",
    r"holidays\b",
    r"how many (casual |sick |earned |privilege )?leaves? (do we get|we get|can i take|"
    r"can we take|are allowed|are we allowed|is allowed|allowed|per year|in a year|"
    r"are there in a year|does the company give|do employees get)",
    r"(leaves?|holidays) (allowed|per year|in a year)",
    r"(can|may) i (carry|encash|take)",
    r"(notice period|probation period) (is|policy|duration|length|for)",
    r"what is the (notice|probation) period",
    r"dress code",
    r"working hours",
    r"office timings?",
    r"code of conduct",
    r"(maternity|paternity|bereavement) leave",
    r"(is|are|will) [a-z ]{1,30}\ba (public |company )?holiday\b",
    r"next holiday\b",
    r"(diwali|holi|eid|christmas|pongal|onam|dussehra|navratri|ganesh|independence day|"
    r"republic day|new year)\b",
)

_WHO_IS_OUT = (
    r"on leave",
    r"who is out",
    r"who all are out",
    r"(people|employees|staff|anyone|anybody|who) (is |are )?(out|off|away)\b",
    r"out of (the )?office",
    r"on holiday",
    r"on vacation",
    r"off today",
    r"(taking|took|taken|applied) (a )?leave",
    r"leave (today|tomorrow|this|next|on|from)",
    r"away\b",
    r"vacation",
    r"work(ing)? from home",
    r"\bwho is not (coming|in)",
)

_ATTENDANCE = (
    r"attendance",
    r"present\b",
    r"absent",
    r"late\b",
    r"late (comers|coming|arrivals?)",
    r"came late",
    r"(punch|punched|punches|punching)\b",
    r"missed punch",
    r"(in|at) (the )?office (today|now|yesterday)",
    r"(who|how many) (came|reached|checked in|clocked in)",
    r"check-?ins?\b",
    r"(did not|didn't|not) (come|came|turn up|show up|report)\b",
)


#: Words that ask for a trend or a picture rather than one month's figures.
_TREND = (
    r"trends?\b",
    r"over (the )?time",
    r"month[ -]?(by|on|over|to)[ -]?month",
    # "Monthly salary" is pay per month, not a trend: only these phrasings are.
    r"\bby (month|quarter|week)\b",
    r"month ?wise",
    r"quarter ?wise",
    r"year ?wise",
    r"graphs?\b",
    r"charts?\b",
    r"\bplot",
    r"growth\b",
    r"\bmom\b",
    r"visuali[sz]",
    r"\bpie\b",
    r"\bdonut\b",
    r"\bdoughnut\b",
    r"how (has|have|did) [a-z ]{0,30}(changed|moved|grown|gone up|gone down)",
    r"compare[ds]? [a-z ]{0,20}months",
)
_COUNTS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "eighteen": 18,
}
_PAYROLL_COST = (
    r"payroll\b",
    r"salar(y|ies)\b",
    r"\bctc\b",
    r"cost to company",
    r"wages?\b",
    r"pay ?outs?\b",
    r"employee cost",
    r"people cost",
    r"staff cost",
    r"manpower cost",
)
_PNL = (
    r"p ?& ?l\b",
    r"\bpnl\b",
    r"profit",
    r"\bmargins?\b",
    r"billed? (vs|versus|against|and) ",
    r"billing (vs|versus|against|and) ",
    r"account (cost|costs|profitability)",
)


def _account_of(words: str) -> str:
    """The client account a P&L question names ("P&L of Kestrel"), or empty for all."""
    account = re.search(
        r"\b(?:of|for|on) (?:the )?(?!each\b|every\b|all\b|our\b|my\b)"
        r"([a-z0-9][a-z0-9 .&-]{1,40}?)(?: account| client)?"
        r"(?: (?:this|last|previous|in) [a-z0-9 ]+)?$",
        words,
    )
    query = account.group(1).strip() if account else ""
    if query in ("account", "accounts", "client", "clients", "company", "the company"):
        return ""
    return query


#: The prospective P&L booked from won orders: ahead, not what was billed.
_FORECAST = (
    r"(prospective|projected|forecast(ed)?|expected|future|upcoming)\b.{0,20}"
    r"(p ?& ?l\b|pnl\b|profit|margins?\b|billing|revenue)",
    r"(p ?& ?l\b|pnl\b|profit|margins?\b|billing|revenue)\b.{0,20}"
    r"\b(forecasts?|projections?|outlook)\b",
    r"(booked|won) orders?\b",
    r"order wins?\b",
)

# ── Saleinx and ProcureFlow ──────────────────────────────────────────────────

#: Saleinx, the sales CRM, by name.
_SALES_NAME = (r"sale ?inx", r"crm\b")
#: Deals and leads in the sales sense: "team leads" are people, "deal with" a verb.
_SALES_WORDS = (
    *_SALES_NAME,
    r"deals?\b(?! with)",
    r"(?<!team )(?<!tech )(?<!who )leads\b(?! (the|a|an|our|my)\b)",
    r"(sales|new|hot|warm|cold) leads?\b",
    r"sales (pipeline|funnel)\b",
)
#: A deal's stage, read from the words; the first that matches wins.
_DEAL_STAGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "awaiting_approval",
        (
            r"(waiting|awaiting|pending|needs?|needing)\b.{0,30}\b(approv|sign-?off|decision)",
            r"to (be )?approved?\b",
            r"approvals?\b",
            r"unapproved\b",
        ),
    ),
    ("negotiation", (r"negotiat",)),
    ("proposal", (r"proposals?\b",)),
    ("qualified", (r"qualified\b",)),
    ("hold", (r"on hold\b", r"parked\b", r"held\b")),
    ("won", (r"won\b", r"wins?\b")),
    ("lost", (r"lost\b",)),
)
_SALES_DROP = (
    "deal",
    "deals",
    "lead",
    "leads",
    "saleinx",
    "sales",
    "sale",
    "inx",
    "crm",
    "pipeline",
    "funnel",
    "stage",
    "stages",
    "won",
    "win",
    "wins",
    "lost",
    "held",
    "parked",
    "negotiation",
    "negotiating",
    "proposal",
    "proposals",
    "qualified",
    "approval",
    "approvals",
    "approve",
    "approved",
    "unapproved",
    "waiting",
    "awaiting",
    "pending",
    "needs",
    "needing",
    "sign-off",
    "decision",
    "manager",
    "manager's",
    "open",
    "active",
    "new",
    "hot",
    "warm",
    "cold",
    "value",
    "worth",
    "biggest",
    "top",
    "latest",
    "recent",
    "by",
    "so",
    "far",
    "year",
    "quarter",
    "closed",
    "closing",
    "close",
    "date",
    "next",
    "step",
    "owner",
    *_MONTHS,
)

#: ProcureFlow, the procure-to-pay system, by name.
_PROCURE_NAME = (r"procure ?flow", r"procurement\b", r"p2p\b", r"procure.to.pay")
#: Which document a question is about. Bare "requisitions" are AskMira's hiring
#: requisitions unless ProcureFlow is named.
_PROCURE_DOCS: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("purchase_orders", (r"purchase orders?\b",), False),
    ("invoices", (r"invoices?\b", r"vendor bills?\b"), False),
    ("requisitions", (r"purchase (requisitions?|requests?)\b",), False),
    ("requisitions", (r"requisitions?\b", r"prs?\b", r"indents?\b"), True),
)
#: Statuses a person names, as ProcureFlow's, and which documents have each.
_DOC_STATUS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "AWAITING_APPROVAL",
        (r"(awaiting|waiting|pending)\b", r"unapproved\b", r"for approval\b"),
        ("requisitions", "purchase_orders", "invoices"),
    ),
    ("APPROVED", (r"approved\b",), ("requisitions", "purchase_orders", "invoices")),
    ("REJECTED", (r"rejected\b", r"declined\b"), ("requisitions", "purchase_orders", "invoices")),
    ("PAID", (r"paid\b", r"settled\b"), ("invoices",)),
    ("DRAFT", (r"drafts?\b",), ("requisitions", "purchase_orders")),
    ("ON_HOLD", (r"on hold\b", r"held\b"), ("purchase_orders", "invoices")),
    ("ISSUED", (r"issued\b", r"sent to (the )?vendors?\b"), ("purchase_orders",)),
    ("RECEIVED", (r"received\b", r"delivered\b"), ("purchase_orders", "invoices")),
    ("SCHEDULED", (r"scheduled\b",), ("invoices",)),
    ("CANCELLED", (r"cancel?led\b",), ("requisitions", "purchase_orders")),
    ("CLOSED", (r"closed\b",), ("requisitions", "purchase_orders")),
)
#: The asker's own decisions there, as against documents in a status.
_PROCURE_APPROVALS = (
    r"(my|me)\b.{0,20}\bapprov",
    r"approv\w*\b.{0,20}\b(my|me)\b",
    r"to approve\b",
    # Plural: "invoices awaiting approval" are documents in that status.
    r"approvals\b",
    r"inbox\b",
    r"waiting (for|on) me\b",
    r"payment runs?\b",
)
_BUDGET_USE = (
    r"cost cent(re|er)s?\b",
    r"(spent|spend|committed|available|utili[sz]|consum|remaining|left|used|usage|"
    r"overrun|breach|exceed|over budget|under budget)",
)
_NOT_PROCUREMENT_BUDGET = (
    "salar",
    "payroll",
    r"ctc\b",
    "hiring",
    "recruit",
    "training",
    "headcount",
    "expense",
)
_PROCURE_DROP = (
    "procureflow",
    "procure",
    "flow",
    "procurement",
    "p2p",
    "purchase",
    "order",
    "orders",
    "requisition",
    "requisitions",
    "request",
    "requests",
    "pr",
    "prs",
    "indent",
    "indents",
    "invoice",
    "invoices",
    "vendor",
    "vendors",
    "bill",
    "bills",
    "pending",
    "awaiting",
    "waiting",
    "approval",
    "approved",
    "unapproved",
    "rejected",
    "declined",
    "paid",
    "settled",
    "draft",
    "drafts",
    "held",
    "issued",
    "sent",
    "received",
    "delivered",
    "scheduled",
    "cancelled",
    "canceled",
    "closed",
    "open",
    "everyone's",
    "everybody's",
    "everyone",
    "org",
    "total",
    "value",
    "amount",
    "latest",
    "recent",
    "new",
    "created",
    "submitted",
    "by",
    "so",
    "far",
    *_MONTHS,
)


def _sales(words: str, offered: set[str]) -> DirectPlan | None:
    """Deals, leads and the pipeline in Saleinx; None when the words are not about them."""
    if not _has(words, *_SALES_WORDS):
        return None
    pipeline = _has(words, r"pipeline\b", r"funnel\b", r"by stages?\b", r"stage.?wise\b")
    if pipeline and "sales_pipeline" in offered:
        return DirectPlan(reply=f"Here is your pipeline in {_SALES}.", tool="sales_pipeline")
    if "sales_deals" not in offered:
        return _unavailable(_SALES)
    stage = next((key for key, pats in _DEAL_STAGES if _has(words, *pats)), "none")
    if stage == "none" and _has(words, r"leads?\b") and not _has(words, r"deals?\b"):
        stage = "lead"
    query = _search_text(words, _SALES_DROP)
    if stage == "awaiting_approval":
        reply = f"Here are the Won deals waiting for a manager's approval in {_SALES}."
    elif stage == "lead":
        reply = f"Here are the leads in {_SALES}."
    elif stage != "none":
        reply = f"Here are the deals in {stage} in {_SALES}."
    else:
        reply = f"Here are the deals in {_SALES}."
    return DirectPlan(reply=reply, tool="sales_deals", inputs={"query": query, "deal_stage": stage})


def _procurement(words: str, offered: set[str]) -> DirectPlan | None:
    """Approvals, documents and budgets in ProcureFlow; None when the words are not about them."""
    named = _has(words, *_PROCURE_NAME)
    document = next(
        (
            key
            for key, pats, needs_name in _PROCURE_DOCS
            if (named or not needs_name) and _has(words, *pats)
        ),
        None,
    )
    if document == "invoices" and not named and _has(words, "expense", "claim", "reimburs"):
        document = None  # an expense claim's invoice is ExpenseGenie's
    budget = (
        _has(words, r"budgets?\b")
        and not _has(words, *_NOT_PROCUREMENT_BUDGET)
        and (named or _has(words, *_BUDGET_USE))
    )
    approvals = _has(words, *_PROCURE_APPROVALS)
    if not (named or document or budget or _has(words, r"payment runs?\b")):
        return None

    def chosen(key: str, reply: str, **inputs: Any) -> DirectPlan | None:
        if key in offered:
            return DirectPlan(reply=reply, tool=key, inputs=inputs)
        return _unavailable(_PROCURE) if named else None

    if budget:
        return chosen("procurement_budgets", f"Here are this period's budgets in {_PROCURE}.")
    if approvals:
        return chosen(
            "procurement_approvals", f"Here is what waits on your approval in {_PROCURE}."
        )
    if document:
        status = next(
            (
                value
                for value, pats, kinds in _DOC_STATUS
                if document in kinds and _has(words, *pats)
            ),
            "",
        )
        scope = (
            "company"
            if document == "requisitions"
            and _has(words, r"(all|every(one|body)'?s?|company|whole|org)\b")
            else "none"
        )
        label = {"purchase_orders": "purchase orders"}.get(document, document)
        return chosen(
            "procurement_documents",
            f"Here are the {label} in {_PROCURE}.",
            document=document,
            doc_status=status,
            query=_search_text(words, _PROCURE_DROP),
            scope=scope,
        )
    return chosen("procurement_overview", f"Here is {_PROCURE} at a glance.")


#: Dates a follow-up's pie chart drops from the grouping.
_DATE_FIELDS = frozenset(
    {
        "pay_month",
        "leave_start",
        "leave_end",
        "attendance_date",
        "incurred_on",
        "raised_on",
        "work_date",
        "last_working_day",
        "given_on",
        "entry_month",
        "date_of_joining",
        "date_of_exit",
        "overtime_date",
        "corrected_date",
        "bill_date",
    }
)


def months_back(words: str) -> int:
    """N from "the last N months" (or "past six months"), else 0."""
    found = re.search(
        r"\b(last|past|previous|recent|trailing|over the last|over the past)\s+"
        r"(\d{1,2}|" + "|".join(_COUNTS) + r")\s+months?\b",
        words,
    )
    if not found:
        return 0
    raw = found.group(2)
    count = int(raw) if raw.isdigit() else _COUNTS[raw]
    return max(1, min(count, 36))


def trending(words: str) -> bool:
    return _has(words, *_TREND) or months_back(words) > 1


def chart_kind(words: str) -> str:
    for pattern, kind in (
        (r"\bpie\b", "pie"),
        (r"\bdo(ugh)?nut\b", "donut"),
        (r"stacked", "stacked_bar"),
        (r"horizontal", "horizontal_bar"),
        (r"\bbars?\b|column chart", "bar"),
        (r"\barea\b", "area"),
        (r"\bline\b", "line"),
        (r"\btable\b|\blist\b", "table"),
    ):
        if re.search(pattern, words):
            return kind
    return "auto"


def _split_by(words: str) -> str:
    """The group a trend is split by: department, location, entity or none."""
    if _has(words, r"(by|per|each|every|across|wise) department", r"department ?wise"):
        return "department"
    if _has(words, r"(by|per|each|every|across) (location|office|city|branch)", r"location ?wise"):
        return "location"
    if _has(words, r"(by|per|each|every|across) (entity|company|companies|legal entit)"):
        return "entity"
    return "none"


def _bucket(words: str) -> str:
    if _has(words, r"quarter"):
        return "quarter"
    if _has(words, r"year ?wise", r"\byearly\b", r"by year", r"each year", r"per year"):
        return "financial_year"
    if _has(words, r"week"):
        return "week"
    return "month"


def _period(words: str, when: dict[str, str]) -> dict[str, Any]:
    count = months_back(words)
    if count:
        return {"months": count, "from": "", "to": ""}
    if when.get("from"):
        return {"months": 0, **when}
    return {"months": 0, "from": "", "to": ""}


def _data(
    reply: str,
    dataset: str,
    measures: list[str],
    words: str,
    when: dict[str, str],
    *,
    group_by: list[str] | None = None,
    filters: list[dict[str, str]] | None = None,
) -> DirectPlan:
    period = _period(words, when)
    if not period["months"] and not period["from"]:
        period["months"] = 12
    chart = chart_kind(words)
    bucket = _bucket(words)
    if chart in ("pie", "donut") and group_by:
        # Shares of a whole: one slice per group, no time axis.
        bucket = "none"
    return DirectPlan(
        reply=reply,
        tool="query_data",
        inputs={
            "dataset": dataset,
            "measures": measures,
            "group_by": group_by or [],
            "bucket": bucket,
            "filters": filters or [],
            "chart": chart,
            **period,
        },
    )


def _trend(
    words: str, offered: set[str], *, today: date, when: dict[str, str], hr: bool
) -> DirectPlan | None:
    """A trend or chart question about a subject this planner knows, or None."""
    period = _period(words, when)
    count = period["months"]
    span = f"over the last {count} months" if count else "month by month"
    split = _split_by(words)

    if _has(words, *_PNL):
        return DirectPlan(
            reply="Here is the P&L of each client account.",
            tool="account_pnl",
            inputs={"query": "", "from": when.get("from", ""), "to": when.get("to", "")},
        )
    if _has(words, *_PAYROLL_COST) and not _has(words, r"\bmy\b"):
        if chart_kind(words) in ("pie", "donut") and split != "none" and hr:
            # Shares of the whole: one slice per department, not a line per month.
            return DirectPlan(
                reply=f"Here is payroll cost by {split}.",
                tool="query_data",
                inputs={
                    "dataset": "payslip",
                    "measures": ["employer_cost:sum"],
                    "group_by": [{"entity": "paid_by_company"}.get(split, split)],
                    "bucket": "none",
                    "chart": chart_kind(words),
                    "filters": [],
                    **_period(words, when),
                },
            )
        if "payroll_trend" in offered or "query_data" not in offered or not hr:
            return DirectPlan(
                reply=f"Here is payroll cost {span}.",
                tool="payroll_trend",
                inputs={**period, "group": split},
            )
        return _data(
            f"Here is payroll cost {span}.",
            "payslip",
            ["employer_cost:sum"],
            words,
            when,
            group_by=[] if split == "none" else [{"entity": "paid_by_company"}.get(split, split)],
        )
    if _has(words, *_HEADCOUNT, *_JOINERS, *_LEAVERS, r"head ?count", r"strength", r"workforce"):
        inputs: dict[str, Any] = {"from": period["from"], "to": period["to"], "months": count}
        return DirectPlan(reply=f"Here is headcount {span}.", tool="headcount_trend", inputs=inputs)
    if _has(words, r"leaves?\b", r"time off", r"\bpto\b", r"absen"):
        by_type = _has(words, r"(by|per|each|wise) (leave )?type", r"type ?wise", r"which leave")
        return _data(
            f"Here are approved leave days {span}.",
            "leave_request",
            ["total_days:sum"],
            words,
            when,
            group_by=["leave_type"]
            if by_type
            else ([split] if split in ("department", "location") else []),
            filters=[{"field": "leave_status", "op": "equals", "value": "approved"}],
        )
    if _has(words, r"expenses?\b", r"claims?\b", r"reimburse"):
        if "expense_summary" in offered:
            # The workspace's claims are in ExpenseGenie: month by month and who claimed.
            first = date(today.year, today.month, 1)
            for _ in range((count or 12) - 1):
                first = (first - timedelta(days=1)).replace(day=1)
            return DirectPlan(
                reply=f"Here are expense claims {span}.",
                tool="expense_summary",
                inputs={
                    "scope": "company" if hr else "mine",
                    "status": "none",
                    "from": when.get("from") or first.isoformat(),
                    "to": when.get("to") or today.isoformat(),
                },
            )
        return _data(
            f"Here are expense claims {span}.", "expense_claim", ["claim_amount:sum"], words, when
        )
    if _has(words, r"tickets?\b", r"helpdesk", r"queries", r"grievance"):
        return _data(
            f"Here are HR helpdesk tickets {span}.",
            "helpdesk_ticket",
            ["ticket_count:count"],
            words,
            when,
        )
    if _has(words, r"late", r"attendance", r"present", r"absent"):
        return _data(
            f"Here is attendance {span}.",
            "attendance_day",
            ["day_count:count"],
            words,
            when,
            group_by=["attendance_status"],
        )
    if _has(words, r"overtime", r"\bot\b"):
        return _data(
            f"Here is overtime {span}.", "overtime_entry", ["approved_minutes:sum"], words, when
        )
    if _has(words, r"timesheet", r"hours"):
        return _data(
            f"Here are timesheet hours {span}.", "timesheet_entry", ["hours:sum"], words, when
        )
    if _has(words, r"shout ?outs?", r"recognition", r"kudos"):
        return _data(
            f"Here is recognition {span}.", "shoutout", ["shoutout_count:count"], words, when
        )
    return None


#: What a follow-up ("for the last 6 months", "as a pie chart") changes, by the
#: tool that answered before.
_FOLLOW_TREND = {
    "payroll_summary": "payroll_trend",
    "payroll_trend": "payroll_trend",
    "salary_totals": "payroll_trend",
    "headcount": "headcount_trend",
    "people_changes": "headcount_trend",
    "headcount_trend": "headcount_trend",
    "account_pnl": "account_pnl",
    "query_data": "query_data",
}


def _follow_up(
    text: str,
    tools: list[Tool],
    *,
    today: date,
    previous: tuple[str, dict[str, Any]],
) -> DirectPlan | None:
    """A message that only changes the period, the chart or the split of the last answer."""
    words = _asked(normalise(text))
    start, end = dates(words, today)
    when = {"from": start, "to": end}
    count = months_back(words)
    chart = chart_kind(words)
    split = _split_by(words)
    if not (trending(words) or start or count or chart != "auto" or split != "none"):
        return None
    offered = {tool.key for tool in tools}
    tool, args = previous
    target = _FOLLOW_TREND.get(tool)
    if target is None or target not in offered:
        return None
    period = _period(words, when)
    span = f"over the last {count} months" if count else (_span(when, "") or "month by month")
    if target == "payroll_trend":
        group = split if split != "none" else str(args.get("group") or "none")
        return DirectPlan(
            reply=f"Here is payroll cost {span}.",
            tool=target,
            inputs={**period, "group": group},
        )
    if target == "headcount_trend":
        return DirectPlan(reply=f"Here is headcount {span}.", tool=target, inputs=period)
    if target == "account_pnl":
        return DirectPlan(
            reply="Here is the P&L again.",
            tool=target,
            inputs={"query": str(args.get("query") or ""), "from": start, "to": end},
        )
    inputs = {k: v for k, v in args.items() if k not in ("months", "from", "to")}
    if count or start:
        inputs.update(period)
    else:
        inputs.update({k: args[k] for k in ("months", "from", "to") if k in args})
    if chart != "auto":
        inputs["chart"] = chart
    if split in ("department", "location"):
        inputs["group_by"] = [split]
    if chart in ("pie", "donut"):
        # A pie shows shares of a whole: drop the time axis, keep what it was split by.
        time_fields = {inputs.get("time_field"), *_DATE_FIELDS}
        dims = [g for g in inputs.get("group_by") or [] if g not in time_fields]
        if dims:
            inputs["bucket"] = "none"
            inputs["group_by"] = dims
        else:
            inputs["chart"] = "bar"
    elif trending(words) and not inputs.get("bucket"):
        inputs["bucket"] = _bucket(words)
    return DirectPlan(reply="Here it is again, changed as you asked.", tool=target, inputs=inputs)


def plan(
    text: str,
    tools: list[Tool],
    *,
    today: date,
    name: str = "Mira",
    hr: bool = False,
    previous: tuple[str, dict[str, Any]] | None = None,
) -> DirectPlan:
    """Match the person's message to one of `tools`, or reply with what can be asked.

    `previous` is the tool (and its inputs) that answered the last message, so a
    follow-up such as "for the last 6 months" or "as a pie chart" changes that answer.
    """
    result = _plan(text, tools, today=today, name=name, hr=hr)
    if result.tool or previous is None:
        return result
    return _follow_up(text, tools, today=today, previous=previous) or result


def _plan(
    text: str, tools: list[Tool], *, today: date, name: str = "Mira", hr: bool = False
) -> DirectPlan:
    """Match the person's message to one of `tools`, or reply with what can be asked.

    `hr` is whether the asker is an admin or sub-admin. For anybody else a question
    that needs another person's record or company-wide figures is refused.
    """
    offered = {tool.key for tool in tools}
    words = normalise(text)
    start, end = dates(words, today)
    when = {"from": start, "to": end}

    def pick(key: str, reply: str, **inputs: Any) -> DirectPlan | None:
        if key in offered:
            return DirectPlan(reply=reply, tool=key, inputs=inputs)
        if not hr and key in _HR_TOOLS:
            return _own_only()
        return None

    if not words or _GREETING.match(words):
        return _help(tools, name, prefix=f"Hi! I'm {name}.")
    words = _asked(words)
    if _GREETING.match(words):
        return _help(tools, name, prefix=f"Hi! I'm {name}.")

    self_words = _has(words, r"my\b", r"mine\b", r"i\b", r"me\b", r"myself\b", r"i'm\b")
    # "My manager's salary" is about somebody else; "who is my manager" is not.
    relation = re.search(
        r"\bmy (manager|boss|team ?lead|lead|colleagues?|team|reportees?|subordinates?|"
        r"direct reports?|team members?|friends?|ceo|hr|juniors?|seniors?)('s)?\b",
        words,
    )
    chosen: DirectPlan | None = None

    # Whether the products are connected at all.
    if _has(words, "connect", "linked", "integrat", "set ?up", "configured") and _has(
        words,
        "an?y? ?time",
        "hire",
        "expense",
        "genie",
        "help ?desk",
        *_SALES_NAME,
        *_PROCURE_NAME,
        "products?\\b",
        "tools?\\b",
        "portals?",
        "apps?\\b",
    ):
        chosen = pick(
            "product_connections",
            "Here is how each linked product is connected for you.",
        )
        if chosen:
            return chosen

    # Raising a ticket happens on the Helpdesk page, where the form is.
    if re.match(
        r"^(please |can you |could you |i want to |i need to |i'd like to |help me |"
        r"how (do|can|to) i |how to |i wanna |want to |need to |to )?"
        r"(raise|create|open|log|file|submit|make|register|put) (a |an |one )?(new )?"
        r"(it |hr |helpdesk |support )?"
        # Singular: "open tickets" asks to see them.
        r"(ticket|request|complaint|issue|query|grievance)\b",
        words,
    ):
        if _has(words, r"hr\b", "grievance", "query") and "hr_tickets" in offered:
            return DirectPlan(
                reply=(
                    "Raise it under Helpdesk → My tickets → Raise a ticket. HR replies on the "
                    "ticket and you are told when they do."
                )
            )
        if "helpdesk_tickets" in offered:
            return DirectPlan(
                reply=(
                    "Raise it under Helpdesk → My tickets → Raise a ticket. It goes straight to "
                    f"{_HELPDESK}, and you can follow the replies there or here."
                )
            )
        if "hr_tickets" in offered:
            return DirectPlan(
                reply=(
                    "Raise it under Helpdesk → My tickets → Raise a ticket. HR replies on the "
                    "ticket and you are told when they do."
                )
            )
        return _unavailable(_HELPDESK)

    # Opening a linked product — unless the words ask for its data.
    opening = re.match(r"^(please |can you )?(open|launch|go to|take me to)\b", words)
    if opening and not _has(
        words,
        r"jobs?\b",
        "candidate",
        "applicant",
        r"interviews\b",
        r"claims?\b",
        r"expenses\b",
        r"tickets\b",
        r"assets\b",
        r"positions\b",
        r"deals?\b",
        r"leads\b",
        "pipeline",
        r"requisitions\b",
        r"orders\b",
        r"invoices\b",
        r"budgets?\b",
        r"approvals\b",
    ):
        if _has(words, "anytime", "hire", "interview tool", "hiring tool", "recruit"):
            chosen = pick("open_product", f"Opening {_HIRE}.", product="interview")
        elif _has(words, "expense"):
            chosen = pick("open_product", f"Opening {_EXPENSE}.", product="expense")
        elif _has(words, "help ?desk", "cgb", "it support", "it portal", "ticket"):
            chosen = pick("open_product", f"Opening {_HELPDESK}.", product="helpdesk")
        elif _has(words, *_SALES_NAME):
            chosen = pick("open_product", f"Opening {_SALES}.", product="saleinx")
        elif _has(words, *_PROCURE_NAME):
            chosen = pick("open_product", f"Opening {_PROCURE}.", product="procurement")
        if chosen:
            return chosen

    # The prospective P&L booked from won orders, before the P&L that was billed.
    if _has(words, *_FORECAST):
        query = _account_of(words)
        chosen = pick(
            "account_forecast",
            f"Here is the prospective P&L of {query}."
            if query
            else "Here is the prospective P&L booked from won orders.",
            query=query,
        )
        if chosen:
            return chosen

    # Deals in Saleinx; approvals, documents and budgets in ProcureFlow.
    chosen = _sales(words, offered) or _procurement(words, offered)
    if chosen:
        return chosen

    # What is waiting on my decision, across AskMira.
    approval = _has(words, *_APPROVAL) and not _has(
        words,
        r"my (own )?(leave|expense|claim)s? (request|application|claim)s? status",
        # "My pending claims" are the asker's own, waiting on somebody else.
        r"my (own )?(pending|open|submitted) (leaves?|claims?|expenses?|reimbursements?|"
        r"requests?|applications?|tickets?|bills?)\b",
    )
    kind = "none"
    if approval:
        for key, patterns in _APPROVAL_KIND:
            if _has(words, *patterns):
                kind = key
                break
        # "Reimbursement pending for me" is the asker's own money, not a decision.
        if kind == "expense" and not _has(
            words, "approv", r"my action", r"sign-?off", r"decide", r"waiting (for|on) me"
        ):
            approval = False
    if approval:
        if kind == "expense" and "expense_claims" in offered:
            status = _status(words, product_tools.CLAIM_STATUSES)
            return DirectPlan(
                reply=f"Here are the expense claims waiting for your approval in {_EXPENSE}.",
                tool="expense_claims",
                inputs={
                    "scope": "approvals",
                    "status": "pending" if status == "none" else status,
                    **when,
                    "query": "",
                },
            )
        chosen = pick(
            "my_approvals",
            "Here is what is waiting on your decision."
            if kind == "none"
            else f"Here are the {kind} items waiting on your decision.",
            kind=kind,
        )
        if chosen:
            return chosen
        return DirectPlan(
            reply="Approvals are not switched on for Ask Mira here. They are all under Approvals."
        )

    # Policy questions need a model of their own.
    if _has(words, *_POLICY) and not _has(
        words,
        r"leave balance",
        r"(leaves?|holidays?) (do i have |i have )?(left|remaining)\b",
        r"(leaves?|holidays?) [a-z ]{0,15}\bleft\b",
    ):
        return DirectPlan(
            reply=(
                "Questions about policies are answered by AI, which is not switched on here. "
                "Ask HR and the handbook have the company's policies."
            )
        )

    # The asker's own PF, ESI, TDS or deductions are on their payslip.
    statutory = r"(pf|epf|esi|esic|tds|professional tax|pt|deductions?|tax deducted)\b"
    if self_words and not relation and _has(words, statutory) and not _has(words, r"we\b"):
        chosen = pick("my_record", "Here is your latest payslip.", lookup="latest_payslip")
        if chosen:
            return chosen

    # A client account's P&L: billed against the cost mapped to it.
    if _has(words, *_PNL):
        query = _account_of(words)
        chosen = pick(
            "account_pnl",
            f"Here is the P&L of {query}." if query else "Here is the P&L of each client account.",
            query=query,
            **when,
        )
        if chosen:
            return chosen

    # Trends and charts: "payroll cost for the last 6 months", "leave by month as a bar chart".
    if trending(words):
        chosen = _trend(words, offered, today=today, when=when, hr=hr)
        if chosen is not None:
            if chosen.tool and chosen.tool not in offered:
                return pick(chosen.tool, chosen.reply, **chosen.inputs) or _help(
                    tools, name, prefix="That is not switched on for Ask Mira here."
                )
            return chosen

    # Payroll figures: a month's runs, and the salary bill.
    counting = _has(words, *_HEADCOUNT)
    paid_out = _has(
        words, r"paid\b", "disburs", "credited", "transferred", "payout", r"deduct", "processed"
    )
    if (
        _has(words, *_SALARY_TOTALS)
        and not (self_words and not _has(words, r"everyone", r"all\b", r"company"))
        and not paid_out
        and not when["from"]
    ):
        chosen = pick("salary_totals", "Here is what the salaries in force today add up to.")
        if chosen:
            return chosen
        if hr:
            return DirectPlan(reply="Payroll figures are not switched on for Ask Mira here.")
    payroll_words = _has(words, *_PAYROLL) or (
        _has(words, *_SALARY_TOTALS) and (paid_out or bool(when["from"]))
    )
    on_payroll = (
        counting
        and _has(words, r"(on|in) (the )?payroll")
        and not _has(words, r"cost", r"amount", r"total pay", r"paid")
    )
    if payroll_words and not on_payroll:
        chosen = pick(
            "payroll_summary",
            f"Here is the payroll {_span(when, 'for the latest month run')}.",
            **when,
        )
        if chosen:
            return chosen
        if hr:
            return DirectPlan(reply="Payroll figures are not switched on for Ask Mira here.")

    # HR helpdesk tickets (AskMira's own helpdesk).
    hr_ticket = _has(words, *_HR_TICKETS)
    it_ticket = _has(words, *_IT_TICKETS) and not hr_ticket
    if hr_ticket or (it_ticket and "helpdesk_tickets" not in offered and "hr_tickets" in offered):
        scope = "mine"
        if _has(words, "assigned to me", "my queue", "on my plate", "i am working", "i'm working"):
            scope = "assigned"
        elif _has(words, "unassigned", "not assigned", "nobody", "no one"):
            scope = "queue"
        elif _has(
            words,
            "company",
            "everyone",
            "everybody",
            r"all (the )?(hr )?(tickets|queries|grievances|requests)",
            "whole",
            "organi[sz]ation",
            r"org\b",
            r"queue\b",
            r"(employees|people|staff)\b",
            r"overdue",
            r"(pending|open) (with|in|at) hr",
        ) or (hr and not self_words):
            scope = "company"
        chosen = pick(
            "hr_tickets",
            "Here are the HR helpdesk tickets.",
            scope=scope,
            status=_ticket_status(words),
            query=_search_text(
                words,
                (
                    "hr",
                    "open",
                    "closed",
                    "new",
                    "pending",
                    "resolved",
                    "done",
                    "fixed",
                    "grievance",
                    "grievances",
                    "queries",
                    "query",
                    "requests",
                    "request",
                    "overdue",
                    "unresolved",
                    "active",
                    "still",
                    "raised",
                    "about",
                    "regarding",
                ),
            )
            if scope != "mine"
            else "",
        )
        if chosen:
            return chosen
        if hr_ticket:
            return DirectPlan(reply="The HR helpdesk is not switched on for Ask Mira here.")

    # Expenses (ExpenseGenie).
    if _has(
        words,
        "expense",
        "claim",
        "reimburs",
        r"spend\b",
        r"spent\b",
        r"spending\b",
        r"bills?\b",
        r"travel (claims?|bills?|expenses?)",
        r"(food|fuel|cab|taxi|hotel|mileage|conveyance) (bills?|claims?|expenses?)",
    ):
        scope = "mine"
        if _has(
            words,
            "to approve",
            "my approval",
            "for approval",
            "awaiting",
            "waiting",
            "approvals",
            "approve\\b",
            r"team\b",
            r"my team",
            r"reportees",
            r"direct reports",
        ):
            scope = "approvals"
        elif _has(
            words,
            "company",
            "everyone",
            "everybody",
            "all employees",
            "whole",
            "organi[sz]ation",
            "org\\b",
            "all claims",
            "all expense",
            r"all the (claims|expenses)",
            r"overall",
            r"department",
            r"(employees|staff|people)\b",
        ):
            scope = "company"
        status = _status(words, product_tools.CLAIM_STATUSES)
        if scope == "approvals" and status == "none" and _has(words, "pending", "waiting"):
            status = "pending"
        if _has(
            words,
            "total",
            "how much",
            "summary",
            r"sum\b",
            "overview",
            r"spent\b",
            r"spend\b",
            r"spending\b",
            "unpaid",
            "breakdown",
            "by category",
            "category wise",
            "categorywise",
            r"amount\b",
        ):
            chosen = pick(
                "expense_summary",
                f"Here are the expense totals from {_EXPENSE}.",
                scope=scope,
                status=status,
                **when,
            )
        else:
            chosen = pick(
                "expense_claims",
                f"Here are the expense claims from {_EXPENSE}.",
                scope=scope,
                status=status,
                **when,
                query=_search_text(
                    words,
                    (
                        "pending",
                        "approved",
                        "rejected",
                        "draft",
                        "approval",
                        "approvals",
                        "approve",
                        "waiting",
                        "total",
                        "submitted",
                        "awaiting",
                        "status",
                        "my",
                        "reportees",
                        "overall",
                        "unpaid",
                        "paid",
                        "bills",
                        "bill",
                        "reimbursed",
                        "submit",
                    ),
                ),
            )
        if chosen:
            return chosen
        return _unavailable(_EXPENSE)

    # IT tickets and assets (CGB Helpdesk).
    if it_ticket:
        if _has(
            words,
            "overview",
            "summary",
            "at a glance",
            "how many",
            r"count\b",
            "stats",
            "dashboard",
        ):
            chosen = pick(
                "helpdesk_overview", f"Here is the helpdesk at a glance from {_HELPDESK}."
            )
        else:
            scope = "mine"
            if _has(
                words, "assigned to me", "my queue", "on my plate", "i am working", "i'm working"
            ):
                scope = "assigned"
            elif _has(words, "unassigned", "queue", "not assigned", "nobody", "no one"):
                scope = "queue"
            elif _has(
                words,
                "company",
                "everyone",
                "everybody",
                "all tickets",
                "all the tickets",
                "whole",
                "organi[sz]ation",
                "org\\b",
                r"(employees|staff|people)\b",
            ):
                scope = "company"
            chosen = pick(
                "helpdesk_tickets",
                f"Here are the tickets from {_HELPDESK}.",
                scope=scope,
                status=_ticket_status(words),
                **when,
                query=_search_text(
                    words,
                    (
                        "open",
                        "closed",
                        "new",
                        "pending",
                        "resolved",
                        "done",
                        "fixed",
                        "raised",
                        "unresolved",
                        "active",
                        "about",
                        "regarding",
                        "it",
                        "solved",
                        "still",
                        "issue",
                        "issues",
                        "problem",
                        "problems",
                        "requests",
                        "request",
                        "complaints",
                        "completed",
                        "not",
                        "yet",
                    ),
                    keep=("email", "mail", "access", "password"),
                ),
            )
        if chosen:
            return chosen
        return _unavailable(_HELPDESK)
    if _has(words, *_ASSETS):
        company = _has(
            words,
            "register",
            "company",
            "all assets",
            "everyone",
            "whole",
            "inventory",
            "spare",
            r"in stock",
            r"unused",
            r"available",
            r"retired",
            r"how many (laptops|assets|devices|monitors)",
            r"in[ -]use\b",
            r"asset list\b",
            r"list of (assets|laptops|devices|monitors)\b",
            r"all (the )?(laptops|devices|monitors|desktops|phones)\b",
            r"(employees|staff|people)\b",
        )
        chosen = pick(
            "helpdesk_assets",
            f"Here are the assets from {_HELPDESK}.",
            scope="company" if company else "mine",
            status=_asset_status(words),
            query=_asset_search(words) if company else "",
        )
        if chosen:
            return chosen
        return _unavailable(_HELPDESK)

    # Joiners and leavers, before hiring: "who did we hire" asks who joined.
    joiners = _has(words, *_JOINERS) and not _has(
        words,
        r"when\b",
        r"date of joining",
        r"joining date",
        r"doj\b",
        "candidate",
        "applicant",
        r"joining (kit|letter|bonus|formalit)",
    )
    leavers = _has(words, *_LEAVERS) and not _has(
        words,
        r"(resignation|exit|notice|attrition) polic",
        r"how (do|can|to) i resign",
        r"exit interview",
        r"leaves?\b",
    )

    # Hiring (AnytimeHire).
    asked_hiring = True
    if joiners or leavers:
        asked_hiring = False
    elif _has(
        words,
        "candidate",
        "applicant",
        "shortlist",
        r"resumes?\b",
        r"cvs?\b",
        "finalist",
        r"profiles? (received|in pipeline)",
    ):
        chosen = pick(
            "hiring_candidates",
            f"Here are the candidates from {_HIRE}.",
            status=_status(words, product_tools.CANDIDATE_STATUSES),
            query=_search_text(
                words,
                (
                    *product_tools.CANDIDATE_STATUSES,
                    "shortlisted",
                    "offer",
                    "offers",
                    "interviewing",
                    "stage",
                    "resumes",
                    "resume",
                    "cv",
                    "cvs",
                    "pipeline",
                    "finalists",
                    "finalist",
                    "received",
                    "profiles",
                    "got",
                    "who",
                ),
            ),
        )
    elif _has(words, "interview") and not _has(words, "interview tool", "exit interview"):
        chosen = pick(
            "hiring_interviews",
            f"Here are the interviews from {_HIRE}.",
            **_capped(when, product_tools.MAX_DAYS),
            query=_search_text(
                words,
                (
                    "upcoming",
                    "held",
                    "coming",
                    "up",
                    "lined",
                    "planned",
                    "conducted",
                    "pending",
                    "taken",
                    "schedule",
                ),
            ),
        )
    elif _has(
        words,
        r"jobs?\b",
        "opening",
        "vacanc",
        r"positions?\b",
        "requisition",
        r"open roles?\b",
        r"roles? open",
        r"job (posts?|postings?|ads?)",
        r"hiring for\b",
        r"(what|which) (roles|positions) are we",
    ):
        chosen = pick(
            "hiring_jobs",
            f"Here are the jobs from {_HIRE}.",
            status=_status(words, product_tools.JOB_STATUSES),
            query=_search_text(
                words,
                (
                    *product_tools.JOB_STATUSES,
                    "closed",
                    "active",
                    "vacancy",
                    "vacancies",
                    "filled",
                    "posted",
                    "live",
                    "postings",
                    "posts",
                    "unfilled",
                    "currently",
                ),
            ),
        )
    elif _has(
        words,
        "hiring",
        "recruit",
        "pipeline",
        "anytime",
        r"talent acquisition\b",
        r"offers? [a-z ]{0,20}\b(made|released|sent|given|rolled|roll|extended)\b",
        r"selected\b",
    ):
        chosen = pick("hiring_overview", f"Here is hiring at a glance from {_HIRE}.")
    else:
        asked_hiring = False
    if chosen:
        return chosen
    if asked_hiring:
        return _unavailable(_HIRE)

    # Birthdays, work anniversaries, probation ends, last working days.
    date_kind = next((key for key, pats in _DATES_KIND if _has(words, *pats)), None)
    if date_kind and not _has(words, r"probation (polic|period (is|for|of))"):
        if self_words and date_kind in ("probation", "last_working_day"):
            # "When does my probation end?" is in the person's own profile.
            chosen = pick("my_record", "Here is your profile.", lookup="my_profile")
        else:
            label = {
                "birthdays": "birthdays",
                "anniversaries": "work anniversaries",
                "probation": "probation periods ending",
                "last_working_day": "last working days",
            }[date_kind]
            chosen = pick(
                "people_dates",
                f"Here are the {label} {_span(when, 'in the next 30 days')}.",
                kind=date_kind,
                **when,
            )
        if chosen:
            return chosen

    # Who joined and who left.
    if joiners or leavers:
        if not when["from"] and _has(words, r"on notice", r"serving notice", r"notice period"):
            reply = "Here is who is serving notice, with this month's joiners and exits."
        else:
            reply = f"Here are the joiners and exits {_span(when, 'this month')}."
        chosen = pick("people_changes", reply, **when)
        if chosen:
            return chosen

    counting = _has(words, *_HEADCOUNT)

    # How many people work here, and how many are on payroll.
    flow = _has(
        words,
        "on leave",
        "absent",
        "present",
        r"late\b",
        "joined",
        "joiners",
        "hired",
        r"left\b",
        "exited",
        r"exits?\b",
        "attrition",
        "resigned",
    )
    if counting and not flow:
        chosen = pick("headcount", "Here is your headcount today.")
        if chosen:
            return chosen

    # A person's record: the asker's own, or (admins and sub-admins) somebody else's.
    topic = _person_topic(words)
    if topic == "profile" and _has(
        words, "contact", r"phone\b", r"mobile\b", r"email (id |address )?(of|for)\b"
    ):
        topic = None  # a directory search, below
    if topic and not (topic == "leave_balance" and _has(words, r"who (is|are) on leave")):
        query, about_me = _person_query(words)
        label = _PERSON_LABEL[topic]
        # "My manager's salary" names nobody but is about somebody else.
        someone = relation is not None and not (
            topic == "manager"
            and relation.group(1) in ("manager", "boss", "lead", "team lead", "teamlead")
        )
        group = _has(
            words,
            r"team\b",
            "everyone",
            "everybody",
            r"all\b",
            "company",
            r"staff\b",
            r"employees\b",
            r"people\b",
            r"who\b",
        )
        if someone or (query and not about_me):
            if not hr:
                return _own_only(f"I can't show another person's {label}.")
            chosen = pick(
                "employee_record",
                "Payslips are under Payroll → Payslips. Here is the current salary."
                if topic == "payslip"
                else "",
                query=query,
                person_lookup="salary" if topic == "payslip" else topic,
            )
        elif topic == "attendance_this_month" and (hr or group) and not self_words:
            chosen = None  # the company's attendance, below
        else:
            # Nobody named: the asker's own ("leave balance", "my ctc").
            chosen = pick("my_record", _OWN_REPLY[topic], lookup=_OWN_LOOKUP[topic])
        if chosen:
            return chosen

    # Who is out, and attendance.
    if _has(words, *_WHO_IS_OUT) and not _has(words, r"absent", r"late\b", r"present\b"):
        chosen = pick(
            "who_is_out",
            f"Here is who is on approved leave {_span(when, 'today')}.",
            **_capped(when, 93),
        )
    elif _has(words, *_ATTENDANCE):
        chosen = pick(
            "attendance_summary",
            f"Here is attendance {_span(when, 'today')}.",
            **_capped(when, 93),
        )
    elif _has(words, r"leave\b", r"leaves\b"):
        chosen = pick(
            "who_is_out",
            f"Here is who is on approved leave {_span(when, 'today')}.",
            **_capped(when, 93),
        )
    if chosen:
        return chosen

    # The organisation.
    if _has(words, r"departments?\b", r"teams\b", r"functions\b", r"business units?\b"):
        chosen = pick("list_departments", "Here are the departments.")
    elif _has(words, r"locations?\b", r"offices?\b", r"branch", r"sites?\b", r"cities\b"):
        chosen = pick("list_locations", "Here are the office locations.")
    if chosen:
        return chosen

    # People: an explicit search, or a short message that reads like a name.
    query = _search_text(
        words,
        (
            "exited",
            "left",
            "former",
            "ex",
            "number",
            "phone",
            "mobile",
            "no",
            "id",
            "address",
            "reach",
            "contact",
            "details",
        ),
    )
    searching = _has(
        words,
        "find",
        "search",
        "who is",
        "contact",
        "email",
        "phone",
        r"mobile\b",
        "people",
        "employee",
        "colleague",
        "directory",
        "works? in",
        "team",
        r"look up",
        r"lookup",
    )
    if query and (searching or len(words.split()) <= 3):
        if not hr:
            return _own_only()
        chosen = pick(
            "search_people",
            f"Here is who matches “{query}”.",
            query=query,
            include_exited=_has(words, "exited", "left the company", "former", "ex-"),
        )
        if chosen:
            return chosen

    return _help(tools, name, prefix="I could not match that to something I can look up.")
