from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from llm_profiles import LlmProfileStore


RUN_TRANSITIONS = {
    "running": {"waiting_approval", "completed", "failed", "cancelled"},
    "waiting_approval": {"running", "failed", "cancelled"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}
MESSAGE_ROLES = frozenset({"system", "user", "assistant", "tool"})


class AgentNotFoundError(LookupError):
    """Raised when an agent session, run, or approval does not exist."""


@dataclass(frozen=True)
class AgentSession:
    id: str
    profile_id: int
    title: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AgentRun:
    id: str
    session_id: str
    profile_id: int
    status: str
    base_url: str
    model: str
    api_key_env: str | None
    timeout_seconds: float
    supports_tools: bool
    started_at: str
    completed_at: str | None
    error: str | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class AgentApproval:
    id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    status: str
    created_at: str
    decided_at: str | None


class AgentStore:
    """Transaction-neutral persistence for agent conversations and runs."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def create_session(
        self,
        profile_id: int,
        *,
        title: str = "New conversation",
        now: datetime | None = None,
    ) -> AgentSession:
        LlmProfileStore(self.db).get(profile_id)
        clean_title = _required_text(title, "Session title", 200)
        session_id = str(uuid.uuid4())
        timestamp = _iso_utc(now)
        self.db.execute(
            """
            INSERT INTO agent_sessions (
                id, profile_id, title, status, created_at, updated_at
            ) VALUES (?, ?, ?, 'active', ?, ?)
            """,
            (session_id, profile_id, clean_title, timestamp, timestamp),
        )
        return self.session(session_id)

    def session(self, session_id: str) -> AgentSession:
        row = self.db.execute(
            "SELECT * FROM agent_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise AgentNotFoundError("Agent session not found")
        return _session(row)

    def list_sessions(self, *, include_archived: bool = False) -> list[AgentSession]:
        if include_archived:
            rows = self.db.execute(
                "SELECT * FROM agent_sessions ORDER BY updated_at DESC, id"
            ).fetchall()
        else:
            rows = self.db.execute(
                """
                SELECT * FROM agent_sessions
                WHERE status = 'active'
                ORDER BY updated_at DESC, id
                """
            ).fetchall()
        return [_session(row) for row in rows]

    def archive_session(
        self, session_id: str, *, now: datetime | None = None
    ) -> AgentSession:
        self.session(session_id)
        self.db.execute(
            "UPDATE agent_sessions SET status = 'archived', updated_at = ? WHERE id = ?",
            (_iso_utc(now), session_id),
        )
        return self.session(session_id)

    def append_message(
        self,
        session_id: str,
        message: Mapping[str, Any],
        *,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> int:
        self.session(session_id)
        role = message.get("role")
        if role not in MESSAGE_ROLES:
            raise ValueError("Message role is invalid")
        if run_id is not None:
            run = self.run(run_id)
            if run.session_id != session_id:
                raise ValueError("Run does not belong to this session")
        serialized = _json(dict(message), "Message")
        timestamp = _iso_utc(now)
        cursor = self.db.execute(
            """
            INSERT INTO agent_messages (
                session_id, run_id, role, message_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, run_id, role, serialized, timestamp),
        )
        self.db.execute(
            "UPDATE agent_sessions SET updated_at = ? WHERE id = ?",
            (timestamp, session_id),
        )
        return int(cursor.lastrowid)

    def messages(self, session_id: str) -> list[dict[str, Any]]:
        self.session(session_id)
        rows = self.db.execute(
            """
            SELECT message_json FROM agent_messages
            WHERE session_id = ? ORDER BY id
            """,
            (session_id,),
        ).fetchall()
        return [json.loads(row["message_json"]) for row in rows]

    def create_run(
        self, session_id: str, *, now: datetime | None = None
    ) -> AgentRun:
        session = self.session(session_id)
        if session.status != "active":
            raise ValueError("Cannot run an archived agent session")
        active = self.db.execute(
            """
            SELECT id FROM agent_runs
            WHERE session_id = ? AND status IN ('running', 'waiting_approval')
            """,
            (session_id,),
        ).fetchone()
        if active is not None:
            raise ValueError("Finish the active agent run before sending another message")
        profile = LlmProfileStore(self.db).get(session.profile_id)
        run_id = str(uuid.uuid4())
        timestamp = _iso_utc(now)
        self.db.execute(
            """
            INSERT INTO agent_runs (
                id, session_id, profile_id, status, base_url, model,
                api_key_env, timeout_seconds, supports_tools, started_at
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                session.id,
                profile.id,
                profile.base_url,
                profile.model,
                profile.api_key_env,
                profile.timeout_seconds,
                int(profile.supports_tools),
                timestamp,
            ),
        )
        self.record_event(run_id, "run_started", now=now)
        return self.run(run_id)

    def run(self, run_id: str) -> AgentRun:
        row = self.db.execute(
            "SELECT * FROM agent_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise AgentNotFoundError("Agent run not found")
        return _run(row)

    def runs_for_session(self, session_id: str) -> list[AgentRun]:
        self.session(session_id)
        rows = self.db.execute(
            """
            SELECT * FROM agent_runs
            WHERE session_id = ? ORDER BY started_at, id
            """,
            (session_id,),
        ).fetchall()
        return [_run(row) for row in rows]

    def transition_run(
        self,
        run_id: str,
        status: str,
        *,
        error: str | None = None,
        usage: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> AgentRun:
        current = self.run(run_id)
        if status not in RUN_TRANSITIONS[current.status]:
            raise ValueError(f"Cannot transition run from {current.status} to {status}")
        if status == "failed" and not error:
            raise ValueError("Failed runs require an error message")
        if error is not None and status != "failed":
            raise ValueError("Only failed runs may include an error message")
        token_usage = _validated_usage(usage or {})
        completed_at = _iso_utc(now) if status in {"completed", "failed", "cancelled"} else None
        self.db.execute(
            """
            UPDATE agent_runs
            SET status = ?, completed_at = ?, error = ?,
                prompt_tokens = prompt_tokens + ?,
                completion_tokens = completion_tokens + ?,
                total_tokens = total_tokens + ?
            WHERE id = ?
            """,
            (
                status,
                completed_at,
                error,
                token_usage["prompt_tokens"],
                token_usage["completion_tokens"],
                token_usage["total_tokens"],
                run_id,
            ),
        )
        self.record_event(
            run_id,
            f"run_{status}",
            {"error": error} if error else {},
            now=now,
        )
        return self.run(run_id)

    def add_usage(
        self, run_id: str, usage: Mapping[str, Any], *, now: datetime | None = None
    ) -> AgentRun:
        current = self.run(run_id)
        if current.status not in {"running", "waiting_approval"}:
            raise ValueError("Token usage can only be added to an active run")
        token_usage = _validated_usage(usage)
        self.db.execute(
            """
            UPDATE agent_runs
            SET prompt_tokens = prompt_tokens + ?,
                completion_tokens = completion_tokens + ?,
                total_tokens = total_tokens + ?
            WHERE id = ?
            """,
            (
                token_usage["prompt_tokens"],
                token_usage["completion_tokens"],
                token_usage["total_tokens"],
                run_id,
            ),
        )
        return self.run(run_id)

    def record_event(
        self,
        run_id: str,
        event_type: str,
        details: Mapping[str, Any] | None = None,
        *,
        now: datetime | None = None,
    ) -> int:
        self.run(run_id)
        clean_type = _required_text(event_type, "Event type", 100)
        sequence = self.db.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1
            FROM agent_run_events WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()[0]
        cursor = self.db.execute(
            """
            INSERT INTO agent_run_events (
                run_id, sequence, event_type, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                sequence,
                clean_type,
                _json(dict(details or {}), "Event details"),
                _iso_utc(now),
            ),
        )
        return int(cursor.lastrowid)

    def events(self, run_id: str) -> list[dict[str, Any]]:
        self.run(run_id)
        rows = self.db.execute(
            """
            SELECT sequence, event_type, details_json, created_at
            FROM agent_run_events WHERE run_id = ? ORDER BY sequence
            """,
            (run_id,),
        ).fetchall()
        return [
            {
                "sequence": row["sequence"],
                "event_type": row["event_type"],
                "details": json.loads(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def request_approval(
        self,
        run_id: str,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        now: datetime | None = None,
    ) -> AgentApproval:
        run = self.run(run_id)
        if run.status not in {"running", "waiting_approval"}:
            raise ValueError("Approvals may only be requested for an active run")
        approval_id = str(uuid.uuid4())
        timestamp = _iso_utc(now)
        self.db.execute(
            """
            INSERT INTO agent_approvals (
                id, run_id, tool_call_id, tool_name, arguments_json,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                approval_id,
                run_id,
                _required_text(tool_call_id, "Tool call id", 200),
                _required_text(tool_name, "Tool name", 100),
                _json(dict(arguments), "Tool arguments"),
                timestamp,
            ),
        )
        self.record_event(
            run_id,
            "approval_requested",
            {"approval_id": approval_id, "tool_name": tool_name},
            now=now,
        )
        if run.status == "running":
            self.transition_run(run_id, "waiting_approval", now=now)
        return self.approval(approval_id)

    def approval(self, approval_id: str) -> AgentApproval:
        row = self.db.execute(
            "SELECT * FROM agent_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise AgentNotFoundError("Agent approval not found")
        return _approval(row)

    def approvals_for_run(self, run_id: str) -> list[AgentApproval]:
        self.run(run_id)
        rows = self.db.execute(
            "SELECT * FROM agent_approvals WHERE run_id = ? ORDER BY created_at, id",
            (run_id,),
        ).fetchall()
        return [_approval(row) for row in rows]

    def decide_approval(
        self,
        approval_id: str,
        approved: bool,
        *,
        now: datetime | None = None,
    ) -> AgentApproval:
        approval = self.approval(approval_id)
        if approval.status != "pending":
            raise ValueError("Approval has already been decided")
        status = "approved" if approved else "rejected"
        self.db.execute(
            "UPDATE agent_approvals SET status = ?, decided_at = ? WHERE id = ?",
            (status, _iso_utc(now), approval_id),
        )
        self.record_event(
            approval.run_id,
            f"approval_{status}",
            {"approval_id": approval_id, "tool_name": approval.tool_name},
            now=now,
        )
        return self.approval(approval_id)

    def mark_approval_consumed(
        self, approval_id: str, *, now: datetime | None = None
    ) -> AgentApproval:
        approval = self.approval(approval_id)
        if approval.status not in {"approved", "rejected"}:
            raise ValueError("Only a decided tool call may be consumed")
        decision = approval.status
        self.db.execute(
            "UPDATE agent_approvals SET status = 'executed' WHERE id = ?",
            (approval_id,),
        )
        self.record_event(
            approval.run_id,
            "approval_consumed",
            {
                "approval_id": approval_id,
                "tool_name": approval.tool_name,
                "decision": decision,
            },
            now=now,
        )
        return self.approval(approval_id)


def _required_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
        raise ValueError(f"{label} must be between 1 and {maximum} characters")
    return value.strip()


def _json(value: Any, label: str) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON serializable") from exc


def _validated_usage(usage: Mapping[str, Any]) -> dict[str, int]:
    names = {"prompt_tokens", "completion_tokens", "total_tokens"}
    unknown = set(usage) - names
    if unknown:
        raise ValueError(f"Unsupported usage field: {sorted(unknown)[0]}")
    result = {}
    for name in names:
        value = usage.get(name, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("Token usage must contain non-negative integers")
        result[name] = value
    return result


def _session(row: sqlite3.Row) -> AgentSession:
    return AgentSession(
        id=row["id"],
        profile_id=int(row["profile_id"]),
        title=row["title"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _run(row: sqlite3.Row) -> AgentRun:
    return AgentRun(
        id=row["id"],
        session_id=row["session_id"],
        profile_id=int(row["profile_id"]),
        status=row["status"],
        base_url=row["base_url"],
        model=row["model"],
        api_key_env=row["api_key_env"],
        timeout_seconds=float(row["timeout_seconds"]),
        supports_tools=bool(row["supports_tools"]),
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        error=row["error"],
        prompt_tokens=int(row["prompt_tokens"]),
        completion_tokens=int(row["completion_tokens"]),
        total_tokens=int(row["total_tokens"]),
    )


def _approval(row: sqlite3.Row) -> AgentApproval:
    return AgentApproval(
        id=row["id"],
        run_id=row["run_id"],
        tool_call_id=row["tool_call_id"],
        tool_name=row["tool_name"],
        arguments=json.loads(row["arguments_json"]),
        status=row["status"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
    )


def _iso_utc(value: datetime | None) -> str:
    instant = value or datetime.now(timezone.utc)
    return instant.astimezone(timezone.utc).isoformat(timespec="seconds")
