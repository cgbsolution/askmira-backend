"""Salary structure templates: a CTC split by rules, the way HR writes one down.

A template is a salary structure whose wage types each carry one rule:

- ``percent``: a percentage of the CTC, of the gross, of PF or ESI wages, or of
  another line of the template (HRA 40% of Basic);
- ``fixed``: an amount a month or a year;
- ``balance``: whatever the CTC leaves (exactly one line, an earning);
- ``slab``: an amount by slab of a monthly figure (Professional Tax by gross).

A rule may cap its base (employer PF on PF wages up to ₹15,000) and may apply
only while a monthly figure is up to, or above, an amount (ESI only while ESI
wages are up to ₹21,000; a tax only above an amount).

Employer contributions that payroll works out at run time (employer PF, employer
ESI) may be kept *inside* the CTC: they are set aside before the balance is
taken, as the HRMS HR moved from did (CTC 6,20,000 = gross 5,81,688 + gratuity
14,904 + employer PF 23,400 and change). They are not wage types; payroll still
works out what is actually contributed each month.

Arithmetic, as that HRMS does it, so a person's figures match the old system:

1. Everything is worked out for a month. The year is twelve months.
2. Each line is rounded once, to the structure's precision (whole rupees unless
   the structure keeps paise), the way its rule says (nearest, up or down).
3. A percentage of another line uses that line's *rounded* amount.
4. The balance is what the CTC leaves after every other line inside it and the
   employer contributions kept inside it, rounded *down*, so the lines never add
   up to more than the CTC. The paise left over are reported, not hidden.

A line that depends on the gross, on PF or ESI wages, or on the balance cannot
be worked out before the balance is known, and the balance depends on it. Such a
template is solved by repetition: the lines are worked out, the balance taken,
and the round repeated with the new gross until nothing moves. Rounding makes
every round land on whole amounts, so it settles in a few rounds; a CTC that
sits exactly on a limit, flipping a line on and off forever, is refused with a
message rather than settled arbitrarily.

No float anywhere. Money is ``Decimal`` from the request to the response.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.models.payroll import ComponentType, WageGroup

__all__ = [
    "AGGREGATES",
    "EMPLOYER_ROWS",
    "Condition",
    "EmployerRow",
    "Line",
    "LineResult",
    "Method",
    "Result",
    "Rule",
    "Slab",
    "Template",
    "TemplateError",
    "calculate",
    "describe",
    "parse_rule",
    "rule_json",
    "validate",
]

ZERO = Decimal(0)
HUNDRED = Decimal(100)
MONTHS = Decimal(12)

CTC = "CTC"
GROSS = "GROSS"
PF_WAGES = "PF_WAGES"
ESI_WAGES = "ESI_WAGES"
#: Figures that are sums of the lines, known only once the balance is.
AGGREGATES = (GROSS, PF_WAGES, ESI_WAGES)
#: Names a rule may be "of" besides another line's code.
RESERVED = frozenset({CTC, *AGGREGATES})

BASE_LABEL = {
    CTC: "CTC",
    GROSS: "gross",
    PF_WAGES: "PF wages",
    ESI_WAGES: "ESI wages",
}

#: Employer contributions payroll works out, which a template may keep inside the CTC.
EMPLOYER_PF = "EMPLOYER_PF"
EMPLOYER_ESI = "EMPLOYER_ESI"
EMPLOYER_ROWS: dict[str, str] = {
    EMPLOYER_PF: "EMPLOYER PF CONTRIBUTION",
    EMPLOYER_ESI: "EMPLOYER ESIC CONTRIBUTION",
}

#: Biggest percentage a rule may take. 100% of a line is a copy; more is a typo.
MAX_PERCENT = HUNDRED
#: Largest amount anywhere in a rule: ₹10 crore, as on the Salary master.
MAX_AMOUNT = Decimal("100000000")
MAX_SLABS = 20
#: Rounds before a template that keeps moving is called unsettled.
MAX_ROUNDS = 40


class TemplateError(ValueError):
    """A template cannot be saved or cannot split this CTC; the message says why."""


class Method(str, enum.Enum):
    PERCENT = "percent"
    FIXED = "fixed"
    BALANCE = "balance"
    SLAB = "slab"


_ROUNDING = {"nearest": ROUND_HALF_UP, "up": ROUND_CEILING, "down": ROUND_FLOOR}


@dataclass(frozen=True, slots=True)
class Slab:
    #: The slab covers a monthly figure up to this amount; None for the last one.
    up_to: Decimal | None
    #: What the slab charges a month.
    amount: Decimal


@dataclass(frozen=True, slots=True)
class Condition:
    """A rule applies only while a monthly figure is up to / above an amount."""

    on: str
    op: str  # "up_to" | "above"
    amount: Decimal


@dataclass(frozen=True, slots=True)
class Rule:
    method: Method
    value: Decimal | None = None
    #: What a percentage, or a slab, is of. Slabs default to the gross.
    of: str | None = None
    #: A fixed amount's period.
    basis: str = "monthly"
    #: A percentage's base is taken at most up to this, a month.
    ceiling: Decimal | None = None
    when: Condition | None = None
    slabs: tuple[Slab, ...] = ()
    rounding: str = "nearest"

    def references(self) -> set[str]:
        """Codes and figures this rule reads."""
        out: set[str] = set()
        if self.method in (Method.PERCENT, Method.SLAB):
            out.add(self.of or GROSS)
        if self.when is not None:
            out.add(self.when.on)
        return out


@dataclass(frozen=True, slots=True)
class Line:
    """A wage type of the template, with its rule."""

    code: str
    name: str
    component_type: ComponentType
    group: WageGroup
    rule: Rule
    part_of_ctc: bool = True
    counts_toward_pf: bool = False
    counts_toward_esi: bool = False
    display_order: int = 0


@dataclass(frozen=True, slots=True)
class EmployerRow:
    """An employer contribution payroll works out, set aside inside the CTC."""

    key: str
    name: str
    rule: Rule


@dataclass(frozen=True, slots=True)
class Template:
    lines: tuple[Line, ...]
    employer: tuple[EmployerRow, ...] = ()
    #: Decimal places every line is rounded to: 0 (whole rupees) or 2 (paise).
    precision: int = 0


@dataclass(frozen=True, slots=True)
class LineResult:
    code: str
    name: str
    monthly: Decimal
    #: False when the rule's condition kept it off for this CTC.
    applied: bool = True
    note: str | None = None


@dataclass(slots=True)
class Result:
    ctc_monthly: Decimal
    lines: list[LineResult] = field(default_factory=list)
    employer: list[LineResult] = field(default_factory=list)
    gross_monthly: Decimal = ZERO
    pf_wages: Decimal = ZERO
    esi_wages: Decimal = ZERO
    #: The lines inside the CTC plus the employer contributions kept in it.
    allocated_monthly: Decimal = ZERO
    #: What rounding left of the CTC, a month (never negative).
    unallocated_monthly: Decimal = ZERO
    rounds: int = 0

    def by_code(self, code: str) -> LineResult | None:
        return next((line for line in self.lines if line.code == code), None)


# ── Reading and writing rules ───────────────────────────────────────────────


def _decimal(raw: Any, what: str) -> Decimal:
    if isinstance(raw, float):
        # JSON numbers are read as strings by the schema; a float here would
        # already have lost the exact figure HR typed.
        raw = str(raw)
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError) as exc:
        raise TemplateError(f"{what} '{raw}' is not a number") from exc
    if not value.is_finite():
        raise TemplateError(f"{what} '{raw}' is not a number")
    return value


def _amount(raw: Any, what: str) -> Decimal:
    value = _decimal(raw, what)
    if value < 0:
        raise TemplateError(f"{what} cannot be negative")
    if value > MAX_AMOUNT:
        raise TemplateError(f"{what} is more than ₹10 crore")
    if value != value.quantize(Decimal("0.01")):
        raise TemplateError(f"{what} {value} has more than two decimals")
    return value


def parse_rule(raw: Mapping[str, Any], where: str) -> Rule:
    """A rule from its stored or posted form, checked on its own.

    `where` names the line in messages ("BASIC SALARY"). Whether the codes it
    reads exist is checked by `validate`, which sees the whole template.
    """
    try:
        method = Method(str(raw.get("method", "")))
    except ValueError as exc:
        raise TemplateError(f"{where}: pick how it is worked out") from exc

    of = raw.get("of")
    of = str(of).strip().upper() if of not in (None, "") else None
    rounding = str(raw.get("rounding") or "nearest")
    if rounding not in _ROUNDING:
        raise TemplateError(f"{where}: rounding is nearest, up or down")

    value: Decimal | None = None
    basis = str(raw.get("basis") or "monthly")
    slabs: tuple[Slab, ...] = ()
    if method is Method.PERCENT:
        if raw.get("value") in (None, ""):
            raise TemplateError(f"{where}: enter the percentage")
        value = _decimal(raw["value"], f"{where}: the percentage")
        if value < 0 or value > MAX_PERCENT:
            raise TemplateError(f"{where}: the percentage must be between 0 and 100")
        if value != value.quantize(Decimal("0.0001")):
            raise TemplateError(f"{where}: the percentage has more than four decimals")
        if not of:
            raise TemplateError(f"{where}: pick what the percentage is of")
    elif method is Method.FIXED:
        if raw.get("value") in (None, ""):
            raise TemplateError(f"{where}: enter the amount")
        value = _amount(raw["value"], f"{where}: the amount")
        if basis not in ("monthly", "annual"):
            raise TemplateError(f"{where}: a fixed amount is a month's or a year's")
    elif method is Method.SLAB:
        raw_slabs = raw.get("slabs") or []
        if not isinstance(raw_slabs, Sequence) or isinstance(raw_slabs, str) or not raw_slabs:
            raise TemplateError(f"{where}: add at least one slab")
        if len(raw_slabs) > MAX_SLABS:
            raise TemplateError(f"{where}: at most {MAX_SLABS} slabs")
        parsed: list[Slab] = []
        for number, item in enumerate(raw_slabs, start=1):
            if not isinstance(item, Mapping):
                raise TemplateError(f"{where}: slab {number} is not readable")
            upper = item.get("up_to")
            up_to = (
                None if upper in (None, "") else _amount(upper, f"{where}: slab {number}'s limit")
            )
            if item.get("amount") in (None, ""):
                raise TemplateError(f"{where}: enter slab {number}'s amount")
            parsed.append(Slab(up_to, _amount(item["amount"], f"{where}: slab {number}'s amount")))
        for number, slab in enumerate(parsed, start=1):
            last = number == len(parsed)
            if slab.up_to is None and not last:
                raise TemplateError(
                    f"{where}: only the last slab is open-ended (slab {number} has no limit)"
                )
            if slab.up_to is not None and last:
                raise TemplateError(
                    f"{where}: the last slab covers everything above the one before it; "
                    "leave its limit empty"
                )
            if number > 1 and slab.up_to is not None:
                before = parsed[number - 2].up_to
                if before is not None and slab.up_to <= before:
                    raise TemplateError(
                        f"{where}: slab limits must go up (slab {number} is not above slab "
                        f"{number - 1})"
                    )
        slabs = tuple(parsed)
        of = of or GROSS

    ceiling = None
    if raw.get("ceiling") not in (None, ""):
        if method is not Method.PERCENT:
            raise TemplateError(f"{where}: only a percentage takes an upper limit on its base")
        ceiling = _amount(raw["ceiling"], f"{where}: the limit")

    when = None
    raw_when = raw.get("when")
    if raw_when:
        if method is Method.BALANCE:
            raise TemplateError(
                f"{where}: the balance always takes what is left; it has no condition"
            )
        if not isinstance(raw_when, Mapping):
            raise TemplateError(f"{where}: the condition is not readable")
        on = str(raw_when.get("on") or GROSS).strip().upper()
        op = str(raw_when.get("op") or "")
        if op not in ("up_to", "above"):
            raise TemplateError(f"{where}: the condition is 'up to' or 'above' an amount")
        if raw_when.get("amount") in (None, ""):
            raise TemplateError(f"{where}: enter the condition's amount")
        when = Condition(on, op, _amount(raw_when["amount"], f"{where}: the condition's amount"))

    return Rule(
        method=method,
        value=value,
        of=of if method in (Method.PERCENT, Method.SLAB) else None,
        basis=basis if method is Method.FIXED else "monthly",
        ceiling=ceiling,
        when=when,
        slabs=slabs,
        rounding=rounding,
    )


def _plain(value: Decimal | None) -> str | None:
    """A decimal as stored: no exponent, no needless trailing zeros."""
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return text


def rule_json(rule: Rule) -> dict[str, Any]:
    """A rule as stored on the structure line (JSONB), amounts as strings."""
    out: dict[str, Any] = {"method": rule.method.value}
    if rule.value is not None:
        out["value"] = _plain(rule.value)
    if rule.of is not None:
        out["of"] = rule.of
    if rule.method is Method.FIXED:
        out["basis"] = rule.basis
    if rule.ceiling is not None:
        out["ceiling"] = _plain(rule.ceiling)
    if rule.when is not None:
        out["when"] = {"on": rule.when.on, "op": rule.when.op, "amount": _plain(rule.when.amount)}
    if rule.slabs:
        out["slabs"] = [
            {"up_to": _plain(slab.up_to), "amount": _plain(slab.amount)} for slab in rule.slabs
        ]
    if rule.rounding != "nearest":
        out["rounding"] = rule.rounding
    return out


# ── Words ───────────────────────────────────────────────────────────────────


def _rupees(amount: Decimal) -> str:
    """₹15,000 / ₹1,600.50, grouped the Indian way."""
    whole, _, paise = f"{abs(amount):.2f}".partition(".")
    head, tail = whole[:-3], whole[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    text = ",".join([*groups, tail]) if groups else tail
    return f"{'-' if amount < 0 else ''}₹{text}" + ("" if paise == "00" else f".{paise}")


def _percent(value: Decimal) -> str:
    return f"{_plain(value)}%"


def _base_name(of: str, names: Mapping[str, str]) -> str:
    return BASE_LABEL.get(of) or names.get(of, of)


def describe(rule: Rule, names: Mapping[str, str]) -> str:
    """The rule in a line of plain words, for the grid and the audit trail."""
    if rule.method is Method.BALANCE:
        text = "Balance of CTC"
    elif rule.method is Method.FIXED:
        assert rule.value is not None
        text = f"{_rupees(rule.value)} a {'year' if rule.basis == 'annual' else 'month'}"
    elif rule.method is Method.PERCENT:
        assert rule.value is not None and rule.of is not None
        text = f"{_percent(rule.value)} of {_base_name(rule.of, names)}"
        if rule.ceiling is not None:
            text += f" up to {_rupees(rule.ceiling)}"
    else:
        parts = []
        lower: Decimal | None = None
        for slab in rule.slabs:
            if slab.up_to is None:
                span = f"above {_rupees(lower)}" if lower is not None else "any amount"
            else:
                span = f"up to {_rupees(slab.up_to)}"
            parts.append(f"{span}: {_rupees(slab.amount)}")
            lower = slab.up_to
        text = f"By {_base_name(rule.of or GROSS, names)} a month — " + "; ".join(parts)
    if rule.when is not None:
        word = "up to" if rule.when.op == "up_to" else "above"
        base = _base_name(rule.when.on, names)
        verb = "are" if base.endswith("wages") else "is"
        text += f", only while {base} {verb} {word} {_rupees(rule.when.amount)} a month"
    if rule.rounding == "up":
        text += ", rounded up"
    elif rule.rounding == "down":
        text += ", rounded down"
    return text


# ── Validation ──────────────────────────────────────────────────────────────


def validate(template: Template) -> None:
    """Refuse a template that cannot split a CTC, with the line to fix named.

    Raises:
        TemplateError: no balance line or more than one, a balance that is not an
            earning inside the CTC, a rule that reads a line not in the template,
            a line that reads itself, or lines that read each other in a circle
            (other than through the balance, which is solved by repetition).
    """
    if not template.lines:
        raise TemplateError("Add at least one wage type to the structure")
    if template.precision not in (0, 2):
        raise TemplateError("Round to whole rupees or to paise")

    codes = [line.code for line in template.lines]
    repeated = sorted({code for code in codes if codes.count(code) > 1})
    if repeated:
        raise TemplateError(f"{', '.join(repeated)} is in the structure more than once")
    clash = sorted(set(codes) & RESERVED)
    if clash:
        raise TemplateError(f"{', '.join(clash)} is a reserved name; rename that wage type")

    balances = [line for line in template.lines if line.rule.method is Method.BALANCE]
    if not balances:
        raise TemplateError(
            "Pick one wage type to take the balance of the CTC (usually SPECIAL ALLOWANCE), "
            "so the structure adds up to the CTC"
        )
    if len(balances) > 1:
        raise TemplateError(
            "Only one wage type can take the balance of the CTC; "
            f"{', '.join(line.name for line in balances)} all do"
        )
    balance = balances[0]
    if balance.component_type is not ComponentType.EARNING or not balance.part_of_ctc:
        raise TemplateError(
            f"{balance.name} cannot take the balance: the balance is an earning inside the CTC"
        )

    for row in template.employer:
        if row.rule.method is Method.BALANCE:
            raise TemplateError(f"{row.name}: an employer contribution cannot be the balance")
    known = set(codes) | RESERVED
    for owner, rule in [(line.name, line.rule) for line in template.lines] + [
        (row.name, row.rule) for row in template.employer
    ]:
        for ref in rule.references():
            if ref not in known:
                raise TemplateError(
                    f"{owner} is worked out from {ref}, which is not in this structure. "
                    "Add it, or pick another base."
                )
    for line in template.lines:
        if line.code in line.rule.references():
            raise TemplateError(f"{line.name} cannot be worked out from itself")

    _order(template.lines)  # raises on a circle


def _order(lines: Sequence[Line]) -> list[Line]:
    """Non-balance lines in an order where each comes after the lines it reads.

    The balance and the sums (gross, PF/ESI wages) are not ordered: they are
    taken from the previous round, which is what repetition is for.
    """
    by_code = {line.code: line for line in lines if line.rule.method is not Method.BALANCE}
    ordered: list[Line] = []
    done: set[str] = set()
    path: list[str] = []

    def visit(code: str) -> None:
        if code in done:
            return
        if code in path:
            cycle = [*path[path.index(code) :], code]
            raise TemplateError(
                "These wage types are worked out from each other in a circle: "
                + " → ".join(by_code[c].name for c in cycle)
            )
        path.append(code)
        for ref in sorted(by_code[code].rule.references()):
            if ref in by_code:
                visit(ref)
        path.pop()
        done.add(code)
        ordered.append(by_code[code])

    for line in sorted(by_code.values(), key=lambda line: (line.display_order, line.code)):
        visit(line.code)
    return ordered


# ── Calculation ─────────────────────────────────────────────────────────────


def _quantum(precision: int) -> Decimal:
    return Decimal(1).scaleb(-precision)


def _figure(
    of: str, ctc: Decimal, values: Mapping[str, Decimal], sums: Mapping[str, Decimal]
) -> Decimal:
    if of == CTC:
        return ctc
    if of in sums:
        return sums[of]
    return values.get(of, ZERO)


def _evaluate(
    rule: Rule,
    *,
    ctc: Decimal,
    values: Mapping[str, Decimal],
    sums: Mapping[str, Decimal],
    quantum: Decimal,
    names: Mapping[str, str],
) -> tuple[Decimal, bool, str | None]:
    """(amount a month, applied, why not applied) for one rule this round."""
    if rule.when is not None:
        test = _figure(rule.when.on, ctc, values, sums)
        within = test <= rule.when.amount if rule.when.op == "up_to" else test > rule.when.amount
        if not within:
            word = "above" if rule.when.op == "up_to" else "not above"
            return (
                ZERO,
                False,
                f"Not applied: {_base_name(rule.when.on, names)} "
                f"{_rupees(test.quantize(quantum, rounding=ROUND_HALF_UP))} a month, {word} "
                f"{_rupees(rule.when.amount)}",
            )

    if rule.method is Method.FIXED:
        assert rule.value is not None
        amount = rule.value if rule.basis == "monthly" else rule.value / MONTHS
    elif rule.method is Method.PERCENT:
        assert rule.value is not None and rule.of is not None
        base = _figure(rule.of, ctc, values, sums)
        if rule.ceiling is not None:
            base = min(base, rule.ceiling)
        amount = base * rule.value / HUNDRED
    elif rule.method is Method.SLAB:
        figure = _figure(rule.of or GROSS, ctc, values, sums)
        amount = ZERO
        for slab in rule.slabs:
            if slab.up_to is None or figure <= slab.up_to:
                amount = slab.amount
                break
    else:  # pragma: no cover - the balance is taken by `calculate`, never evaluated
        raise TemplateError("The balance is not a rule to evaluate")

    if amount < 0:
        amount = ZERO
    return amount.quantize(quantum, rounding=_ROUNDING[rule.rounding]), True, None


def calculate(
    template: Template,
    ctc_monthly: Decimal,
    *,
    pinned: Mapping[str, Decimal] | None = None,
) -> Result:
    """Split a month's CTC by the template.

    Args:
        template: a template that passed `validate`.
        ctc_monthly: the CTC for a month, exact (a year's CTC / 12 is fine).
        pinned: amounts a month typed for some lines; they replace the rule
            (the balance cannot be pinned: it is what is left).

    Raises:
        TemplateError: the lines inside the CTC need more than the CTC, or the
            template keeps flipping a line on and off at this CTC.
    """
    if not isinstance(ctc_monthly, Decimal):
        raise TemplateError("The CTC must be a Decimal")
    if ctc_monthly <= 0:
        raise TemplateError("CTC TOTAL must be more than 0")
    validate(template)

    quantum = _quantum(template.precision)
    pinned = dict(pinned or {})
    names = {line.code: line.name for line in template.lines}
    balance = next(line for line in template.lines if line.rule.method is Method.BALANCE)
    if balance.code in pinned:
        raise TemplateError(
            f"{balance.name} is the balance of the CTC: it takes what the other lines leave. "
            "Change the CTC or another line instead."
        )
    unknown = sorted(set(pinned) - set(names))
    if unknown:
        raise TemplateError(f"{', '.join(unknown)} is not in this structure")
    for code, amount in pinned.items():
        if amount < 0:
            raise TemplateError(f"{names[code]} cannot be negative")

    ordered = _order(template.lines)
    reads_sums = {
        line.code for line in template.lines if line.rule.references() & {*AGGREGATES, balance.code}
    } | {
        row.key for row in template.employer if row.rule.references() & {*AGGREGATES, balance.code}
    }
    # Lines that read a line which reads the sums, transitively.
    changed = True
    while changed:
        changed = False
        for line in template.lines:
            if line.code not in reads_sums and line.rule.references() & reads_sums:
                reads_sums.add(line.code)
                changed = True
        for row in template.employer:
            if row.key not in reads_sums and row.rule.references() & reads_sums:
                reads_sums.add(row.key)
                changed = True

    def sums_of(values: Mapping[str, Decimal]) -> dict[str, Decimal]:
        gross = pf = esi = ZERO
        for line in template.lines:
            amount = values.get(line.code, ZERO)
            if line.component_type is ComponentType.EARNING:
                gross += amount
            if line.counts_toward_pf:
                pf += amount
            if line.counts_toward_esi:
                esi += amount
        return {GROSS: gross, PF_WAGES: pf, ESI_WAGES: esi}

    def one_round(
        balance_amount: Decimal, sums: Mapping[str, Decimal] | None
    ) -> tuple[dict[str, LineResult], dict[str, LineResult], Decimal, Decimal]:
        """Every line and employer row once; then the balance, and what is left over.

        `sums=None` is the first round: anything that reads the gross or the
        balance is taken as nothing, so the round sees the CTC as if no such line
        existed, and an "only while gross is up to" rule is decided on that.
        """
        values: dict[str, Decimal] = {balance.code: balance_amount}
        results: dict[str, LineResult] = {}
        for line in ordered:
            if line.code in pinned:
                amount = pinned[line.code]
                results[line.code] = LineResult(line.code, line.name, amount, True, "Typed")
            elif sums is None and line.code in reads_sums:
                amount = ZERO
                results[line.code] = LineResult(line.code, line.name, amount)
            else:
                amount, applied, note = _evaluate(
                    line.rule,
                    ctc=ctc_monthly,
                    values=values,
                    sums=sums or {},
                    quantum=quantum,
                    names=names,
                )
                results[line.code] = LineResult(line.code, line.name, amount, applied, note)
            values[line.code] = amount
        employer: dict[str, LineResult] = {}
        for row in template.employer:
            if sums is None and row.key in reads_sums:
                employer[row.key] = LineResult(row.key, row.name, ZERO)
                continue
            amount, applied, note = _evaluate(
                row.rule,
                ctc=ctc_monthly,
                values=values,
                sums=sums or {},
                quantum=quantum,
                names=names,
            )
            employer[row.key] = LineResult(row.key, row.name, amount, applied, note)
        inside = sum(
            (results[line.code].monthly for line in ordered if line.part_of_ctc), ZERO
        ) + sum((item.monthly for item in employer.values()), ZERO)
        left = ctc_monthly - inside
        new_balance = left.quantize(quantum, rounding=ROUND_FLOOR)
        return results, employer, new_balance, inside

    results, employer, balance_amount, inside = one_round(ZERO, None)
    seen: list[tuple] = []
    rounds = 1
    while True:
        values = {code: item.monthly for code, item in results.items()}
        values[balance.code] = max(balance_amount, ZERO)
        sums = sums_of(values)
        state = (
            balance_amount,
            tuple(sorted((code, item.monthly) for code, item in results.items())),
            tuple(sorted((key, item.monthly) for key, item in employer.items())),
        )
        if seen and state == seen[-1]:
            break
        if state in seen:
            raise TemplateError(
                "At this CTC the structure keeps switching a line on and off (it sits right on "
                "a limit such as the ESI wage limit). Change the CTC by a rupee, or type the "
                "amounts by hand."
            )
        seen.append(state)
        if rounds >= MAX_ROUNDS:
            raise TemplateError(
                "The structure does not settle at this CTC. Type the amounts by hand."
            )
        results, employer, balance_amount, inside = one_round(max(balance_amount, ZERO), sums)
        rounds += 1

    if balance_amount < 0:
        over = (inside - ctc_monthly).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        raise TemplateError(
            f"The lines inside the CTC need {_rupees(over)} a month more than the CTC of "
            f"{_rupees(ctc_monthly.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))}. "
            "Lower a percentage or a fixed amount, or raise the CTC."
        )

    final_values = {code: item.monthly for code, item in results.items()}
    final_values[balance.code] = balance_amount
    sums = sums_of(final_values)
    lines = []
    for line in sorted(template.lines, key=lambda line: (line.display_order, line.code)):
        if line.code == balance.code:
            lines.append(LineResult(line.code, line.name, balance_amount))
        else:
            lines.append(results[line.code])
    allocated = inside + balance_amount
    return Result(
        ctc_monthly=ctc_monthly,
        lines=lines,
        employer=[employer[row.key] for row in template.employer],
        gross_monthly=sums[GROSS],
        pf_wages=sums[PF_WAGES],
        esi_wages=sums[ESI_WAGES],
        allocated_monthly=allocated,
        unallocated_monthly=ctc_monthly - allocated,
        rounds=rounds,
    )
