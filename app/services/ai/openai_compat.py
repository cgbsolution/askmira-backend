"""OpenAI, Gemini and Ollama behind the same two calls as Claude.

AskMiraa keeps a conversation in one shape everywhere: Anthropic-style messages
(text, tool_use and tool_result blocks), which is how Ask Miraa stores and
replays it. A backend here translates that shape at its edge, both ways, so
the agent, the tools and the saved conversations never depend on which
provider a workspace chose — and a workspace can switch provider mid-way
through a conversation without breaking its history.

- `OpenAIBackend` speaks the Chat Completions API: OpenAI itself, and Gemini
  through Google's OpenAI-compatible endpoint.
- `OllamaBackend` speaks Ollama's own /api/chat, because only that one lets a
  request raise the context window; Ollama's default (a few thousand tokens)
  would silently cut Ask Miraa's tool list and history short.

A response's blocks go back unchanged within a turn, exactly as for Claude.
Gemini attaches a signature to each tool call that it checks on the next step;
it rides along in the block (`extra`) and is sent back as it came.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from app.core.config import settings
from app.services.ai.client import (
    AiDeclinedError,
    AiUnavailableError,
    Completion,
    Step,
    ToolCall,
)
from app.services.ai.providers import Route

__all__ = ["OllamaBackend", "OpenAIBackend", "to_chat_messages"]


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


def _get(block: Any, key: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if _get(block, "type") == "text":
            parts.append(str(_get(block, "text") or ""))
    return "\n\n".join(p for p in parts if p)


def to_chat_messages(
    system: str, messages: list[dict[str, Any]], *, arguments_as: str = "json"
) -> list[dict[str, Any]]:
    """Anthropic-shaped messages as chat messages: system, user, assistant, tool.

    `arguments_as="json"` writes a tool call's arguments as a JSON string (the
    OpenAI wire format); "object" keeps them an object (Ollama's).
    """
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    names: dict[str, str] = {}
    for message in messages:
        content = message.get("content")
        if message.get("role") == "user":
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
                continue
            texts: list[str] = []
            for block in content or []:
                kind = _get(block, "type")
                if kind == "tool_result":
                    call_id = str(_get(block, "tool_use_id"))
                    entry: dict[str, Any] = {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": _text_of(_get(block, "content")) or "(no result)",
                    }
                    if arguments_as == "object":
                        entry["tool_name"] = names.get(call_id, "")
                    out.append(entry)
                elif kind == "text":
                    texts.append(str(_get(block, "text") or ""))
            if texts:
                out.append({"role": "user", "content": "\n\n".join(texts)})
            continue

        texts = []
        calls: list[dict[str, Any]] = []
        for block in content if isinstance(content, list) else [content]:
            if isinstance(block, str):
                texts.append(block)
                continue
            kind = _get(block, "type")
            if kind == "text":
                texts.append(str(_get(block, "text") or ""))
            elif kind == "tool_use":
                call_id = str(_get(block, "id"))
                name = str(_get(block, "name"))
                names[call_id] = name
                arguments = dict(_get(block, "input") or {})
                call: dict[str, Any] = {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False)
                        if arguments_as == "json"
                        else arguments,
                    },
                }
                extra = _get(block, "extra")
                if extra:
                    call["extra_content"] = extra
                calls.append(call)
            # Reasoning blocks from another provider are not replayed.
        text = "\n\n".join(t for t in texts if t.strip())
        entry = {"role": "assistant", "content": text or ("" if calls else "…")}
        if calls:
            entry["tool_calls"] = calls
        out.append(entry)
    return out


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _strip_schema(schema: Any, drop: frozenset[str]) -> Any:
    if isinstance(schema, dict):
        return {k: _strip_schema(v, drop) for k, v in schema.items() if k not in drop}
    if isinstance(schema, list):
        return [_strip_schema(v, drop) for v in schema]
    return schema


def _error(label: str, exc: Exception) -> AiUnavailableError:
    if isinstance(exc, httpx.TimeoutException):
        return AiUnavailableError(f"{label} took too long to answer. Try again.")
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        detail = ""
        try:
            body = exc.response.json()
            error = body.get("error") if isinstance(body, dict) else None
            detail = str(error.get("message") if isinstance(error, dict) else error or "")[:200]
        except ValueError:
            detail = exc.response.text[:200]
        if code in (401, 403):
            return AiUnavailableError(f"{label} rejected the API key.")
        if code == 404:
            return AiUnavailableError(
                f"{label} does not know that model. Check the model name in Settings → AI."
                + (f" ({detail})" if detail else "")
            )
        if code == 429:
            return AiUnavailableError(f"{label} is busy for this workspace. Try again in a minute.")
        return AiUnavailableError(
            f"{label} returned an error ({code}). Nothing was answered."
            + (f" {detail}" if detail else "")
        )
    return AiUnavailableError(f"{label} could not be reached.")


class OpenAIBackend:
    """OpenAI's Chat Completions API, and Gemini's OpenAI-compatible endpoint."""

    def __init__(self, route: Route) -> None:
        self._route = route
        self._label = route.spec.label.split(" (")[0]
        headers = {"Content-Type": "application/json"}
        if route.api_key:
            headers["Authorization"] = f"Bearer {route.api_key}"
        self._client = httpx.AsyncClient(
            base_url=(route.base_url or "").rstrip("/") + "/",
            headers=headers,
            timeout=httpx.Timeout(120.0, connect=15.0),
        )

    def _body(self, *, max_tokens: int, effort: str) -> dict[str, Any]:
        model = self._route.model
        body: dict[str, Any] = {"model": model}
        if self._route.provider == "openai":
            # Reasoning models take this name for the cap and refuse `max_tokens`.
            body["max_completion_tokens"] = max_tokens
            if model.startswith(("gpt-5", "o1", "o3", "o4")):
                body["reasoning_effort"] = effort if effort in ("low", "medium", "high") else "low"
        else:
            body["max_tokens"] = max_tokens
            if self._route.provider == "gemini" and any(tag in model for tag in ("2.5", "-3")):
                body["reasoning_effort"] = effort if effort in ("low", "medium", "high") else "low"
        return body

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post("chat/completions", json=body)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise _error(self._label, exc) from exc
        if not isinstance(data, dict) or not data.get("choices"):
            raise AiUnavailableError(f"{self._label} sent back an answer this server cannot read.")
        return data

    def _check(self, choice: dict[str, Any]) -> None:
        reason = choice.get("finish_reason")
        message = choice.get("message") or {}
        if reason == "content_filter" or message.get("refusal"):
            raise AiDeclinedError("The AI model declined this request.")
        if reason == "length":
            raise AiUnavailableError(
                "The answer was cut off before it was complete, so it is not shown."
            )

    def _tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        drop = (
            frozenset({"additionalProperties"}) if self._route.provider == "gemini" else frozenset()
        )
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": _strip_schema(tool.get("input_schema") or {}, drop),
                },
            }
            for tool in tools
        ]

    async def complete(
        self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int, effort: str
    ) -> Completion:
        drop = (
            frozenset({"additionalProperties"}) if self._route.provider == "gemini" else frozenset()
        )
        body = self._body(max_tokens=max_tokens, effort=effort)
        body["messages"] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "answer",
                "schema": _strip_schema(schema, drop),
                "strict": False,
            },
        }
        data = await self._post(body)
        choice = data["choices"][0]
        self._check(choice)
        usage = data.get("usage") or {}
        return Completion(
            text=str((choice.get("message") or {}).get("content") or ""),
            model=str(data.get("model") or self._route.model),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )

    async def read_images(
        self,
        *,
        system: str,
        prompt: str,
        images: list[tuple[str, bytes]],
        schema: dict[str, Any],
        max_tokens: int,
        effort: str,
    ) -> Completion:
        """`complete` with pictures: each sent as a data URL beside the words."""
        drop = (
            frozenset({"additionalProperties"}) if self._route.provider == "gemini" else frozenset()
        )
        body = self._body(max_tokens=max_tokens, effort=effort)
        parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        parts.extend(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{_b64(data)}"},
            }
            for media_type, data in images
        )
        body["messages"] = [
            {"role": "system", "content": system},
            {"role": "user", "content": parts},
        ]
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "answer",
                "schema": _strip_schema(schema, drop),
                "strict": False,
            },
        }
        data = await self._post(body)
        choice = data["choices"][0]
        self._check(choice)
        usage = data.get("usage") or {}
        return Completion(
            text=str((choice.get("message") or {}).get("content") or ""),
            model=str(data.get("model") or self._route.model),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )

    async def step(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        effort: str,
        allow_tools: bool = True,
    ) -> Step:
        body = self._body(max_tokens=max_tokens, effort=effort)
        body["messages"] = to_chat_messages(system, messages)
        if tools:
            body["tools"] = self._tools(tools)
            body["tool_choice"] = "auto" if allow_tools else "none"
        data = await self._post(body)
        choice = data["choices"][0]
        self._check(choice)
        message = choice.get("message") or {}
        text = str(message.get("content") or "").strip()
        content: list[dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            call_id = str(raw.get("id") or f"call_{uuid.uuid4().hex[:12]}")
            arguments = _parse_arguments(function.get("arguments"))
            block: dict[str, Any] = {
                "type": "tool_use",
                "id": call_id,
                "name": str(function.get("name") or ""),
                "input": arguments,
            }
            if raw.get("extra_content"):
                block["extra"] = raw["extra_content"]
            content.append(block)
            calls.append(ToolCall(id=call_id, name=block["name"], input=arguments))
        usage = data.get("usage") or {}
        return Step(
            content=content,
            text=text,
            tool_calls=calls,
            stop_reason=str(choice.get("finish_reason") or ""),
            model=str(data.get("model") or self._route.model),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )


def ollama_root(base_url: str) -> str:
    """http://host:11434 from any of its usual spellings (…/v1, …/api)."""
    root = base_url.strip().rstrip("/")
    for suffix in ("/v1", "/api"):
        if root.endswith(suffix):
            root = root[: -len(suffix)]
    return root


class OllamaBackend:
    """An Ollama server, through its own chat API."""

    def __init__(self, route: Route) -> None:
        self._route = route
        headers = {"Content-Type": "application/json"}
        if route.api_key:
            headers["Authorization"] = f"Bearer {route.api_key}"
        # A model on a CPU can take minutes over a long prompt; the first call
        # also loads the model into memory.
        self._client = httpx.AsyncClient(
            base_url=ollama_root(route.base_url or "") + "/",
            headers=headers,
            timeout=httpx.Timeout(settings.OLLAMA_TIMEOUT_SECONDS, connect=15.0),
        )

    def _options(self, max_tokens: int) -> dict[str, Any]:
        return {"num_ctx": settings.OLLAMA_NUM_CTX, "num_predict": min(max_tokens, 8192)}

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        body = {**body, "stream": False, "keep_alive": "30m"}
        try:
            response = await self._client.post("api/chat", json=body)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise _error("Ollama", exc) from exc
        if not isinstance(data, dict) or "message" not in data:
            raise AiUnavailableError("Ollama sent back an answer this server cannot read.")
        if data.get("done_reason") == "length":
            raise AiUnavailableError(
                "The answer was cut off before it was complete, so it is not shown."
            )
        return data

    async def complete(
        self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int, effort: str
    ) -> Completion:
        data = await self._post(
            {
                "model": self._route.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "format": schema,
                "options": self._options(max_tokens),
            }
        )
        return Completion(
            text=str((data.get("message") or {}).get("content") or ""),
            model=str(data.get("model") or self._route.model),
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
        )

    async def read_images(
        self,
        *,
        system: str,
        prompt: str,
        images: list[tuple[str, bytes]],
        schema: dict[str, Any],
        max_tokens: int,
        effort: str,
    ) -> Completion:
        """`complete` with pictures. Only a vision model (llava, qwen2.5vl…) can read them;
        any other refuses, and that refusal is what the person is told."""
        data = await self._post(
            {
                "model": self._route.model,
                "messages": [
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": prompt,
                        "images": [_b64(item) for _, item in images],
                    },
                ],
                "format": schema,
                "options": self._options(max_tokens),
            }
        )
        return Completion(
            text=str((data.get("message") or {}).get("content") or ""),
            model=str(data.get("model") or self._route.model),
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
        )

    async def step(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        effort: str,
        allow_tools: bool = True,
    ) -> Step:
        body: dict[str, Any] = {
            "model": self._route.model,
            "messages": to_chat_messages(system, messages, arguments_as="object"),
            "options": self._options(max_tokens),
        }
        if tools and allow_tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema") or {},
                    },
                }
                for tool in tools
            ]
        data = await self._post(body)
        message = data.get("message") or {}
        text = str(message.get("content") or "").strip()
        content: list[dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            call_id = str(raw.get("id") or f"call_{uuid.uuid4().hex[:12]}")
            arguments = _parse_arguments(function.get("arguments"))
            name = str(function.get("name") or "")
            content.append({"type": "tool_use", "id": call_id, "name": name, "input": arguments})
            calls.append(ToolCall(id=call_id, name=name, input=arguments))
        return Step(
            content=content,
            text=text,
            tool_calls=calls if allow_tools else [],
            stop_reason=str(data.get("done_reason") or ""),
            model=str(data.get("model") or self._route.model),
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
        )
