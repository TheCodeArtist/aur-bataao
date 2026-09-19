from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from task_service import TaskService


class ToolNotFoundError(LookupError):
    """Raised when a model requests a tool that is not registered."""


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    mutating: bool
    handler: Callable[[Mapping[str, Any], datetime | None], Any]

    def openai_dict(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class TaskToolRegistry:
    """OpenAI tool schemas and their task-domain implementations."""

    def __init__(self, db: sqlite3.Connection, timezone_info: ZoneInfo) -> None:
        self.db = db
        self.tasks = TaskService(db, timezone_info)
        self._definitions = {
            definition.name: definition for definition in self._build_definitions()
        }

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._definitions.values())

    def openai_tools(self) -> list[dict[str, Any]]:
        return [definition.openai_dict() for definition in self.definitions()]

    def requires_approval(self, name: str) -> bool:
        return self._definition(name).mutating

    def execute(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> Any:
        if not isinstance(arguments, Mapping):
            raise ValueError("Tool arguments must be an object")
        return self._definition(name).handler(arguments, now)

    def _definition(self, name: str) -> ToolDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise ToolNotFoundError(f"Unknown tool: {name}") from exc

    def _build_definitions(self) -> tuple[ToolDefinition, ...]:
        return (
            ToolDefinition(
                "list_tasks",
                "List tasks in backlog order, optionally filtered by workflow status.",
                _object_schema(
                    {
                        "status": {
                            "type": "string",
                            "enum": ["todo", "in_progress", "done"],
                        },
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    }
                ),
                False,
                self._list_tasks,
            ),
            ToolDefinition(
                "get_task",
                "Get a task with comments, labels, dependencies, and waiting details.",
                _object_schema({"task_id": _task_id_schema()}, required=("task_id",)),
                False,
                self._get_task,
            ),
            ToolDefinition(
                "create_task",
                "Create a task. This changes application data and requires approval.",
                _object_schema(
                    {
                        "title": {"type": "string", "minLength": 1, "maxLength": 200},
                        "description": {"type": "string", "maxLength": 5000},
                        "due_date": _nullable_date_schema(),
                    },
                    required=("title",),
                ),
                True,
                self._create_task,
            ),
            ToolDefinition(
                "update_task",
                "Update task text, status, or due date. This requires approval.",
                _object_schema(
                    {
                        "task_id": _task_id_schema(),
                        "title": {"type": "string", "minLength": 1, "maxLength": 200},
                        "description": {"type": "string", "maxLength": 5000},
                        "status": {
                            "type": "string",
                            "enum": ["todo", "in_progress", "done"],
                        },
                        "due_date": _nullable_date_schema(),
                    },
                    required=("task_id",),
                ),
                True,
                self._update_task,
            ),
            ToolDefinition(
                "add_comment",
                "Add a progress comment to a task. This requires approval.",
                _object_schema(
                    {
                        "task_id": _task_id_schema(),
                        "body": {"type": "string", "minLength": 1, "maxLength": 2000},
                        "counts_as_progress": {"type": "boolean"},
                    },
                    required=("task_id", "body"),
                ),
                True,
                self._add_comment,
            ),
            ToolDefinition(
                "set_waiting",
                "Record that a task is waiting on a person. This requires approval.",
                _object_schema(
                    {
                        "task_id": _task_id_schema(),
                        "person_name": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 100,
                        },
                        "note": {"type": "string", "maxLength": 1000},
                        "next_follow_up_on": _nullable_date_schema(),
                        "next_follow_up_time": {
                            "type": ["string", "null"],
                            "pattern": "^[0-2][0-9]:[0-5][0-9]$",
                        },
                    },
                    required=("task_id", "person_name"),
                ),
                True,
                self._set_waiting,
            ),
            ToolDefinition(
                "record_follow_up",
                "Record a follow-up on a waiting task. This requires approval.",
                _object_schema(
                    {
                        "task_id": _task_id_schema(),
                        "note": {"type": "string", "maxLength": 2000},
                        "next_follow_up_on": _nullable_date_schema(),
                        "next_follow_up_time": {
                            "type": ["string", "null"],
                            "pattern": "^[0-2][0-9]:[0-5][0-9]$",
                        },
                    },
                    required=("task_id",),
                ),
                True,
                self._record_follow_up,
            ),
            ToolDefinition(
                "resolve_waiting",
                "Mark a task as no longer waiting on a person. This requires approval.",
                _object_schema({"task_id": _task_id_schema()}, required=("task_id",)),
                True,
                self._resolve_waiting,
            ),
            ToolDefinition(
                "add_label",
                "Add a manual label to a task. This requires approval.",
                _object_schema(
                    {
                        "task_id": _task_id_schema(),
                        "name": {"type": "string", "minLength": 1, "maxLength": 32},
                    },
                    required=("task_id", "name"),
                ),
                True,
                self._add_label,
            ),
            ToolDefinition(
                "remove_label",
                "Remove a manual label from a task. This requires approval.",
                _object_schema(
                    {"task_id": _task_id_schema(), "label_id": _positive_integer()},
                    required=("task_id", "label_id"),
                ),
                True,
                self._remove_label,
            ),
            ToolDefinition(
                "add_dependency",
                "Make one task block another task. This requires approval.",
                _object_schema(
                    {
                        "blocked_task_id": _task_id_schema(),
                        "blocker_task_id": _task_id_schema(),
                    },
                    required=("blocked_task_id", "blocker_task_id"),
                ),
                True,
                self._add_dependency,
            ),
            ToolDefinition(
                "remove_dependency",
                "Remove a task dependency. This requires approval.",
                _object_schema(
                    {
                        "blocked_task_id": _task_id_schema(),
                        "blocker_task_id": _task_id_schema(),
                    },
                    required=("blocked_task_id", "blocker_task_id"),
                ),
                True,
                self._remove_dependency,
            ),
        )

    def _list_tasks(
        self, arguments: Mapping[str, Any], _: datetime | None
    ) -> list[dict[str, Any]]:
        _check_keys(arguments, {"status", "limit"})
        status = arguments.get("status")
        if status is not None and status not in {"todo", "in_progress", "done"}:
            raise ValueError("Task status is invalid")
        limit = arguments.get("limit", 50)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("Limit must be an integer between 1 and 100")
        query = "SELECT * FROM tasks"
        parameters: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            parameters.append(status)
        query += " ORDER BY rank_key, id LIMIT ?"
        parameters.append(limit)
        return [
            self._task_summary(row)
            for row in self.db.execute(query, parameters).fetchall()
        ]

    def _get_task(
        self, arguments: Mapping[str, Any], _: datetime | None
    ) -> dict[str, Any]:
        _check_keys(arguments, {"task_id"})
        task_id = _required_id(arguments, "task_id")
        task = dict(self.tasks.task(task_id))
        waiting = self.tasks.active_waiting(task_id)
        task["waiting"] = dict(waiting) if waiting else None
        task["comments"] = [
            dict(row)
            for row in self.db.execute(
                "SELECT id, body, created_at FROM comments WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        ]
        task["labels"] = [
            dict(row)
            for row in self.db.execute(
                """
                SELECT l.id, l.name, l.type
                FROM task_labels tl JOIN labels l ON l.id = tl.label_id
                WHERE tl.task_id = ? AND tl.removed_at IS NULL
                ORDER BY l.name COLLATE NOCASE
                """,
                (task_id,),
            ).fetchall()
        ]
        task["blockers"] = [
            dict(row)
            for row in self.db.execute(
                """
                SELECT t.id, t.title, t.status
                FROM task_dependencies d JOIN tasks t ON t.id = d.blocker_task_id
                WHERE d.blocked_task_id = ? ORDER BY t.rank_key, t.id
                """,
                (task_id,),
            ).fetchall()
        ]
        return task

    def _create_task(
        self, arguments: Mapping[str, Any], now: datetime | None
    ) -> dict[str, Any]:
        _check_keys(arguments, {"title", "description", "due_date"})
        title = _required_string(arguments, "title", 200)
        result = self.tasks.create_task(
            title=title,
            now=now,
        )
        values = {
            key: arguments[key]
            for key in ("description", "due_date")
            if key in arguments
        }
        if "description" in values and not isinstance(values["description"], str):
            raise ValueError("description must be text")
        if values:
            self.tasks.update_task(result.task_id, values, now=now)
        return self._task_summary(self.tasks.task(result.task_id))

    def _update_task(
        self, arguments: Mapping[str, Any], now: datetime | None
    ) -> dict[str, Any]:
        allowed = {"task_id", "title", "description", "status", "due_date"}
        _check_keys(arguments, allowed)
        task_id = _required_id(arguments, "task_id")
        values = {key: value for key, value in arguments.items() if key != "task_id"}
        if not values:
            raise ValueError("At least one task field is required")
        for name in ("title", "description", "status"):
            if name in values and not isinstance(values[name], str):
                raise ValueError(f"{name} must be text")
        self.tasks.update_task(task_id, values, now=now)
        return self._task_summary(self.tasks.task(task_id))

    def _add_comment(
        self, arguments: Mapping[str, Any], now: datetime | None
    ) -> dict[str, Any]:
        _check_keys(arguments, {"task_id", "body", "counts_as_progress"})
        task_id = _required_id(arguments, "task_id")
        body = _required_string(arguments, "body", 2000)
        counts_as_progress = arguments.get("counts_as_progress", True)
        if not isinstance(counts_as_progress, bool):
            raise ValueError("counts_as_progress must be a boolean")
        comment_id = self.tasks.add_comment(
            task_id,
            body,
            counts_as_progress=counts_as_progress,
            now=now,
        )
        return {"task_id": task_id, "comment_id": comment_id}

    def _set_waiting(
        self, arguments: Mapping[str, Any], now: datetime | None
    ) -> dict[str, Any]:
        allowed = {
            "task_id",
            "person_name",
            "note",
            "next_follow_up_on",
            "next_follow_up_time",
        }
        _check_keys(arguments, allowed)
        task_id = _required_id(arguments, "task_id")
        values = {key: value for key, value in arguments.items() if key != "task_id"}
        values["person_name"] = _required_string(arguments, "person_name", 100)
        result = self.tasks.set_waiting(task_id, values, now=now)
        return {
            "task_id": task_id,
            "created": result.created,
            "changed": result.changed,
        }

    def _record_follow_up(
        self, arguments: Mapping[str, Any], now: datetime | None
    ) -> dict[str, Any]:
        allowed = {"task_id", "note", "next_follow_up_on", "next_follow_up_time"}
        _check_keys(arguments, allowed)
        task_id = _required_id(arguments, "task_id")
        values = {key: value for key, value in arguments.items() if key != "task_id"}
        self.tasks.record_follow_up(task_id, values, now=now)
        return {"task_id": task_id, "recorded": True}

    def _resolve_waiting(
        self, arguments: Mapping[str, Any], now: datetime | None
    ) -> dict[str, Any]:
        _check_keys(arguments, {"task_id"})
        task_id = _required_id(arguments, "task_id")
        self.tasks.resolve_waiting(task_id, now=now)
        return {"task_id": task_id, "resolved": True}

    def _add_label(
        self, arguments: Mapping[str, Any], now: datetime | None
    ) -> dict[str, Any]:
        _check_keys(arguments, {"task_id", "name"})
        task_id = _required_id(arguments, "task_id")
        name = _required_string(arguments, "name", 32)
        label_id = self.tasks.add_label(task_id, name, now=now)
        return {"task_id": task_id, "label_id": label_id}

    def _remove_label(
        self, arguments: Mapping[str, Any], now: datetime | None
    ) -> dict[str, Any]:
        _check_keys(arguments, {"task_id", "label_id"})
        task_id = _required_id(arguments, "task_id")
        label_id = _required_id(arguments, "label_id")
        removed = self.tasks.remove_label(task_id, label_id, now=now)
        return {"task_id": task_id, "label_id": label_id, "removed": removed}

    def _add_dependency(
        self, arguments: Mapping[str, Any], now: datetime | None
    ) -> dict[str, Any]:
        _check_keys(arguments, {"blocked_task_id", "blocker_task_id"})
        blocked_id = _required_id(arguments, "blocked_task_id")
        blocker_id = _required_id(arguments, "blocker_task_id")
        created = self.tasks.add_dependency(blocked_id, blocker_id, now=now)
        return {
            "blocked_task_id": blocked_id,
            "blocker_task_id": blocker_id,
            "created": created,
        }

    def _remove_dependency(
        self, arguments: Mapping[str, Any], now: datetime | None
    ) -> dict[str, Any]:
        _check_keys(arguments, {"blocked_task_id", "blocker_task_id"})
        blocked_id = _required_id(arguments, "blocked_task_id")
        blocker_id = _required_id(arguments, "blocker_task_id")
        removed = self.tasks.remove_dependency(blocked_id, blocker_id, now=now)
        return {
            "blocked_task_id": blocked_id,
            "blocker_task_id": blocker_id,
            "removed": removed,
        }

    def _task_summary(self, row: sqlite3.Row) -> dict[str, Any]:
        task_id = int(row["id"])
        waiting = self.tasks.active_waiting(task_id)
        unresolved_blockers = self.db.execute(
            """
            SELECT count(*)
            FROM task_dependencies d JOIN tasks t ON t.id = d.blocker_task_id
            WHERE d.blocked_task_id = ? AND t.status <> 'done'
            """,
            (task_id,),
        ).fetchone()[0]
        return {
            "id": task_id,
            "title": row["title"],
            "description": row["description"],
            "status": row["status"],
            "due_date": row["due_date"],
            "waiting_on": waiting["person_name"] if waiting else None,
            "unresolved_blockers": unresolved_blockers,
            "updated_at": row["updated_at"],
        }


def _object_schema(
    properties: dict[str, Any], *, required: tuple[str, ...] = ()
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _positive_integer() -> dict[str, Any]:
    return {"type": "integer", "minimum": 1}


def _task_id_schema() -> dict[str, Any]:
    return _positive_integer()


def _nullable_date_schema() -> dict[str, Any]:
    return {"type": ["string", "null"], "format": "date"}


def _check_keys(arguments: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = set(arguments) - allowed
    if unknown:
        raise ValueError(f"Unsupported tool argument: {sorted(unknown)[0]}")


def _required_id(arguments: Mapping[str, Any], name: str) -> int:
    value = arguments.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _required_string(
    arguments: Mapping[str, Any], name: str, maximum: int
) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum} characters")
    return value.strip()
