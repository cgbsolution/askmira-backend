"""The one place AskMira calls a language model.

Every feature asks for one structured answer: a JSON object validated against a
schema the feature wrote. Free text never flows from the model straight to a
screen — a feature reads named fields, so "the model said something odd" can
only ever be "a field held something odd", which a validator can catch.

Failures are exceptions with sentences a person can read. `AiUnavailableError` means
nothing was answered (not configured, unreachable, cut off, malformed);
`AiDeclinedError` means the model refused. Neither is ever papered over with a
guessed answer.

Tests never reach the network: `use_backend` swaps in a deterministic backend
for the length of a `with` block.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.config import settings

__all__ = [
    "AiDeclinedError",
    "AiUnavailableError",
    "Backend",
    "Completion",
    "configured",
    "model_name",
    "structured",
    "use_backend",
]

T = TypeVar("T", bound=BaseModel)

#: Opted in on every request. If the model's safety classifiers decline, the
#: API re-runs the request on Anthropic's recommended fallback model inside the
#: same call, routed by why it was declined, rather than returning nothing.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AiUnavailableError(Exception):
    """Nothing was answered. The message says why, in words a person can use."""


class AiDeclinedError(Exception):
    """The model declined the request."""


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    model: str
    input_tokens: int
    output_tokens: int

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class Backend(Protocol):
    async def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int,
        effort: str,
    ) -> Completion: ...


class AnthropicBackend:
    """Claude, through the official SDK."""

    def __init__(self, *, api_key: str, model: str) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=90.0, max_retries=2)
        self._model = model

    async def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int,
        effort: str,
    ) -> Completion:
        anthropic = self._anthropic
        try:
            response = await self._client.beta.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                # The system prompt is the stable part of every request for a
                # feature; caching it is free when it is long enough to count.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
                betas=[_FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as exc:
            raise AiUnavailableError("The AI provider rejected this server's API key.") from exc
        except anthropic.RateLimitError as exc:
            raise AiUnavailableError(
                "The AI provider is busy for this workspace. Try again in a minute."
            ) from exc
        except anthropic.APIStatusError as exc:
            raise AiUnavailableError(
                f"The AI provider returned an error ({exc.status_code}). Nothing was answered."
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise AiUnavailableError("The AI provider could not be reached.") from exc

        if response.stop_reason == "refusal":
            raise AiDeclinedError("The AI model declined this request.")
        if response.stop_reason == "max_tokens":
            raise AiUnavailableError(
                "The answer was cut off before it was complete, so it is not shown."
            )
        text = next((block.text for block in response.content if block.type == "text"), "")
        return Completion(
            text=text,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


_override: Backend | None = None
_default: AnthropicBackend | None = None


def configured() -> bool:
    """Whether this server can call a model at all."""
    return _override is not None or bool(settings.ANTHROPIC_API_KEY)


def model_name() -> str:
    return settings.AI_MODEL


def _backend() -> Backend:
    global _default
    if _override is not None:
        return _override
    if not settings.ANTHROPIC_API_KEY:
        raise AiUnavailableError(
            "No AI provider is configured on this server, so AI features are off."
        )
    if _default is None:
        _default = AnthropicBackend(api_key=settings.ANTHROPIC_API_KEY, model=settings.AI_MODEL)
    return _default


@contextmanager
def use_backend(backend: Backend) -> Iterator[Backend]:
    """Route every call through `backend` for the length of the block."""
    global _override
    previous = _override
    _override = backend
    try:
        yield backend
    finally:
        _override = previous


async def structured(
    *,
    system: str,
    user: str,
    output: type[T],
    schema: dict[str, Any],
    max_tokens: int = 4000,
    effort: str = "medium",
) -> tuple[T, Completion]:
    """One request, one validated object.

    `schema` is the JSON schema the API constrains the answer to; `output` is
    the Pydantic model that reads it back. Both are written by the feature, so
    a field the screen reads is a field the model was told to fill.
    """
    completion = await _backend().complete(
        system=system, user=user, schema=schema, max_tokens=max_tokens, effort=effort
    )
    try:
        return output.model_validate_json(completion.text), completion
    except ValidationError as exc:
        raise AiUnavailableError(
            "The AI answer did not match the shape it was asked for, so it is not shown."
        ) from exc
