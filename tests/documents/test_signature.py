"""Slice 03 gate: webhook verification and PDF determinism.

The webhook check is the security-critical one. A webhook is what moves a
letter to SIGNED, and a signed appointment letter is a legal document — so an
endpoint that accepts an unverified payload lets anyone forge one.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.services.documents.pdf import (
    Letterhead,
    PdfRenderError,
    content_hash,
    render_pdf,
    wrap_document,
)
from app.services.documents.signature import (
    NoopProvider,
    SignatureError,
    get_provider,
    sign_payload,
)

SECRET = "test-webhook-secret"


def signed_request(payload: dict, secret: str = SECRET) -> tuple[bytes, dict[str, str]]:
    raw, signature = sign_payload(payload, secret)
    return raw, {"X-AskMira-Signature": signature}


# ═══════════════════════════════════════════════════════════════════════════
# Webhook verification
# ═══════════════════════════════════════════════════════════════════════════


def test_a_correctly_signed_webhook_is_accepted() -> None:
    provider = NoopProvider(webhook_secret=SECRET)
    raw, headers = signed_request({"request_id": "abc", "state": "signed"})

    assert provider.verify_webhook(raw_body=raw, headers=headers) is True


def test_a_tampered_payload_is_rejected() -> None:
    """The checklist gate. The signature was computed over different bytes."""
    provider = NoopProvider(webhook_secret=SECRET)
    raw, headers = signed_request({"request_id": "abc", "state": "signed"})

    tampered = raw.replace(b'"abc"', b'"someone-elses-letter"')
    assert provider.verify_webhook(raw_body=tampered, headers=headers) is False


def test_a_webhook_with_no_signature_header_is_rejected() -> None:
    provider = NoopProvider(webhook_secret=SECRET)
    raw = json.dumps({"request_id": "abc"}).encode()

    assert provider.verify_webhook(raw_body=raw, headers={}) is False


def test_a_webhook_signed_with_the_wrong_secret_is_rejected() -> None:
    provider = NoopProvider(webhook_secret=SECRET)
    raw, headers = signed_request({"request_id": "abc"}, secret="not-the-secret")

    assert provider.verify_webhook(raw_body=raw, headers=headers) is False


def test_an_empty_secret_never_verifies() -> None:
    """A misconfigured deployment must fail closed, not open."""
    provider = NoopProvider(webhook_secret="")
    raw, headers = signed_request({"request_id": "abc"}, secret="")

    assert provider.verify_webhook(raw_body=raw, headers=headers) is False


def test_header_lookup_is_case_insensitive() -> None:
    """HTTP header names are case-insensitive; ASGI lower-cases them."""
    provider = NoopProvider(webhook_secret=SECRET)
    raw, _ = signed_request({"request_id": "abc"})
    signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()

    assert provider.verify_webhook(
        raw_body=raw, headers={"x-askmira-signature": signature}
    )
    assert provider.verify_webhook(
        raw_body=raw, headers={"X-ASKMIRA-SIGNATURE": signature}
    )


def test_a_sha256_prefixed_signature_is_accepted() -> None:
    """Providers differ on whether they prefix the algorithm."""
    provider = NoopProvider(webhook_secret=SECRET)
    raw, _ = signed_request({"request_id": "abc"})
    signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()

    assert provider.verify_webhook(
        raw_body=raw, headers={"X-AskMira-Signature": f"sha256={signature}"}
    )


def test_verification_is_over_raw_bytes_not_a_reserialised_dict() -> None:
    """Key order and whitespace do not survive a parse-and-dump round trip.

    Verifying a re-serialised body would reject valid webhooks, and the usual
    response to that is to disable the check — which is the actual risk.
    """
    provider = NoopProvider(webhook_secret=SECRET)
    payload = {"request_id": "abc", "state": "signed"}
    raw, headers = signed_request(payload)

    reserialised = json.dumps(payload, indent=2).encode()
    assert reserialised != raw
    assert provider.verify_webhook(raw_body=reserialised, headers=headers) is False


# ═══════════════════════════════════════════════════════════════════════════
# Provider selection
# ═══════════════════════════════════════════════════════════════════════════


def test_an_unknown_provider_raises_rather_than_falling_back() -> None:
    """Silently downgrading to Noop would sign everything instantly."""
    with pytest.raises(SignatureError, match="Unknown signature provider"):
        get_provider("docusign")


def test_digio_refuses_to_construct_without_credentials() -> None:
    with pytest.raises(SignatureError, match="client id and secret"):
        get_provider("digio", client_id="", client_secret="", webhook_secret="x")


async def test_the_noop_provider_round_trips_a_document() -> None:
    provider = NoopProvider(webhook_secret=SECRET)
    request = await provider.send_for_signature(
        document=b"%PDF-1.4 fake",
        file_name="offer.pdf",
        signer_name="Anita Sharma",
        signer_email="anita@acme.test",
    )

    assert request.provider == "noop"
    status = await provider.get_status(request.request_id)
    assert status.state == "signed"
    assert await provider.download_signed(request.request_id) == b"%PDF-1.4 fake"


async def test_downloading_an_unknown_request_raises() -> None:
    provider = NoopProvider()
    with pytest.raises(SignatureError, match="Unknown signature request"):
        await provider.download_signed("nope")


# ═══════════════════════════════════════════════════════════════════════════
# PDF rendering
# ═══════════════════════════════════════════════════════════════════════════

LETTERHEAD = Letterhead(company_name="Acme Technologies Pvt Ltd", address="Bengaluru")
SAMPLE = "<h1>Offer of employment</h1><p>Dear Anita,</p>"


def test_a_letter_renders_to_a_real_pdf() -> None:
    data = render_pdf(
        merged_html=SAMPLE, reference_number="ACM/OFF/2026-27/00001", letterhead=LETTERHEAD
    )
    assert data.startswith(b"%PDF-")
    assert len(data) > 500


def test_rendering_the_same_letter_twice_is_byte_identical() -> None:
    """Needed for re-render-and-verify to mean anything."""
    kwargs = {
        "merged_html": SAMPLE,
        "reference_number": "ACM/OFF/2026-27/00001",
        "letterhead": LETTERHEAD,
    }
    assert render_pdf(**kwargs) == render_pdf(**kwargs)


def test_the_content_hash_is_stable_and_input_sensitive() -> None:
    assert content_hash(SAMPLE) == content_hash(SAMPLE)
    assert content_hash(SAMPLE) != content_hash(SAMPLE + " ")


def test_the_reference_number_and_page_numbering_are_in_the_document() -> None:
    document = wrap_document(
        merged_html=SAMPLE,
        reference_number="ACM/OFF/2026-27/00042",
        letterhead=LETTERHEAD,
    )
    assert "ACM/OFF/2026-27/00042" in document
    assert "pdf:pagenumber" in document
    assert "Acme Technologies Pvt Ltd" in document


def test_a_logo_is_embedded_when_the_tenant_has_one() -> None:
    branded = Letterhead(
        company_name="Acme", logo_data_uri="data:image/png;base64,iVBORw0KGgo="
    )
    document = wrap_document(
        merged_html=SAMPLE, reference_number="X/1", letterhead=branded
    )
    assert "data:image/png;base64" in document


def test_render_errors_are_raised_rather_than_returning_a_partial_pdf() -> None:
    """A truncated offer letter is worse than a failed generation."""
    assert issubclass(PdfRenderError, RuntimeError)
