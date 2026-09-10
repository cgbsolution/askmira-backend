"""One serialiser per bank format, each isolated and independently testable.

Same rule as the statutory serialisers in slice 10: pure functions from a
canonical structure to a string. No database, no configuration lookups beyond
the `format_config` handed in, no arithmetic that decides anything.

The variation between banks is entirely in these dimensions, and nothing else:

- fixed-width or delimited, and which delimiter
- amount in rupees with paise, or in whole paise as an integer
- date format
- a header record, a trailer record, both, or neither
- the transaction-type code the corporate is enrolled for
- the beneficiary name length limit, and what to do at the boundary

Every one of those is data. `FormatSpec` holds them, and the two generic
renderers below cover every format in this file. A new bank is usually a new
`FormatSpec`, not new code — and when it genuinely needs its own function,
that function is small and sits next to the others.

**Truncation is the sharp edge.** Banks silently truncate an over-length
beneficiary name, and a name truncated mid-word can fail the bank's own name
match and bounce the credit. So truncation happens here, visibly, at a
configured length, and on a word boundary where one is available.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.models.disbursement import BankFileFormat

__all__ = [
    "FORMATS",
    "FormatError",
    "FormatSpec",
    "PaymentInstruction",
    "PaymentLine",
    "file_hash",
    "serialise",
    "truncate_name",
]


class FormatError(ValueError):
    """A payment instruction cannot be rendered in the requested format."""


# ── The canonical structure ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PaymentLine:
    beneficiary_name: str
    account_number: str
    ifsc: str
    amount: Decimal
    employee_code: str
    narration: str = "SALARY"


@dataclass(frozen=True, slots=True)
class PaymentInstruction:
    debit_account: str
    debit_ifsc: str
    corporate_id: str
    value_date: date
    batch_reference: str
    lines: list[PaymentLine] = field(default_factory=list)

    @property
    def total_amount(self) -> Decimal:
        return sum((line.amount for line in self.lines), start=Decimal(0))


# ── The dimensions banks vary in ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FormatSpec:
    code: str
    label: str
    delimiter: str | None  # None means fixed-width
    date_format: str
    amount_in_paise: bool
    name_length: int
    transaction_code: str
    has_header: bool
    has_trailer: bool
    line_ending: str = "\r\n"
    # Fixed-width formats only: (field, width) in order.
    widths: tuple[tuple[str, int], ...] = ()


HDFC = FormatSpec(
    code="hdfc",
    label="HDFC corporate net banking bulk upload",
    delimiter=",",
    date_format="%d/%m/%Y",
    amount_in_paise=False,
    name_length=40,
    transaction_code="N",  # NEFT
    has_header=False,
    has_trailer=False,
)

ICICI = FormatSpec(
    code="icici",
    label="ICICI Corporate Internet Banking bulk transfer",
    delimiter="|",
    date_format="%d-%m-%Y",
    amount_in_paise=False,
    name_length=50,
    transaction_code="NEFT",
    has_header=True,
    has_trailer=True,
)

SBI = FormatSpec(
    code="sbi",
    label="SBI Corporate Internet Banking salary upload",
    delimiter=None,  # fixed-width
    date_format="%d%m%Y",
    amount_in_paise=True,
    name_length=35,
    transaction_code="SAL",
    has_header=True,
    has_trailer=True,
    widths=(
        ("record_type", 2),
        ("account_number", 18),
        ("ifsc", 11),
        ("beneficiary_name", 35),
        ("amount", 15),
        ("narration", 30),
    ),
)

AXIS = FormatSpec(
    code="axis",
    label="Axis Corporate Internet Banking bulk payment",
    delimiter=",",
    date_format="%d/%m/%Y",
    amount_in_paise=False,
    name_length=40,
    transaction_code="NEFT",
    has_header=True,
    has_trailer=False,
)

KOTAK = FormatSpec(
    code="kotak",
    label="Kotak corporate bulk payment",
    delimiter="~",
    date_format="%d%m%Y",
    amount_in_paise=True,
    name_length=40,
    transaction_code="NEFT",
    has_header=False,
    has_trailer=True,
)

GENERIC_NEFT = FormatSpec(
    code="generic_neft",
    label="Generic NEFT bulk upload",
    delimiter=",",
    date_format="%d/%m/%Y",
    amount_in_paise=False,
    name_length=50,
    transaction_code="NEFT",
    has_header=True,
    has_trailer=True,
)

FORMATS: dict[BankFileFormat, FormatSpec] = {
    BankFileFormat.HDFC: HDFC,
    BankFileFormat.ICICI: ICICI,
    BankFileFormat.SBI: SBI,
    BankFileFormat.AXIS: AXIS,
    BankFileFormat.KOTAK: KOTAK,
    BankFileFormat.GENERIC_NEFT: GENERIC_NEFT,
}


# ── Rendering ───────────────────────────────────────────────────────────────


def truncate_name(name: str, limit: int) -> str:
    """Shorten a beneficiary name to the bank's limit.

    On a word boundary where one exists within the limit, because a name cut
    mid-word can fail the bank's own beneficiary name match and bounce the
    credit. Where the first word alone is longer than the limit there is no
    boundary to use and a hard cut is all that is left.

    Doing this here rather than letting the bank do it silently is the point: a
    truncation that happens in our file is visible in our file.
    """
    if limit <= 0:
        raise FormatError("A beneficiary name length limit of zero cannot be used.")

    cleaned = " ".join(name.split())
    if len(cleaned) <= limit:
        return cleaned

    cut = cleaned[:limit]

    # If the character just past the limit is a space, the cut already landed
    # on a word end and backing off further would drop a whole name for no
    # reason — "Anita Kumari Sharma" at twelve is "Anita Kumari", not "Anita".
    if cleaned[limit] == " ":
        return cut

    boundary = cut.rfind(" ")
    if boundary > 0:
        return cut[:boundary]
    return cut


def _amount(value: Decimal, *, in_paise: bool) -> str:
    """Rupees-with-paise, or whole paise as an integer.

    Sending 1234.50 to a paise-based format as "1234.50" is a rejection at
    best; as "1234" it silently underpays by fifty paise per employee.
    """
    if not isinstance(value, Decimal):
        raise FormatError("A payment amount must be a Decimal, not a float.")
    if value <= 0:
        raise FormatError(f"A payment amount of {value} would not be sent to a bank.")

    rupees = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if in_paise:
        return str(int(rupees * 100))
    return str(rupees)


def _guard(value: str, delimiter: str | None, field_name: str) -> str:
    """A delimiter inside a field shifts every column after it."""
    if delimiter and delimiter in value:
        raise FormatError(
            f"The {field_name} '{value}' contains '{delimiter}', which is this format's "
            "delimiter. Every column after it would shift."
        )
    return value


def serialise(
    instruction: PaymentInstruction,
    bank_format: BankFileFormat,
    *,
    config: dict | None = None,
) -> str:
    """Render an instruction in a bank's format.

    `config` overrides the spec per corporate: banks routinely set a different
    beneficiary name limit or transaction code for one customer, so the spec is
    the default and the profile's `format_config` wins.
    """
    spec = FORMATS.get(bank_format)
    if spec is None:
        raise FormatError(f"No serialiser for {bank_format}.")
    if not instruction.lines:
        raise FormatError("A disbursement file with no payment lines would be rejected.")

    spec = _apply_config(spec, config or {})
    if spec.delimiter is None:
        return _fixed_width(instruction, spec)
    return _delimited(instruction, spec)


def _apply_config(spec: FormatSpec, config: dict) -> FormatSpec:
    from dataclasses import replace

    overrides = {}
    if "name_length" in config:
        overrides["name_length"] = int(config["name_length"])
    if "transaction_code" in config:
        overrides["transaction_code"] = str(config["transaction_code"])
    if "date_format" in config:
        overrides["date_format"] = str(config["date_format"])
    return replace(spec, **overrides) if overrides else spec


def _delimited(instruction: PaymentInstruction, spec: FormatSpec) -> str:
    d = spec.delimiter
    assert d is not None
    rows: list[str] = []

    if spec.has_header:
        rows.append(
            d.join(
                [
                    "H",
                    _guard(instruction.corporate_id, d, "corporate id"),
                    _guard(instruction.debit_account, d, "debit account"),
                    instruction.value_date.strftime(spec.date_format),
                    _guard(instruction.batch_reference, d, "batch reference"),
                    str(len(instruction.lines)),
                    _amount(instruction.total_amount, in_paise=spec.amount_in_paise),
                ]
            )
        )

    for line in instruction.lines:
        name = truncate_name(line.beneficiary_name, spec.name_length)
        rows.append(
            d.join(
                [
                    "D" if spec.has_header else spec.transaction_code,
                    _guard(instruction.debit_account, d, "debit account"),
                    _guard(line.account_number, d, "account number"),
                    _guard(line.ifsc, d, "IFSC"),
                    _guard(name, d, "beneficiary name"),
                    _amount(line.amount, in_paise=spec.amount_in_paise),
                    spec.transaction_code,
                    instruction.value_date.strftime(spec.date_format),
                    _guard(line.narration, d, "narration"),
                ]
            )
        )

    if spec.has_trailer:
        rows.append(
            d.join(
                [
                    "T",
                    str(len(instruction.lines)),
                    _amount(instruction.total_amount, in_paise=spec.amount_in_paise),
                ]
            )
        )

    return spec.line_ending.join(rows) + spec.line_ending


def _fixed_width(instruction: PaymentInstruction, spec: FormatSpec) -> str:
    """Fixed-width, where a field one character long puts every later field in
    the wrong place for the rest of the file."""
    rows: list[str] = []

    if spec.has_header:
        header = (
            "01"
            + instruction.corporate_id.ljust(20)[:20]
            + instruction.debit_account.rjust(18, "0")[:18]
            + instruction.value_date.strftime(spec.date_format)
            + str(len(instruction.lines)).rjust(7, "0")
            + _amount(instruction.total_amount, in_paise=spec.amount_in_paise).rjust(15, "0")
        )
        rows.append(header)

    for line in instruction.lines:
        values = {
            "record_type": "02",
            "account_number": line.account_number.rjust(18, "0"),
            "ifsc": line.ifsc.ljust(11),
            "beneficiary_name": truncate_name(line.beneficiary_name, spec.name_length),
            "amount": _amount(line.amount, in_paise=spec.amount_in_paise).rjust(15, "0"),
            "narration": line.narration,
        }
        row = ""
        for name, width in spec.widths:
            value = values[name]
            if len(value) > width:
                raise FormatError(
                    f"The {name} '{value}' is {len(value)} characters and the field is "
                    f"{width}. A field over its width shifts every field after it for "
                    "the rest of the file."
                )
            row += value.ljust(width)
        rows.append(row)

    if spec.has_trailer:
        rows.append(
            "09"
            + str(len(instruction.lines)).rjust(7, "0")
            + _amount(instruction.total_amount, in_paise=spec.amount_in_paise).rjust(15, "0")
        )

    return spec.line_ending.join(rows) + spec.line_ending


def file_hash(content: str) -> str:
    """What an approver approved.

    Regenerating the file changes this, the stored approval no longer matches,
    and release refuses — which is what stops an approval given on one file
    being used to release another.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
