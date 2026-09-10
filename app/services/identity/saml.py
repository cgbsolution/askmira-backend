"""SAML 2.0 assertion validation.

A SAML assertion is a bearer credential: whoever holds a valid one is that
user. So validation is not a formality, and every check below exists because
skipping it is a documented way in.

What is checked, and why each matters:

1. **Signature.** An assertion without a valid signature from the configured
   IdP certificate is forged. An *unsigned* assertion is rejected outright
   rather than "accepted with lower trust" — there is no such thing.
2. **Audience.** An assertion issued for a different service provider is not
   for us. Without this check, an assertion captured from any other SP that
   shares the IdP logs the holder in here.
3. **Recipient / Destination.** Guards against the assertion being replayed at
   a different endpoint.
4. **NotBefore / NotOnOrAfter.** An expired assertion is a captured one.
5. **Replay.** Even inside its window, an assertion id may be consumed once.
   Enforced by the caller against `SamlAssertionSeen`.
6. **No role claims are read.** Deliberately. Roles are assigned in AskMira; an
   IdP asserting `role=admin` must not make anyone an admin. This module does
   not extract group or role attributes at all.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from lxml import etree

__all__ = [
    "SamlAssertion",
    "SamlValidationError",
    "parse_and_validate",
]

NS = {
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
}

# Tolerance for clock skew between us and the IdP. Five minutes is the usual
# convention; wider is a meaningfully larger replay window.
CLOCK_SKEW = timedelta(minutes=5)

# Guard against XML bombs before parsing. A 5 MB assertion is not legitimate.
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class SamlValidationError(ValueError):
    """The assertion is malformed, unsigned, forged, expired or misaddressed."""


@dataclass(frozen=True, slots=True)
class SamlAssertion:
    """The subset of a validated assertion we are willing to act on.

    Note what is not here: no roles, no groups, no permissions. See the module
    docstring.
    """

    assertion_id: str
    issuer: str
    name_id: str
    not_on_or_after: datetime
    attributes: dict[str, list[str]] = field(default_factory=dict)
    in_response_to: str | None = None

    def first(self, attribute: str) -> str | None:
        values = self.attributes.get(attribute)
        return values[0] if values else None


def _parser() -> etree.XMLParser:
    """A parser with every external-entity feature switched off.

    XXE and billion-laughs are the standard attacks on any XML endpoint, and a
    SAML endpoint is unauthenticated by definition.
    """
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        huge_tree=False,
        dtd_validation=False,
        load_dtd=False,
    )


def _parse_instant(value: str) -> datetime:
    """Parse a SAML xs:dateTime, which is always UTC with a trailing Z."""
    cleaned = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise SamlValidationError(f"'{value}' is not a valid SAML timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _normalise_certificate(cert: str) -> str:
    """Accept a bare base64 blob or a full PEM block.

    IdP admin consoles hand out both, and a copy-paste that happens to omit the
    PEM header should not read as a configuration error.
    """
    cleaned = cert.strip()
    if "BEGIN CERTIFICATE" in cleaned:
        return cleaned
    body = re.sub(r"\s+", "", cleaned)
    wrapped = "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN CERTIFICATE-----\n{wrapped}\n-----END CERTIFICATE-----"


def parse_and_validate(
    *,
    saml_response_b64: str,
    idp_certificate: str,
    expected_audience: str,
    expected_destination: str | None = None,
    expected_in_response_to: str | None = None,
    now: datetime | None = None,
) -> SamlAssertion:
    """Validate a base64 SAMLResponse and return what it asserts.

    Args:
        saml_response_b64: the `SAMLResponse` form field, base64 encoded.
        idp_certificate: the IdP's signing certificate, PEM or bare base64.
        expected_audience: our SP entity id. An assertion for anyone else is
            refused.
        expected_destination: our ACS URL, when the response carries one.
        expected_in_response_to: the `AuthnRequest` id for an SP-initiated
            flow. Omitted for IdP-initiated.

    Raises:
        SamlValidationError: on any failed check. The message says which one.
    """
    moment = now or datetime.now(UTC)

    try:
        raw = base64.b64decode(saml_response_b64, validate=True)
    except Exception as exc:
        raise SamlValidationError("SAMLResponse is not valid base64") from exc

    if len(raw) > MAX_RESPONSE_BYTES:
        raise SamlValidationError("SAMLResponse is implausibly large; refusing to parse")

    try:
        root = etree.fromstring(raw, parser=_parser())
    except etree.XMLSyntaxError as exc:
        raise SamlValidationError(f"SAMLResponse is not well-formed XML: {exc}") from exc

    # ── 1. Signature ────────────────────────────────────────────────────────
    # Verify before reading anything else. Parsing values out of an unverified
    # document and validating them afterwards means acting on attacker-chosen
    # data in between.
    verified_root = _verify_signature(raw, root, idp_certificate)

    # ── Status ──────────────────────────────────────────────────────────────
    status_code = verified_root.find(".//samlp:Status/samlp:StatusCode", NS)
    if status_code is not None:
        value = status_code.get("Value", "")
        if not value.endswith(":Success"):
            raise SamlValidationError(f"The identity provider returned status {value}")

    assertion = verified_root.find(".//saml:Assertion", NS)
    if assertion is None:
        raise SamlValidationError("SAMLResponse contains no assertion")

    assertion_id = assertion.get("ID")
    if not assertion_id:
        raise SamlValidationError("The assertion has no ID, so replay cannot be prevented")

    issuer_element = assertion.find("saml:Issuer", NS)
    issuer = (issuer_element.text or "").strip() if issuer_element is not None else ""

    # ── 2. Audience ─────────────────────────────────────────────────────────
    audiences = [
        (element.text or "").strip()
        for element in assertion.findall(".//saml:AudienceRestriction/saml:Audience", NS)
    ]
    if not audiences:
        raise SamlValidationError(
            "The assertion carries no AudienceRestriction, so it cannot be confirmed as "
            "intended for this service"
        )
    if expected_audience not in audiences:
        raise SamlValidationError(
            f"The assertion is addressed to {', '.join(audiences)}, not to "
            f"{expected_audience}"
        )

    # ── 3 and 4. Subject confirmation, recipient and validity window ────────
    confirmation = assertion.find(
        ".//saml:Subject/saml:SubjectConfirmation/saml:SubjectConfirmationData", NS
    )
    if confirmation is None:
        raise SamlValidationError("The assertion has no SubjectConfirmationData")

    not_on_or_after_raw = confirmation.get("NotOnOrAfter")
    if not not_on_or_after_raw:
        raise SamlValidationError(
            "The assertion has no NotOnOrAfter, so it would never expire"
        )
    not_on_or_after = _parse_instant(not_on_or_after_raw)
    if moment >= not_on_or_after + CLOCK_SKEW:
        raise SamlValidationError(
            f"The assertion expired at {not_on_or_after.isoformat()}"
        )

    conditions = assertion.find("saml:Conditions", NS)
    if conditions is not None and conditions.get("NotBefore"):
        not_before = _parse_instant(conditions.get("NotBefore"))
        if moment < not_before - CLOCK_SKEW:
            raise SamlValidationError(
                f"The assertion is not valid until {not_before.isoformat()}"
            )

    recipient = confirmation.get("Recipient")
    if expected_destination and recipient and recipient != expected_destination:
        raise SamlValidationError(
            f"The assertion names recipient {recipient}, not {expected_destination}"
        )

    destination = verified_root.get("Destination")
    if expected_destination and destination and destination != expected_destination:
        raise SamlValidationError(
            f"The response is addressed to {destination}, not {expected_destination}"
        )

    in_response_to = confirmation.get("InResponseTo")
    if expected_in_response_to and in_response_to != expected_in_response_to:
        raise SamlValidationError(
            "The assertion does not answer the authentication request we sent"
        )

    # ── Subject and attributes ──────────────────────────────────────────────
    name_id_element = assertion.find(".//saml:Subject/saml:NameID", NS)
    name_id = (name_id_element.text or "").strip() if name_id_element is not None else ""
    if not name_id:
        raise SamlValidationError("The assertion has no NameID, so it identifies nobody")

    attributes: dict[str, list[str]] = {}
    for attribute in assertion.findall(".//saml:AttributeStatement/saml:Attribute", NS):
        name = attribute.get("Name") or attribute.get("FriendlyName")
        if not name:
            continue
        values = [
            (value.text or "").strip()
            for value in attribute.findall("saml:AttributeValue", NS)
        ]
        attributes[name] = [v for v in values if v]
        friendly = attribute.get("FriendlyName")
        if friendly and friendly != name:
            attributes[friendly] = attributes[name]

    return SamlAssertion(
        assertion_id=assertion_id,
        issuer=issuer,
        name_id=name_id,
        not_on_or_after=not_on_or_after,
        attributes=attributes,
        in_response_to=in_response_to,
    )


def _verify_signature(raw: bytes, root: etree._Element, certificate: str) -> etree._Element:
    """Verify the XML signature and return the *verified* subtree.

    Returning the verified data rather than the originally parsed tree is the
    important part: signature wrapping attacks work precisely by having the
    verifier check one element and the application read another. Everything
    downstream reads only what came back from here.
    """
    from signxml import XMLVerifier

    signature = root.find(".//ds:Signature", NS)
    if signature is None:
        raise SamlValidationError(
            "The SAML response is not signed. Unsigned assertions are refused — "
            "there is no lower level of trust to accept them at."
        )

    if not certificate or not certificate.strip():
        raise SamlValidationError(
            "No signing certificate is configured for this identity provider, so the "
            "assertion cannot be verified"
        )

    try:
        verified = XMLVerifier().verify(
            raw,
            x509_cert=_normalise_certificate(certificate),
            # Only the configured certificate is acceptable. Without this the
            # verifier would happily trust a certificate the attacker embedded
            # in the document itself.
            ignore_ambiguous_key_info=True,
        )
    except Exception as exc:
        raise SamlValidationError(
            f"The assertion signature is not valid for the configured certificate: {exc}"
        ) from exc

    verified_root = verified.signed_xml
    if verified_root is None:
        raise SamlValidationError("Signature verification produced no signed content")

    # The signature may cover the Response or the Assertion. Normalise so
    # callers always get something they can search from.
    if verified_root.tag.endswith("}Assertion"):
        wrapper = etree.Element("{urn:oasis:names:tc:SAML:2.0:protocol}Response")
        wrapper.append(verified_root)
        for attribute in ("Destination", "ID", "InResponseTo"):
            if root.get(attribute):
                wrapper.set(attribute, root.get(attribute))
        return wrapper

    return verified_root
