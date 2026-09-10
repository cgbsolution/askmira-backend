"""Consent notices: itemised, plain, and in a language the person reads.

═══════════════════════════════════════════════════════════════════════════════
THE EIGHTH SCHEDULE IS NOT A NICE-TO-HAVE FOR US
═══════════════════════════════════════════════════════════════════════════════

DPDP s.5(3) requires the notice to be available in English or any language in
the Eighth Schedule to the Constitution. For a product selling to the Indian
mid-market this is not a compliance footnote — a factory in Coimbatore has
several hundred people whose working language is Tamil, and a consent notice
they cannot read is not informed consent by any reading of s.6(1).

So the mechanism is built for all 22 scheduled languages plus English, and
**English and Hindi are the only translations that ship**. Those two facts are
kept visibly apart:

- `LANGUAGES` lists every language the mechanism accepts.
- `SHIPPED_LANGUAGES` lists the ones we actually have text for.
- Asking for a language in the first set and not the second returns English
  **with `is_fallback=True` and a line saying so in the returned notice**.

A silent fallback would be worse than refusing. Somebody would set the tenant's
language to Marathi, see a notice render, and believe their workforce had been
given a notice they could read.

═══════════════════════════════════════════════════════════════════════════════
ITEMISED MEANS ITEMISED
═══════════════════════════════════════════════════════════════════════════════

A notice is built one purpose at a time, each with what it does, what data it
uses, on what basis, and — where the basis is consent — a choice. A single
paragraph saying "we process your data for HR purposes" is the thing this
replaces.

Pure.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from app.services.dpdp.categories import LawfulBasis, category
from app.services.dpdp.consent import PURPOSES, Purpose

__all__ = [
    "SHIPPED_LANGUAGES",
    "Language",
    "Notice",
    "NoticeError",
    "NoticeItem",
    "render_notice",
]


class NoticeError(ValueError):
    """A notice that cannot be rendered as asked."""


class Language(str, enum.Enum):
    """English plus the twenty-two Eighth Schedule languages.

    The full list, not the ones we have text for. A product that only enumerated
    what it had translated would make "which languages must we support" a
    question nobody could answer from the code.
    """

    ENGLISH = "en"
    ASSAMESE = "as"
    BENGALI = "bn"
    BODO = "brx"
    DOGRI = "doi"
    GUJARATI = "gu"
    HINDI = "hi"
    KANNADA = "kn"
    KASHMIRI = "ks"
    KONKANI = "kok"
    MAITHILI = "mai"
    MALAYALAM = "ml"
    MANIPURI = "mni"
    MARATHI = "mr"
    NEPALI = "ne"
    ODIA = "or"
    PUNJABI = "pa"
    SANSKRIT = "sa"
    SANTALI = "sat"
    SINDHI = "sd"
    TAMIL = "ta"
    TELUGU = "te"
    URDU = "ur"

    @property
    def english_name(self) -> str:
        return _LANGUAGE_NAMES[self]


_LANGUAGE_NAMES: dict[Language, str] = {
    Language.ENGLISH: "English",
    Language.ASSAMESE: "Assamese",
    Language.BENGALI: "Bengali",
    Language.BODO: "Bodo",
    Language.DOGRI: "Dogri",
    Language.GUJARATI: "Gujarati",
    Language.HINDI: "Hindi",
    Language.KANNADA: "Kannada",
    Language.KASHMIRI: "Kashmiri",
    Language.KONKANI: "Konkani",
    Language.MAITHILI: "Maithili",
    Language.MALAYALAM: "Malayalam",
    Language.MANIPURI: "Manipuri",
    Language.MARATHI: "Marathi",
    Language.NEPALI: "Nepali",
    Language.ODIA: "Odia",
    Language.PUNJABI: "Punjabi",
    Language.SANSKRIT: "Sanskrit",
    Language.SANTALI: "Santali",
    Language.SINDHI: "Sindhi",
    Language.TAMIL: "Tamil",
    Language.TELUGU: "Telugu",
    Language.URDU: "Urdu",
}

#: What we actually have text for today. Kept deliberately separate from
#: `Language`, and an invariant asserts it is a strict subset — the day it
#: equals `Language` is the day this product genuinely supports all of them,
#: and that day should require editing this line.
SHIPPED_LANGUAGES: frozenset[Language] = frozenset(
    {Language.ENGLISH, Language.HINDI}
)


#: Framing text per language. Purpose descriptions live on the `Purpose` and
#: are English-only today; `render_notice` reports exactly which parts came
#: back untranslated rather than presenting a half-translated notice as a
#: translated one.
_FRAMING: dict[Language, dict[str, str]] = {
    Language.ENGLISH: {
        "title": "How your information is used",
        "intro": (
            "This tells you what information about you is held in AskMira, what "
            "it is used for, and why. Where you have a choice, it is marked."
        ),
        "consent_heading": "Where you have a choice",
        "no_choice_heading": "Where there is no choice, and why",
        "basis_line": "Why this is allowed:",
        "data_line": "Information used:",
        "rights_line": (
            "You can ask to see what is held about you, to have it corrected, "
            "or to have it deleted. Where something has to be kept by law, you "
            "will be told which law and for how long."
        ),
        "withdraw_line": (
            "You can change any of these choices at any time, as easily as you "
            "made them, and the processing stops."
        ),
        "fallback_line": (
            "This notice is not yet available in the language you asked for, so "
            "it is shown in English. Ask your HR team for a translation."
        ),
    },
    Language.HINDI: {
        "title": "आपकी जानकारी का उपयोग कैसे किया जाता है",
        "intro": (
            "यह बताता है कि AskMira में आपके बारे में कौन सी जानकारी रखी जाती है, "
            "उसका उपयोग किस लिए होता है, और क्यों। जहाँ आपके पास विकल्प है, वह "
            "चिह्नित है।"
        ),
        "consent_heading": "जहाँ आपके पास विकल्प है",
        "no_choice_heading": "जहाँ विकल्प नहीं है, और क्यों",
        "basis_line": "यह किस आधार पर किया जाता है:",
        "data_line": "उपयोग की जाने वाली जानकारी:",
        "rights_line": (
            "आप यह देखने के लिए कह सकते हैं कि आपके बारे में क्या रखा गया है, उसे "
            "ठीक कराने के लिए, या उसे हटाने के लिए। जहाँ कानून के अनुसार कुछ रखना "
            "आवश्यक है, आपको बताया जाएगा कि कौन सा कानून और कितने समय के लिए।"
        ),
        "withdraw_line": (
            "आप इनमें से कोई भी विकल्प कभी भी बदल सकते हैं, उतनी ही आसानी से जितनी "
            "आसानी से आपने चुना था, और प्रोसेसिंग रुक जाएगी।"
        ),
        "fallback_line": "",
    },
}


@dataclass(frozen=True, slots=True)
class NoticeItem:
    """One purpose, as it appears in the notice.

    `offers_a_choice` is the only thing on this object the reader acts on, and
    it is derived from the lawful basis rather than set independently — a
    checkbox next to a legal obligation cannot happen by editing a template.
    """

    purpose_key: str
    heading: str
    body: str
    lawful_basis: LawfulBasis
    basis_explanation: str
    data_used: tuple[str, ...]
    offers_a_choice: bool
    stops_if_declined: tuple[str, ...]
    #: Named here, in the notice, rather than buried in a policy page. If a
    #: purpose sends data outside India the reader is told so at the point of
    #: deciding, which is the only point at which it is useful to them.
    sub_processors: tuple[str, ...]
    #: True where the body is English because no translation exists.
    body_untranslated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose_key,
            "heading": self.heading,
            "body": self.body,
            "lawful_basis": self.lawful_basis.value,
            "basis_explanation": self.basis_explanation,
            "data_used": list(self.data_used),
            "offers_a_choice": self.offers_a_choice,
            "stops_if_declined": list(self.stops_if_declined),
            "sub_processors": list(self.sub_processors),
            "body_untranslated": self.body_untranslated,
        }


@dataclass(frozen=True, slots=True)
class Notice:
    """One rendered consent notice.

    `notice_id` is what a consent record points at. Without it, "what did the
    notice say when they agreed" is answerable only by whatever the template
    says today — which is to say, not answerable.
    """

    notice_id: str
    version: int
    language: Language
    is_fallback: bool
    title: str
    intro: str
    choices: tuple[NoticeItem, ...]
    disclosures: tuple[NoticeItem, ...]
    rights_line: str
    #: Empty unless this is a fallback rendering. Present in the notice body,
    #: not only in the metadata — the reader is the person who needs to know.
    fallback_line: str

    @property
    def untranslated_items(self) -> int:
        return sum(
            1 for i in (*self.choices, *self.disclosures) if i.body_untranslated
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "notice_id": self.notice_id,
            "version": self.version,
            "language": self.language.value,
            "language_name": self.language.english_name,
            "is_fallback": self.is_fallback,
            "fallback_line": self.fallback_line,
            "title": self.title,
            "intro": self.intro,
            "untranslated_items": self.untranslated_items,
            "choices": [i.as_dict() for i in self.choices],
            "disclosures": [i.as_dict() for i in self.disclosures],
            "rights_line": self.rights_line,
        }


def _item(spec: Purpose, framing: dict[str, str], *, translated: bool) -> NoticeItem:
    data_used = tuple(
        category(key).name for key in spec.data_categories
    )
    return NoticeItem(
        purpose_key=spec.key,
        heading=spec.name,
        body=spec.description,
        lawful_basis=spec.lawful_basis,
        basis_explanation=f"{framing['basis_line']} {spec.lawful_basis.label}",
        data_used=data_used,
        offers_a_choice=spec.requires_consent,
        stops_if_declined=spec.stops,
        sub_processors=spec.sub_processors,
        # Purpose text is authored in English. A notice rendered in Hindi has
        # translated framing and English bodies, and says how many.
        body_untranslated=not translated,
    )


def render_notice(
    *,
    language: Language,
    notice_id: str,
    version: int = 1,
    purpose_keys: tuple[str, ...] = (),
) -> Notice:
    """Build the notice a person is shown before they decide.

    Splits into `choices` — purposes resting on consent, where the answer can
    be no — and `disclosures`, where it cannot. Presenting the two in one
    undifferentiated list is how a person comes to believe they consented to
    payroll, and how they later come to believe they can withdraw it.
    """
    if language not in Language:
        raise NoticeError(f"{language!r} is not a language this notice supports.")

    is_fallback = language not in SHIPPED_LANGUAGES
    effective = Language.ENGLISH if is_fallback else language
    framing = _FRAMING[effective]

    keys = purpose_keys or tuple(PURPOSES)
    unknown = [key for key in keys if key not in PURPOSES]
    if unknown:
        raise NoticeError(
            f"Cannot render a notice for {', '.join(sorted(unknown))} — no such "
            "purpose. A notice describing a purpose nobody defined would be a "
            "notice about processing that either does not happen or happens "
            "undeclared."
        )

    # Purpose bodies are English regardless; only the framing is translated.
    translated = effective is Language.ENGLISH

    items = [_item(PURPOSES[key], framing, translated=translated) for key in keys]
    choices = tuple(i for i in items if i.offers_a_choice)
    disclosures = tuple(i for i in items if not i.offers_a_choice)

    return Notice(
        notice_id=notice_id,
        version=version,
        language=effective,
        is_fallback=is_fallback,
        title=framing["title"],
        intro=framing["intro"],
        choices=choices,
        disclosures=disclosures,
        rights_line=(
            f"{framing['rights_line']} {framing['withdraw_line']}"
            if choices
            else framing["rights_line"]
        ),
        fallback_line=(
            _FRAMING[Language.ENGLISH]["fallback_line"] if is_fallback else ""
        ),
    )
