"""Which model answers: the four providers, and how a workspace's choice resolves.

A workspace admin picks a provider in Settings → AI — Anthropic, OpenAI
(ChatGPT), Google Gemini or an Ollama server — and may save their own API key.
A workspace that has not chosen uses the server's `AI_PROVIDER`. A key the
workspace saved wins over the server's; with neither, that provider is not
available and AI features stay off (the screen says why).

Each provider is a registered sub-processor (services/dpdp/transfers.py) under
its own key, so the transfer log names the one that actually received a prompt.

Ollama runs wherever its owner runs it. An address set on the server
(`OLLAMA_BASE_URL`) may be private — it is the operator's own machine. An address
a workspace types is called from this server, so it must be public https:
otherwise a workspace could point the server at its own private network.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.core.config import settings

__all__ = [
    "PROVIDERS",
    "Provider",
    "Route",
    "UnsafeAddressError",
    "check_public_url",
    "provider",
    "server_route",
]


@dataclass(frozen=True, slots=True)
class Provider:
    key: str
    label: str
    #: The sub-processor that receives the prompt (transfers.SUB_PROCESSORS).
    sub_processor: str
    #: "anthropic" uses the Anthropic SDK; "openai" the OpenAI-compatible chat API.
    protocol: str
    needs_key: bool
    #: Models to suggest on the settings screen. Any other name the provider
    #: serves may be typed instead.
    suggested_models: tuple[str, ...]
    #: Where to get a key, for the settings screen.
    key_help: str = ""

    def default_model(self) -> str:
        return {
            "anthropic": settings.AI_MODEL,
            "openai": settings.OPENAI_MODEL,
            "gemini": settings.GEMINI_MODEL,
            "ollama": settings.OLLAMA_MODEL,
        }[self.key]

    def server_key(self) -> str | None:
        return {
            "anthropic": settings.ANTHROPIC_API_KEY,
            "openai": settings.OPENAI_API_KEY,
            "gemini": settings.GEMINI_API_KEY,
            "ollama": settings.OLLAMA_API_KEY,
        }[self.key] or None

    def server_base_url(self) -> str | None:
        return {
            "anthropic": None,
            "openai": settings.OPENAI_BASE_URL or "https://api.openai.com/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
            "ollama": settings.OLLAMA_BASE_URL,
        }[self.key] or None


PROVIDERS: dict[str, Provider] = {
    p.key: p
    for p in (
        Provider(
            key="anthropic",
            label="Anthropic (Claude)",
            sub_processor="anthropic",
            protocol="anthropic",
            needs_key=True,
            suggested_models=("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"),
            key_help="console.anthropic.com → API keys",
        ),
        Provider(
            key="openai",
            label="OpenAI (ChatGPT)",
            sub_processor="openai",
            protocol="openai",
            needs_key=True,
            suggested_models=("gpt-5", "gpt-5-mini", "gpt-4.1"),
            key_help="platform.openai.com → API keys",
        ),
        Provider(
            key="gemini",
            label="Google Gemini",
            sub_processor="google_ai",
            protocol="openai",
            needs_key=True,
            suggested_models=("gemini-2.5-pro", "gemini-2.5-flash"),
            key_help="aistudio.google.com → Get API key",
        ),
        Provider(
            key="ollama",
            label="Ollama (your own model server)",
            sub_processor="ollama",
            protocol="openai",
            needs_key=False,
            suggested_models=("qwen2.5:7b-instruct", "llama3.1:8b", "qwen3:8b", "mistral-nemo"),
            key_help="Only if your Ollama server sits behind a key.",
        ),
    )
}


def provider(key: str) -> Provider:
    try:
        return PROVIDERS[key]
    except KeyError as exc:
        raise ValueError(
            f"'{key}' is not an AI provider here. Choose one of {list(PROVIDERS)}."
        ) from exc


@dataclass(frozen=True, slots=True)
class Route:
    """Everything one call needs: who answers, with which model, key and address."""

    provider: str
    model: str
    #: Never printed: a route ends up in logs and tracebacks.
    api_key: str | None = field(repr=False)
    base_url: str | None
    #: "workspace" when the workspace saved the key (or address), else "server".
    key_source: str = "server"

    @property
    def spec(self) -> Provider:
        return PROVIDERS[self.provider]

    @property
    def sub_processor(self) -> str:
        return self.spec.sub_processor

    @property
    def available(self) -> bool:
        if self.spec.protocol == "anthropic":
            return bool(self.api_key)
        if self.spec.needs_key and not self.api_key:
            return False
        return bool(self.base_url)

    def cache_key(self) -> tuple[str, str, str, str]:
        return (self.provider, self.model, self.api_key or "", self.base_url or "")


def server_route(key: str | None = None) -> Route:
    """The server's own route for a provider (default: AI_PROVIDER)."""
    spec = provider(key or settings.AI_PROVIDER)
    return Route(
        provider=spec.key,
        model=spec.default_model(),
        api_key=spec.server_key(),
        base_url=spec.server_base_url(),
    )


# ── Addresses a workspace types ─────────────────────────────────────────────


class UnsafeAddressError(ValueError):
    """An address this server will not call on a workspace's say-so."""


def _blocked(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def check_public_url(url: str) -> str:
    """The address, tidied, if it is public https; else a reason a person can act on."""
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise UnsafeAddressError(
            "Give the full https address of your Ollama server, for example "
            "https://ollama.example.com/v1."
        )
    if parts.username or parts.password:
        raise UnsafeAddressError("Leave the user name and password out of the address.")
    host = parts.hostname
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, parts.port or 443, proto=socket.IPPROTO_TCP
        )
    except OSError as exc:
        raise UnsafeAddressError(f"{host} could not be found. Check the address.") from exc
    if not infos or any(_blocked(info[4][0]) for info in infos):
        raise UnsafeAddressError(
            f"{host} is a private or local address, which this server will not call for a "
            "workspace. Use your server's public https address, or ask the AskMiraa operator "
            "to set OLLAMA_BASE_URL on the server."
        )
    return url
