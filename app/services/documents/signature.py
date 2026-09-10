"""E-signature providers.

Abstracted behind one interface so the vendor is a configuration choice rather
than a rewrite. Two implementations ship:

- `DigioProvider` — Digio, an Indian Aadhaar-eSign capable vendor. Chosen over
  DocuSign for this market on both price and Aadhaar support. **Needs live
  credentials to do anything; the API shapes here follow Digio's documented
  contract and must be verified against a sandbox account before launch.**
- `NoopProvider` — local development and tests. Signs instantly, stores
  nothing, and is never selectable in production.

═══════════════════════════════════════════════════════════════════════════════
WEBHOOK VERIFICATION
═══════════════════════════════════════════════════════════════════════════════

A webhook is what moves a letter to SIGNED. An unverified webhook endpoint is
therefore a forgery vector: anyone who can POST to it can mark any letter
signed, and a signed appointment letter is a legal document.

Every provider must implement `verify_webhook`, and the router calls it before
looking at the payload at all. Comparison is constant-time — a timing oracle on
a signature check is a real, demonstrated attack, not a theoretical one.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "DigioProvider",
    "NoopProvider",
    "SignatureError",
    "SignatureProvider",
    "SignatureRequest",
    "SignatureStatus",
    "get_provider",
]


class SignatureError(RuntimeError):
    """The provider rejected the request, or a webhook failed verification."""


@dataclass(frozen=True, slots=True)
class SignatureRequest:
    """What comes back when a document is sent for signature."""

    provider: str
    request_id: str
    signing_url: str | None = None


@dataclass(frozen=True, slots=True)
class SignatureStatus:
    """Where a signature request currently stands."""

    request_id: str
    # requested | viewed | signed | declined | expired
    state: str
    signed_at: datetime | None = None
    signed_document: bytes | None = None


class SignatureProvider(ABC):
    """The contract every e-signature vendor adapter satisfies."""

    name: str = "abstract"

    @abstractmethod
    async def send_for_signature(
        self,
        *,
        document: bytes,
        file_name: str,
        signer_name: str,
        signer_email: str,
        signer_phone: str | None = None,
        reason: str | None = None,
    ) -> SignatureRequest:
        """Push a document to the vendor and return the tracking id."""

    @abstractmethod
    async def get_status(self, request_id: str) -> SignatureStatus:
        """Poll a request. Used as a fallback when a webhook is missed."""

    @abstractmethod
    async def download_signed(self, request_id: str) -> bytes:
        """Fetch the signed document."""

    @abstractmethod
    def verify_webhook(self, *, raw_body: bytes, headers: dict[str, str]) -> bool:
        """Confirm a webhook actually came from the provider.

        Called *before* the payload is parsed or trusted. Returning True on an
        unverifiable payload turns this endpoint into a way for anyone to mark
        any letter signed.
        """

    @abstractmethod
    def parse_webhook(self, payload: dict[str, Any]) -> SignatureStatus:
        """Turn a verified provider payload into our own status shape."""


class NoopProvider(SignatureProvider):
    """Local development and tests.

    Signs immediately and keeps everything in memory. `verify_webhook` still
    checks a shared secret, so the verification path is exercised in
    development rather than only in production — a check that is bypassed
    locally is a check nobody has tested.
    """

    name = "noop"

    def __init__(self, webhook_secret: str = "dev-secret") -> None:
        self._webhook_secret = webhook_secret
        self._documents: dict[str, bytes] = {}

    async def send_for_signature(
        self,
        *,
        document: bytes,
        file_name: str,
        signer_name: str,
        signer_email: str,
        signer_phone: str | None = None,
        reason: str | None = None,
    ) -> SignatureRequest:
        request_id = f"noop-{uuid.uuid4().hex[:16]}"
        self._documents[request_id] = document
        return SignatureRequest(
            provider=self.name,
            request_id=request_id,
            signing_url=f"https://example.invalid/sign/{request_id}",
        )

    async def get_status(self, request_id: str) -> SignatureStatus:
        if request_id not in self._documents:
            raise SignatureError(f"Unknown signature request {request_id}")
        return SignatureStatus(
            request_id=request_id, state="signed", signed_at=datetime.now(UTC)
        )

    async def download_signed(self, request_id: str) -> bytes:
        document = self._documents.get(request_id)
        if document is None:
            raise SignatureError(f"Unknown signature request {request_id}")
        return document

    def verify_webhook(self, *, raw_body: bytes, headers: dict[str, str]) -> bool:
        return _verify_hmac_sha256(
            raw_body=raw_body,
            secret=self._webhook_secret,
            supplied=_header(headers, "x-askmira-signature"),
        )

    def parse_webhook(self, payload: dict[str, Any]) -> SignatureStatus:
        return SignatureStatus(
            request_id=payload["request_id"],
            state=payload.get("state", "signed"),
            signed_at=datetime.now(UTC),
        )


class DigioProvider(SignatureProvider):
    """Digio adapter.

    Digio is Indian, Aadhaar-eSign capable, and materially cheaper than
    DocuSign for this market.

    **Unverified against a live account.** The endpoints and payload shapes
    below follow Digio's published API, but no request in this class has been
    executed against a real sandbox. Before launch: obtain credentials, run the
    full send -> sign -> webhook -> download cycle, and correct whatever differs.
    Treat this as a scaffold with the right shape, not as working integration.
    """

    name = "digio"
    BASE_URL = "https://api.digio.in"

    def __init__(self, client_id: str, client_secret: str, webhook_secret: str) -> None:
        if not client_id or not client_secret:
            raise SignatureError("Digio requires a client id and secret")
        self._client_id = client_id
        self._client_secret = client_secret
        self._webhook_secret = webhook_secret

    async def send_for_signature(
        self,
        *,
        document: bytes,
        file_name: str,
        signer_name: str,
        signer_email: str,
        signer_phone: str | None = None,
        reason: str | None = None,
    ) -> SignatureRequest:
        import base64

        import httpx

        payload = {
            "file_name": file_name,
            "file_data": base64.b64encode(document).decode("ascii"),
            "signers": [
                {
                    "identifier": signer_email,
                    "name": signer_name,
                    "reason": reason or "Employment document",
                    "sign_type": "aadhaar",
                }
            ],
            "expire_in_days": 15,
            "display_on_page": "all",
        }

        async with httpx.AsyncClient(base_url=self.BASE_URL, timeout=30) as client:
            response = await client.post(
                "/v2/client/document/uploadpdf",
                json=payload,
                auth=(self._client_id, self._client_secret),
            )
        if response.status_code >= 400:
            raise SignatureError(
                f"Digio rejected the signature request ({response.status_code}): "
                f"{response.text[:300]}"
            )

        body = response.json()
        return SignatureRequest(
            provider=self.name,
            request_id=body["id"],
            signing_url=body.get("signing_parties", [{}])[0].get("signature_url"),
        )

    async def get_status(self, request_id: str) -> SignatureStatus:
        import httpx

        async with httpx.AsyncClient(base_url=self.BASE_URL, timeout=30) as client:
            response = await client.get(
                f"/v2/client/document/{request_id}",
                auth=(self._client_id, self._client_secret),
            )
        if response.status_code >= 400:
            raise SignatureError(f"Digio status check failed ({response.status_code})")

        body = response.json()
        return SignatureStatus(
            request_id=request_id,
            state=str(body.get("agreement_status", "requested")).lower(),
        )

    async def download_signed(self, request_id: str) -> bytes:
        import httpx

        async with httpx.AsyncClient(base_url=self.BASE_URL, timeout=60) as client:
            response = await client.get(
                f"/v2/client/document/download?document_id={request_id}",
                auth=(self._client_id, self._client_secret),
            )
        if response.status_code >= 400:
            raise SignatureError(f"Digio download failed ({response.status_code})")
        return response.content

    def verify_webhook(self, *, raw_body: bytes, headers: dict[str, str]) -> bool:
        return _verify_hmac_sha256(
            raw_body=raw_body,
            secret=self._webhook_secret,
            supplied=_header(headers, "x-digio-signature"),
        )

    def parse_webhook(self, payload: dict[str, Any]) -> SignatureStatus:
        entity = payload.get("entities", {}).get("document", payload)
        state = str(entity.get("agreement_status", "")).lower()
        return SignatureStatus(
            request_id=entity.get("id", ""),
            state="signed" if state in {"completed", "signed"} else state or "requested",
            signed_at=datetime.now(UTC) if state in {"completed", "signed"} else None,
        )


# ── Helpers ─────────────────────────────────────────────────────────────────


def _header(headers: dict[str, str], name: str) -> str:
    """Case-insensitive header lookup. HTTP header names are not case-sensitive."""
    lowered = {key.lower(): value for key, value in headers.items()}
    return lowered.get(name.lower(), "")


def _verify_hmac_sha256(*, raw_body: bytes, secret: str, supplied: str) -> bool:
    """Constant-time HMAC comparison over the raw request body.

    Over the *raw* bytes, not a re-serialised dict: JSON key order and
    whitespace are not stable across parse-and-dump, so verifying a
    round-tripped body would reject valid webhooks and, worse, tempt someone
    into disabling the check.
    """
    if not supplied or not secret:
        return False

    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    # Providers vary on prefixing; accept "sha256=..." as well as the bare hex.
    candidate = supplied.split("=", 1)[-1].strip() if "=" in supplied else supplied.strip()
    return hmac.compare_digest(expected, candidate)


def sign_payload(payload: dict[str, Any], secret: str) -> tuple[bytes, str]:
    """Produce a body and its signature. Test and development helper."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return raw, hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


_PROVIDERS: dict[str, type[SignatureProvider]] = {
    "noop": NoopProvider,
    "digio": DigioProvider,
}


def get_provider(name: str, **kwargs: Any) -> SignatureProvider:
    """Construct a provider by name.

    Raises on an unknown name rather than falling back to Noop: silently
    downgrading to a provider that signs everything instantly would be a
    catastrophic default in production.
    """
    provider_class = _PROVIDERS.get(name.lower())
    if provider_class is None:
        raise SignatureError(
            f"Unknown signature provider '{name}'. Available: "
            f"{', '.join(sorted(_PROVIDERS))}"
        )
    return provider_class(**kwargs)
