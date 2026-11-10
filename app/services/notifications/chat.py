"""Slack and Microsoft Teams: a person's notifications as direct chat messages.

Each workspace connects its own app — a Slack app in its Slack workspace, a
Teams bot in its Microsoft 365 tenant — and AskMiraa messages each person
directly, found by their work email. Approval requests carry the same signed
Approve and Reject buttons as the approval email (approvals.email_actions):
each names one approver and one request, so a forwarded message decides
nothing for anyone else, and once the request is decided the link cannot
change it.

Like every adapter here, these know nothing about the database. Where a person
was found comes in on the message (`chat_address`) and goes back on the result
(`DeliveryResult.recipient_address`); dispatch keeps it.

Failures are typed as elsewhere: a person with no account there, a revoked
token or a missing permission will not fix themselves (permanent); a rate limit,
a timeout or a 5xx will (transient).
"""

from __future__ import annotations

import time
import urllib.parse
from typing import Any, ClassVar

from app.services.notifications.adapters import (
    ChannelAdapter,
    DeliveryResult,
    MessageAction,
    OutboundMessage,
    PermanentFailure,
    TransientFailure,
)

__all__ = [
    "ChatCheck",
    "SlackAdapter",
    "TeamsAdapter",
    "slack_blocks",
    "slack_manifest",
    "teams_card",
    "teams_package",
]

TIMEOUT = 15

ChatCheck = dict[str, Any]


def _client():
    import httpx

    return httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False)


def _buttons(message: OutboundMessage) -> tuple[MessageAction, ...]:
    return message.actions


# ═══════════════════════════════════════════════════════════════════════════
# Slack
# ═══════════════════════════════════════════════════════════════════════════


def slack_blocks(message: OutboundMessage) -> list[dict[str, Any]]:
    """The message as Slack Block Kit: the subject in bold, the body, then the buttons."""
    text = message.body.strip()
    if message.subject:
        text = f"*{_slack_escape(message.subject)}*\n{_slack_escape(text)}"
    else:
        text = _slack_escape(text)
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}
    ]
    buttons = _buttons(message)
    if buttons:
        elements = []
        for index, action in enumerate(buttons[:5]):
            button: dict[str, Any] = {
                "type": "button",
                "text": {"type": "plain_text", "text": action.label[:75]},
                "url": action.url,
                "action_id": f"askmira_{index}",
            }
            if action.style in ("primary", "danger"):
                button["style"] = action.style
            elements.append(button)
        blocks.append({"type": "actions", "elements": elements})
    return blocks


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackAdapter(ChannelAdapter):
    """A direct message from the workspace's own Slack app (bot token, xoxb-…).

    The app needs the bot scopes `chat:write`, `users:read` and
    `users:read.email` — the manifest AskMiraa gives the admin asks for exactly
    those. Buttons are links (to AskMiraa's signed approval pages), so the app
    needs no interactivity endpoint.
    """

    name = "slack"

    API = "https://slack.com/api"

    #: Slack's own error codes that will not change on a retry.
    PERMANENT: ClassVar[set[str]] = {
        "users_not_found",
        "user_not_found",
        "channel_not_found",
        "cannot_dm_bot",
        "user_disabled",
        "invalid_auth",
        "not_authed",
        "account_inactive",
        "token_revoked",
        "token_expired",
        "missing_scope",
        "no_permission",
        "ekm_access_denied",
        "team_access_not_granted",
        "invalid_blocks",
        "msg_too_long",
    }
    #: What each permanent failure means, in words the admin can act on.
    EXPLAIN: ClassVar[dict[str, str]] = {
        "users_not_found": "No one in the Slack workspace has this person's email.",
        "user_not_found": "No one in the Slack workspace has this person's email.",
        "invalid_auth": "Slack did not accept the bot token. Paste it again.",
        "not_authed": "Slack did not accept the bot token. Paste it again.",
        "token_revoked": "The Slack app was removed or its token revoked. Reinstall it.",
        "account_inactive": "The Slack app was removed or its token revoked. Reinstall it.",
        "missing_scope": (
            "The Slack app is missing a permission. It needs chat:write, users:read and "
            "users:read.email — create it from AskMiraa's manifest."
        ),
        "user_disabled": "This person's Slack account is deactivated.",
    }

    def __init__(self, bot_token: str) -> None:
        if not bot_token:
            raise PermanentFailure("Slack needs the app's bot token.")
        self._token = bot_token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    async def _call(self, method: str, *, json: dict | None = None, params: dict | None = None):
        import httpx

        try:
            async with _client() as client:
                if json is not None:
                    response = await client.post(
                        f"{self.API}/{method}", json=json, headers=self._headers()
                    )
                else:
                    response = await client.get(
                        f"{self.API}/{method}", params=params, headers=self._headers()
                    )
        except httpx.HTTPError as exc:
            raise TransientFailure(f"Could not reach Slack: {exc}") from exc
        if response.status_code == 429:
            wait = response.headers.get("Retry-After", "a few")
            raise TransientFailure(f"Slack asked to slow down; retrying in {wait} seconds.")
        if response.status_code >= 500:
            raise TransientFailure(f"Slack returned {response.status_code}; will retry.")
        try:
            body = response.json()
        except ValueError as exc:
            raise TransientFailure(f"Slack sent back something unexpected ({exc}).") from exc
        if not body.get("ok"):
            code = str(body.get("error") or "unknown_error")
            words = self.EXPLAIN.get(code, f"Slack said {code}.")
            if code in self.PERMANENT:
                raise PermanentFailure(words)
            raise TransientFailure(words)
        return body

    async def check(self) -> ChatCheck:
        """Who the token answers as: the Slack workspace's name."""
        body = await self._call("auth.test", json={})
        return {"connected_as": body.get("team") or body.get("url") or "Slack"}

    async def lookup(self, email: str) -> str:
        body = await self._call("users.lookupByEmail", params={"email": email})
        user = body.get("user") or {}
        if not user.get("id"):
            raise PermanentFailure("No one in the Slack workspace has this person's email.")
        if user.get("deleted"):
            raise PermanentFailure("This person's Slack account is deactivated.")
        return str(user["id"])

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        email = (message.recipient_email or "").strip()
        address = message.chat_address
        if not address:
            if not email:
                raise PermanentFailure("The person has no email to find them in Slack by.")
            address = await self.lookup(email)
        payload = {
            "channel": address,
            "text": (message.subject or message.body)[:300],
            "blocks": slack_blocks(message),
            "unfurl_links": False,
        }
        try:
            body = await self._call("chat.postMessage", json=payload)
        except PermanentFailure:
            # The address kept from last time may be stale (a re-created account).
            if not message.chat_address or not email:
                raise
            address = await self.lookup(email)
            body = await self._call("chat.postMessage", json={**payload, "channel": address})
        return DeliveryResult(
            provider_message_id=f"slack-{body.get('channel', '')}-{body.get('ts', '')}",
            recipient_address=address,
        )


def slack_manifest(portal_url: str | None, events_url: str | None = None) -> dict[str, Any]:
    """The Slack app manifest: api.slack.com → Create an app → From a manifest.

    With `events_url` (Ask Miraa on in Slack) people may message the app, and Slack
    sends each direct message there.
    """
    manifest: dict[str, Any] = {
        "display_information": {
            "name": "AskMiraa",
            "description": "Approvals and updates from AskMiraa, your HR workspace.",
            "background_color": "#7d0712",
        },
        "features": {
            "bot_user": {"display_name": "AskMiraa", "always_online": True},
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": True,
            },
        },
        "oauth_config": {
            "scopes": {"bot": ["chat:write", "users:read", "users:read.email"]},
        },
        "settings": {
            "org_deploy_enabled": False,
            "socket_mode_enabled": False,
            "token_rotation_enabled": False,
        },
    }
    if events_url:
        manifest["features"]["app_home"]["messages_tab_read_only_enabled"] = False
        manifest["oauth_config"]["scopes"]["bot"].append("im:history")
        manifest["settings"]["event_subscriptions"] = {
            "request_url": events_url,
            "bot_events": ["message.im"],
        }
    if portal_url:
        reads = (
            "It reads each person's email, to find them, and the direct messages people send "
            "the app, to answer them as Ask Miraa; nothing else in Slack."
            if events_url
            else "It reads nothing in Slack except each person's email, to find them."
        )
        manifest["display_information"]["long_description"] = (
            "AskMiraa sends each person the approvals waiting on them and the updates they "
            f"chose, with buttons that open {portal_url.rstrip('/')}. {reads}"
        )
    return manifest


# ═══════════════════════════════════════════════════════════════════════════
# Microsoft Teams
# ═══════════════════════════════════════════════════════════════════════════


def teams_card(message: OutboundMessage) -> dict[str, Any]:
    """The message as an Adaptive Card: the subject, the body, then the buttons."""
    body: list[dict[str, Any]] = []
    if message.subject:
        body.append(
            {"type": "TextBlock", "text": message.subject, "weight": "Bolder", "wrap": True}
        )
    body.append({"type": "TextBlock", "text": message.body.strip()[:4000], "wrap": True})
    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }
    buttons = _buttons(message)
    if buttons:
        card["actions"] = [
            {
                "type": "Action.OpenUrl",
                "title": action.label[:60],
                "url": action.url,
                **(
                    {"style": "positive" if action.style == "primary" else "destructive"}
                    if action.style in ("primary", "danger")
                    else {}
                ),
            }
            for action in buttons[:5]
        ]
    return card


class TeamsAdapter(ChannelAdapter):
    """A one-to-one chat message from the workspace's own Teams bot (Azure Bot).

    The admin registers a single-tenant Azure Bot in their Microsoft 365
    tenant, gives its app registration the Microsoft Graph application
    permission `User.Read.All` (to find each person by email), and installs the
    AskMiraa Teams app — the package AskMiraa gives them — for their people.
    AskMiraa then signs in as the bot (client credentials), finds the person,
    opens a one-to-one conversation and posts an Adaptive Card.
    """

    name = "teams"

    LOGIN = "https://login.microsoftonline.com"
    GRAPH = "https://graph.microsoft.com/v1.0"
    #: Bot Framework's global service address for proactive messages.
    SERVICE = "https://smba.trafficmanager.net/teams/"

    _tokens: ClassVar[dict[tuple[str, str, str], tuple[str, float]]] = {}

    def __init__(self, app_id: str, app_secret: str, directory_tenant_id: str) -> None:
        if not (app_id and app_secret and directory_tenant_id):
            raise PermanentFailure(
                "Teams needs the bot's app id, its client secret and your Microsoft 365 tenant id."
            )
        self._app_id = app_id
        self._secret = app_secret
        self._directory = directory_tenant_id

    async def _token(self, scope: str) -> str:
        import httpx

        key = (self._directory, self._app_id, scope)
        cached = self._tokens.get(key)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        try:
            async with _client() as client:
                response = await client.post(
                    f"{self.LOGIN}/{self._directory}/oauth2/v2.0/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._app_id,
                        "client_secret": self._secret,
                        "scope": scope,
                    },
                )
        except httpx.HTTPError as exc:
            raise TransientFailure(f"Could not reach Microsoft sign-in: {exc}") from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise TransientFailure(f"Microsoft sign-in returned {response.status_code}.")
        body = response.json() if response.content else {}
        if response.status_code >= 400 or not body.get("access_token"):
            detail = body.get("error_description") or body.get("error") or response.text[:200]
            raise PermanentFailure(
                "Microsoft did not accept the bot's app id, secret or tenant id: "
                f"{str(detail).splitlines()[0][:300]}"
            )
        token = str(body["access_token"])
        self._tokens[key] = (token, time.time() + float(body.get("expires_in") or 3000))
        return token

    async def check(self) -> ChatCheck:
        """Sign in as the bot, for both Bot Framework and Graph."""
        await self._token("https://api.botframework.com/.default")
        await self._token("https://graph.microsoft.com/.default")
        return {"connected_as": f"Microsoft 365 tenant {self._directory}"}

    async def _graph_user(self, email: str) -> str:
        import httpx

        token = await self._token("https://graph.microsoft.com/.default")
        headers = {"Authorization": f"Bearer {token}"}
        quoted = urllib.parse.quote(email, safe="@")
        try:
            async with _client() as client:
                response = await client.get(
                    f"{self.GRAPH}/users/{quoted}", params={"$select": "id"}, headers=headers
                )
                if response.status_code == 404:
                    # The work email may be the mailbox, not the sign-in name.
                    escaped = email.replace("'", "''")
                    response = await client.get(
                        f"{self.GRAPH}/users",
                        params={"$filter": f"mail eq '{escaped}'", "$select": "id"},
                        headers=headers,
                    )
        except httpx.HTTPError as exc:
            raise TransientFailure(f"Could not reach Microsoft Graph: {exc}") from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise TransientFailure(f"Microsoft Graph returned {response.status_code}.")
        if response.status_code in (401, 403):
            raise PermanentFailure(
                "The bot may not look people up. Give its app registration the Microsoft "
                "Graph application permission User.Read.All, with admin consent."
            )
        body = response.json() if response.content else {}
        found = body.get("id") or next(iter(body.get("value") or []), {}).get("id")
        if response.status_code >= 400 or not found:
            raise PermanentFailure("No one in your Microsoft 365 tenant has this person's email.")
        return str(found)

    async def _conversation(self, aad_id: str) -> str:
        import httpx

        token = await self._token("https://api.botframework.com/.default")
        body = {
            "bot": {"id": f"28:{self._app_id}"},
            "members": [{"id": f"8:orgid:{aad_id}", "aadObjectId": aad_id}],
            "isGroup": False,
            "tenantId": self._directory,
            "channelData": {"tenant": {"id": self._directory}},
        }
        try:
            async with _client() as client:
                response = await client.post(
                    f"{self.SERVICE}v3/conversations",
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            raise TransientFailure(f"Could not reach Teams: {exc}") from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise TransientFailure(f"Teams returned {response.status_code}; will retry.")
        if response.status_code in (401, 403, 404):
            raise PermanentFailure(
                "Teams would not open a chat with this person. Install the AskMiraa app for "
                "them (Teams admin centre → Setup policies → Installed apps)."
            )
        payload = response.json() if response.content else {}
        if response.status_code >= 400 or not payload.get("id"):
            raise PermanentFailure(f"Teams refused the chat: {response.text[:200]}")
        return str(payload["id"])

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        import httpx

        aad_id, _, conversation = (message.chat_address or "").partition("|")
        if not conversation:
            email = (message.recipient_email or "").strip()
            if not email:
                raise PermanentFailure("The person has no email to find them in Teams by.")
            aad_id = await self._graph_user(email)
            conversation = await self._conversation(aad_id)
        token = await self._token("https://api.botframework.com/.default")
        activity = {
            "type": "message",
            "text": message.subject or "",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": teams_card(message),
                }
            ],
        }
        path = urllib.parse.quote(conversation, safe=":@;=-_.")
        try:
            async with _client() as client:
                response = await client.post(
                    f"{self.SERVICE}v3/conversations/{path}/activities",
                    json=activity,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            raise TransientFailure(f"Could not reach Teams: {exc}") from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise TransientFailure(f"Teams returned {response.status_code}; will retry.")
        if response.status_code >= 400:
            raise PermanentFailure(
                "Teams did not take the message. If the AskMiraa app was removed for this "
                f"person, install it again ({response.status_code})."
            )
        payload = response.json() if response.content else {}
        return DeliveryResult(
            provider_message_id=f"teams-{payload.get('id', '')}",
            recipient_address=f"{aad_id}|{conversation}",
        )


def _png(size: int, rgba: tuple[int, int, int, int], *, ring: bool = False) -> bytes:
    """A plain square (or, for the outline icon, a white ring on transparent) as PNG."""
    import struct
    import zlib

    rows = bytearray()
    centre = (size - 1) / 2
    outer, inner = size * 0.42, size * 0.30
    for y in range(size):
        rows.append(0)
        for x in range(size):
            if ring:
                d = ((x - centre) ** 2 + (y - centre) ** 2) ** 0.5
                pixel = (255, 255, 255, 255) if inner <= d <= outer else (0, 0, 0, 0)
            else:
                pixel = rgba
            rows.extend(pixel)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


def teams_package(app_id: str, portal_url: str | None) -> bytes:
    """The Teams app package (manifest and icons) the admin uploads in the Teams admin centre."""
    import io
    import json
    import uuid
    import zipfile

    site = (portal_url or "https://askmira.com").rstrip("/")
    manifest = {
        "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.17/MicrosoftTeams.schema.json",
        "manifestVersion": "1.17",
        "version": "1.0.0",
        # Stable per bot, so uploading it again updates the same app.
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"askmira-teams:{app_id}")),
        "developer": {
            "name": "AskMiraa",
            "websiteUrl": site,
            "privacyUrl": f"{site}/privacy",
            "termsOfUseUrl": f"{site}/terms",
        },
        "name": {"short": "AskMiraa", "full": "AskMiraa HR"},
        "description": {
            "short": "Approvals and updates from AskMiraa.",
            "full": (
                "AskMiraa sends each person the approvals waiting on them and the updates they "
                "chose, with buttons that open AskMiraa. It reads nothing in Teams."
            ),
        },
        "icons": {"color": "color.png", "outline": "outline.png"},
        "accentColor": "#7D0712",
        "bots": [
            {
                "botId": app_id,
                "scopes": ["personal"],
                "supportsFiles": False,
                "isNotificationOnly": True,
            }
        ],
        "permissions": ["identity"],
        "validDomains": [urllib.parse.urlparse(site).hostname or "askmira.com"],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        archive.writestr("color.png", _png(192, (125, 7, 18, 255)))
        archive.writestr("outline.png", _png(32, (0, 0, 0, 0), ring=True))
    return buffer.getvalue()
