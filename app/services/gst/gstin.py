"""GSTIN structure and checksum, and why a failure flags rather than blocks.

═══════════════════════════════════════════════════════════════════════════════
A BAD GSTIN DOES NOT MAKE THE EXPENSE GO AWAY
═══════════════════════════════════════════════════════════════════════════════

Somebody had lunch with a client and the restaurant's printed GSTIN has a typo,
or the OCR read a `0` as an `O`. The expense is real, it is reimbursable, and the
input credit probably is not claimable. Those are three separate facts and a
system that refuses the claim because the fifteenth character does not check out
has confused the third with the first.

So every function here returns a **verdict with a reason**. Nothing raises on a
malformed number, and the caller records the expense either way.

═══════════════════════════════════════════════════════════════════════════════
THE CHECKSUM IS WORTH IMPLEMENTING PROPERLY
═══════════════════════════════════════════════════════════════════════════════

A length-and-regex check passes `27AAPFU0939F1ZZ` — a real GSTIN with the last
character changed — and that is exactly the failure mode that matters, because
it is what a transcription error looks like. The modulus-36 checksum catches it.

Pure. No network, no clock, no database. The GST portal lookup that would confirm
a GSTIN is *active* is **not implemented**; `PortalStatus.NOT_CHECKED` is the
honest default and it travels with every verdict.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CHARSET",
    "GST_CODES_BY_STATE",
    "STATE_CODES",
    "GstinVerdict",
    "PortalStatus",
    "checksum_char",
    "state_of",
    "validate_gstin",
]

#: The GSTIN alphabet, in the order its checksum depends on.
CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

#: State and union-territory codes as they appear in the first two characters.
#: `97` is "Other Territory" and `99` is the centre; both are real and both are
#: routinely missing from hand-rolled validators, which then reject a legitimate
#: invoice.
STATE_CODES: dict[str, str] = {
    "01": "Jammu and Kashmir",
    "02": "Himachal Pradesh",
    "03": "Punjab",
    "04": "Chandigarh",
    "05": "Uttarakhand",
    "06": "Haryana",
    "07": "Delhi",
    "08": "Rajasthan",
    "09": "Uttar Pradesh",
    "10": "Bihar",
    "11": "Sikkim",
    "12": "Arunachal Pradesh",
    "13": "Nagaland",
    "14": "Manipur",
    "15": "Mizoram",
    "16": "Tripura",
    "17": "Meghalaya",
    "18": "Assam",
    "19": "West Bengal",
    "20": "Jharkhand",
    "21": "Odisha",
    "22": "Chhattisgarh",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "25": "Daman and Diu",
    "26": "Dadra and Nagar Haveli and Daman and Diu",
    "27": "Maharashtra",
    "28": "Andhra Pradesh (old)",
    "29": "Karnataka",
    "30": "Goa",
    "31": "Lakshadweep",
    "32": "Kerala",
    "33": "Tamil Nadu",
    "34": "Puducherry",
    "35": "Andaman and Nicobar Islands",
    "36": "Telangana",
    "37": "Andhra Pradesh",
    "38": "Ladakh",
    "97": "Other Territory",
    "99": "Centre",
}

#: 2 digits, then a PAN (5 letters, 4 digits, 1 letter), an entity code, a
#: registration-type character and the checksum.
_GSTIN_SHAPE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}[A-Z]{1}[0-9A-Z]{1}$")


class PortalStatus(str, enum.Enum):
    """Whether the GST portal has confirmed this registration is active.

    `NOT_CHECKED` is the default and stays the default, because no portal
    integration exists. A structurally valid GSTIN belonging to a cancelled
    registration is a credit that will be denied, and pretending otherwise is
    worse than saying nothing.
    """

    NOT_CHECKED = "not_checked"
    ACTIVE = "active"
    CANCELLED = "cancelled"
    NOT_FOUND = "not_found"


class GstinProblem(str, enum.Enum):
    NONE = "none"
    EMPTY = "empty"
    WRONG_LENGTH = "wrong_length"
    BAD_SHAPE = "bad_shape"
    UNKNOWN_STATE = "unknown_state"
    CHECKSUM_FAILED = "checksum_failed"


def checksum_char(first_fourteen: str) -> str:
    """The fifteenth character of a GSTIN, from the first fourteen.

    Modulus 36 over the alphanumeric charset, with alternating weights of 1 and
    2 and the product folded (quotient plus remainder) before summing — the
    same construction the GSTN publishes.
    """
    if len(first_fourteen) != 14:
        raise ValueError("The GSTIN checksum is computed from fourteen characters.")

    total = 0
    for index, char in enumerate(first_fourteen.upper()):
        value = CHARSET.find(char)
        if value < 0:
            raise ValueError(f"{char!r} is not a GSTIN character.")
        weight = 1 if index % 2 == 0 else 2
        product = value * weight
        total += product // 36 + product % 36
    return CHARSET[(36 - total % 36) % 36]


#: The GSTIN state codes a location's ISO state code may appear under. Old and
#: new codes both count where a state was split or merged (Andhra Pradesh,
#: Dadra and Nagar Haveli and Daman and Diu).
GST_CODES_BY_STATE: dict[str, frozenset[str]] = {
    "JK": frozenset({"01"}), "HP": frozenset({"02"}), "PB": frozenset({"03"}),
    "CH": frozenset({"04"}), "UK": frozenset({"05"}), "UT": frozenset({"05"}),
    "HR": frozenset({"06"}), "DL": frozenset({"07"}), "RJ": frozenset({"08"}),
    "UP": frozenset({"09"}), "BR": frozenset({"10"}), "SK": frozenset({"11"}),
    "AR": frozenset({"12"}), "NL": frozenset({"13"}), "MN": frozenset({"14"}),
    "MZ": frozenset({"15"}), "TR": frozenset({"16"}), "ML": frozenset({"17"}),
    "AS": frozenset({"18"}), "WB": frozenset({"19"}), "JH": frozenset({"20"}),
    "OD": frozenset({"21"}), "OR": frozenset({"21"}), "CG": frozenset({"22"}),
    "CT": frozenset({"22"}), "MP": frozenset({"23"}), "GJ": frozenset({"24"}),
    "DD": frozenset({"25", "26"}), "DN": frozenset({"26"}), "DH": frozenset({"25", "26"}),
    "MH": frozenset({"27"}), "AP": frozenset({"28", "37"}), "KA": frozenset({"29"}),
    "GA": frozenset({"30"}), "LD": frozenset({"31"}), "KL": frozenset({"32"}),
    "TN": frozenset({"33"}), "PY": frozenset({"34"}), "AN": frozenset({"35"}),
    "TG": frozenset({"36"}), "TS": frozenset({"36"}), "LA": frozenset({"38"}),
}


def state_of(gstin: str) -> str | None:
    return STATE_CODES.get(gstin[:2]) if len(gstin) >= 2 else None


@dataclass(frozen=True, slots=True)
class GstinVerdict:
    """What is known about one GSTIN, and what is not.

    `is_valid` refers to the structure and checksum only. Whether the
    registration is live is `portal_status`, and it is `NOT_CHECKED` until
    somebody builds the lookup.
    """

    gstin: str
    is_valid: bool
    problem: GstinProblem
    state_code: str | None
    state_name: str | None
    pan: str | None
    portal_status: PortalStatus
    message: str

    @property
    def blocks_the_expense(self) -> bool:
        """Always False, and returned so a caller cannot forget.

        The expense is real and reimbursable whatever the GSTIN says. Only the
        input credit is in question.
        """
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "gstin": self.gstin,
            "is_valid": self.is_valid,
            "problem": self.problem.value,
            "state_code": self.state_code,
            "state_name": self.state_name,
            "pan": self.pan,
            "portal_status": self.portal_status.value,
            "blocks_the_expense": self.blocks_the_expense,
            "message": self.message,
        }


def validate_gstin(
    raw: str | None, *, portal_status: PortalStatus = PortalStatus.NOT_CHECKED
) -> GstinVerdict:
    """Check structure and checksum. Never raises, never blocks.

    A number that fails here means the input credit is doubtful, not that the
    expense is refused — those are different facts and conflating them teaches
    employees that the receipt scanner rejects real receipts.
    """
    gstin = (raw or "").strip().upper().replace(" ", "")

    def verdict(
        ok: bool, problem: GstinProblem, message: str
    ) -> GstinVerdict:
        return GstinVerdict(
            gstin=gstin,
            is_valid=ok,
            problem=problem,
            state_code=gstin[:2] if len(gstin) >= 2 else None,
            state_name=state_of(gstin),
            pan=gstin[2:12] if len(gstin) >= 12 else None,
            portal_status=portal_status,
            message=message,
        )

    if not gstin:
        return verdict(
            False,
            GstinProblem.EMPTY,
            "No GSTIN on the invoice. The expense stands; there is no input "
            "credit to claim without one.",
        )
    if len(gstin) != 15:
        return verdict(
            False,
            GstinProblem.WRONG_LENGTH,
            f"A GSTIN is fifteen characters and this is {len(gstin)}. Most "
            "often an OCR misread or a truncated field - worth re-checking the "
            "invoice before writing the credit off.",
        )
    if not _GSTIN_SHAPE.match(gstin):
        return verdict(
            False,
            GstinProblem.BAD_SHAPE,
            "The GSTIN does not have the expected shape: two digits, a PAN, an "
            "entity code, a registration character and a checksum. A zero read "
            "as an O does this.",
        )
    if gstin[:2] not in STATE_CODES:
        return verdict(
            False,
            GstinProblem.UNKNOWN_STATE,
            f"{gstin[:2]} is not a GST state code, so the first two characters "
            "were misread or the number is not a GSTIN.",
        )

    expected = checksum_char(gstin[:14])
    if gstin[14] != expected:
        return verdict(
            False,
            GstinProblem.CHECKSUM_FAILED,
            "The GSTIN checksum does not match. This is what a single "
            "transcription error looks like - a length-and-format check would "
            "have passed it. The credit is not claimable against this number.",
        )

    live = {
        PortalStatus.NOT_CHECKED: (
            " The registration has not been checked against the GST portal - no "
            "portal lookup is built - so an active registration is assumed and "
            "not verified."
        ),
        PortalStatus.ACTIVE: " The registration is active.",
        PortalStatus.CANCELLED: (
            " **The registration is cancelled.** Structurally valid and not "
            "claimable."
        ),
        PortalStatus.NOT_FOUND: (
            " The portal does not know this number, which usually means it was "
            "never issued."
        ),
    }[portal_status]

    return verdict(
        portal_status is not PortalStatus.CANCELLED,
        GstinProblem.NONE,
        f"Valid GSTIN registered in {state_of(gstin)}.{live}",
    )
