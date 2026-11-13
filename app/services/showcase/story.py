"""The showcase story: one signed order, from signature to day one, with Ask Miraa.

What a visitor sees after scanning an exhibition QR code. A client signs an
order for a team (by default: 6 Java developers for 12 months, ₹50 lakh, with
₹30 lakh for the team, so a 40% margin floor), and Ask Miraa carries it
through every system in one chat:

    Saleinx (order) → HRMS (requisition) → AnytimeHire (job, résumé match,
    invites, AI interviews, ranking) → HR (shortlist) → HRMS + Accounts
    (offers inside the budget, a negotiation the budget refuses) → HRMS
    (joiners) → ProcureFlow (laptops) → CGB Helpdesk (day-one IT) →
    Expense Genie (kick-off travel) → Accounts (P&L and cash flow).

**A sandbox, and it says so.** The candidates, vendors and claims below are
sample data written into this module; nothing here reads or writes any
workspace, product or person. The arithmetic is real: offers are fitted to the
budget, a counter-offer that breaks the margin floor is refused with the
figures, and the cash flow follows salaries, the client's payment terms,
laptops and travel month by month. Money is Decimal throughout.

**Pure.** `start` builds a state, `say` and `decide` move it on, `view` is
what the page shows. The state is plain JSON (amounts as strings) so a run can
be kept in a JSONB column and read back exactly.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass
from datetime import date, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from app.services.documents.indian_numbers import group_indian

__all__ = [
    "CHAPTERS",
    "MAX_HEADCOUNT",
    "Scenario",
    "StoryError",
    "decide",
    "lakh",
    "say",
    "start",
    "view",
]

IST = timezone(timedelta(hours=5, minutes=30))
VERSION = 1

ZERO = Decimal(0)
ONE = Decimal(1)
PAISA = Decimal("0.01")
TEN_K = Decimal(10_000)
LAKH = Decimal(100_000)
CRORE = Decimal(10_000_000)
HUNDRED = Decimal(100)

#: The pool holds 17 interviewed people: a shortlist of headcount + 2 must fit in it.
MAX_HEADCOUNT = 10
#: Share of the team budget Ask Miraa keeps back when it makes offers, for negotiation.
RESERVE_SHARE = Decimal("0.02")
#: Messages kept in a run, and visitor turns before it asks for a fresh start.
MAX_MESSAGES = 160
MAX_TURNS = 200
MAX_TEXT = 500


class StoryError(ValueError):
    """Something a visitor sent that the story cannot take (too long, finished run)."""


# ── Systems ──────────────────────────────────────────────────────────────────

#: Each system the story moves through, as its badge reads.
SYSTEMS: dict[str, str] = {
    "saleinx": "Saleinx",
    "hrms": "HRMS",
    "anytimehire": "AnytimeHire",
    "procureflow": "ProcureFlow",
    "helpdesk": "CGB Helpdesk",
    "expensegenie": "Expense Genie",
    "accounts": "Accounts",
}


# ── The scenario ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Scenario:
    """The order a showcase link tells: set by the super admin per event."""

    company: str
    client: str
    role: str
    headcount: int
    months: int
    order_value: Decimal
    team_budget: Decimal
    laptop_price: Decimal
    payment_days: int

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("order_value", "team_budget", "laptop_price"):
            data[key] = f"{data[key]:.2f}"
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Scenario:
        return cls(
            company=str(data["company"]),
            client=str(data["client"]),
            role=str(data["role"]),
            headcount=int(data["headcount"]),
            months=int(data["months"]),
            order_value=Decimal(str(data["order_value"])),
            team_budget=Decimal(str(data["team_budget"])),
            laptop_price=Decimal(str(data["laptop_price"])),
            payment_days=int(data["payment_days"]),
        )

    @property
    def margin_floor(self) -> Decimal:
        """The margin the team budget leaves: (order - budget) ÷ order, as a percentage."""
        return (self.order_value - self.team_budget) / self.order_value * HUNDRED

    @property
    def annual_budget(self) -> Decimal:
        """The team budget as a year's CTC for the whole team."""
        return self.team_budget * 12 / self.months

    @property
    def cap(self) -> Decimal:
        """One person's share of the budget as a year's CTC, to the rupee."""
        return (self.annual_budget / self.headcount).quantize(ONE, rounding=ROUND_DOWN)


# ── Money as people read it ──────────────────────────────────────────────────


def inr(amount: Decimal) -> str:
    """₹30,00,000: whole rupees, Indian grouping."""
    sign = "-" if amount < 0 else ""
    return f"{sign}₹{group_indian(abs(amount).quantize(ONE, rounding=ROUND_HALF_UP))}"


def lakh(amount: Decimal) -> str:
    """₹5.8 L, ₹1.25 Cr, ₹38,470: how an amount is said in a sentence."""
    sign = "-" if amount < 0 else ""
    value = abs(amount)
    if value >= CRORE:
        number, unit = value / CRORE, " Cr"
    elif value >= LAKH:
        number, unit = value / LAKH, " L"
    else:
        return f"{sign}₹{group_indian(value.quantize(ONE, rounding=ROUND_HALF_UP))}"
    text = f"{number.quantize(PAISA, rounding=ROUND_HALF_UP):f}".rstrip("0").rstrip(".")
    return f"{sign}₹{text}{unit}"


def _one_place(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}"


def pct(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"


def _money(amount: Decimal) -> str:
    return f"{amount.quantize(PAISA, rounding=ROUND_HALF_UP):f}"


def _round10k(amount: Decimal) -> Decimal:
    return (amount / TEN_K).quantize(ONE, rounding=ROUND_HALF_UP) * TEN_K


# ── Sample data: the résumé database, vendors, claims ────────────────────────

#: Skills by the kind of role: what the job asks for and what the interviewer scores.
_SKILLS: list[tuple[tuple[str, ...], dict[str, Any]]] = [
    (
        ("java", "spring"),
        {
            "must": ["Java 17", "Spring Boot", "Microservices", "REST APIs", "SQL"],
            "nice": ["Kafka", "AWS", "Docker"],
            "framework": "Spring Boot",
        },
    ),
    (
        ("python", "django"),
        {
            "must": ["Python 3", "Django or FastAPI", "REST APIs", "PostgreSQL", "Testing"],
            "nice": ["Celery", "AWS", "Docker"],
            "framework": "Django/FastAPI",
        },
    ),
    (
        ("react", "front", "ui"),
        {
            "must": ["React", "TypeScript", "State management", "REST APIs", "CSS"],
            "nice": ["Next.js", "Testing Library", "Accessibility"],
            "framework": "React",
        },
    ),
    (
        ("devops", "cloud", "sre"),
        {
            "must": ["Linux", "AWS or Azure", "Terraform", "CI/CD", "Kubernetes"],
            "nice": ["Prometheus", "Helm", "Python"],
            "framework": "Cloud",
        },
    ),
    (
        ("test", "qa", "quality"),
        {
            "must": ["Test design", "Selenium or Playwright", "API testing", "SQL", "Agile"],
            "nice": ["Java or Python", "JMeter", "CI/CD"],
            "framework": "Automation",
        },
    ),
]


def _skills(role: str) -> dict[str, Any]:
    lowered = role.lower()
    for keys, skills in _SKILLS:
        if any(key in lowered for key in keys):
            return skills
    return _SKILLS[0][1]


@dataclass(frozen=True)
class _Person:
    name: str
    match: int
    years: str
    city: str
    notice: int
    #: Expected CTC as a share of one person's budget (Scenario.cap).
    expects: Decimal
    current: Decimal
    replied: bool
    #: Technical depth, problem solving, framework in practice, coding round, communication.
    signals: tuple[int, int, int, int, int] | None
    confidence: Decimal | None


def _p(
    name: str,
    match: int,
    years: str,
    city: str,
    notice: int,
    current: str,
    expects: str,
    signals: tuple[int, int, int, int, int] | None = None,
    confidence: str | None = None,
) -> _Person:
    return _Person(
        name=name,
        match=match,
        years=years,
        city=city,
        notice=notice,
        expects=Decimal(expects),
        current=Decimal(current),
        replied=signals is not None,
        signals=signals,
        confidence=Decimal(confidence) if confidence else None,
    )


#: The sample résumé database, best match first. The first 20 clear the 70% bar;
#: three of them never reply to the invitation.
_POOL: tuple[_Person, ...] = (
    _p("Ananya Iyer", 94, "4.5", "Bengaluru", 30, "0.86", "1.04", (90, 88, 90, 87, 89), "0.91"),
    _p("Karthik Subramanian", 92, "5", "Chennai", 60, "0.90", "1.10", (88, 86, 89, 85, 84), "0.88"),
    _p("Rohan Mehta", 91, "4", "Pune", 30, "0.92", "1.24", (93, 92, 90, 89, 88), "0.90"),
    _p("Priya Nair", 90, "3.5", "Kochi", 30, "0.78", "0.94", (82, 80, 81, 83, 79), "0.84"),
    _p("Vikram Singh", 89, "5", "Delhi", 90, "0.95", "1.20"),
    _p("Sneha Kulkarni", 88, "3", "Pune", 15, "0.80", "0.96", (86, 85, 84, 86, 83), "0.89"),
    _p("Arjun Reddy", 87, "4", "Hyderabad", 30, "0.84", "1.00", (85, 84, 83, 84, 82), "0.86"),
    _p("Meera Joshi", 86, "3", "Mumbai", 60, "0.76", "0.92", (78, 76, 77, 75, 80), "0.80"),
    _p("Rahul Verma", 85, "2.5", "Noida", 30, "0.70", "0.86", (74, 72, 73, 70, 75), "0.78"),
    _p("Divya Menon", 84, "3", "Bengaluru", 30, "0.74", "0.88", (83, 82, 82, 81, 84), "0.87"),
    _p("Aditya Rao", 83, "2.5", "Mysuru", 15, "0.72", "0.88", (80, 79, 78, 79, 77), "0.83"),
    _p("Pooja Sharma", 82, "3", "Jaipur", 30, "0.75", "0.90", (76, 75, 74, 72, 78), "0.79"),
    _p("Siddharth Jain", 81, "4", "Indore", 60, "0.85", "1.06"),
    _p("Kavya Pillai", 80, "2", "Chennai", 30, "0.66", "0.80", (72, 70, 71, 69, 76), "0.74"),
    _p("Nikhil Gupta", 79, "3.5", "Gurugram", 45, "0.82", "1.02", (70, 68, 69, 66, 72), "0.76"),
    _p("Ishita Banerjee", 78, "2", "Kolkata", 30, "0.64", "0.78", (68, 67, 66, 64, 74), "0.72"),
    _p("Manish Yadav", 76, "3", "Lucknow", 30, "0.70", "0.84", (66, 64, 65, 62, 68), "0.70"),
    _p("Shruti Deshpande", 74, "2.5", "Nagpur", 30, "0.68", "0.82"),
    _p("Varun Malhotra", 73, "4", "Chandigarh", 60, "0.88", "1.08", (64, 62, 63, 60, 66), "0.69"),
    _p("Neha Chauhan", 71, "2", "Ahmedabad", 15, "0.62", "0.76", (62, 61, 60, 58, 67), "0.68"),
)
#: Profiles searched in all: the 20 above and six under the bar.
_SEARCHED = 26
_MATCH_BAR = 70

_WEIGHTS = (Decimal("0.30"), Decimal("0.25"), Decimal("0.20"), Decimal("0.15"), Decimal("0.10"))
#: Short, so a ranking of 17 fits a phone; each is one of the interviewer's scored signals.
_SIGNAL_NAMES = ("Technical", "Problem solving", "{framework}", "Coding", "Communication")

#: Laptop quotes: the lowest is the link's laptop price; the others a little over it.
_VENDORS = (
    ("Nexora IT Supplies", Decimal("1.00"), "5 days"),
    ("Brightline Systems", Decimal("1.04"), "4 days"),
    ("Corelink Technologies", Decimal("1.07"), "7 days"),
)
_LAPTOP = "14-inch laptop · i7 · 16 GB · 512 GB SSD · 3-year warranty"

#: The client kick-off visit, as Expense Genie would show it.
_CLAIMS = (
    ("Neha Kapoor", "Account manager", "Flight", Decimal("9850"), True),
    ("Neha Kapoor", "Account manager", "Hotel, 2 nights", Decimal("6400"), False),
    ("Vivek Rao", "Delivery lead", "Flight", Decimal("10120"), True),
    ("Vivek Rao", "Delivery lead", "Hotel, 2 nights", Decimal("6400"), True),
    ("Vivek Rao", "Delivery lead", "Cabs", Decimal("2250"), False),
)


def _score(person: _Person) -> int:
    assert person.signals is not None
    total = sum((Decimal(s) * w for s, w in zip(person.signals, _WEIGHTS, strict=True)), ZERO)
    return int(total.quantize(ONE, rounding=ROUND_HALF_UP))


def _expects(person: _Person, sc: Scenario) -> Decimal:
    return _round10k(sc.cap * person.expects)


def _current(person: _Person, sc: Scenario) -> Decimal:
    return _round10k(sc.cap * person.current)


def _ranked() -> list[int]:
    """Pool indexes of everyone interviewed, best interview score first."""
    interviewed = [i for i, person in enumerate(_POOL) if person.replied]
    return sorted(interviewed, key=lambda i: (-_score(_POOL[i]), _POOL[i].name))


def _first(index: int) -> str:
    return _POOL[index].name.split()[0]


def _confidence(person: _Person) -> str:
    assert person.confidence is not None
    band = "High" if person.confidence >= Decimal("0.85") else "Medium"
    return f"{band} ({person.confidence})"


# ── The chapters ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Chapter:
    key: str
    title: str
    systems: tuple[str, ...]
    blurb: str


CHAPTERS: tuple[_Chapter, ...] = (
    _Chapter(
        "order",
        "Order signed",
        ("saleinx", "accounts"),
        "Book the order; the account opens with its margin.",
    ),
    _Chapter(
        "job", "Requisition and job", ("hrms", "anytimehire"), "Budget checked, approved, job live."
    ),
    _Chapter(
        "match", "Résumés matched", ("anytimehire",), "The résumé database, best match first."
    ),
    _Chapter(
        "invite",
        "Interview invites",
        ("anytimehire",),
        "Email and WhatsApp; candidates pick a slot.",
    ),
    _Chapter(
        "interview",
        "AI interviews ranked",
        ("anytimehire",),
        "Scores with their signals and confidence.",
    ),
    _Chapter("shortlist", "HR shortlist", ("hrms",), "People decide; the ranking only advises."),
    _Chapter(
        "offer",
        "Offers inside the budget",
        ("hrms", "accounts"),
        "Fitted to the budget, some kept back.",
    ),
    _Chapter(
        "negotiate", "Negotiation", ("hrms", "accounts"), "A counter-offer meets the margin floor."
    ),
    _Chapter(
        "hire", "Hired: joiners added", ("hrms", "anytimehire"), "Codes, letters, onboarding."
    ),
    _Chapter(
        "laptops",
        "Laptops and IT for day one",
        ("procureflow", "helpdesk"),
        "Quotes, PO, IT tickets.",
    ),
    _Chapter("travel", "Kick-off travel", ("expensegenie",), "Claims by person; approve the rest."),
    _Chapter("cash", "P&L and cash flow", ("accounts",), "Is the account cash positive, and when?"),
)
_KEYS = tuple(chapter.key for chapter in CHAPTERS)
_BY_KEY = {chapter.key: chapter for chapter in CHAPTERS}

#: How a visitor asks for each chapter, checked in this order.
_INTENTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("travel", re.compile(r"travel|expense|spen[dt]|claim|reimburs")),
    ("laptops", re.compile(r"laptop|equipment|device|procure|purchase|it setup|day one|day-one")),
    ("cash", re.compile(r"cash|p ?& ?l|pnl|profit|loss|positive|negative|account")),
    ("invite", re.compile(r"invit")),
    ("shortlist", re.compile(r"short ?-?list|\bdrop\b|\bremove\b")),
    ("interview", re.compile(r"interview|rank|score")),
    ("match", re.compile(r"match|r[eé]sum[eé]|\bcvs?\b|database|profiles|search")),
    ("hire", re.compile(r"\bhire|joiner|onboard|accepted")),
    ("offer", re.compile(r"offer|within (?:the )?budget")),
    ("order", re.compile(r"sign|book|order|won\b|deal|requisition|open (?:the |two |\w+ )?roles?")),
)
_YES = re.compile(
    r"^(?:yes|yeah|yep|ok(?:ay)?|sure|go ahead|do it|confirm|approve|send|proceed|y)\b"
    r"|approve (?:it|the|them)|send (?:them|it|the)"
)
_NO = re.compile(r"^(?:no|nope|cancel|stop|don'?t|not now|n)\b")
_HELP = re.compile(r"\bhelp\b|what can you|how does this work|what is this")
_STATUS = re.compile(r"margin|budget|headroom|how much")


# ── State ────────────────────────────────────────────────────────────────────


def start(scenario: Scenario, *, today: date) -> dict[str, Any]:
    """A fresh story, opening with Ask Miraa's welcome."""
    if not 1 <= scenario.headcount <= MAX_HEADCOUNT:
        raise StoryError(f"A showcase team is 1 to {MAX_HEADCOUNT} people.")
    joining = today + timedelta(days=21)
    joining += timedelta(days=(7 - joining.weekday()) % 7)  # the Monday on or after
    state: dict[str, Any] = {
        "v": VERSION,
        "scenario": scenario.to_json(),
        "start": today.isoformat(),
        "joining": joining.isoformat(),
        "seq": 0,
        "done": [],
        "pending": None,
        "messages": [],
        "activity": [],
        "shortlist": [],
        "offers": {},
        "accepted": [],
        "counter": None,
        "budget": None,
        "laptops": None,
        "claims_approved": False,
    }
    sc = scenario
    _reply(
        state,
        0,
        (
            f"Hi, I'm **Miraa**. This is {_whose(sc.company)} story of one order, from signature "
            f"to "
            f"day one, across six systems in this one chat.\n\n"
            f"**{sc.client}** is about to sign: **{sc.headcount} "
            f"{_plural(sc.role, sc.headcount)}** "
            f"for **{sc.months} months**, **{lakh(sc.order_value)}**. Tell me when it's signed, "
            f"or tap the first step."
        ),
        systems=(),
    )
    return state


def _sc(state: dict[str, Any]) -> Scenario:
    return Scenario.from_json(state["scenario"])


def _budget(state: dict[str, Any]) -> Decimal:
    """The team budget in force: the link's, or a higher one the visitor approved."""
    sc = _sc(state)
    return Decimal(state["budget"]) if state.get("budget") else sc.team_budget


def _day(state: dict[str, Any], offset: int) -> date:
    return date.fromisoformat(state["start"]) + timedelta(days=offset)


def _label(day: date) -> str:
    return f"{day:%a} {day.day} {day:%b}"


def _plural(role: str, count: int) -> str:
    return role if count == 1 or role.endswith("s") else f"{role}s"


def _whose(name: str) -> str:
    """Acme's, Acme Technologies'."""
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def _team(sc: Scenario) -> str:
    """6 Java Developers."""
    return f"{sc.headcount} {_plural(sc.role, sc.headcount)}"


def _next_id(state: dict[str, Any], prefix: str) -> str:
    state["seq"] += 1
    return f"{prefix}{state['seq']}"


def _message(
    state: dict[str, Any],
    role: str,
    text: str,
    *,
    offset: int,
    systems: tuple[str, ...] = (),
    cards: list[dict[str, Any]] | None = None,
    elapsed: str | None = None,
) -> dict[str, Any]:
    message = {
        "id": _next_id(state, "m"),
        "role": role,
        "text": text,
        "systems": list(systems),
        "cards": cards or [],
        "when": _label(_day(state, offset)),
        "elapsed": elapsed,
    }
    state["messages"].append(message)
    if len(state["messages"]) > MAX_MESSAGES:
        del state["messages"][: len(state["messages"]) - MAX_MESSAGES]
    return message


def _reply(
    state: dict[str, Any],
    offset: int,
    text: str,
    *,
    systems: tuple[str, ...],
    cards: list[dict[str, Any]] | None = None,
    elapsed: str | None = None,
) -> dict[str, Any]:
    return _message(
        state, "assistant", text, offset=offset, systems=systems, cards=cards, elapsed=elapsed
    )


def _log(state: dict[str, Any], offset: int, system: str, text: str) -> None:
    state["activity"].append({"when": _label(_day(state, offset)), "system": system, "text": text})


def _done(state: dict[str, Any], key: str) -> bool:
    return key in state["done"]


def _finish(state: dict[str, Any], key: str) -> None:
    if key not in state["done"]:
        state["done"].append(key)


def _next_chapter(state: dict[str, Any]) -> str | None:
    for key in _KEYS:
        if key not in state["done"]:
            return key
    return None


# ── Cards ────────────────────────────────────────────────────────────────────


def _figure(label: str, value: str) -> dict[str, str]:
    return {"label": label, "value": value}


def _proposal(
    state: dict[str, Any],
    kind: str,
    *,
    title: str,
    figures: list[dict[str, str]],
    notes: list[str],
    confirm: str,
    question: str,
    table: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"showcase:{state['start']}:{state['seq']}:{kind}")
    )
    state["pending"] = {"id": action_id, "kind": kind}
    card: dict[str, Any] = {
        "type": "proposal",
        "action_id": action_id,
        "kind": kind,
        "status": "proposed",
        "expires_at": (_day(state, 30)).isoformat() + "T00:00:00+05:30",
        "title": title,
        "figures": figures,
        "notes": notes,
        "confirm_label": confirm,
        "question": question,
        "result": None,
    }
    if table:
        card["table"] = table
    return card


def _close_pending(state: dict[str, Any], status: str, text: str) -> str | None:
    """Mark the open offer card done or cancelled; returns its kind."""
    pending = state.get("pending")
    if not pending:
        return None
    for message in reversed(state["messages"]):
        for card in message["cards"]:
            if card.get("type") == "proposal" and card.get("action_id") == pending["id"]:
                card["status"] = status
                card["result"] = {"text": text}
                break
    state["pending"] = None
    return str(pending["kind"])


# ── Figures ──────────────────────────────────────────────────────────────────


def _offered(state: dict[str, Any]) -> dict[int, Decimal]:
    return {int(k): Decimal(v) for k, v in state["offers"].items()}


def _team_annual(state: dict[str, Any]) -> Decimal | None:
    offers = _offered(state)
    return sum(offers.values(), ZERO) if offers else None


def _contract(state: dict[str, Any], annual: Decimal) -> Decimal:
    return (annual * _sc(state).months / 12).quantize(PAISA, rounding=ROUND_HALF_UP)


def _margin(state: dict[str, Any], team_cost: Decimal) -> Decimal:
    sc = _sc(state)
    return (sc.order_value - team_cost) / sc.order_value * HUNDRED


def _fit_offers(
    sc: Scenario, people: list[int], budget_annual: Decimal
) -> tuple[dict[int, Decimal], Decimal]:
    """Offers for these people that fit the budget with some kept back; and the most anyone gets.

    Everyone is offered what they expect, except that nobody gets more than one
    ceiling: the highest, in steps of ₹10,000, at which the team still fits the
    budget less what is kept back for negotiation.
    """
    expects = {i: _expects(_POOL[i], sc) for i in people}
    reserve = (budget_annual * RESERVE_SHARE / TEN_K).quantize(ONE, rounding=ROUND_DOWN) * TEN_K
    target = budget_annual - reserve
    ceiling = max(expects.values())
    while ceiling > 0 and sum(min(e, ceiling) for e in expects.values()) > target:
        ceiling -= TEN_K
    ceiling = max(ceiling, TEN_K)
    return {i: min(e, ceiling) for i, e in expects.items()}, ceiling


def _cash(state: dict[str, Any]) -> dict[str, Any]:
    """Month by month from the joining month: what comes in, what goes out, cash to date."""
    sc = _sc(state)
    months = sc.months
    lag = min(3, (sc.payment_days + 29) // 30)
    billing = (sc.order_value / months).quantize(PAISA, rounding=ROUND_DOWN)
    last_billing = sc.order_value - billing * (months - 1)
    annual = _team_annual(state)
    team = _contract(state, annual) if annual is not None else _budget(state)
    salary = (team / months).quantize(PAISA, rounding=ROUND_DOWN)
    last_salary = team - salary * (months - 1)
    one_off = _laptops_total(state) + _travel_total(state)

    joining = date.fromisoformat(state["joining"])
    rows: list[dict[str, str]] = []
    running = ZERO
    lowest, lowest_at, positive_from = ZERO, None, None
    for m in range(1, months + lag + 1):
        inflow = ZERO
        if 1 <= m - lag <= months:
            inflow = last_billing if m - lag == months else billing
        outflow = ZERO
        if m <= months:
            outflow += last_salary if m == months else salary
        if m == 1:
            outflow += one_off
        running += inflow - outflow
        year, month = divmod(joining.month - 1 + m - 1, 12)
        label = f"{date(joining.year + year, month + 1, 1):%b %Y}"
        if running < lowest:
            lowest, lowest_at = running, label
        if running >= 0 and positive_from is None and lowest < 0:
            positive_from = label
        rows.append(
            {
                "label": label,
                "inflow": _money(inflow),
                "outflow": _money(outflow),
                "net": _money(inflow - outflow),
                "cumulative": _money(running),
            }
        )
    return {
        "months": rows,
        "lowest": _money(lowest),
        "lowest_at": lowest_at,
        "positive_from": positive_from,
        "final": _money(running),
        "basis": "offers" if annual is not None else "budget",
        "lag_months": lag,
    }


def _laptops_total(state: dict[str, Any]) -> Decimal:
    return Decimal(state["laptops"]["total"]) if state.get("laptops") else ZERO


def _travel_total(state: dict[str, Any]) -> Decimal:
    return sum((c[3] for c in _CLAIMS), ZERO) if _done(state, "travel") else ZERO


def _pnl(state: dict[str, Any]) -> dict[str, str]:
    sc = _sc(state)
    annual = _team_annual(state)
    team = _contract(state, annual) if annual is not None else _budget(state)
    laptops = _laptops_total(state)
    travel = _travel_total(state)
    net = sc.order_value - team - laptops - travel
    return {
        "revenue": _money(sc.order_value),
        "team": _money(team),
        "laptops": _money(laptops),
        "travel": _money(travel),
        "net": _money(net),
        "net_pct": _one_place(net / sc.order_value * HUNDRED),
        "gross_pct": f"{_margin(state, team).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}",
        "basis": "offers" if annual is not None else "budget",
    }


# ── Reading what the visitor wrote ───────────────────────────────────────────

_AMOUNT = re.compile(
    r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d+)?)\s*(lakhs?|lacs?|l\b|k\b|crores?|cr\b)?",
    re.IGNORECASE,
)


def _amount(text: str) -> Decimal | None:
    """The first rupee amount in the text: 5.8 lakh, ₹5.8 L, 580000, 5,80,000.

    A bare number under 10,000 ("top 8") is not an amount.
    """
    for match in _AMOUNT.finditer(text):
        raw, unit = match.group(1).replace(",", ""), (match.group(2) or "").lower()
        try:
            number = Decimal(raw)
        except ArithmeticError:
            continue
        if unit.startswith(("lakh", "lac")) or unit == "l":
            number *= LAKH
        elif unit.startswith("cr"):
            number *= CRORE
        elif unit == "k":
            number *= 1000
        elif number < 10_000:
            continue
        if number <= 0 or number > 100 * CRORE:
            continue
        return number.quantize(ONE, rounding=ROUND_HALF_UP)
    return None


def _named(text: str, among: list[int]) -> int | None:
    lowered = text.lower()
    for i in among:
        name = _POOL[i].name.lower()
        if name in lowered or re.search(rf"\b{re.escape(name.split()[0])}\b", lowered):
            return i
    return None


# ── Moving the story on ──────────────────────────────────────────────────────


def say(state: dict[str, Any], text: str) -> dict[str, Any]:
    """The visitor wrote something: Ask Miraa answers and the story moves on."""
    text = " ".join((text or "").split())
    if not text:
        raise StoryError("Write a message first.")
    if len(text) > MAX_TEXT:
        raise StoryError(f"Keep it under {MAX_TEXT} characters.")
    asked = _message(state, "user", text, offset=_offset(state))
    _answer(state, text.lower())
    _stamp(state, asked)
    return state


def _answer(state: dict[str, Any], lowered: str) -> None:
    pending = state.get("pending")
    if pending and _YES.search(lowered):
        _confirm(state)
        return
    if pending and _NO.match(lowered):
        _cancel(state)
        return

    if _done(state, "order") and re.search(r"(?:raise|increase|up) (?:the )?budget", lowered):
        _propose_budget(state, _amount(lowered))
        return
    if state.get("counter") and _negotiate(state, lowered):
        return

    wanted = _intent(state, lowered)
    if wanted is None:
        if _HELP.search(lowered):
            _help(state)
        elif _STATUS.search(lowered) and _done(state, "order"):
            _status(state)
        else:
            _help(state, unsure=True)
        return
    _run(state, wanted, lowered)


def _stamp(state: dict[str, Any], asked: dict[str, Any]) -> None:
    """The visitor's message carries the story day of the answer it got."""
    messages = state["messages"]
    for index, message in enumerate(messages):
        if message is asked and index + 1 < len(messages):
            reply = messages[index + 1]
            asked["when"] = reply["when"]
            # A time skip belongs to the visitor's step, before it.
            if reply.get("elapsed"):
                asked["elapsed"], reply["elapsed"] = reply["elapsed"], None
            return


def decide(state: dict[str, Any], action_id: str, confirm: bool) -> dict[str, Any]:
    """The visitor pressed Confirm or Cancel on an offer card."""
    pending = state.get("pending")
    if not pending or pending["id"] != action_id:
        raise StoryError("That offer is no longer open.")
    asked = _message(state, "user", "Yes." if confirm else "Cancel.", offset=_offset(state))
    if confirm:
        _confirm(state)
    else:
        _cancel(state)
    _stamp(state, asked)
    return state


def _offset(state: dict[str, Any]) -> int:
    """Story days since the order: each chapter happens on its own day."""
    days = {
        "order": 0,
        "job": 0,
        "match": 0,
        "invite": 1,
        "interview": 3,
        "shortlist": 4,
        # Offers go out on day 4; the counter-offer comes back on day 5.
        "offer": 5,
        "negotiate": 5,
        "hire": 6,
        "laptops": 6,
        "travel": 6,
        "cash": 6,
    }
    reached = [days[key] for key in state["done"]]
    return max(reached, default=0)


def _intent(state: dict[str, Any], lowered: str) -> str | None:
    """Which chapter the visitor asked for.

    The next chapter when the words fit it, else the first chapter that fits.
    """
    following = _next_chapter(state)
    hits = [key for key, pattern in _INTENTS if pattern.search(lowered)]
    if not hits:
        return None
    if following in hits:
        return following
    # "approve the requisition" before the order is "book the order".
    return hits[0]


def _run(state: dict[str, Any], key: str, lowered: str) -> None:
    pending = state.get("pending")
    if pending:
        _reply(
            state,
            _offset(state),
            "One thing first: the offer above is waiting. Say **yes** to go ahead, or **cancel**.",
            systems=(),
        )
        return
    if key == "job":
        key = "order"
    order = list(_KEYS)
    needs = {
        "order": None,
        "match": "job",
        "invite": "match",
        "interview": "invite",
        "shortlist": "interview",
        "offer": "shortlist",
        "negotiate": "offer",
        "hire": "negotiate",
        "laptops": "hire",
        "travel": "order",
        "cash": "order",
    }
    before = needs.get(key)
    if before and not _done(state, before):
        missing = next(k for k in order if not _done(state, k))
        _reply(
            state,
            _offset(state),
            f"Not yet: that comes after **{_BY_KEY[before].title.lower()}**. "
            f"Next is **{_BY_KEY[missing].title.lower()}**: {_prompt(state, missing)}",
            systems=(),
        )
        return
    if key == "order" and _done(state, "order") and not _done(state, "job"):
        _reply(
            state,
            0,
            "The requisition is ready again. Nothing opens until you say yes.",
            systems=("hrms",),
            cards=[_requisition(state)],
        )
        return
    if key == "shortlist" and _done(state, "shortlist") and not _done(state, "offer"):
        _shortlist(state, lowered)
        return
    if _done(state, key) and key not in ("cash", "travel"):
        _recap(state, key)
        return
    if key == "shortlist":
        _shortlist(state, lowered)
        return
    {
        "order": _order,
        "match": _match,
        "invite": _invite,
        "interview": _interviews,
        "offer": _offer,
        "negotiate": _counter_reminder,
        "hire": _hire,
        "laptops": _laptops,
        "travel": _travel,
        "cash": _cash_chapter,
    }[key](state)


def _confirm(state: dict[str, Any]) -> None:
    kind = state["pending"]["kind"]
    {
        "requisition": _job,
        "invite": _invited,
        "offers": _offers_sent,
        "budget": _budget_raised,
        "hire": _hired,
        "laptops": _laptops_ordered,
        "claims": _claims_approved,
    }[kind](state)


def _cancel(state: dict[str, Any]) -> None:
    kind = _close_pending(state, "cancelled", "Cancelled. Nothing was sent.")
    again = {
        "requisition": "Say **approve the requisition** when you're ready.",
        "invite": "Ask me to **invite** them again when you're ready.",
        "offers": "Ask me to **make offers** again, or change the shortlist first.",
        "budget": "The budget stays as it was.",
        "hire": "Say **hire them** when you're ready.",
        "laptops": "Ask for **laptops** again when you're ready.",
        "claims": "The claims stay pending.",
    }.get(kind or "", "")
    _reply(state, _offset(state), f"Cancelled; nothing was sent. {again}".strip(), systems=())


# ── Chapter 1-2: the order, the requisition, the job ─────────────────────────


def _po(state: dict[str, Any]) -> str:
    initials = "".join(word[0] for word in _sc(state).client.split() if word[:1].isalpha())[:3]
    return f"PO-{(initials or 'CL').upper()}-{_day(state, 0):%y%m}"


def _order(state: dict[str, Any]) -> None:
    sc = _sc(state)
    _finish(state, "order")
    floor = sc.margin_floor
    _log(state, 0, "saleinx", f"Deal won and approved: {sc.client}, {lakh(sc.order_value)}")
    _log(state, 0, "accounts", f"Account opened: forecast margin {pct(floor)}")
    _reply(
        state,
        0,
        (
            f"Order booked in **Saleinx**: {sc.client}, {sc.headcount} "
            f"{_plural(sc.role, sc.headcount)} for {sc.months} months, "
            f"**{inr(sc.order_value)}**.\n\n"
            f"A team budget of **{inr(sc.team_budget)}** keeps your **{pct(floor)} margin**: up to "
            f"**{lakh(sc.cap)} a year** per person. The account is open with that forecast.\n\n"
            f"A requisition for {sc.headcount} {_plural(sc.role, sc.headcount)} is waiting for "
            f"your yes."
        ),
        systems=("saleinx", "hrms", "accounts"),
        cards=[_order_card(state), _requisition(state)],
    )


def _order_card(state: dict[str, Any]) -> dict[str, Any]:
    sc = _sc(state)
    floor = sc.margin_floor
    return {
        "type": "record",
        "title": f"{sc.client}: order booked",
        "subtitle": "Saleinx · Won and approved",
        "heading": "The order",
        "figures": [
            _figure("Order value", inr(sc.order_value)),
            _figure("Team", _team(sc)),
            _figure("Months", str(sc.months)),
            _figure("Team budget", inr(sc.team_budget)),
            _figure("Margin floor", pct(floor)),
            _figure("PO", _po(state)),
        ],
        "note": (
            "Sample order. In a workspace this is the deal in Saleinx, and AskMiraa checks "
            "the PO matches the team's billing."
        ),
    }


def _requisition(state: dict[str, Any]) -> dict[str, Any]:
    sc = _sc(state)
    return _proposal(
        state,
        "requisition",
        title=f"Requisition: {_team(sc)}",
        figures=[
            _figure("Positions", str(sc.headcount)),
            _figure("Up to, a year each", inr(sc.cap)),
            _figure("Team over the contract", f"≤ {inr(sc.team_budget)}"),
            _figure("Approver", "You, as account owner"),
        ],
        notes=[
            "The headcount budget has room: no other requisition draws on this account.",
            "On approval the job opens in AnytimeHire, written from the order.",
        ],
        confirm="Approve and open the job",
        question="Approve the requisition?",
    )


def _job(state: dict[str, Any]) -> None:
    sc = _sc(state)
    skills = _skills(sc.role)
    _close_pending(state, "done", "Approved. The job is open in AnytimeHire.")
    _finish(state, "job")
    _log(state, 0, "hrms", f"Requisition approved: {_team(sc)}")
    _log(state, 0, "anytimehire", f"Job opened: {_team(sc)}")
    _reply(
        state,
        0,
        (
            f"Approved. The job is live in **AnytimeHire**: {_team(sc)}, up to "
            f"{lakh(sc.cap)} a year, on site with {sc.client}. I wrote the description from the "
            f"order."
        ),
        systems=("hrms", "anytimehire"),
        cards=[
            {
                "type": "record",
                "title": f"{sc.role}: job description",
                "subtitle": f"AnytimeHire · {sc.headcount} openings",
                "heading": "What the job asks for",
                "figures": [
                    _figure("Openings", str(sc.headcount)),
                    _figure("Experience", "2 to 5 years"),
                    _figure("CTC, a year", f"up to {lakh(sc.cap)}"),
                    _figure("Where", f"On site with {sc.client}"),
                    _figure("Must have", ", ".join(skills["must"])),
                    _figure("Good to have", ", ".join(skills["nice"])),
                ],
                "note": (
                    "Sample job. In a workspace it opens in AnytimeHire only after the "
                    "requisition is approved."
                ),
            }
        ],
    )


# ── Chapter 3-5: match, invite, interviews ───────────────────────────────────


def _match(state: dict[str, Any]) -> None:
    sc = _sc(state)
    skills = _skills(sc.role)
    _finish(state, "match")
    _log(
        state,
        0,
        "anytimehire",
        f"{len(_POOL)} of {_SEARCHED} profiles matched {_MATCH_BAR}% or more",
    )
    rows = [
        [
            n,
            person.name,
            f"{person.years} yrs",
            f"{person.match}%",
            ", ".join(skills["must"][: 3 if person.match >= 85 else 2]),
            inr(_expects(person, sc)),
            f"{person.notice} days",
        ]
        for n, person in enumerate(_POOL, start=1)
    ]
    _reply(
        state,
        0,
        (
            f"I searched the résumé database in **AnytimeHire**: **{_SEARCHED}** {sc.role} "
            f"profiles, and **{len(_POOL)}** match {_MATCH_BAR}% or more of the must-haves. "
            f"Here they are, best first."
        ),
        systems=("anytimehire",),
        cards=[
            {
                "type": "table",
                "title": f"Best matches: {sc.role}",
                "caption": "Sample résumé database · match = must-have skills and experience",
                "columns": [
                    "#",
                    "Candidate",
                    "Experience",
                    "Match",
                    "Key skills",
                    "Expects (a year)",
                    "Notice",
                ],
                "rows": rows,
                "source": "AnytimeHire",
            }
        ],
    )


def _invite(state: dict[str, Any]) -> None:
    sc = _sc(state)
    first = _POOL[0].name.split()[0]
    proposal = _proposal(
        state,
        "invite",
        title=f"Invite {len(_POOL)} to an AI interview",
        figures=[
            _figure("Candidates", str(len(_POOL))),
            _figure("Channel", "Email and WhatsApp"),
            _figure("Interview", "35 min, AI interviewer"),
            _figure("When", "Any slot in the next 48 hours"),
        ],
        notes=[
            f"Subject: You're shortlisted: {sc.role} at {sc.company}",
            (
                f"“Hi {first}, your profile matches our {sc.role} opening for a client project. "
                f"Pick a time for a 35-minute interview with our AI interviewer in the next 48 "
                f"hours.”"
            ),
            "Each candidate accepts or declines from the message; replies come back here.",
        ],
        confirm="Send invitations",
        question=f"Send {len(_POOL)} invitations?",
    )
    _reply(
        state,
        0,
        f"Here is the invitation for the top {len(_POOL)}. Nothing goes out until you say yes.",
        systems=("anytimehire",),
        cards=[proposal],
    )


def _invited(state: dict[str, Any]) -> None:
    _close_pending(state, "done", f"Sent to {len(_POOL)} candidates.")
    _finish(state, "invite")
    replied = [p for p in _POOL if p.replied]
    silent = [p for p in _POOL if not p.replied]
    _log(state, 0, "anytimehire", f"{len(_POOL)} interview invitations sent")
    _log(state, 1, "anytimehire", f"{len(replied)} accepted and booked a slot")
    _reply(
        state,
        0,
        f"Sent: **{len(_POOL)} invitations** by email and WhatsApp.",
        systems=("anytimehire",),
    )
    _reply(
        state,
        1,
        (
            f"**{len(replied)} of {len(_POOL)}** accepted and booked a slot. "
            f"{len(silent)} haven't replied ({', '.join(p.name for p in silent)}); "
            f"I'll remind them at noon."
        ),
        systems=("anytimehire",),
        elapsed="Next morning",
        cards=[
            {
                "type": "table",
                "title": "Replies to the invitation",
                "caption": "Sample replies",
                "columns": ["Candidate", "Reply", "Interview slot"],
                "rows": [
                    [
                        p.name,
                        "Accepted" if p.replied else "No reply yet",
                        (
                            f"{_label(_day(state, 1 + (n % 2)))}, {10 + (n % 7)}:00"
                            if p.replied
                            else "—"
                        ),
                    ]
                    for n, p in enumerate(_POOL)
                ],
                "source": "AnytimeHire",
            }
        ],
    )


def _interviews(state: dict[str, Any]) -> None:
    sc = _sc(state)
    framework = _skills(sc.role)["framework"]
    names = [name.format(framework=framework) for name in _SIGNAL_NAMES]
    _finish(state, "interview")
    ranked = _ranked()
    _log(state, 3, "anytimehire", f"{len(ranked)} AI interviews done and ranked")
    rows = []
    for rank, i in enumerate(ranked, start=1):
        person = _POOL[i]
        assert person.signals is not None
        order = sorted(range(5), key=lambda s: -person.signals[s])
        rows.append(
            [
                rank,
                person.name,
                _score(person),
                _confidence(person),
                ", ".join(f"{names[s]} {person.signals[s]}" for s in order[:2]),
                f"{names[order[-1]]} {person.signals[order[-1]]}",
                inr(_expects(person, sc)),
            ]
        )
    _reply(
        state,
        3,
        (
            f"The **AnytimeHire** interviewer finished **{len(ranked)} interviews**, 35 minutes "
            f"each. "
            f"Here they are ranked by interview score; it is not the résumé order any more. "
            f"**{_POOL[ranked[0]].name}** came out on top."
        ),
        systems=("anytimehire",),
        elapsed="Two days later",
        cards=[
            {
                "type": "table",
                "title": "AI interview ranking",
                "caption": "Sample scores · each with its signals and confidence",
                "columns": [
                    "Rank",
                    "Candidate",
                    "Score",
                    "Confidence",
                    "Strongest",
                    "Weakest",
                    "Expects (a year)",
                ],
                "rows": rows,
                "source": "AnytimeHire",
            },
            {
                "type": "notice",
                "tone": "info",
                "text": (
                    "A score is advice for HR, never a decision. Each one shows the signals it "
                    "came from and how confident the interviewer is; the recordings are in "
                    "AnytimeHire."
                ),
            },
        ],
    )


# ── Chapter 6-8: shortlist, offers, negotiation ──────────────────────────────


def _shortlist(state: dict[str, Any], lowered: str) -> None:
    sc = _sc(state)
    ranked = _ranked()
    current = list(state["shortlist"]) or ranked[: sc.headcount + 2]
    chosen: list[int]
    dropped = (
        _named(lowered, ranked) if re.search(r"\bdrop\b|\bremove\b|\bwithout\b", lowered) else None
    )
    numbers = [int(n) for n in re.findall(r"\b(\d{1,2})\b", lowered)]
    if dropped is not None:
        chosen = [i for i in current if i != dropped]
        if len(chosen) == len(current):
            chosen = [i for i in ranked[: sc.headcount + 3] if i != dropped][: sc.headcount + 2]
    elif re.search(r"\btop\s+\d", lowered) and numbers:
        chosen = ranked[: numbers[0]]
    elif len(numbers) >= 2:
        chosen = [ranked[n - 1] for n in dict.fromkeys(numbers) if 1 <= n <= len(ranked)]
    else:
        chosen = current
    if len(chosen) < sc.headcount:
        _reply(
            state,
            4,
            (
                f"A shortlist needs at least {sc.headcount} people for {sc.headcount} openings. "
                f"Try **shortlist the top {sc.headcount + 2}**."
            ),
            systems=(),
        )
        return
    state["shortlist"] = chosen
    _finish(state, "shortlist")
    _log(state, 4, "hrms", f"HR shortlisted {len(chosen)} after the recordings")
    rows = [
        [
            ranked.index(i) + 1,
            _POOL[i].name,
            _score(_POOL[i]),
            inr(_current(_POOL[i], sc)),
            inr(_expects(_POOL[i], sc)),
            "Offer" if n < sc.headcount else "Back-up",
        ]
        for n, i in enumerate(chosen)
    ]
    _reply(
        state,
        4,
        (
            f"Shortlisted **{len(chosen)}**. The hiring manager watched the recordings and agreed. "
            f"The first {sc.headcount} get offers; {len(chosen) - sc.headcount} stay as back-ups. "
            f"Say **drop** and a name to change it."
        ),
        systems=("hrms",),
        cards=[
            {
                "type": "table",
                "title": "HR shortlist",
                "caption": "Decided by people, in rank order",
                "columns": ["Rank", "Candidate", "Score", "Earns now", "Expects", "Next"],
                "rows": rows,
            }
        ],
    )


def _offer(state: dict[str, Any]) -> None:
    sc = _sc(state)
    people = state["shortlist"][: sc.headcount]
    budget = _budget(state)
    annual_budget = budget * 12 / sc.months
    offers, ceiling = _fit_offers(sc, people, annual_budget)
    total = sum(offers.values(), ZERO)
    team = _contract(state, total)
    kept = budget - team
    state["draft_offers"] = {str(i): _money(v) for i, v in offers.items()}
    rows = []
    for i in people:
        expects = _expects(_POOL[i], sc)
        rows.append(
            [
                _POOL[i].name,
                inr(expects),
                inr(offers[i]),
                "As asked" if offers[i] >= expects else f"{lakh(expects - offers[i])} under ask",
            ]
        )
    capped = [i for i in people if offers[i] < _expects(_POOL[i], sc)]
    proposal = _proposal(
        state,
        "offers",
        title=f"Offers: {_team(sc)}",
        figures=[
            _figure("Team, a year", inr(total)),
            _figure("Over the contract", inr(team)),
            _figure("Team budget", inr(budget)),
            _figure("Kept for negotiation", inr(kept)),
            _figure("Margin", pct(_margin(state, team))),
            _figure("Floor", pct(sc.margin_floor)),
        ],
        table={"columns": ["Candidate", "Expects", "Offer", "Note"], "rows": rows},
        notes=[
            (
                f"Nobody is offered more than {lakh(ceiling)}, so the team fits with "
                f"{lakh(kept)} kept back for negotiation."
                if capped
                else "Everyone is offered what they asked, and the team still fits the budget."
            ),
            "Offers go out by email with the letter to e-sign.",
        ],
        confirm="Send offers",
        question="Send these offers?",
    )
    _reply(
        state,
        4,
        (
            f"Here are offers for the first {sc.headcount} on the shortlist, fitted to the "
            f"**{inr(budget)}** budget. "
            + (
                f"{len(capped)} asked for more than the budget allows, so they are capped at "
                f"**{lakh(ceiling)}**."
                if capped
                else "Everyone gets what they asked."
            )
        ),
        systems=("hrms", "accounts"),
        cards=[proposal],
    )


def _offers_sent(state: dict[str, Any]) -> None:
    sc = _sc(state)
    _close_pending(state, "done", f"Sent to {sc.headcount} candidates.")
    state["offers"] = state.pop("draft_offers", {})
    _finish(state, "offer")
    _log(state, 4, "hrms", f"{sc.headcount} offers sent")
    _reply(
        state,
        4,
        f"Sent: **{sc.headcount} offers**, each with its letter to e-sign.",
        systems=("hrms",),
    )
    # The one furthest under their ask comes back with a counter.
    offers = _offered(state)
    gaps = {i: _expects(_POOL[i], sc) - v for i, v in offers.items()}
    who = max(gaps, key=lambda i: (gaps[i], -i))
    ask = _expects(_POOL[who], sc) if gaps[who] > 0 else _round10k(offers[who] * Decimal("1.1"))
    state["counter"] = {"who": who, "ask": _money(ask)}
    accepted = [i for i in offers if i != who]
    state["accepted"] = accepted
    _counter_message(state, who, ask, elapsed="Next day")


def _counter_figures(state: dict[str, Any], who: int, amount: Decimal) -> dict[str, Decimal]:
    offers = _offered(state)
    total = sum(offers.values(), ZERO) - offers[who] + amount
    team = _contract(state, total)
    budget = _budget(state)
    annual_budget = budget * 12 / _sc(state).months
    headroom = annual_budget - (sum(offers.values(), ZERO) - offers[who])
    most = (headroom / TEN_K).quantize(ONE, rounding=ROUND_DOWN) * TEN_K
    return {"team": team, "over": team - budget, "margin": _margin(state, team), "most": most}


def _backup(state: dict[str, Any]) -> int | None:
    sc = _sc(state)
    taken = set(_offered(state))
    for i in state["shortlist"][sc.headcount :]:
        if i not in taken and i not in state.get("declined", []):
            return i
    return None


def _counter_message(state: dict[str, Any], who: int, ask: Decimal, *, elapsed: str | None) -> None:
    sc = _sc(state)
    offers = _offered(state)
    at_ask = _counter_figures(state, who, ask)
    name, first = _POOL[who].name, _first(who)
    backup = _backup(state)
    others = len(offers) - 1
    lines = [
        f"**{others} of {len(offers)}** accepted. **{name}** came back asking **{lakh(ask)}** "
        f"(offered {lakh(offers[who])})."
    ]
    if at_ask["over"] > 0:
        lines.append(
            f"At {lakh(ask)} the team costs {lakh(at_ask['team'])} over the contract: "
            f"**{lakh(at_ask['over'])} over budget**, and the margin falls to "
            f"**{pct(at_ask['margin'])}**, under your {pct(sc.margin_floor)} floor."
        )
        most = lakh(min(at_ask["most"], ask))
        option = f"The most I can offer {first} inside the budget is **{most}**"
        option += (
            f", or **{_POOL[backup].name}** (back-up, expects {lakh(_expects(_POOL[backup], sc))}) "
            f"can take the seat."
            if backup is not None
            else "."
        )
        lines.append(option)
    else:
        lines.append(
            f"That still fits: the team would cost {lakh(at_ask['team'])}, margin "
            f"{pct(at_ask['margin'])}. Say **agree** to give it."
        )
    figures = [
        _figure("Asked", inr(ask)),
        _figure("Offered", inr(offers[who])),
        _figure("Most within budget", inr(min(at_ask["most"], ask))),
        _figure("Team at the ask", inr(at_ask["team"])),
        _figure("Margin at the ask", pct(at_ask["margin"])),
        _figure("Floor", pct(sc.margin_floor)),
    ]
    _log(state, 5, "hrms", f"{name} countered at {lakh(ask)}")
    _reply(
        state,
        5,
        "\n\n".join(lines),
        systems=("hrms", "accounts"),
        elapsed=elapsed,
        cards=[
            {
                "type": "record",
                "title": f"Counter-offer: {name}",
                "subtitle": "Checked against the team budget and margin floor",
                "heading": "The figures",
                "figures": figures,
                "note": (
                    "A counter that breaks the floor is never sent without a budget change "
                    "you approve."
                ),
            }
        ],
    )


def _counter_reminder(state: dict[str, Any]) -> None:
    counter = state.get("counter")
    if counter:
        _counter_message(state, int(counter["who"]), Decimal(counter["ask"]), elapsed=None)
    else:
        _recap(state, "negotiate")


def _negotiate(state: dict[str, Any], lowered: str) -> bool:
    """A reply to the counter-offer. False when the words were about something else."""
    sc = _sc(state)
    counter = state["counter"]
    who, ask = int(counter["who"]), Decimal(counter["ask"])
    offers = _offered(state)
    first = _first(who)
    if re.search(r"swap|replace|back-?up|instead|next (?:one|candidate)", lowered):
        _swap(state, who, reason="swap")
        return True
    if re.search(r"\bhold\b|\bkeep\b|\bstay\b|no more|don'?t (?:raise|increase)", lowered):
        _swap(state, who, reason="hold")
        return True
    amount = _amount(lowered)
    agree = (
        r"\bagree\b|\baccept (?:it|the|his|her|their)|\bgive (?:him|her|them|it)"
        r"|match (?:it|the ask)|^(?:ok|okay|yes|fine)\b"
    )
    if amount is None and re.search(agree, lowered):
        amount = ask
    if amount is None:
        return False
    if amount < offers[who]:
        _reply(
            state,
            5,
            f"That's below the {lakh(offers[who])} already offered to {first}. Try a higher "
            f"amount.",
            systems=(),
        )
        return True
    figures = _counter_figures(state, who, amount)
    if figures["over"] > 0:
        _reply(
            state,
            5,
            (
                f"I can't send {lakh(amount)}: the team would cost {lakh(figures['team'])}, "
                f"**{lakh(figures['over'])} over the {lakh(_budget(state))} budget**, margin "
                f"**{pct(figures['margin'])}** against your {pct(sc.margin_floor)} floor.\n\n"
                f"You can offer up to **{lakh(figures['most'])}**, swap in the back-up, or "
                f"**raise the budget to {lakh(figures['team'])}** (that needs your approval, and "
                f"is logged)."
            ),
            systems=("accounts",),
            cards=[
                {
                    "type": "notice",
                    "tone": "warning",
                    "text": (
                        f"Refused: {lakh(amount)} breaks the margin floor. "
                        f"Nothing was sent to {first}."
                    ),
                }
            ],
        )
        return True
    state["offers"][str(who)] = _money(amount)
    midpoint = (offers[who] + ask) / 2
    if amount >= midpoint:
        state["accepted"].append(who)
        state["counter"] = None
        _log(state, 5, "hrms", f"{_POOL[who].name} accepted {lakh(amount)}")
        _all_accepted(state, f"{first} accepted **{lakh(amount)}**.")
    else:
        _swap(state, who, reason="low", amount=amount)
    return True


def _swap(state: dict[str, Any], who: int, *, reason: str, amount: Decimal | None = None) -> None:
    sc = _sc(state)
    first = _first(who)
    backup = _backup(state)
    state.setdefault("declined", []).append(who)
    if backup is None:
        _reply(
            state,
            5,
            f"There's no back-up left on the shortlist. Offer {first} more, or shortlist more "
            f"people.",
            systems=(),
        )
        state["declined"].remove(who)
        return
    offers = _offered(state)
    offers.pop(who)
    budget_annual = _budget(state) * 12 / sc.months
    room = budget_annual - sum(offers.values(), ZERO)
    take = min(
        _expects(_POOL[backup], sc), (room / TEN_K).quantize(ONE, rounding=ROUND_DOWN) * TEN_K
    )
    offers[backup] = take
    state["offers"] = {str(i): _money(v) for i, v in offers.items()}
    state["accepted"] = [i for i in state["accepted"] if i != who] + [backup]
    state["counter"] = None
    why = {
        "hold": f"You held at the first offer, and {first} declined.",
        "low": f"{first} declined {lakh(amount or ZERO)}.",
        "swap": f"{first} is released with thanks.",
    }[reason]
    _log(state, 5, "hrms", f"{_POOL[who].name} out; {_POOL[backup].name} in at {lakh(take)}")
    _all_accepted(state, f"{why} The back-up, **{_POOL[backup].name}**, accepted **{lakh(take)}**.")


def _all_accepted(state: dict[str, Any], opening: str) -> None:
    sc = _sc(state)
    total = _team_annual(state) or ZERO
    team = _contract(state, total)
    _finish(state, "negotiate")
    _reply(
        state,
        5,
        (
            f"{opening} **All {sc.headcount} offers are accepted.**\n\n"
            f"The team costs **{inr(team)}** over the contract against a {inr(_budget(state))} "
            f"budget, so the margin is **{pct(_margin(state, team))}** "
            f"(floor {pct(sc.margin_floor)})."
        ),
        systems=("hrms", "accounts"),
    )


def _propose_budget(state: dict[str, Any], amount: Decimal | None) -> None:
    sc = _sc(state)
    counter = state.get("counter")
    if amount is None and counter:
        amount = _counter_figures(state, int(counter["who"]), Decimal(counter["ask"]))["team"]
    if amount is None or amount <= _budget(state):
        _reply(
            state,
            _offset(state),
            f"Say the new budget, above {lakh(_budget(state))}: for example **raise the budget to "
            f"{lakh(_budget(state) + LAKH)}**.",
            systems=(),
        )
        return
    if amount >= sc.order_value:
        _reply(
            state,
            _offset(state),
            f"A team budget of {lakh(amount)} would leave no margin on a {lakh(sc.order_value)} "
            f"order. Pick a lower one.",
            systems=(),
        )
        return
    margin = (sc.order_value - amount) / sc.order_value * HUNDRED
    state["draft_budget"] = _money(amount)
    proposal = _proposal(
        state,
        "budget",
        title="Raise the team budget",
        figures=[
            _figure("Budget now", inr(_budget(state))),
            _figure("New budget", inr(amount)),
            _figure("Margin then", pct(margin)),
            _figure("Floor", pct(sc.margin_floor)),
        ],
        notes=[
            f"This takes the account under its {pct(sc.margin_floor)} margin floor.",
            "It is recorded against the account with who approved it.",
        ],
        confirm="Approve the higher budget",
        question="Raise the budget?",
    )
    _reply(
        state,
        _offset(state),
        "Raising the budget needs your approval as account owner.",
        systems=("accounts",),
        cards=[proposal],
    )


def _budget_raised(state: dict[str, Any]) -> None:
    amount = Decimal(state.pop("draft_budget"))
    _close_pending(state, "done", f"Budget raised to {inr(amount)}.")
    state["budget"] = _money(amount)
    sc = _sc(state)
    margin = (sc.order_value - amount) / sc.order_value * HUNDRED
    _log(
        state,
        _offset(state),
        "accounts",
        f"Team budget raised to {lakh(amount)} (margin {pct(margin)})",
    )
    counter = state.get("counter")
    tail = (
        f" Say **agree** to give {_first(int(counter['who']))} {lakh(Decimal(counter['ask']))}."
        if counter
        else ""
    )
    _reply(
        state,
        _offset(state),
        f"Budget raised to **{inr(amount)}** with your approval and logged. The account now "
        f"plans for a **{pct(margin)}** margin, under its {pct(sc.margin_floor)} floor.{tail}",
        systems=("accounts",),
    )


# ── Chapter 9-10: hire, laptops and IT ───────────────────────────────────────


def _hire(state: dict[str, Any]) -> None:
    sc = _sc(state)
    joining = date.fromisoformat(state["joining"])
    offers = _offered(state)
    rows = [
        [_POOL[i].name, inr(v), _label(joining), f"EMP{1041 + n}"]
        for n, (i, v) in enumerate(sorted(offers.items(), key=lambda kv: _ranked().index(kv[0])))
    ]
    proposal = _proposal(
        state,
        "hire",
        title=f"Hire {sc.headcount}: add them as joiners",
        figures=[
            _figure("Joining", _label(joining)),
            _figure("Joiners", str(len(offers))),
            _figure("Team, a year", inr(sum(offers.values(), ZERO))),
            _figure("Onboarding", "Checklist, documents, letters"),
        ],
        table={"columns": ["Joiner", "CTC", "Joining", "Code"], "rows": rows},
        notes=[
            "AnytimeHire marks them Hired and closes the job.",
            "Each joiner gets the pre-joining form and the document list by email.",
        ],
        confirm="Hire and add joiners",
        question="Add them as joiners?",
    )
    _reply(
        state,
        6,
        "Ready to hire. This adds them to the HRMS and starts onboarding.",
        systems=("hrms",),
        cards=[proposal],
    )


def _hired(state: dict[str, Any]) -> None:
    sc = _sc(state)
    joining = date.fromisoformat(state["joining"])
    _close_pending(state, "done", "Joiners added.")
    _finish(state, "hire")
    _log(state, 6, "hrms", f"{sc.headcount} joiners added for {_label(joining)}")
    _log(state, 6, "anytimehire", "Candidates marked Hired; job closed")
    _reply(
        state,
        6,
        (
            f"Done. **{sc.headcount} joiners** are in the HRMS for **{_label(joining)}**: "
            f"employee codes, "
            f"pre-joining forms and the onboarding checklist. AnytimeHire marked them Hired and "
            f"closed the job."
        ),
        systems=("hrms", "anytimehire"),
    )


def _quotes(state: dict[str, Any]) -> list[tuple[str, Decimal, str]]:
    sc = _sc(state)
    return [
        (name, (sc.laptop_price * factor / 100).quantize(ONE, rounding=ROUND_HALF_UP) * 100, days)
        for name, factor, days in _VENDORS
    ]


def _laptops(state: dict[str, Any]) -> None:
    sc = _sc(state)
    quotes = _quotes(state)
    vendor, price, _days = quotes[0]
    total = price * sc.headcount
    joining = date.fromisoformat(state["joining"])
    proposal = _proposal(
        state,
        "laptops",
        title=f"Laptops for {sc.headcount} joiners",
        figures=[
            _figure("Item", _LAPTOP),
            _figure("Quantity", str(sc.headcount)),
            _figure("Lowest quote", f"{inr(price)} · {vendor}"),
            _figure("Total", inr(total)),
            _figure("Needed by", _label(joining - timedelta(days=3))),
        ],
        table={
            "columns": ["Vendor", "Each", "Delivery", "Total"],
            "rows": [
                [name, inr(each), days, inr(each * sc.headcount)] for name, each, days in quotes
            ],
        },
        notes=[
            "ProcureFlow raises the purchase request, sends it for approval and the PO to the "
            "lowest quote.",
            "CGB Helpdesk gets a day-one IT ticket per joiner: email, VPN, laptop set-up.",
        ],
        confirm="Raise the purchase",
        question="Raise the purchase?",
    )
    _reply(
        state,
        6,
        f"Three quotes came back in **ProcureFlow**. The lowest is {vendor}.",
        systems=("procureflow",),
        cards=[proposal],
    )


def _laptops_ordered(state: dict[str, Any]) -> None:
    sc = _sc(state)
    vendor, price, _days = _quotes(state)[0]
    total = price * sc.headcount
    joining = date.fromisoformat(state["joining"])
    po = f"PO-{_day(state, 6):%y%m}-031"
    state["laptops"] = {"vendor": vendor, "unit": _money(price), "total": _money(total), "po": po}
    _close_pending(state, "done", f"{po} raised to {vendor}.")
    _finish(state, "laptops")
    _log(state, 6, "procureflow", f"{po}: {sc.headcount} laptops, {inr(total)} to {vendor}")
    _log(
        state,
        6,
        "helpdesk",
        f"{sc.headcount} day-one IT tickets, due {_label(joining - timedelta(days=1))}",
    )
    tickets = [
        [
            f"HD-{_day(state, 6):%y%m}-{101 + n}",
            _POOL[i].name,
            "Email, VPN, laptop set-up",
            _label(joining - timedelta(days=1)),
        ]
        for n, i in enumerate(sorted(_offered(state), key=_ranked().index))
    ]
    _reply(
        state,
        6,
        (
            f"Laptops requested in **ProcureFlow** and approved: **{po}** to {vendor}, "
            f"{inr(total)}, "
            f"delivery before {_label(joining - timedelta(days=3))}.\n\n"
            f"IT set-up is lined up in **CGB Helpdesk**: {sc.headcount} tickets, due the day "
            f"before joining."
        ),
        systems=("procureflow", "helpdesk"),
        cards=[
            {
                "type": "table",
                "title": "Day-one IT tickets",
                "caption": "CGB Helpdesk · sample tickets",
                "columns": ["Ticket", "For", "Tasks", "Due"],
                "rows": tickets,
                "source": "CGB Helpdesk",
            }
        ],
    )


# ── Chapter 11-12: travel, P&L and cash ──────────────────────────────────────


def _travel(state: dict[str, Any]) -> None:
    sc = _sc(state)
    if _done(state, "travel") and state["claims_approved"]:
        _travel_table(state, again=True)
        return
    again = _done(state, "travel")
    _finish(state, "travel")
    pending = [c for c in _CLAIMS if not c[4]]
    total = sum((c[3] for c in _CLAIMS), ZERO)
    if not again:
        _log(
            state,
            6,
            "expensegenie",
            f"Kick-off travel: {inr(total)}, {len(pending)} claims pending",
        )
    proposal = _proposal(
        state,
        "claims",
        title=f"Approve {len(pending)} claims",
        figures=[
            _figure("Claims", str(len(pending))),
            _figure("Amount", inr(sum((c[3] for c in pending), ZERO))),
            _figure("Charged to", f"{sc.client} account"),
        ],
        table={
            "columns": ["Person", "Claim", "Amount"],
            "rows": [[c[0], c[2], inr(c[3])] for c in pending],
        },
        notes=["Within policy: bills attached, under the grade's cap."],
        confirm="Approve claims",
        question="Approve them?",
    )
    _travel_table(state, again=False, extra=[proposal])


def _travel_table(
    state: dict[str, Any], *, again: bool, extra: list[dict[str, Any]] | None = None
) -> None:
    sc = _sc(state)
    people: dict[str, dict[str, Any]] = {}
    for name, role, _what, amount, approved in _CLAIMS:
        row = people.setdefault(name, {"role": role, "claims": 0, "amount": ZERO, "pending": 0})
        row["claims"] += 1
        row["amount"] += amount
        if not approved and not state["claims_approved"]:
            row["pending"] += 1
    total = sum((c[3] for c in _CLAIMS), ZERO)
    _reply(
        state,
        6,
        (
            f"Travel this month for the {sc.client} kick-off, by person, from **Expense Genie**: "
            f"**{inr(total)}** in all."
            + ("" if again or state["claims_approved"] else " A few claims need your approval.")
        ),
        systems=("expensegenie",),
        cards=[
            {
                "type": "table",
                "title": "Travel by person",
                "caption": "Expense Genie · sample claims",
                "columns": ["Person", "Role", "Claims", "Amount", "Waiting for you"],
                "rows": [
                    [name, row["role"], row["claims"], inr(row["amount"]), row["pending"] or "—"]
                    for name, row in people.items()
                ],
                "source": "Expense Genie",
            },
            *(extra or []),
        ],
    )


def _claims_approved(state: dict[str, Any]) -> None:
    sc = _sc(state)
    pending = [c for c in _CLAIMS if not c[4]]
    amount = sum((c[3] for c in pending), ZERO)
    _close_pending(state, "done", "Approved.")
    state["claims_approved"] = True
    _log(state, 6, "expensegenie", f"{len(pending)} claims approved, {inr(amount)}")
    _reply(
        state,
        6,
        f"Approved: {len(pending)} claims, **{inr(amount)}**, charged to the {sc.client} account.",
        systems=("expensegenie", "accounts"),
    )


def _cash_chapter(state: dict[str, Any]) -> None:
    sc = _sc(state)
    _finish(state, "cash")
    cash = _cash(state)
    pnl = _pnl(state)
    lowest = Decimal(cash["lowest"])
    final = Decimal(cash["final"])
    basis = (
        "the accepted offers" if cash["basis"] == "offers" else "the team budget (no offers yet)"
    )
    if lowest < 0:
        first = (
            f"Cash dips to **{lakh(lowest)}** in {cash['lowest_at']}: salaries"
            + (" and laptops" if _laptops_total(state) else "")
            + f" go out before {_whose(sc.client)} first payment, {sc.payment_days} days after "
            f"each "
            f"invoice. "
            + (
                f"It turns positive in **{cash['positive_from']}**"
                if cash["positive_from"]
                else "It stays below zero"
            )
            + f" and ends at **{lakh(final)}**."
        )
    else:
        first = f"Cash never goes below zero, and the account ends at **{lakh(final)}**."
    verdict = "Yes, over the contract." if final >= 0 else "No: this account loses money."
    _log(state, 6, "accounts", f"Account net {lakh(Decimal(pnl['net']))} ({pnl['net_pct']}%)")
    _reply(
        state,
        6,
        (
            f"{verdict} {first}\n\n"
            f"Worked out from {basis}, {_whose(sc.client)} payment terms"
            + (", the laptops" if _laptops_total(state) else "")
            + (" and the travel" if _travel_total(state) else "")
            + "."
        ),
        systems=("accounts",),
        cards=[
            {
                "type": "chart",
                "title": "Cash to date",
                "caption": f"{sc.client} account · month by month from joining",
                "chart": "area",
                "unit": "inr",
                "x": [row["label"] for row in cash["months"]],
                "series": [
                    {
                        "name": "Cash to date",
                        "values": [row["cumulative"] for row in cash["months"]],
                    },
                ],
            },
            {
                "type": "record",
                "title": f"{sc.client}: account P&L",
                "subtitle": "Over the contract",
                "heading": "P&L",
                "figures": [
                    _figure("Revenue", inr(Decimal(pnl["revenue"]))),
                    _figure("Team", inr(Decimal(pnl["team"]))),
                    _figure("Laptops", inr(Decimal(pnl["laptops"]))),
                    _figure("Travel", inr(Decimal(pnl["travel"]))),
                    _figure("Net", inr(Decimal(pnl["net"]))),
                    _figure("Net margin", f"{pnl['net_pct']}%"),
                    _figure("Margin on the team", f"{pnl['gross_pct']}%"),
                ],
                "note": (
                    "Laptops stay company assets after the contract; they are counted here in full."
                ),
            },
        ],
    )
    if _next_chapter(state) is None:
        _reply(
            state,
            6,
            (
                "That's the whole order: signed, staffed inside its margin, equipped and paid for, "
                "across six systems in one chat. Every step asked for approval and is logged.\n\n"
                "Want to see it with your own numbers? Book a live demo with us."
            ),
            systems=(),
        )


# ── Answers outside the chapters ─────────────────────────────────────────────


def _recap(state: dict[str, Any], key: str) -> None:
    following = _next_chapter(state)
    nxt = (
        f" Next: **{_BY_KEY[following].title.lower()}**. {_prompt(state, following)}"
        if following
        else ""
    )
    _reply(state, _offset(state), f"**{_BY_KEY[key].title}** is done.{nxt}", systems=())


def _status(state: dict[str, Any]) -> None:
    sc = _sc(state)
    annual = _team_annual(state)
    team = _contract(state, annual) if annual is not None else None
    budget = _budget(state)
    figures = [
        _figure("Order", inr(sc.order_value)),
        _figure("Team budget", inr(budget)),
        _figure("Committed", inr(team) if team is not None else "No offers yet"),
        _figure("Headroom", inr(budget - team) if team is not None else inr(budget)),
        _figure("Margin", pct(_margin(state, team if team is not None else budget))),
        _figure("Floor", pct(sc.margin_floor)),
    ]
    _reply(
        state,
        _offset(state),
        "Where the account stands right now:",
        systems=("accounts",),
        cards=[
            {
                "type": "record",
                "title": f"{sc.client}: budget and margin",
                "subtitle": "Live for this story",
                "heading": "Figures",
                "figures": figures,
                "note": "",
            }
        ],
    )


def _help(state: dict[str, Any], *, unsure: bool = False) -> None:
    following = _next_chapter(state)
    head = (
        "I'm running one order in this showcase, so I can't look that up here. "
        if unsure
        else "This showcase runs one order from signature to day one. "
    )
    if state.get("counter"):
        counter = state["counter"]
        tail = (
            f"{_first(int(counter['who']))} is waiting for an answer: say **agree**, "
            f"**offer {_first(int(counter['who']))}** and an amount, **hold**, or **swap**."
        )
    elif state.get("pending"):
        tail = "The offer above is waiting: say **yes** or **cancel**."
    elif following:
        tail = f"Next: {_prompt(state, following)}"
    else:
        tail = "The story is complete. Start over to run it again."
    _reply(state, _offset(state), head + tail, systems=())


def _prompt(state: dict[str, Any], key: str) -> str:
    """What the visitor could say for this chapter: the chip's text."""
    sc = _sc(state)
    if key == "order":
        return (
            f"{sc.client} just signed: {sc.headcount} {_plural(sc.role, sc.headcount)} for "
            f"{sc.months} months, {lakh(sc.order_value)}. Book the order and open the roles."
        )
    if key == "negotiate" and state.get("counter"):
        counter = state["counter"]
        who = int(counter["who"])
        figures = _counter_figures(state, who, Decimal(counter["ask"]))
        most = min(figures["most"], Decimal(counter["ask"]))
        return f"Offer {_first(who)} {lakh(most)}."
    return {
        "job": "Yes, approve the requisition.",
        "match": "Find the best matches in our résumé database.",
        "invite": f"Invite the top {len(_POOL)} to an AI interview.",
        "interview": "How did the interviews go?",
        "shortlist": f"Shortlist the top {sc.headcount + 2}.",
        "offer": "Make offers within the budget.",
        "negotiate": "Make offers within the budget.",
        "hire": "Everyone accepted. Hire them.",
        "laptops": "Have laptops ready for day one.",
        "travel": "What did the team spend on travel this month?",
        "cash": "Is this account cash positive?",
    }[key]


# ── What the page shows ──────────────────────────────────────────────────────


def _suggestions(state: dict[str, Any]) -> list[str]:
    if state.get("pending"):
        return ["Yes", "Cancel"]
    counter = state.get("counter")
    if counter:
        who, ask = int(counter["who"]), Decimal(counter["ask"])
        first = _first(who)
        figures = _counter_figures(state, who, ask)
        most = min(figures["most"], ask)
        options = [f"Offer {first} {lakh(most)}."]
        if most < ask:
            options.append(f"Agree to {lakh(ask)}.")
            options.append(f"Raise the budget to {lakh(figures['team'])}.")
        else:
            options.append(f"Agree to {lakh(ask)}.")
        options.append(f"Hold at {lakh(_offered(state)[who])}.")
        if _backup(state) is not None:
            options.append(f"Swap {first} for the back-up.")
        return options
    following = _next_chapter(state)
    return [_prompt(state, following)] if following else []


def _autoplay(state: dict[str, Any]) -> dict[str, Any] | None:
    """The step a booth screen takes next when it plays the story by itself."""
    pending = state.get("pending")
    if pending:
        return {"decide": pending["id"], "confirm": True}
    following = _next_chapter(state)
    if following is None:
        return None
    return {"say": _prompt(state, following)}


def _funnel(state: dict[str, Any]) -> list[dict[str, Any]]:
    sc = _sc(state)
    interviewed = len(_ranked())

    def at(key: str, count: int) -> int | None:
        return count if _done(state, key) else None

    return [
        {"key": "matched", "label": "Matched", "count": at("match", len(_POOL))},
        {"key": "invited", "label": "Invited", "count": at("invite", len(_POOL))},
        {"key": "interviewed", "label": "Interviewed", "count": at("interview", interviewed)},
        {
            "key": "shortlisted",
            "label": "Shortlisted",
            "count": at("shortlist", len(state["shortlist"])),
        },
        {"key": "offered", "label": "Offered", "count": at("offer", sc.headcount)},
        {
            "key": "accepted",
            "label": "Accepted",
            # While one offer is still being negotiated, the others have already said yes.
            "count": (
                sc.headcount
                if _done(state, "negotiate")
                else (len(state["accepted"]) if _done(state, "offer") else None)
            ),
        },
        {"key": "hired", "label": "Hired", "count": at("hire", sc.headcount)},
    ]


def view(state: dict[str, Any]) -> dict[str, Any]:
    """Everything the showcase page draws: the chat, the chapters and the live board."""
    sc = _sc(state)
    following = _next_chapter(state)
    annual = _team_annual(state)
    team = _contract(state, annual) if annual is not None else None
    budget = _budget(state)
    margin_now = _margin(state, team if team is not None else budget)
    joining = date.fromisoformat(state["joining"])
    laptops = state.get("laptops")
    claims_pending = [c for c in _CLAIMS if not c[4]]
    chapters = [
        {
            "key": chapter.key,
            "title": chapter.title,
            "blurb": chapter.blurb,
            "systems": list(chapter.systems),
            "state": "done"
            if chapter.key in state["done"]
            else ("current" if chapter.key == following else "next"),
            "prompt": _prompt(state, chapter.key),
        }
        for chapter in CHAPTERS
    ]
    return {
        "scenario": {
            "company": sc.company,
            "client": sc.client,
            "role": sc.role,
            "headcount": sc.headcount,
            "months": sc.months,
            "order_value": _money(sc.order_value),
            "team_budget": _money(sc.team_budget),
            "payment_days": sc.payment_days,
        },
        "systems": SYSTEMS,
        "chapters": chapters,
        "messages": state["messages"],
        "suggestions": _suggestions(state),
        "autoplay": _autoplay(state),
        "finished": following is None,
        "board": {
            "order": {
                "booked": _done(state, "order"),
                "po": _po(state),
                "value": _money(sc.order_value),
            },
            "budget": {
                "total": _money(budget),
                "original": _money(sc.team_budget),
                "raised": state.get("budget") is not None,
                "committed": _money(team) if team is not None else None,
                "headroom": _money(budget - team) if team is not None else None,
                "per_head": _money(sc.cap),
            },
            "margin": {
                "floor": f"{sc.margin_floor.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}",
                "now": f"{margin_now.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}",
                "basis": "offers" if team is not None else "budget",
            },
            "funnel": _funnel(state),
            "joining": joining.isoformat(),
            "equipment": {
                "ordered": laptops is not None,
                "count": sc.headcount if laptops else 0,
                "total": laptops["total"] if laptops else None,
                "vendor": laptops["vendor"] if laptops else None,
                "po": laptops["po"] if laptops else None,
                "tickets": sc.headcount if laptops else 0,
            },
            "travel": {
                "shown": _done(state, "travel"),
                "total": _money(_travel_total(state)),
                "pending": 0
                if state["claims_approved"] or not _done(state, "travel")
                else len(claims_pending),
            },
            "pnl": _pnl(state),
            "cash": _cash(state),
            "activity": list(reversed(state["activity"]))[:40],
        },
    }
