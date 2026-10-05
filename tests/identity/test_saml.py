"""Slice 04 gate: SAML assertion validation.

A SAML assertion is a bearer credential. Each test below corresponds to a way
in that exists when the matching check is missing, so these are attack cases
rather than unit tests of a parser.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from signxml import XMLSigner

from app.services.identity.saml import SamlValidationError, parse_and_validate

AUDIENCE = "https://app.askmira.test/saml/metadata"
ACS_URL = "https://app.askmira.test/api/v1/sso/saml/acs"


def _keypair(common_name: str = "idp.example.test"):
    """A throwaway signing key and self-signed certificate."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return key_pem, pem


IDP_KEY, IDP_CERT = _keypair()
OTHER_KEY, OTHER_CERT = _keypair("attacker.example.test")


def build_response(
    *,
    assertion_id: str = "_assertion-1",
    audience: str = AUDIENCE,
    recipient: str = ACS_URL,
    destination: str = ACS_URL,
    not_on_or_after: datetime | None = None,
    not_before: datetime | None = None,
    email: str = "anita@acme.test",
    extra_attributes: str = "",
    in_response_to: str | None = None,
) -> str:
    """Assemble a SAML Response document (unsigned)."""
    expiry = not_on_or_after or (datetime.now(UTC) + timedelta(minutes=5))
    nb = not_before or (datetime.now(UTC) - timedelta(minutes=5))
    in_response = f' InResponseTo="{in_response_to}"' if in_response_to else ""

    return f"""<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
    xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
    ID="_response-1" Version="2.0" Destination="{destination}"
    IssueInstant="{datetime.now(UTC).isoformat()}"{in_response}>
  <saml:Issuer>https://idp.example.test</saml:Issuer>
  <samlp:Status>
    <samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/>
  </samlp:Status>
  <saml:Assertion ID="{assertion_id}" Version="2.0"
      IssueInstant="{datetime.now(UTC).isoformat()}">
    <saml:Issuer>https://idp.example.test</saml:Issuer>
    <saml:Subject>
      <saml:NameID
        Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
      >{email}</saml:NameID>
      <saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">
        <saml:SubjectConfirmationData Recipient="{recipient}"
            NotOnOrAfter="{expiry.isoformat()}"{in_response}/>
      </saml:SubjectConfirmation>
    </saml:Subject>
    <saml:Conditions NotBefore="{nb.isoformat()}" NotOnOrAfter="{expiry.isoformat()}">
      <saml:AudienceRestriction>
        <saml:Audience>{audience}</saml:Audience>
      </saml:AudienceRestriction>
    </saml:Conditions>
    <saml:AttributeStatement>
      <saml:Attribute Name="email">
        <saml:AttributeValue>{email}</saml:AttributeValue></saml:Attribute>
      <saml:Attribute Name="given_name">
        <saml:AttributeValue>Anita</saml:AttributeValue></saml:Attribute>
      <saml:Attribute Name="family_name">
        <saml:AttributeValue>Sharma</saml:AttributeValue></saml:Attribute>
      {extra_attributes}
    </saml:AttributeStatement>
  </saml:Assertion>
</samlp:Response>"""


def sign(xml: str, key_pem: str = IDP_KEY, cert_pem: str = IDP_CERT) -> str:
    """Sign and base64 encode, as an IdP would POST it."""
    from lxml import etree

    root = etree.fromstring(xml.encode())
    signed = XMLSigner(
        method=__import__("signxml").methods.enveloped,
        signature_algorithm="rsa-sha256",
        digest_algorithm="sha256",
        c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
    ).sign(root, key=key_pem, cert=cert_pem)
    return base64.b64encode(etree.tostring(signed)).decode()


def validate(encoded: str, **overrides):
    kwargs = {
        "saml_response_b64": encoded,
        "idp_certificate": IDP_CERT,
        "expected_audience": AUDIENCE,
        "expected_destination": ACS_URL,
    }
    kwargs.update(overrides)
    return parse_and_validate(**kwargs)


# ═══════════════════════════════════════════════════════════════════════════
# The happy path, so the failures below mean something
# ═══════════════════════════════════════════════════════════════════════════


def test_a_correctly_signed_assertion_validates() -> None:
    assertion = validate(sign(build_response()))

    assert assertion.name_id == "anita@acme.test"
    assert assertion.first("email") == "anita@acme.test"
    assert assertion.first("given_name") == "Anita"
    assert assertion.assertion_id == "_assertion-1"


# ═══════════════════════════════════════════════════════════════════════════
# Signature
# ═══════════════════════════════════════════════════════════════════════════


def test_an_unsigned_assertion_is_rejected_outright() -> None:
    """There is no lower level of trust at which an unsigned one is acceptable."""
    encoded = base64.b64encode(build_response().encode()).decode()

    with pytest.raises(SamlValidationError) as exc:
        validate(encoded)
    assert "not signed" in str(exc.value)


def test_an_assertion_signed_by_the_wrong_key_is_rejected() -> None:
    """The checklist gate: invalid signature.

    Signed with a real, well-formed certificate — just not the one configured
    for this provider.
    """
    encoded = sign(build_response(), key_pem=OTHER_KEY, cert_pem=OTHER_CERT)

    with pytest.raises(SamlValidationError) as exc:
        validate(encoded)
    assert "signature is not valid" in str(exc.value)


def test_a_tampered_assertion_is_rejected() -> None:
    """Changing the email after signing must invalidate the signature."""
    encoded = sign(build_response(email="anita@acme.test"))
    raw = base64.b64decode(encoded)
    tampered = raw.replace(b"anita@acme.test", b"admin@acme.test")

    with pytest.raises(SamlValidationError):
        validate(base64.b64encode(tampered).decode())


def test_validation_without_a_configured_certificate_is_refused() -> None:
    with pytest.raises(SamlValidationError, match="No signing certificate"):
        validate(sign(build_response()), idp_certificate="")


def test_a_bare_base64_certificate_is_accepted() -> None:
    """IdP consoles hand out certificates with and without PEM headers."""
    bare = (
        IDP_CERT.replace("-----BEGIN CERTIFICATE-----", "")
        .replace("-----END CERTIFICATE-----", "")
        .strip()
    )
    assert validate(sign(build_response()), idp_certificate=bare).name_id


# ═══════════════════════════════════════════════════════════════════════════
# Audience and recipient
# ═══════════════════════════════════════════════════════════════════════════


def test_an_assertion_for_a_different_service_provider_is_rejected() -> None:
    """Without this, any assertion from a shared IdP logs the holder in here."""
    encoded = sign(build_response(audience="https://someone-elses-app.test"))

    with pytest.raises(SamlValidationError) as exc:
        validate(encoded)
    assert "addressed to" in str(exc.value)


def test_an_assertion_with_no_audience_restriction_is_rejected() -> None:
    import re

    xml = re.sub(
        r"<saml:AudienceRestriction>.*?</saml:AudienceRestriction>",
        "",
        build_response(),
        flags=re.DOTALL,
    )
    with pytest.raises(SamlValidationError, match="no AudienceRestriction"):
        validate(sign(xml))


def test_a_recipient_for_a_different_endpoint_is_rejected() -> None:
    encoded = sign(build_response(recipient="https://app.askmira.test/other/acs"))

    with pytest.raises(SamlValidationError, match="recipient"):
        validate(encoded)


# ═══════════════════════════════════════════════════════════════════════════
# Validity window
# ═══════════════════════════════════════════════════════════════════════════


def test_an_expired_assertion_is_rejected() -> None:
    """An expired assertion is a captured one."""
    encoded = sign(
        build_response(not_on_or_after=datetime.now(UTC) - timedelta(hours=1))
    )

    with pytest.raises(SamlValidationError, match="expired"):
        validate(encoded)


def test_an_assertion_not_yet_valid_is_rejected() -> None:
    future = datetime.now(UTC) + timedelta(hours=2)
    encoded = sign(
        build_response(not_before=future, not_on_or_after=future + timedelta(minutes=5))
    )

    with pytest.raises(SamlValidationError, match="not valid until"):
        validate(encoded)


def test_clock_skew_within_tolerance_is_accepted() -> None:
    """A two-minute skew between us and the IdP must not break sign-in."""
    encoded = sign(
        build_response(not_on_or_after=datetime.now(UTC) - timedelta(minutes=2))
    )
    assert validate(encoded).name_id == "anita@acme.test"


def test_an_assertion_with_no_expiry_is_rejected() -> None:
    """An assertion that never expires is a permanent credential."""
    xml = build_response()
    import re

    xml = re.sub(r'\s*NotOnOrAfter="[^"]*"(/>)', r"\1", xml, count=1)
    with pytest.raises(SamlValidationError, match="no NotOnOrAfter"):
        validate(sign(xml))


# ═══════════════════════════════════════════════════════════════════════════
# Privilege escalation — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


def test_the_parser_does_not_surface_role_claims_as_anything_privileged() -> None:
    """The checklist gate: an IdP role claim cannot escalate.

    The claim is parsed as an ordinary attribute — the guarantee is that
    nothing in provisioning reads it. `SamlAssertion` exposes no role concept,
    so there is no path from this attribute to a `UserRole`.
    """
    encoded = sign(
        build_response(
            extra_attributes=(
                '<saml:Attribute Name="role">'
                "<saml:AttributeValue>admin</saml:AttributeValue></saml:Attribute>"
                '<saml:Attribute Name="groups">'
                "<saml:AttributeValue>Administrators</saml:AttributeValue></saml:Attribute>"
            )
        )
    )
    assertion = validate(encoded)

    assert not hasattr(assertion, "role")
    assert not hasattr(assertion, "roles")
    assert not hasattr(assertion, "groups")
    # It is readable as raw data, and nothing acts on it.
    assert assertion.first("role") == "admin"


# ═══════════════════════════════════════════════════════════════════════════
# Malformed input
# ═══════════════════════════════════════════════════════════════════════════


def test_non_base64_input_is_rejected() -> None:
    with pytest.raises(SamlValidationError, match="not valid base64"):
        validate("this is not base64 !!!")


def test_malformed_xml_is_rejected() -> None:
    encoded = base64.b64encode(b"<not-closed").decode()
    with pytest.raises(SamlValidationError, match="not well-formed"):
        validate(encoded)


def test_an_oversized_response_is_refused_before_parsing() -> None:
    """XML bombs are cheap to send and expensive to parse."""
    huge = base64.b64encode(b"<a>" + b"x" * (6 * 1024 * 1024) + b"</a>").decode()
    with pytest.raises(SamlValidationError, match="implausibly large"):
        validate(huge)


def test_a_failed_idp_status_is_reported() -> None:
    xml = build_response().replace(
        "urn:oasis:names:tc:SAML:2.0:status:Success",
        "urn:oasis:names:tc:SAML:2.0:status:AuthnFailed",
    )
    with pytest.raises(SamlValidationError, match="returned status"):
        validate(sign(xml))


def test_an_in_response_to_mismatch_is_rejected() -> None:
    """SP-initiated flows must get an answer to the request they sent."""
    encoded = sign(build_response(in_response_to="_request-abc"))

    with pytest.raises(SamlValidationError, match="does not answer"):
        validate(encoded, expected_in_response_to="_request-xyz")


def test_a_matching_in_response_to_is_accepted() -> None:
    encoded = sign(build_response(in_response_to="_request-abc"))
    assertion = validate(encoded, expected_in_response_to="_request-abc")
    assert assertion.in_response_to == "_request-abc"
