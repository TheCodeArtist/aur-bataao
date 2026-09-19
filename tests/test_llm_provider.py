from types import SimpleNamespace

import pytest

from llm_provider import (
    ChatCompletionsProvider,
    LlmCapabilityError,
    LlmConfig,
    LlmConfigurationError,
    LlmProtocolError,
    normalize_base_url,
)


class FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.response


class FakeModels:
    def __init__(self, models=()):
        self.models = models

    def list(self):
        return {"data": [{"id": model} for model in self.models]}


def fake_client(response, models=()):
    return SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions(response)),
        models=FakeModels(models),
    )


def test_normalizes_base_and_completion_urls():
    assert normalize_base_url("http://localhost:1234/v1/") == "http://localhost:1234/v1"
    assert (
        normalize_base_url("https://example.test/custom/v1/chat/completions")
        == "https://example.test/custom/v1"
    )
    assert normalize_base_url("https://example.test") == "https://example.test/v1"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "localhost:1234/v1",
        "file:///tmp/model",
        "https://user:secret@example.test/v1",
        "https://example.test/v1?token=secret",
    ],
)
def test_rejects_unsafe_or_ambiguous_base_urls(value):
    with pytest.raises(LlmConfigurationError):
        normalize_base_url(value)


def test_config_public_dict_never_exposes_api_key():
    config = LlmConfig(
        base_url="https://example.test/v1",
        model="test-model",
        api_key="super-secret",
    )

    public = config.public_dict()

    assert public["api_key_configured"] is True
    assert "super-secret" not in repr(public)
    assert "api_key" not in public


def test_completion_uses_portable_fields_and_parses_parallel_tool_calls():
    response = {
        "id": "chatcmpl-1",
        "model": "served-model",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "list_tasks",
                                "arguments": '{"status":"todo"}',
                            },
                        },
                        {
                            "id": "call-2",
                            "type": "function",
                            "function": {
                                "name": "add_comment",
                                "arguments": '{"task_id":7,"body":"Checked"}',
                            },
                        },
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
    }
    client = fake_client(response)
    provider = ChatCompletionsProvider(
        LlmConfig("http://localhost:1234/v1", "local-model"), client=client
    )
    tools = [
        {
            "type": "function",
            "function": {"name": "list_tasks", "parameters": {"type": "object"}},
        }
    ]

    result = provider.complete(
        [{"role": "user", "content": "What should I do?"}], tools=tools
    )

    assert client.chat.completions.requests == [
        {
            "model": "local-model",
            "messages": [{"role": "user", "content": "What should I do?"}],
            "tools": tools,
            "tool_choice": "auto",
        }
    ]
    assert [call.name for call in result.tool_calls] == ["list_tasks", "add_comment"]
    assert result.assistant_message()["tool_calls"][1]["id"] == "call-2"
    assert result.usage == {
        "prompt_tokens": 12,
        "completion_tokens": 8,
        "total_tokens": 20,
    }


def test_completion_omits_tools_when_none_are_supplied():
    response = {
        "choices": [
            {"finish_reason": "stop", "message": {"content": "Start with task 7."}}
        ]
    }
    client = fake_client(response)
    provider = ChatCompletionsProvider(
        LlmConfig("https://example.test/v1", "remote-model"), client=client
    )

    result = provider.complete([{"role": "user", "content": "What next?"}])

    assert result.content == "Start with task 7."
    assert client.chat.completions.requests[0] == {
        "model": "remote-model",
        "messages": [{"role": "user", "content": "What next?"}],
    }


def test_disabled_tool_capability_fails_before_request():
    client = fake_client({"choices": []})
    provider = ChatCompletionsProvider(
        LlmConfig("https://example.test/v1", "model", supports_tools=False),
        client=client,
    )

    with pytest.raises(LlmCapabilityError):
        provider.complete(
            [{"role": "user", "content": "Help"}],
            tools=[{"type": "function", "function": {"name": "list_tasks"}}],
        )

    assert client.chat.completions.requests == []


def test_lists_unique_models_in_stable_order():
    provider = ChatCompletionsProvider(
        LlmConfig("https://example.test/v1", "fallback"),
        client=fake_client({}, models=("zeta", "Alpha", "zeta")),
    )

    assert provider.list_models() == ("Alpha", "zeta")


def test_invalid_completion_response_has_a_clear_protocol_error():
    provider = ChatCompletionsProvider(
        LlmConfig("https://example.test/v1", "model"),
        client=fake_client({"choices": []}),
    )

    with pytest.raises(LlmProtocolError, match="did not contain a choice"):
        provider.complete([{"role": "user", "content": "Help"}])
