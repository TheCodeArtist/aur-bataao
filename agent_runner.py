from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from agent_store import AgentApproval, AgentRun, AgentStore
from agent_tools import TaskToolRegistry, ToolNotFoundError
from llm_profiles import LlmProfileStore
from llm_provider import (
    ChatCompletionsProvider,
    CompletionResult,
    CompletionToolCall,
    LlmConfig,
)


SYSTEM_PROMPT = """You are the interactive task assistant inside Aur Bataao.
Use the provided tools to inspect task data instead of guessing. Prefer concise,
actionable answers. Tool calls that change data require user approval. Never say
a change happened until its tool result confirms success. If a tool reports an
error or rejection, explain it plainly and offer a safe next step."""


@dataclass(frozen=True)
class AgentOutcome:
    run: AgentRun
    pending_approvals: tuple[AgentApproval, ...]
    latest_content: str | None


class AgentRunner:
    """Runs a bounded, durable Chat Completions tool loop."""

    def __init__(
        self,
        db: sqlite3.Connection,
        timezone_info: ZoneInfo,
        *,
        environ: dict[str, str] | None = None,
        provider_factory: Callable[[LlmConfig], Any] = ChatCompletionsProvider,
        max_steps: int = 8,
    ) -> None:
        if not 1 <= max_steps <= 32:
            raise ValueError("max_steps must be between 1 and 32")
        self.db = db
        self.timezone = timezone_info
        self.environ = environ
        self.provider_factory = provider_factory
        self.max_steps = max_steps
        self.store = AgentStore(db)
        self.tools = TaskToolRegistry(db, timezone_info)

    def start(
        self,
        session_id: str,
        user_content: str,
        *,
        now: datetime | None = None,
    ) -> AgentOutcome:
        content = _message_text(user_content)
        run = self.store.create_run(session_id, now=now)
        self.store.append_message(
            session_id,
            {"role": "user", "content": content},
            run_id=run.id,
            now=now,
        )
        self.db.commit()
        return self._drive(run.id, now=now)

    def decide(
        self,
        approval_id: str,
        approved: bool,
        *,
        now: datetime | None = None,
    ) -> AgentOutcome:
        if not isinstance(approved, bool):
            raise ValueError("approved must be a boolean")
        approval = self.store.approval(approval_id)
        run = self.store.run(approval.run_id)
        if run.status != "waiting_approval":
            raise ValueError("This run is not waiting for approval")
        self.store.decide_approval(approval_id, approved, now=now)
        self.db.commit()

        approvals = self.store.approvals_for_run(run.id)
        pending = tuple(item for item in approvals if item.status == "pending")
        if pending:
            return AgentOutcome(self.store.run(run.id), pending, None)

        self.store.transition_run(run.id, "running", now=now)
        for item in approvals:
            if item.status not in {"approved", "rejected"}:
                continue
            if item.status == "approved":
                result = self._execute_tool(item.tool_name, item.arguments, now=now)
            else:
                result = {"ok": False, "error": "User rejected this change"}
            self.store.append_message(
                run.session_id,
                _tool_message(item.tool_call_id, result),
                run_id=run.id,
                now=now,
            )
            self.store.mark_approval_consumed(item.id, now=now)
        self.db.commit()
        return self._drive(run.id, now=now)

    def _drive(
        self, run_id: str, *, now: datetime | None = None
    ) -> AgentOutcome:
        run = self.store.run(run_id)
        profile_store = LlmProfileStore(self.db, environ=self.environ)
        config = profile_store.provider_config(run.profile_id)
        provider = self.provider_factory(config)
        latest_content = None

        for _ in range(self.max_steps):
            run = self.store.run(run_id)
            if run.status != "running":
                return self._outcome(run_id, latest_content)
            messages = [self._system_message(now), *self.store.messages(run.session_id)]
            try:
                completion = provider.complete(messages, tools=self.tools.openai_tools())
            except Exception as exc:
                return self._fail(run_id, _safe_error(exc, config.api_key), now=now)

            latest_content = completion.content or latest_content
            self.store.append_message(
                run.session_id,
                completion.assistant_message(),
                run_id=run.id,
                now=now,
            )
            self.store.add_usage(run.id, completion.usage, now=now)
            self.store.record_event(
                run.id,
                "llm_response",
                {
                    "finish_reason": completion.finish_reason,
                    "tool_names": [call.name for call in completion.tool_calls],
                    "response_id": completion.response_id,
                },
                now=now,
            )

            if not completion.tool_calls:
                if not completion.content:
                    self.db.rollback()
                    return self._fail(
                        run_id, "The model returned neither text nor tool calls", now=now
                    )
                self.store.transition_run(run.id, "completed", now=now)
                self.db.commit()
                return self._outcome(run_id, latest_content)

            waiting = self._handle_tool_calls(run, completion, now=now)
            self.db.commit()
            if waiting:
                return self._outcome(run_id, latest_content)

        return self._fail(
            run_id,
            f"Agent exceeded the {self.max_steps}-step limit",
            now=now,
        )

    def _handle_tool_calls(
        self,
        run: AgentRun,
        completion: CompletionResult,
        *,
        now: datetime | None,
    ) -> bool:
        waiting = False
        for call in completion.tool_calls:
            arguments, error = _tool_arguments(call)
            if error:
                self.store.append_message(
                    run.session_id,
                    _tool_message(call.id, {"ok": False, "error": error}),
                    run_id=run.id,
                    now=now,
                )
                continue
            try:
                requires_approval = self.tools.requires_approval(call.name)
            except ToolNotFoundError as exc:
                self.store.append_message(
                    run.session_id,
                    _tool_message(call.id, {"ok": False, "error": str(exc)}),
                    run_id=run.id,
                    now=now,
                )
                continue
            if requires_approval:
                self.store.request_approval(
                    run.id,
                    tool_call_id=call.id,
                    tool_name=call.name,
                    arguments=arguments,
                    now=now,
                )
                waiting = True
                continue

            result = self._execute_tool(call.name, arguments, now=now)
            self.store.append_message(
                run.session_id,
                _tool_message(call.id, result),
                run_id=run.id,
                now=now,
            )
        return waiting

    def _execute_tool(
        self, name: str, arguments: dict[str, Any], *, now: datetime | None
    ) -> dict[str, Any]:
        self.db.execute("SAVEPOINT agent_tool")
        try:
            value = self.tools.execute(name, arguments, now=now)
        except Exception as exc:
            self.db.execute("ROLLBACK TO agent_tool")
            self.db.execute("RELEASE agent_tool")
            return {"ok": False, "error": _safe_error(exc)}
        self.db.execute("RELEASE agent_tool")
        return {"ok": True, "result": value}

    def _system_message(self, now: datetime | None) -> dict[str, str]:
        instant = now or datetime.now(timezone.utc)
        local = instant.astimezone(self.timezone).isoformat(timespec="minutes")
        return {
            "role": "system",
            "content": f"{SYSTEM_PROMPT}\nCurrent local date and time: {local}.",
        }

    def _fail(
        self, run_id: str, error: str, *, now: datetime | None
    ) -> AgentOutcome:
        self.db.rollback()
        run = self.store.run(run_id)
        if run.status == "running":
            self.store.transition_run(run_id, "failed", error=error[:2000], now=now)
            self.db.commit()
        return self._outcome(run_id, None)

    def _outcome(self, run_id: str, latest_content: str | None) -> AgentOutcome:
        run = self.store.run(run_id)
        pending = tuple(
            approval
            for approval in self.store.approvals_for_run(run_id)
            if approval.status == "pending"
        )
        return AgentOutcome(run, pending, latest_content)


def _message_text(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 8000:
        raise ValueError("Message must be between 1 and 8000 characters")
    return value.strip()


def _tool_arguments(call: CompletionToolCall) -> tuple[dict[str, Any], str | None]:
    try:
        value = json.loads(call.arguments)
    except json.JSONDecodeError:
        return {}, "Tool arguments were not valid JSON"
    if not isinstance(value, dict):
        return {}, "Tool arguments must be a JSON object"
    return value, None


def _tool_message(tool_call_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
    }


def _safe_error(error: Exception, secret: str | None = None) -> str:
    text = str(error).strip() or error.__class__.__name__
    if secret:
        text = text.replace(secret, "***")
    return text[:2000]
