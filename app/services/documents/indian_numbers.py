"""Indian numbering: grouping and amount-in-words.

Every Indian offer letter carries the CTC in words, and it is written in the
Indian system — lakh and crore, not million and billion. "Rupees Twelve Lakh
Fifty Thousand Only", never "Rupees One Point Two Five Million". Getting this
wrong on an offer letter is the kind of detail that makes a product look
foreign in this market.

Grouping follows the same system: 12,50,000 rather than 1,250,000 — the last
three digits, then pairs.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

__all__ = ["amount_in_words", "format_inr", "group_indian", "number_in_words"]

_ONES = (
    "Zero",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
    "Thirteen",
    "Fourteen",
    "Fifteen",
    "Sixteen",
    "Seventeen",
    "Eighteen",
    "Nineteen",
)
_TENS = (
    "",
    "",
    "Twenty",
    "Thirty",
    "Forty",
    "Fifty",
    "Sixty",
    "Seventy",
    "Eighty",
    "Ninety",
)


def group_indian(value: Decimal | int | str) -> str:
    """Format with Indian digit grouping: 1250000 becomes "12,50,000"."""
    amount = Decimal(str(value))
    sign = "-" if amount < 0 else ""
    text = str(abs(amount))
    whole, _, fraction = text.partition(".")

    if len(whole) <= 3:
        grouped = whole
    else:
        last3 = whole[-3:]
        rest = whole[:-3]
        # Pairs from the right, which is what makes 1,23,45,678 rather than
        # 12,345,678.
        pairs = []
        while len(rest) > 2:
            pairs.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            pairs.insert(0, rest)
        grouped = ",".join([*pairs, last3])

    return f"{sign}{grouped}" + (f".{fraction}" if fraction else "")


def format_inr(value: Decimal | int | float | str | None) -> str:
    """An amount as people read it in a message: "₹1,25,000.50".

    Rupee sign, Indian grouping, always two decimals. Every user-facing sentence
    that names an amount uses this, so ₹1500.00 and ₹1,500.00 never sit side
    by side on one screen.
    """
    amount = Decimal(str(value if value is not None else 0)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return f"₹{group_indian(amount)}"


def _below_hundred(number: int) -> str:
    if number < 20:
        return _ONES[number]
    tens, ones = divmod(number, 10)
    return _TENS[tens] + (f" {_ONES[ones]}" if ones else "")


def _below_thousand(number: int) -> str:
    hundreds, rest = divmod(number, 100)
    parts = []
    if hundreds:
        parts.append(f"{_ONES[hundreds]} Hundred")
    if rest:
        parts.append(_below_hundred(rest))
    return " ".join(parts)


def number_in_words(number: int) -> str:
    """Spell a whole number in the Indian system.

    Splits as crore / lakh / thousand / hundreds, which is the whole point —
    the Western system would split at million and billion instead.
    """
    if number < 0:
        return f"Minus {number_in_words(abs(number))}"
    if number == 0:
        return "Zero"

    parts: list[str] = []

    crore, number = divmod(number, 10_000_000)
    if crore:
        # Beyond 99 crore the Indian system keeps counting in crore
        # (e.g. "One Thousand Two Hundred Crore"), so recurse.
        parts.append(f"{number_in_words(crore)} Crore")

    lakh, number = divmod(number, 100_000)
    if lakh:
        parts.append(f"{_below_hundred(lakh)} Lakh")

    thousand, number = divmod(number, 1_000)
    if thousand:
        parts.append(f"{_below_hundred(thousand)} Thousand")

    if number:
        parts.append(_below_thousand(number))

    return " ".join(parts)


def amount_in_words(value: Decimal | int | str, currency: str = "Rupees") -> str:
    """Spell a money amount the way an Indian letter does.

    >>> amount_in_words(1250000)
    'Rupees Twelve Lakh Fifty Thousand Only'
    >>> amount_in_words(Decimal("1250000.50"))
    'Rupees Twelve Lakh Fifty Thousand and Fifty Paise Only'
    """
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    negative = amount < 0
    amount = abs(amount)

    rupees = int(amount)
    paise = int((amount - rupees) * 100)

    words = f"{currency} {number_in_words(rupees)}"
    if paise:
        words += f" and {number_in_words(paise)} Paise"
    words += " Only"

    return f"Minus {words}" if negative else words
