from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit


class LlmConfigurationError(ValueError):
    """Raised when an LLM endpoint cannot be used safely."""


class LlmProtocolError(RuntimeError):
    """Raised when an endpoint returns an invalid Chat Completions response."""


class LlmCapabilityError(RuntimeError):
    """Raised when a requested endpoint capability is disabled."""


def normalize_base_url(value: str) -> str:
    """Return an OpenAI-compatible API base URL, not a completion URL."""
    if not isinstance(value, str) or not value.strip():
        raise LlmConfigurationError("Base URL is required")

    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LlmConfigurationError("Base URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise LlmConfigurationError("Base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise LlmConfigurationError("Base URL must not contain a query or fragment")

    path = parsed.path.rstrip("/")
    suffix = "/chat/completions"
    if path.endswith(suffix):
        path = path[: -len(suffix)]
    if not path:
        path = "/v1"

    return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class LlmConfig:
    base_url: str
    model: str
    api_key: str | None = None
    timeout_seconds: float = 60.0
    supports_tools: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", normalize_base_url(self.base_url))
        if not isinstance(self.model, str) or not self.model.strip():
            raise LlmConfigurationError("Model is required")
        object.__setattr__(self, "model", self.model.strip())
        if self.api_key is not None and not isinstance(self.api_key, str):
            raise LlmConfigurationError("API key must be text")
        if not isinstance(self.timeout_seconds, (int, float)) or isinstance(
            self.timeout_seconds, bool
        ):
            raise LlmConfigurationError("Timeout must be a number")
        if not 1 <= float(self.timeout_seconds) <= 600:
            raise LlmConfigurationError("Timeout must be between 1 and 600 seconds")
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))

    def public_dict(self) -> dict[str, Any]:
        """Return settings safe to expose to the browser and logs."""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "supports_tools": self.supports_tools,
            "api_key_configured": bool(self.api_key),
        }


@dataclass(frozen=True)
class CompletionToolCall:
    id: str
    name: str
    arguments: str

    def as_message_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(frozen=True)
class CompletionResult:
    content: str | None
    tool_calls: tuple[CompletionToolCall, ...]
    finish_reason: str | None
    response_id: str | None
    model: str | None
    usage: dict[str, int]

    def assistant_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [call.as_message_dict() for call in self.tool_calls]
        return message


class ChatCompletionsProvider:
    """Small adapter around the OpenAI-compatible Chat Completions API."""

    def __init__(self, config: LlmConfig, *, client: Any | None = None) -> None:
        self.config = config
        self._client = client if client is not None else self._create_client()

    def _create_client(self) -> Any:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - packaging protects this path
            raise RuntimeError("Install the 'openai' package to use agent mode") from exc

        return OpenAI(
            api_key=self.config.api_key or "not-required",
            base_url=self.config.base_url,
            timeout=self.config.timeout_seconds,
        )

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] = (),
        tool_choice: str | Mapping[str, Any] = "auto",
    ) -> CompletionResult:
        if not messages:
            raise ValueError("At least one message is required")
        if tools and not self.config.supports_tools:
            raise LlmCapabilityError("Tool calling is disabled for this endpoint")

        request: dict[str, Any] = {
            "model": self.config.model,
            "messages": [dict(message) for message in messages],
        }
        if tools:
            request["tools"] = [dict(tool) for tool in tools]
            request["tool_choice"] = tool_choice

        response = self._client.chat.completions.create(**request)
        choices = _field(response, "choices")
        if not choices:
            raise LlmProtocolError("Chat Completions response did not contain a choice")

        choice = choices[0]
        message = _field(choice, "message")
        if message is None:
            raise LlmProtocolError("Chat Completions choice did not contain a message")

        content = _field(message, "content")
        if content is not None and not isinstance(content, str):
            raise LlmProtocolError("Assistant content must be text or null")

        tool_calls = tuple(_parse_tool_call(call) for call in (_field(message, "tool_calls") or ()))
        return CompletionResult(
            content=content,
            tool_calls=tool_calls,
            finish_reason=_optional_text(_field(choice, "finish_reason")),
            response_id=_optional_text(_field(response, "id")),
            model=_optional_text(_field(response, "model")),
            usage=_parse_usage(_field(response, "usage")),
        )

    def list_models(self) -> tuple[str, ...]:
        response = self._client.models.list()
        models = _field(response, "data") or ()
        identifiers = {
            identifier
            for model in models
            if (identifier := _optional_text(_field(model, "id")))
        }
        return tuple(sorted(identifiers, key=str.casefold))


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_tool_call(value: Any) -> CompletionToolCall:
    call_id = _optional_text(_field(value, "id"))
    function = _field(value, "function")
    name = _optional_text(_field(function, "name"))
    arguments = _field(function, "arguments")
    if not call_id or not name or not isinstance(arguments, str):
        raise LlmProtocolError("Assistant tool call is missing an id, name, or arguments")
    return CompletionToolCall(id=call_id, name=name, arguments=arguments)


def _parse_usage(value: Any) -> dict[str, int]:
    if value is None:
        return {}
    result: dict[str, int] = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        count = _field(value, name)
        if isinstance(count, int) and not isinstance(count, bool):
            result[name] = count
    return result
