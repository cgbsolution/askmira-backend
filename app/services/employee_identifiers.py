"""Indian statutory identifiers on the employee record: validation and masking.

Pure — no database, no request. The same rules run when HR types a PAN and when
an employee proposes one in a change request, so the two cannot disagree about
what a valid PAN is.

What each check can and cannot prove:

- **PAN** has a structure (five letters, four digits, a letter) and the fourth
  character names the holder type — `P` for an individual. A company PAN typed
  into an employee record is the commonest mistake, and it is caught here.
  There is no checksum; only the Income Tax portal can say a PAN exists.
- **UAN** is twelve digits. EPFO publishes no checksum either.
- **Aadhaar** is twelve digits, never starting with 0 or 1, with a Verhoeff
  check digit — so a single mistyped or swapped digit is caught. Only the last
  four digits are ever stored (Aadhaar Act s.29 and the UIDAI storage
  regulations); the full number is validated and then dropped.
- **IFSC** reuses the disbursement module's own check, so a code the bank file
  would refuse is refused on the profile too.
"""

from __future__ import annotations

import re

from app.services.disbursement.validation import validate_ifsc as _validate_ifsc

__all__ = [
    "IdentifierError",
    "aadhaar_last4",
    "mask_aadhaar",
    "mask_account",
    "mask_tail",
    "normalise_pan",
    "validate_aadhaar",
    "validate_account_number",
    "validate_esi",
    "validate_ifsc",
    "validate_pan",
    "validate_pf_number",
    "validate_uan",
    "verhoeff_valid",
]

PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
UAN_PATTERN = re.compile(r"^[0-9]{12}$")
AADHAAR_PATTERN = re.compile(r"^[2-9][0-9]{11}$")
#: The ESIC insurance number (IP number) is ten digits.
ESI_PATTERN = re.compile(r"^[0-9]{10}$")
#: An EPF member id: region/office/establishment/extension/account, e.g.
#: `MH/BAN/0012345/000/0000123`, with or without the slashes.
PF_PATTERN = re.compile(r"^[A-Z]{2}/?[A-Z]{3}/?[0-9]{1,7}/?[0-9]{0,3}/?[0-9]{1,7}$")
ACCOUNT_PATTERN = re.compile(r"^[0-9]{9,18}$")


class IdentifierError(ValueError):
    """An identifier that is malformed, with a message a person can act on."""


def _clean(value: str) -> str:
    return re.sub(r"[\s\-]", "", value or "").upper()


# ── PAN ────────────────────────────────────────────────────────────────────


def normalise_pan(value: str) -> str:
    return _clean(value)


def validate_pan(value: str) -> str:
    """The PAN, upper-cased, or `IdentifierError` saying what is wrong."""
    pan = normalise_pan(value)
    if len(pan) != 10:
        raise IdentifierError(f"A PAN is ten characters; '{value}' has {len(pan)}.")
    if not PAN_PATTERN.match(pan):
        raise IdentifierError(
            f"'{value}' is not a PAN. A PAN is five letters, four digits and a letter, "
            "for example ABCPE1234F."
        )
    if pan[3] != "P":
        raise IdentifierError(
            f"'{pan}' is not an individual's PAN: the fourth character of a person's PAN "
            f"is P, and '{pan[3]}' marks a company, firm or trust. Check the PAN card."
        )
    return pan


# ── UAN, PF, ESI ───────────────────────────────────────────────────────────


def validate_uan(value: str) -> str:
    uan = _clean(value)
    if not UAN_PATTERN.match(uan):
        raise IdentifierError(f"A UAN is twelve digits; '{value}' is not.")
    return uan


def validate_pf_number(value: str) -> str:
    pf = _clean(value)
    if not PF_PATTERN.match(pf) or len(pf) > 32:
        raise IdentifierError(
            f"'{value}' is not an EPF member id. It reads like MH/BAN/0012345/000/0000123."
        )
    return pf


def validate_esi(value: str) -> str:
    esi = _clean(value)
    if not ESI_PATTERN.match(esi):
        raise IdentifierError(f"An ESIC insurance number is ten digits; '{value}' is not.")
    return esi


# ── Aadhaar (Verhoeff) ─────────────────────────────────────────────────────

# The dihedral group D5 multiplication table, the position permutation and the
# inverse, as published with the algorithm. Aadhaar's last digit is the
# Verhoeff check digit over the first eleven.
_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def verhoeff_valid(digits: str) -> bool:
    """Whether a digit string carries a valid Verhoeff check digit at the end."""
    if not digits.isdigit():
        return False
    check = 0
    for position, char in enumerate(reversed(digits)):
        check = _D[check][_P[position % 8][int(char)]]
    return check == 0


def validate_aadhaar(value: str) -> str:
    """The twelve digits, or `IdentifierError`. The caller keeps only the last four."""
    number = _clean(value)
    if len(number) != 12 or not number.isdigit():
        raise IdentifierError("An Aadhaar number is twelve digits.")
    if not AADHAAR_PATTERN.match(number):
        raise IdentifierError("An Aadhaar number never starts with 0 or 1.")
    if not verhoeff_valid(number):
        raise IdentifierError(
            "That Aadhaar number fails its check digit, so one digit is mistyped or two "
            "are swapped. Check it against the card."
        )
    return number


def aadhaar_last4(value: str) -> str:
    """Validate a full Aadhaar number and return only what may be stored."""
    return validate_aadhaar(value)[-4:]


# ── Bank ───────────────────────────────────────────────────────────────────


def validate_ifsc(value: str) -> str:
    code = _clean(value)
    problem = _validate_ifsc(code)
    if problem:
        raise IdentifierError(problem)
    return code


def validate_account_number(value: str) -> str:
    number = re.sub(r"\s", "", value or "")
    if not ACCOUNT_PATTERN.match(number):
        raise IdentifierError("A bank account number is 9 to 18 digits.")
    return number


# ── Masking ────────────────────────────────────────────────────────────────


def mask_tail(value: str | None, *, keep: int = 4) -> str | None:
    """Everything but the last `keep` characters replaced with X."""
    if not value:
        return None
    if len(value) <= keep:
        return "X" * len(value)
    return "X" * (len(value) - keep) + value[-keep:]


def mask_aadhaar(last4: str | None) -> str | None:
    """How an Aadhaar number is shown anywhere in the product: last four only."""
    if not last4:
        return None
    return f"XXXX XXXX {last4[-4:]}"


def mask_account(last4: str | None) -> str | None:
    if not last4:
        return None
    return f"XXXXXX{last4[-4:]}"
