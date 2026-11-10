"""OpenAI, Gemini and Ollama behind the same calls as Claude, and the guard on addresses.

What these hold:

- a conversation kept in AskMiraa's one shape (text, tool_use, tool_result) is
  sent to a chat API as system, user, assistant-with-tool-calls and tool
  messages, in that order, and a reply's tool calls come back in the same shape;
- reasoning blocks from another provider are never replayed, and Gemini's
  signature on a tool call goes back as it came;
- a cut-off answer, a refusal, a rejected key and an unknown model are reported
  in words, never answered with a guess;
- Ollama is asked for a context window large enough for Ask Miraa's tools;
- a workspace may only name a public https address for its Ollama server;
- a route never prints its key.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.services.ai import client, openai_compat, providers
from app.services.ai.client import AiDeclinedError, AiUnavailableError
from app.services.ai.providers import Route, UnsafeAddressError, check_public_url

TOOLS = [
    {
        "name": "query_data",
        "description": "Figures as a chart.",
        "input_schema": {
            "type": "object",
            "properties": {"dataset": {"type": "string"}},
            "required": [],
            "additionalProperties": False,
        },
    }
]

CONVERSATION: list[dict[str, Any]] = [
    {"role": "user", "content": [{"type": "text", "text": "payroll trend"}]},
    {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "secret reasoning", "signature": "sig"},
            {"type": "text", "text": "Let me look."},
            {
                "type": "tool_use",
                "id": "call_1",
                "name": "query_data",
                "input": {"dataset": "payslip"},
                "extra": {"google": {"thought_signature": "abc"}},
            },
        ],
    },
    {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "Data: 3 months."},
            {"type": "text", "text": "and by department?"},
        ],
    },
]


def _route(provider: str, model: str = "m-1", key: str | None = "sk-test-123456") -> Route:
    base = {
        "openai": "https://api.openai.test/v1",
        "gemini": "https://gemini.test/v1beta/openai",
        "ollama": "http://ollama.test:11434/v1",
    }[provider]
    return Route(provider=provider, model=model, api_key=key, base_url=base)


class Recorder:
    """A transport that records each request and answers with the next reply."""

    def __init__(self, *replies: httpx.Response) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.replies.pop(0)

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def _backend(cls: type, route: Route, recorder: Recorder) -> Any:
    backend = cls(route)
    backend._client = httpx.AsyncClient(
        base_url=backend._client.base_url,
        headers=backend._client.headers,
        transport=httpx.MockTransport(recorder),
    )
    return backend


def _chat(message: dict[str, Any], finish: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "m-1",
            "choices": [{"message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        },
    )


# ── The shape, both ways ─────────────────────────────────────────────────────


def test_a_conversation_becomes_chat_messages_in_order() -> None:
    out = openai_compat.to_chat_messages("Be helpful.", CONVERSATION)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "user"]
    assistant = out[2]
    assert assistant["content"] == "Let me look."
    [call] = assistant["tool_calls"]
    assert call["id"] == "call_1" and call["function"]["name"] == "query_data"
    assert json.loads(call["function"]["arguments"]) == {"dataset": "payslip"}
    # Gemini's signature goes back with its call; the other provider's reasoning does not.
    assert call["extra_content"] == {"google": {"thought_signature": "abc"}}
    assert "secret reasoning" not in json.dumps(out)
    assert out[3] == {"role": "tool", "tool_call_id": "call_1", "content": "Data: 3 months."}
    assert out[4] == {"role": "user", "content": "and by department?"}


def test_ollama_gets_arguments_as_an_object_and_the_tool_name() -> None:
    out = openai_compat.to_chat_messages("s", CONVERSATION, arguments_as="object")
    assert out[2]["tool_calls"][0]["function"]["arguments"] == {"dataset": "payslip"}
    assert out[3]["tool_name"] == "query_data"


async def test_openai_step_reads_tool_calls_into_the_same_shape() -> None:
    recorder = Recorder(
        _chat(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_9",
                        "type": "function",
                        "function": {
                            "name": "query_data",
                            "arguments": '{"dataset": "leave_request"}',
                        },
                    }
                ],
            },
            "tool_calls",
        )
    )
    backend = _backend(openai_compat.OpenAIBackend, _route("openai", "gpt-5-mini"), recorder)
    step = await backend.step(
        system="s", messages=CONVERSATION, tools=TOOLS, max_tokens=900, effort="low"
    )
    assert [c.name for c in step.tool_calls] == ["query_data"]
    assert step.tool_calls[0].input == {"dataset": "leave_request"}
    assert step.content == [
        {
            "type": "tool_use",
            "id": "call_9",
            "name": "query_data",
            "input": {"dataset": "leave_request"},
        }
    ]
    assert step.input_tokens == 120 and step.output_tokens == 30
    body = recorder.body()
    # A reasoning model takes this cap, and the effort.
    assert body["max_completion_tokens"] == 900 and "max_tokens" not in body
    assert body["reasoning_effort"] == "low"
    assert body["tools"][0]["function"]["parameters"]["additionalProperties"] is False
    assert body["tool_choice"] == "auto"
    assert recorder.requests[0].headers["authorization"] == "Bearer sk-test-123456"
    assert recorder.requests[0].url.path.endswith("/v1/chat/completions")


async def test_gemini_is_sent_schemas_it_accepts_and_its_signature_is_kept() -> None:
    recorder = Recorder(
        _chat(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "fc_1",
                        "type": "function",
                        "function": {"name": "query_data", "arguments": "{}"},
                        "extra_content": {"google": {"thought_signature": "zzz"}},
                    }
                ],
            }
        )
    )
    backend = _backend(openai_compat.OpenAIBackend, _route("gemini", "gemini-2.5-flash"), recorder)
    step = await backend.step(
        system="s", messages=CONVERSATION, tools=TOOLS, max_tokens=500, effort="low"
    )
    assert step.content[0]["extra"] == {"google": {"thought_signature": "zzz"}}
    body = recorder.body()
    assert "additionalProperties" not in json.dumps(body["tools"])
    assert body["max_tokens"] == 500


async def test_closing_step_asks_for_words_only() -> None:
    recorder = Recorder(_chat({"role": "assistant", "content": "Here is the answer."}))
    backend = _backend(openai_compat.OpenAIBackend, _route("openai", "gpt-4.1"), recorder)
    step = await backend.step(
        system="s",
        messages=CONVERSATION,
        tools=TOOLS,
        max_tokens=500,
        effort="low",
        allow_tools=False,
    )
    assert step.text == "Here is the answer." and step.tool_calls == []
    body = recorder.body()
    assert body["tool_choice"] == "none"
    # Not a reasoning model: no effort sent.
    assert "reasoning_effort" not in body


@pytest.mark.parametrize(
    ("response", "error", "words"),
    [
        (_chat({"role": "assistant", "content": "half"}, "length"), AiUnavailableError, "cut off"),
        (
            _chat({"role": "assistant", "content": ""}, "content_filter"),
            AiDeclinedError,
            "declined",
        ),
        (
            _chat({"role": "assistant", "content": None, "refusal": "no"}),
            AiDeclinedError,
            "declined",
        ),
        (
            httpx.Response(401, json={"error": {"message": "bad key"}}),
            AiUnavailableError,
            "rejected the API key",
        ),
        (
            httpx.Response(404, json={"error": {"message": "no such model"}}),
            AiUnavailableError,
            "model",
        ),
        (httpx.Response(429, json={}), AiUnavailableError, "busy"),
        (httpx.Response(500, text="boom"), AiUnavailableError, r"error \(500\)"),
        (httpx.Response(200, json={"nothing": True}), AiUnavailableError, "cannot read"),
    ],
)
async def test_failures_are_said_in_words(
    response: httpx.Response, error: type, words: str
) -> None:
    backend = _backend(openai_compat.OpenAIBackend, _route("openai"), Recorder(response))
    with pytest.raises(error, match=words):
        await backend.step(
            system="s", messages=CONVERSATION, tools=TOOLS, max_tokens=50, effort="low"
        )


async def test_structured_answers_are_asked_for_by_schema() -> None:
    recorder = Recorder(_chat({"role": "assistant", "content": '{"ok": true}'}))
    backend = _backend(openai_compat.OpenAIBackend, _route("openai"), recorder)
    completion = await backend.complete(
        system="s", user="u", schema={"type": "object"}, max_tokens=100, effort="low"
    )
    assert completion.text == '{"ok": true}'
    assert recorder.body()["response_format"]["type"] == "json_schema"


async def test_ollama_is_asked_for_a_large_context_and_its_calls_get_ids() -> None:
    recorder = Recorder(
        httpx.Response(
            200,
            json={
                "model": "qwen2.5:7b-instruct",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "query_data", "arguments": {"dataset": "payslip"}}}
                    ],
                },
                "done_reason": "stop",
                "prompt_eval_count": 900,
                "eval_count": 20,
            },
        )
    )
    backend = _backend(
        openai_compat.OllamaBackend, _route("ollama", "qwen2.5:7b-instruct", None), recorder
    )
    step = await backend.step(
        system="s", messages=CONVERSATION, tools=TOOLS, max_tokens=16000, effort="low"
    )
    assert step.tool_calls[0].name == "query_data" and step.tool_calls[0].id.startswith("call_")
    body = recorder.body()
    assert recorder.requests[0].url.path == "/api/chat"
    assert body["options"]["num_ctx"] >= 16384 and body["stream"] is False
    assert "authorization" not in recorder.requests[0].headers
    assert step.input_tokens == 900


def test_an_ollama_address_is_read_in_any_usual_spelling() -> None:
    for spelling in (
        "http://h:11434",
        "http://h:11434/",
        "http://h:11434/v1",
        "http://h:11434/api/",
    ):
        assert openai_compat.ollama_root(spelling) == "http://h:11434"


# ── Choosing and guarding ────────────────────────────────────────────────────


def test_a_route_never_prints_its_key() -> None:
    route = _route("openai", key="sk-very-secret-key")
    assert "sk-very-secret-key" not in repr(route) and "sk-very-secret-key" not in str(route)


def test_each_provider_is_a_registered_sub_processor() -> None:
    from app.services.dpdp import transfers

    anthropic = set(transfers.sub_processor("anthropic").categories_permitted)
    for spec in providers.PROVIDERS.values():
        registered = transfers.sub_processor(spec.sub_processor)
        assert registered.is_ai_model_provider
        # Switching provider changes who receives a prompt, never what it may carry.
        assert set(registered.categories_permitted) == anthropic


def test_availability_needs_a_key_or_an_address() -> None:
    assert not Route("openai", "m", None, "https://x").available
    assert Route("openai", "m", "k" * 10, "https://x").available
    assert Route("ollama", "m", None, "http://localhost:11434").available
    assert not Route("ollama", "m", None, None).available
    assert not Route("anthropic", "m", None, None).available


def test_the_route_is_per_request() -> None:
    route = _route("openai", "gpt-5")
    with client.use_route(route):
        assert client.model_name() == "gpt-5" and client.sub_processor() == "openai"
    assert client.current_route().provider == providers.server_route().provider


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://ollama.example.com", "https"),
        ("https://127.0.0.1:11434", "private or local"),
        ("https://10.1.2.3", "private or local"),
        ("https://169.254.169.254/latest", "private or local"),
        ("https://[::1]:11434", "private or local"),
        ("https://user:pw@8.8.8.8", "user name"),
        ("ftp://8.8.8.8", "https"),
        ("not a url", "https"),
    ],
)
async def test_a_workspace_cannot_point_the_server_at_a_private_address(
    url: str, reason: str
) -> None:
    with pytest.raises(UnsafeAddressError, match=reason):
        await check_public_url(url)


async def test_a_public_https_address_is_accepted() -> None:
    assert await check_public_url(" https://8.8.8.8/v1/ ") == "https://8.8.8.8/v1"
