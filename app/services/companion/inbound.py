"""Messages to Ask Miraa from Slack, Microsoft Teams and email.

Each place proves the message is its own before anything is read:

* **Slack** signs every event with the app's signing secret (``v0=`` HMAC-SHA256
  over ``v0:<timestamp>:<body>``); older than five minutes is refused. Only a
  direct message to the app is answered, from a person whose Slack email is an
  active AskMiraa login of the workspace (``users.info``, scope users:read.email).
* **Teams** sends a Bot Framework activity with a JWT signed by Microsoft's keys
  (login.botframework.com), for this bot's app id and the activity's own
  service address. The person is found by their Entra object id in Graph.
* **Email** comes from the workspace's inbound mail provider, the one its admin
  chose, to a secret address only that provider knows. The sender is the one
  address in From, and it must be vouched for by its own domain: DKIM passed for
  the From domain, or SPF passed for an envelope sender that is the same address.
  A pass for some other domain proves nothing about who wrote. SendGrid Inbound
  Parse posts a fixed set of fields; Mailgun also posts every header the sender
  wrote, so its webhook signature is checked and a field sent twice is refused.
  Nothing is confirmed by an emailed reply: the person confirms in AskMiraa.

Then channels.answer asks Ask Miraa as that person and the reply goes back to
the same chat or mail thread. Slack and Teams want an answer within seconds, so
the router acknowledges at once and this runs in the background with its own
database session. Nobody is told whether an address belongs to anyone.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from email.utils import getaddresses
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.deps import tenant_filter
from app.models.notifications import Channel, TenantChatApp
from app.models.tenant import Tenant
from app.services.companion import channels
from app.services.notifications import dispatch
from app.services.notifications.adapters import (
    OutboundMessage,
    PermanentFailure,
    TransientFailure,
)

__all__ = [
    "EMAIL_PROVIDERS",
    "ask_mira_app",
    "email_settings",
    "email_status",
    "handle_email",
    "handle_slack",
    "handle_teams",
    "slack_signature_ok",
    "teams_claims",
]

logger = logging.getLogger("askmira.companion.inbound")

SLACK_MAX_AGE = 300
#: A Mailgun post older than this is refused (it re-sends only when it was not answered).
MAILGUN_MAX_AGE = 900
EMAIL_PROVIDERS = ("sendgrid", "mailgun")
BOTFRAMEWORK_OPENID = "https://login.botframework.com/v1/.well-known/openidconfiguration"
BOTFRAMEWORK_ISSUER = "https://api.botframework.com"


async def ask_mira_app(
    session: AsyncSession, tenant_id: uuid.UUID, provider: Channel
) -> TenantChatApp | None:
    row = await session.scalar(
        select(TenantChatApp).where(
            tenant_filter(TenantChatApp, tenant_id),
            TenantChatApp.provider == provider,
            TenantChatApp.enabled.is_(True),
            TenantChatApp.ask_mira.is_(True),
        )
    )
    return row if row is not None and row.secret_encrypted else None


# ── Slack ────────────────────────────────────────────────────────────────────


def slack_signature_ok(
    secret: str, timestamp: str, body: bytes, signature: str, *, now: float | None = None
) -> bool:
    """Whether Slack signed this body, recently."""
    try:
        sent = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((now or time.time()) - sent) > SLACK_MAX_AGE:
        return False
    base = b"v0:" + str(sent).encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


async def slack_secret(session: AsyncSession, tenant_id: uuid.UUID) -> str | None:
    row = await ask_mira_app(session, tenant_id, Channel.SLACK)
    if row is None or not row.signing_secret_encrypted:
        return None
    return decrypt_secret(row.signing_secret_encrypted, tenant_id) or None


async def handle_slack(session: AsyncSession, tenant_id: uuid.UUID, event: dict[str, Any]) -> str:
    """Answer one Slack direct message. Returns what happened, for the log."""
    if event.get("type") != "message" or event.get("channel_type") != "im":
        return "not a direct message"
    if event.get("bot_id") or event.get("subtype"):
        return "a bot's own message or an edit"
    row = await ask_mira_app(session, tenant_id, Channel.SLACK)
    if row is None:
        return "Ask Miraa is off in Slack"
    adapter = dispatch.chat_adapter(row)
    try:
        info = await adapter._call("users.info", params={"user": str(event.get("user") or "")})  # type: ignore[attr-defined]
    except (PermanentFailure, TransientFailure) as exc:
        return f"Slack could not say who wrote: {exc}"
    email = str(((info.get("user") or {}).get("profile") or {}).get("email") or "")
    acting = await channels.acting_person(session, tenant_id, email)
    person = acting.user
    if person is None:
        text = acting.refusal or (
            "I can only answer people who sign in to AskMiraa with the same work email as their "
            "Slack account."
        )
    else:
        reply = await channels.answer(
            session,
            person,
            str(event.get("text") or ""),
            place="slack",
            thread=str(event.get("channel")),
        )
        text = reply.text
    try:
        await adapter._call(  # type: ignore[attr-defined]
            "chat.postMessage",
            json={"channel": event.get("channel"), "text": text[:39000], "unfurl_links": False},
        )
    except (PermanentFailure, TransientFailure) as exc:
        return f"the answer could not be posted: {exc}"
    return "answered" if person else "not an AskMiraa login"


# ── Microsoft Teams ──────────────────────────────────────────────────────────

_jwks_clients: dict[str, Any] = {}


async def _jwks_uri() -> str:
    import httpx

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(BOTFRAMEWORK_OPENID)
        response.raise_for_status()
        return str(response.json()["jwks_uri"])


async def teams_claims(token: str, app_id: str, service_url: str) -> dict[str, Any] | None:
    """The activity's token checked against Microsoft's keys, or None."""
    import asyncio

    import httpx
    import jwt

    try:
        uri = await _jwks_uri()
        client = _jwks_clients.setdefault(uri, jwt.PyJWKClient(uri, cache_keys=True))
        key = await asyncio.to_thread(client.get_signing_key_from_jwt, token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=app_id,
            issuer=BOTFRAMEWORK_ISSUER,
            leeway=300,
        )
    except (jwt.PyJWTError, httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("A Teams activity's token was refused: %s", exc)
        return None
    if str(claims.get("serviceurl") or claims.get("serviceUrl") or "").rstrip(
        "/"
    ) != service_url.rstrip("/"):
        return None
    return claims


def _plain(html: str) -> str:
    text = re.sub(r"<at>.*?</at>", " ", html or "", flags=re.S)
    return re.sub(r"<[^>]+>", " ", text)


async def handle_teams(
    session: AsyncSession, tenant_id: uuid.UUID, activity: dict[str, Any]
) -> str:
    """Answer one Teams message to the bot (its token already checked)."""
    if activity.get("type") != "message":
        return "not a message"
    row = await ask_mira_app(session, tenant_id, Channel.TEAMS)
    if row is None:
        return "Ask Miraa is off in Teams"
    # Only Teams vouches for the Entra id in `from` (Web Chat and Direct Line let the
    # client write it), and only the workspace's own Microsoft 365 tenant is answered.
    directory = str(((activity.get("channelData") or {}).get("tenant") or {}).get("id") or "")
    if activity.get("channelId") != "msteams":
        return "not from Teams"
    if not row.directory_tenant_id or directory.lower() != row.directory_tenant_id.lower():
        return "from another Microsoft 365 tenant"
    adapter = dispatch.chat_adapter(row)
    sender = activity.get("from") or {}
    aad = str(sender.get("aadObjectId") or "")
    email = ""
    if re.fullmatch(r"[0-9a-fA-F-]{36}", aad):
        import httpx

        try:
            token = await adapter._token("https://graph.microsoft.com/.default")  # type: ignore[attr-defined]
            async with httpx.AsyncClient(timeout=10) as client:
                found = await client.get(
                    f"{adapter.GRAPH}/users/{aad}",  # type: ignore[attr-defined]
                    params={"$select": "mail,userPrincipalName"},
                    headers={"Authorization": f"Bearer {token}"},
                )
            body = found.json() if found.status_code == 200 else {}
            email = str(body.get("mail") or body.get("userPrincipalName") or "")
        except (PermanentFailure, TransientFailure, httpx.HTTPError, ValueError) as exc:
            logger.warning("Teams could not say who wrote: %s", exc)
    acting = await channels.acting_person(session, tenant_id, email)
    person = acting.user
    if person is None:
        text = acting.refusal or (
            "I can only answer people who sign in to AskMiraa with the same work email as their "
            "Microsoft 365 account."
        )
    else:
        thread = str((activity.get("conversation") or {}).get("id") or aad)[-24:]
        reply = await channels.answer(
            session, person, _plain(str(activity.get("text") or "")), place="teams", thread=thread
        )
        text = reply.text
    service = str(activity.get("serviceUrl") or "").rstrip("/")
    conversation = (activity.get("conversation") or {}).get("id")
    try:
        import httpx

        token = await adapter._token("https://api.botframework.com/.default")  # type: ignore[attr-defined]
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"{service}/v3/conversations/{conversation}/activities/{activity.get('id')}",
                json={"type": "message", "text": text[:25000], "textFormat": "plain",
                      "replyToId": activity.get("id")},
                headers={"Authorization": f"Bearer {token}"},
            )  # fmt: skip
    except (PermanentFailure, TransientFailure, httpx.HTTPError) as exc:
        return f"the answer could not be posted: {exc}"
    return "answered" if person else "not an AskMiraa login"


# ── Email ────────────────────────────────────────────────────────────────────

_ADDRESS = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def email_settings(tenant: Tenant) -> dict[str, Any]:
    return dict(((tenant.settings or {}).get("companion_email")) or {})


def token_ok(tenant: Tenant, token: str) -> bool:
    saved = email_settings(tenant)
    return bool(saved.get("enabled") and saved.get("token_hash")) and hmac.compare_digest(
        str(saved["token_hash"]), _hash(token or "")
    )


def _sender(fields: dict[str, str]) -> str:
    """The one address in From, or "" (none, several, or not an address).

    The display name is never read: ``"priya@company.com" <x@evil.in>`` is x@evil.in.
    """
    found = [address for _name, address in getaddresses([fields.get("from") or ""]) if address]
    if len(found) != 1:
        return ""
    address = found[0].strip().lower()
    return address if _ADDRESS.fullmatch(address) else ""


def _sendgrid_vouched(fields: dict[str, str], sender: str) -> bool:
    """DKIM passed for the From domain, or SPF passed for that same address as envelope sender."""
    domain = sender.rsplit("@", 1)[-1]
    try:
        envelope = json.loads(fields.get("envelope") or "{}")
    except ValueError:
        envelope = {}
    envelope_from = str(envelope.get("from") or "") if isinstance(envelope, dict) else ""
    spf = (fields.get("SPF") or "").strip().lower()
    if spf == "pass" and envelope_from.strip().lower() == sender:
        return True
    # "{@example.com : pass, @sendgrid.net : pass}": one result per signing domain.
    results = re.findall(r"@([\w.-]+)\s*:\s*(\w+)", fields.get("dkim") or "")
    return any(d.lower() == domain and r.lower() == "pass" for d, r in results)


_mailgun_seen: dict[str, float] = {}


def _mailgun_signed(fields: dict[str, str], key: str, *, now: float | None = None) -> bool:
    """Mailgun's own HMAC over timestamp+token with the workspace's signing key, used once."""
    timestamp, token = fields.get("timestamp") or "", fields.get("token") or ""
    signature = fields.get("signature") or ""
    if not (key and timestamp and token and signature):
        return False
    try:
        sent = int(timestamp)
    except ValueError:
        return False
    clock = now or time.time()
    if abs(clock - sent) > MAILGUN_MAX_AGE:
        return False
    expected = hmac.new(key.encode(), (timestamp + token).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False
    for seen, at in list(_mailgun_seen.items()):
        if clock - at > MAILGUN_MAX_AGE:
            _mailgun_seen.pop(seen, None)
    if token in _mailgun_seen:
        return False  # the same post again
    _mailgun_seen[token] = clock
    return True


def _mailgun_vouched(fields: dict[str, str], sender: str) -> bool:
    """SPF passed for an envelope sender that is the From address itself.

    Mailgun's DKIM result does not say for which domain it passed, so it is not used.
    """
    envelope = (fields.get("sender") or "").strip().lower()
    spf = (fields.get("X-Mailgun-Spf") or "").strip().lower()
    return spf == "pass" and envelope == sender


#: The fields read from an inbound post. One sent twice is refused: a header the
#: sender wrote (Mailgun posts them all) must never stand in for the provider's own.
_READ = frozenset(
    {"from", "envelope", "SPF", "dkim", "sender", "X-Mailgun-Spf", "timestamp", "token",
     "signature", "subject", "stripped-text", "text", "body-plain"}
)  # fmt: skip


def _verified(fields: dict[str, str], sender: str, saved: dict[str, Any], tenant_id: Any) -> bool:
    """Whether the workspace's own provider vouches that the From address wrote this."""
    if not sender:
        return False
    if (saved.get("provider") or "sendgrid") == "mailgun":
        key = decrypt_secret(saved.get("signing_key_encrypted") or "", tenant_id) or ""
        return _mailgun_signed(fields, key) and _mailgun_vouched(fields, sender)
    return _sendgrid_vouched(fields, sender)


def _words(fields: dict[str, str]) -> str:
    body = fields.get("stripped-text") or fields.get("text") or fields.get("body-plain") or ""
    kept = []
    for line in body.splitlines():
        if line.startswith(">") or re.match(r"^On .+ wrote:$", line.strip()):
            break
        kept.append(line)
    return "\n".join(kept).strip()


async def handle_email(
    session: AsyncSession,
    tenant: Tenant,
    fields: dict[str, str],
    *,
    repeated: frozenset[str] = frozenset(),
) -> str:
    """Answer one email to the workspace's Ask Miraa address (its token already checked).

    `repeated` names the fields the post carried more than once.
    """
    if repeated & _READ:
        return "a field came twice, so the provider's own values cannot be told apart"
    sender = _sender(fields)
    if not _verified(fields, sender, email_settings(tenant), tenant.id):
        return "the From address was not vouched for by its own domain (DKIM, or SPF aligned)"
    acting = await channels.acting_person(session, tenant.id, sender)
    person = acting.user
    if person is None and (acting.login is None or acting.suspended):
        return "the workspace is suspended" if acting.suspended else "not an AskMiraa login"
    subject = re.sub(
        r"^((re|fwd?|aw)\s*:\s*)+", "", fields.get("subject") or "", flags=re.I
    ).strip()
    thread = hashlib.sha256(subject.lower().encode()).hexdigest()[:12]
    question = _words(fields) or subject
    if person is None:
        # A login refused here (the two-step rule covers them) is told why, at their own address.
        assert acting.login is not None
        body, name, address = str(acting.refusal), acting.login.full_name, acting.login.email
    else:
        reply = await channels.answer(session, person, question, place="email", thread=thread)
        body, name, address = reply.text, person.full_name, person.email
    registered = dispatch.get_adapter(Channel.EMAIL)
    if registered is not None and registered is not dispatch._DEFAULT_EMAIL_ADAPTER:
        adapter = registered
    else:
        adapter, _relay = await dispatch.email_adapter_for(session, tenant.id)
    try:
        await adapter.send(
            OutboundMessage(
                recipient_name=name,
                recipient_email=address,
                recipient_phone=None,
                subject=f"Re: {subject}" if subject else "Ask Miraa",
                body=body,
                event_key="companion.email_reply",
            )
        )
    except (PermanentFailure, TransientFailure) as exc:
        return f"the answer could not be sent: {exc}"
    return "answered" if person is not None else "refused: " + str(acting.refusal)


# ── The admin's switch for email ─────────────────────────────────────────────


def email_url(tenant_id: uuid.UUID, token: str) -> str:
    from app.core.config import settings

    base = (settings.PUBLIC_API_URL or settings.WEB_APP_URL or "").rstrip("/")
    return f"{base}/api/v1/companion/inbound/email/{tenant_id}/{token}"


def email_status(tenant: Tenant | None) -> dict[str, Any]:
    saved = email_settings(tenant) if tenant is not None else {}
    return {
        "enabled": bool(saved.get("enabled")),
        "provider": saved.get("provider") or "sendgrid",
        "has_signing_key": bool(saved.get("signing_key_encrypted")),
    }


async def set_email(
    session: AsyncSession,
    user: Any,
    *,
    enabled: bool,
    provider: str | None = None,
    signing_key: str | None = None,
    request: Any = None,
) -> dict[str, Any]:
    """Switch Ask Miraa by email on (a new secret address, shown once) or off.

    `provider` is the workspace's inbound mail provider (the saved one when not
    given). Mailgun also needs its HTTP webhook signing key, kept encrypted.
    """
    import secrets

    from app.core import permissions
    from app.models.audit import AuditAction
    from app.services import audit

    if not permissions.is_admin(user):
        raise permissions.forbidden("Only an admin sets up Ask Miraa by email.")
    tenant = await session.get(Tenant, user.tenant_id)
    assert tenant is not None
    saved = email_settings(tenant)
    chosen = provider or saved.get("provider") or "sendgrid"
    if chosen not in EMAIL_PROVIDERS:
        raise HTTPException(status_code=422, detail="Choose SendGrid or Mailgun.")
    key = (signing_key or "").strip()
    kept = saved.get("provider") == "mailgun" and bool(saved.get("signing_key_encrypted"))
    if enabled and chosen == "mailgun":
        if key and not re.fullmatch(r"[\w-]{16,128}", key):
            raise HTTPException(
                status_code=422,
                detail=(
                    "That is not a Mailgun webhook signing key. Copy it from Mailgun: Sending, "
                    "Webhooks, HTTP webhook signing key."
                ),
            )
        if not key and not kept:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Mailgun needs its HTTP webhook signing key, so AskMiraa can tell Mailgun's "
                    "posts from anyone else's."
                ),
            )
    token = secrets.token_urlsafe(32) if enabled else ""
    saved.update(
        {"enabled": enabled, "token_hash": _hash(token) if enabled else None, "provider": chosen}
    )
    if chosen != "mailgun":
        saved.pop("signing_key_encrypted", None)
    elif key:
        saved["signing_key_encrypted"] = encrypt_secret(key, tenant.id)
    tenant.settings = {**(tenant.settings or {}), "companion_email": saved}
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="tenant",
        entity_id=tenant.id,
        actor=user,
        changes={
            "companion_email": {"enabled": enabled, "provider": chosen, "new_address": enabled}
        },
        summary="Ask Miraa by email "
        + ("switched on with a new address" if enabled else "switched off"),
        request=request,
    )
    await session.commit()
    return {
        **email_status(tenant),
        "inbound_url": email_url(tenant.id, token) if enabled else None,
    }
