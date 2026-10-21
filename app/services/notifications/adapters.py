"""Channel adapters.

Every provider SDK in the system lives behind this interface, and nowhere else.
A module that wants to tell someone something raises an event; it never knows
which channel carried it, and it certainly never imports an email SDK.

Adapters distinguish two kinds of failure, and the distinction is the whole
point of the type:

- **Transient** — the provider timed out, rate-limited us, returned a 5xx.
  Retry with backoff.
- **Permanent** — the address is malformed, the recipient unsubscribed, the
  template was rejected. Retrying is pointless and, for an unsubscribe,
  actively harmful.

Treating everything as transient burns a rate limit re-sending to an address
that will never accept mail. Treating everything as permanent loses messages to
a five-second outage.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

__all__ = [
    "ChannelAdapter",
    "ConsoleEmailAdapter",
    "DeliveryResult",
    "ExpoPushAdapter",
    "InAppAdapter",
    "NullPushAdapter",
    "PermanentFailure",
    "SmtpEmailAdapter",
    "TransientFailure",
    "WhatsAppCloudAdapter",
]


class TransientFailure(RuntimeError):
    """Worth retrying: a timeout, a 5xx, a rate limit."""


class PermanentFailure(RuntimeError):
    """Not worth retrying: bad address, unsubscribed, rejected template."""


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    provider_message_id: str
    # Some providers confirm delivery synchronously; most confirm by webhook.
    delivered: bool = False


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """What an adapter needs, decoupled from the ORM.

    Keeping adapters off the model layer is what lets them be tested without a
    database and swapped without touching the pipeline.
    """

    recipient_name: str
    recipient_email: str | None
    recipient_phone: str | None
    subject: str | None
    body: str
    event_key: str
    # WhatsApp only: the approved Meta template and its ordered parameters.
    whatsapp_template_name: str | None = None
    whatsapp_parameters: list[str] | None = None
    push_token: str | None = None


class ChannelAdapter(ABC):
    """One channel's provider integration."""

    name: str = "abstract"

    @abstractmethod
    async def send(self, message: OutboundMessage) -> DeliveryResult:
        """Deliver, or raise `TransientFailure` / `PermanentFailure`."""


class ConsoleEmailAdapter(ChannelAdapter):
    """Development email adapter.

    Logs rather than sends, and validates the address the way a real provider
    would — so the permanent-failure path is exercised locally rather than
    discovered in production.

    Production uses SES; that adapter needs credentials and is not written
    speculatively here.
    """

    name = "email"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        import logging

        address = (message.recipient_email or "").strip()
        if not address or "@" not in address or address.startswith("@"):
            raise PermanentFailure(f"'{address}' is not a usable email address. Not retrying.")

        self.sent.append(message)
        logging.getLogger("askmira.notifications").info(
            "EMAIL to %s: %s", address, message.subject or message.event_key
        )
        return DeliveryResult(provider_message_id=f"console-{uuid.uuid4().hex[:16]}")


class InAppAdapter(ChannelAdapter):
    """In-app notifications.

    The `Notification` row *is* the message, so there is nothing to send. The
    row is created by the pipeline before dispatch; this marks it sent so the
    in-app feed and the SSE stream pick it up.
    """

    name = "in_app"

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        return DeliveryResult(provider_message_id=f"inapp-{uuid.uuid4().hex[:16]}", delivered=True)


class NullPushAdapter(ChannelAdapter):
    """Push, pending FCM/APNs credentials (slice 06 brings the mobile app).

    Fails permanently without a device token rather than pretending to send:
    a push nobody could have received should not be recorded as delivered.
    """

    name = "push"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        if not message.push_token:
            raise PermanentFailure(
                "The recipient has no registered device token, so there is nothing to "
                "push to. Not retrying."
            )
        self.sent.append(message)
        return DeliveryResult(provider_message_id=f"push-{uuid.uuid4().hex[:16]}")


class WhatsAppCloudAdapter(ChannelAdapter):
    """Meta WhatsApp Cloud API.

    Consent and template approval are checked *before* a message reaches this
    adapter — see `dispatch.py`. By the time send is called, both are already
    established; this adapter's job is the HTTP call and classifying what comes
    back.

    **Unverified against a live Meta account.** The request shape follows the
    published Cloud API contract, but no request here has run against a real
    business account. Before launch: obtain a WABA, register templates, and run
    the full send-and-webhook cycle.
    """

    name = "whatsapp"

    BASE_URL = "https://graph.facebook.com/v21.0"

    # Meta error codes that will never succeed on retry.
    PERMANENT_CODES: ClassVar[set[int]] = {
        131_026,  # message undeliverable — recipient not on WhatsApp
        131_047,  # re-engagement required, outside the 24-hour window
        132_000,  # template parameter count mismatch
        132_001,  # template does not exist
        132_005,  # template hydrated text too long
        132_007,  # template format character policy violation
        131_051,  # unsupported message type
    }

    def __init__(self, phone_number_id: str, access_token: str) -> None:
        if not phone_number_id or not access_token:
            raise PermanentFailure("WhatsApp needs a phone number id and an access token")
        self._phone_number_id = phone_number_id
        self._access_token = access_token

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        import httpx

        if not message.recipient_phone:
            raise PermanentFailure("The recipient has no phone number recorded")
        if not message.whatsapp_template_name:
            raise PermanentFailure(
                "A business-initiated WhatsApp message must name an approved template"
            )

        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": message.recipient_phone,
            "type": "template",
            "template": {
                "name": message.whatsapp_template_name,
                "language": {"code": "en"},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": value}
                            for value in (message.whatsapp_parameters or [])
                        ],
                    }
                ],
            },
        }

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    f"{self.BASE_URL}/{self._phone_number_id}/messages",
                    json=body,
                    headers={"Authorization": f"Bearer {self._access_token}"},
                )
        except httpx.HTTPError as exc:
            raise TransientFailure(f"Could not reach the WhatsApp API: {exc}") from exc

        if response.status_code >= 500 or response.status_code == 429:
            raise TransientFailure(f"WhatsApp returned {response.status_code}; will retry")

        if response.status_code >= 400:
            payload = response.json() if response.content else {}
            code = int((payload.get("error") or {}).get("code", 0))
            detail = (payload.get("error") or {}).get("message", response.text[:200])
            if code in self.PERMANENT_CODES:
                raise PermanentFailure(f"WhatsApp refused this message ({code}): {detail}")
            raise TransientFailure(f"WhatsApp error {code}: {detail}")

        payload = response.json()
        messages = payload.get("messages") or []
        if not messages:
            raise TransientFailure("WhatsApp accepted the request but returned no message id")

        return DeliveryResult(provider_message_id=messages[0]["id"])


class ExpoPushAdapter(ChannelAdapter):
    """Push to the AskMira app through Expo's push service.

    The app registers an Expo push token on sign-in; Expo relays to FCM or APNs
    with the credentials uploaded to the Expo project. A token Expo calls
    `DeviceNotRegistered` fails permanently, and the dispatcher revokes it.
    """

    name = "push"

    URL = "https://exp.host/--/api/v2/push/send"

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        import httpx

        if not message.push_token:
            raise PermanentFailure(
                "The recipient has no phone with the app signed in, so there is nothing to "
                "push to. Not retrying."
            )
        body = {
            "to": message.push_token,
            "title": (message.subject or "AskMira")[:120],
            "body": message.body[:1000],
            "data": {"event_key": message.event_key},
            "sound": "default",
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    self.URL, json=body, headers={"Accept": "application/json"}
                )
        except httpx.HTTPError as exc:
            raise TransientFailure(f"Could not reach Expo's push service: {exc}") from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise TransientFailure(f"Expo returned {response.status_code}; will retry")
        if response.status_code >= 400:
            raise PermanentFailure(f"Expo refused the push ({response.status_code})")
        ticket = (response.json() or {}).get("data") or {}
        if isinstance(ticket, list):
            ticket = ticket[0] if ticket else {}
        if ticket.get("status") == "error":
            reason = (ticket.get("details") or {}).get("error") or ticket.get("message", "")
            if reason == "DeviceNotRegistered":
                raise PermanentFailure("DeviceNotRegistered: the app is gone from that phone")
            raise TransientFailure(f"Expo could not send: {reason}")
        return DeliveryResult(provider_message_id=str(ticket.get("id") or "expo"))


def _under_test_suite() -> bool:
    import sys

    return "pytest" in sys.modules


class SmtpEmailAdapter(ChannelAdapter):
    """Email over SMTP, with nothing but the standard library.

    Works with any relay a company already has — Microsoft 365, Google
    Workspace, SES's SMTP interface, an on-premises Postfix — which is the
    realistic first integration for an Indian mid-market tenant, and needs no
    SDK in this codebase.

    Failure classification follows SMTP itself: a 4xx reply is transient and
    retried with backoff; a 5xx reply, a refused recipient or refused
    credentials is permanent. Unreachable relays (DNS, TCP, TLS) are transient.

    **It refuses to send from inside the test suite** unless a test injects its
    own `smtp_factory`. A test run that emails real people is an incident.
    """

    name = "email"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        from_address: str,
        username: str | None = None,
        password: str | None = None,
        use_starttls: bool = True,
        use_ssl: bool = False,
        from_name: str | None = None,
        timeout: int = 20,
        smtp_factory=None,
    ) -> None:
        if not host or not from_address or "@" not in from_address:
            raise ValueError("An SMTP relay needs a host and a sender address.")
        self._host = host
        self._port = port
        self._from_address = from_address
        self._from_name = from_name
        self._username = username
        self._password = password
        self._use_starttls = use_starttls
        self._use_ssl = use_ssl
        self._timeout = timeout
        self._smtp_factory = smtp_factory

    @property
    def relay(self) -> str:
        return f"{self._host}:{self._port}"

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        import asyncio
        from email.message import EmailMessage
        from email.utils import formataddr, make_msgid

        address = (message.recipient_email or "").strip()
        if (
            not address
            or "@" not in address
            or address.startswith("@")
            or any(ch in address for ch in "\r\n,;")
        ):
            raise PermanentFailure(f"'{address}' is not a usable email address. Not retrying.")
        if self._smtp_factory is None and _under_test_suite():
            raise PermanentFailure(
                "Refusing to send real email from the test suite. Inject an SMTP factory "
                "to exercise this adapter."
            )

        subject = (message.subject or message.event_key).replace("\r", " ").replace("\n", " ")
        email = EmailMessage()
        email["From"] = (
            formataddr((self._from_name, self._from_address))
            if self._from_name
            else self._from_address
        )
        email["To"] = address
        email["Subject"] = subject[:250]
        message_id = make_msgid(domain=self._from_address.rsplit("@", 1)[-1])
        email["Message-ID"] = message_id
        email.set_content(message.body)

        await asyncio.to_thread(self._deliver, email, address)
        return DeliveryResult(provider_message_id=message_id.strip("<>"))

    def _deliver(self, email, address: str) -> None:
        import smtplib
        import ssl

        factory = self._smtp_factory or (smtplib.SMTP_SSL if self._use_ssl else smtplib.SMTP)
        try:
            with factory(self._host, self._port, timeout=self._timeout) as client:
                if self._use_starttls and not self._use_ssl:
                    client.starttls(context=ssl.create_default_context())
                if self._username:
                    client.login(self._username, self._password or "")
                refused = client.send_message(email)
        except smtplib.SMTPRecipientsRefused as exc:
            raise PermanentFailure(f"The mail relay refused {address}: {exc.recipients}") from exc
        except smtplib.SMTPAuthenticationError as exc:
            raise PermanentFailure(
                "The mail relay refused the configured credentials. Fix the SMTP settings; "
                "retrying will not help."
            ) from exc
        except smtplib.SMTPSenderRefused as exc:
            raise PermanentFailure(
                f"The mail relay refused the sender address {self._from_address}."
            ) from exc
        except smtplib.SMTPConnectError as exc:
            raise TransientFailure(f"Could not connect to the mail relay: {exc}") from exc
        except smtplib.SMTPResponseException as exc:
            if 400 <= exc.smtp_code < 500:
                raise TransientFailure(
                    f"The mail relay deferred the message ({exc.smtp_code})."
                ) from exc
            raise PermanentFailure(
                f"The mail relay rejected the message ({exc.smtp_code})."
            ) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise TransientFailure(f"Could not reach the mail relay: {exc}") from exc
        if refused and address in refused:
            raise PermanentFailure(f"The mail relay refused {address}.")
