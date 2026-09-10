"""Pre-generation validation for a disbursement batch.

Everything here **blocks**. That is a deliberate departure from the statutory
returns in slice 10, which have a warning tier: a rejected return is an
annoyance, but a salary credit sent to a wrong or malformed account is money
that has left the company and an employee who has not been paid. There is no
condition on this list worth proceeding past.

The one judgement call is duplicate account numbers. Two employees genuinely
can share an account — a married couple who bank together — so it blocks with a
message saying so, and the fix is to confirm the pair rather than to delete
one. Blocking is still right: far more often it is a copy-paste that pays one
person twice and leaves another unpaid.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

__all__ = [
    "BatchException",
    "BatchValidation",
    "name_similarity",
    "normalise_name",
    "validate_ifsc",
    "validate_lines",
]

# An IFSC is eleven characters: four letters of bank code, a reserved zero,
# then six alphanumeric characters of branch code. The reserved zero is the
# part people get wrong when they retype from a cheque.
IFSC_PATTERN = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")

# An Indian account number is 9 to 18 digits depending on the bank. Some banks
# pad with leading zeros, which is why this is not an integer anywhere.
ACCOUNT_PATTERN = re.compile(r"^[0-9]{9,18}$")


@dataclass(frozen=True, slots=True)
class BatchException:
    code: str
    subject: str
    field: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BatchValidation:
    exceptions: list[BatchException]

    @property
    def is_blocked(self) -> bool:
        return bool(self.exceptions)

    def as_list(self) -> list[dict[str, Any]]:
        return [e.as_dict() for e in self.exceptions]

    def summary(self) -> str:
        if not self.exceptions:
            return "No exceptions."
        return f"{len(self.exceptions)} exception(s) blocking release."


def validate_ifsc(ifsc: str | None) -> str | None:
    """`None` if the code is well formed, else why it is not.

    Structure only. There is no checksum in an IFSC — the eleven characters
    carry no redundancy — so a code that passes this can still name a branch
    that does not exist. Penny-drop verification is what catches that, and it
    is why `verification_method` is on the account record.
    """
    if not ifsc:
        return "No IFSC on the account."
    code = ifsc.strip().upper()
    if len(code) != 11:
        return f"'{ifsc}' is {len(code)} characters; an IFSC is eleven."
    if not IFSC_PATTERN.match(code):
        if code[4] != "0":
            return (
                f"'{ifsc}' does not have the reserved zero in the fifth position. That "
                "is the character most often mistyped from a cheque."
            )
        return f"'{ifsc}' is not a valid IFSC."
    return None


def normalise_name(name: str) -> str:
    """Strip everything a bank ignores when it matches a beneficiary name."""
    without_titles = re.sub(
        r"\b(mr|mrs|ms|miss|dr|shri|smt|sri)\b\.?", " ", name.lower()
    )
    return " ".join(re.sub(r"[^a-z ]", " ", without_titles).split())


def name_similarity(left: str, right: str) -> Decimal:
    """How alike two names are, 0 to 1, by shared word tokens.

    Token overlap rather than an edit distance, because the differences that
    matter here are whole words: a middle name present on one side, a maiden
    surname, an initial spelled out. "Anita Sharma" and "Anita K Sharma" are
    the same person; an edit distance would score them as further apart than
    "Anita Sharma" and "Amita Sharma", which are not.
    """
    left_tokens = set(normalise_name(left).split())
    right_tokens = set(normalise_name(right).split())
    if not left_tokens or not right_tokens:
        return Decimal(0)

    shared = left_tokens & right_tokens
    # Against the smaller set: an extra middle name should not be penalised.
    return (Decimal(len(shared)) / Decimal(min(len(left_tokens), len(right_tokens)))).quantize(
        Decimal("0.01")
    )


def validate_lines(
    rows: list[dict[str, Any]], *, name_match_threshold: Decimal = Decimal("0.5")
) -> BatchValidation:
    """Everything that stops a disbursement file being produced.

    Each row is `{"subject", "employee_name", "account_holder_name",
    "account_number", "ifsc", "amount"}`.

    `name_match_threshold` is configuration because tolerance is a business
    decision: a company that penny-drops every account can afford to be loose,
    one that does not cannot.
    """
    found: list[BatchException] = []
    accounts: dict[str, list[str]] = {}

    for row in rows:
        subject = str(row.get("subject", "unknown"))

        # ── The account ─────────────────────────────────────────────────────
        account = (row.get("account_number") or "").strip()
        if not account:
            found.append(
                BatchException(
                    "ACCOUNT_MISSING", subject, "account_number",
                    "No bank account on record. This employee cannot be paid.",
                )
            )
        elif not ACCOUNT_PATTERN.match(account):
            found.append(
                BatchException(
                    "ACCOUNT_MALFORMED", subject, "account_number",
                    f"'{account}' is not a plausible account number — 9 to 18 digits, "
                    "no spaces or punctuation.",
                )
            )
        else:
            accounts.setdefault(account, []).append(subject)

        # ── The IFSC ────────────────────────────────────────────────────────
        problem = validate_ifsc(row.get("ifsc"))
        if problem:
            found.append(BatchException("IFSC_INVALID", subject, "ifsc", problem))

        # ── The amount ──────────────────────────────────────────────────────
        amount = row.get("amount")
        if not isinstance(amount, Decimal):
            found.append(
                BatchException(
                    "AMOUNT_NOT_DECIMAL", subject, "amount",
                    "The amount is not a Decimal. Money in a payment file is never a "
                    "float.",
                )
            )
        elif amount <= 0:
            found.append(
                BatchException(
                    "AMOUNT_NOT_POSITIVE", subject, "amount",
                    f"Net pay is {amount}. A zero or negative credit is not something "
                    "to send to a bank — if this employee is owed nothing, leave them "
                    "off the batch.",
                )
            )

        # ── The name ────────────────────────────────────────────────────────
        employee_name = row.get("employee_name") or ""
        holder = row.get("account_holder_name") or ""
        if not holder.strip():
            found.append(
                BatchException(
                    "HOLDER_NAME_MISSING", subject, "account_holder_name",
                    "No account holder name. Banks match on it.",
                )
            )
        elif employee_name:
            score = name_similarity(employee_name, holder)
            if score < name_match_threshold:
                found.append(
                    BatchException(
                        "NAME_MISMATCH", subject, "account_holder_name",
                        f"The account is held by '{holder}' but the employee is "
                        f"'{employee_name}' (similarity {score}). Confirm this is the "
                        "right account — a credit to the wrong person is not "
                        "recoverable by the company.",
                    )
                )

    # ── Shared accounts ─────────────────────────────────────────────────────
    for account, subjects in accounts.items():
        if len(subjects) > 1:
            found.append(
                BatchException(
                    "ACCOUNT_DUPLICATE",
                    ", ".join(sorted(subjects)),
                    "account_number",
                    f"Account ending {account[-4:]} appears on {len(subjects)} "
                    "employees. This is occasionally legitimate — a couple who bank "
                    "together — but far more often it pays one person twice and leaves "
                    "another unpaid. Confirm the pair before releasing.",
                )
            )

    return BatchValidation(found)
