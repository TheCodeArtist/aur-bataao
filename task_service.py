from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo


STATUS_LABELS = {
    "todo": "To do",
    "in_progress": "In progress",
    "done": "Done",
}
STATUSES = frozenset(STATUS_LABELS)
RANK_SPACING = 1024
SQLITE_INTEGER_MIN = -(2**63)


class TaskNotFoundError(LookupError):
    """Raised when a domain operation references a missing task."""


@dataclass(frozen=True)
class TaskCreateResult:
    task_id: int
    created: bool


@dataclass(frozen=True)
class TaskUpdateResult:
    changed: bool
    values: dict[str, Any]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def valid_due_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("Due date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("Due date must be YYYY-MM-DD") from exc


def _create_request_id(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("Invalid task submission; reopen the form and try again") from exc
    return parsed.hex


def rank_ordered_task_ids(db: sqlite3.Connection) -> list[int]:
    return [
        row["id"]
        for row in db.execute("SELECT id FROM tasks ORDER BY rank_key, id").fetchall()
    ]


def rebalance_task_ranks(
    db: sqlite3.Connection, ordered_ids: list[int]
) -> None:
    """Assign compact unique ranks without transient uniqueness collisions."""
    db.execute("UPDATE tasks SET rank_key = NULL")
    db.executemany(
        "UPDATE tasks SET rank_key = ? WHERE id = ?",
        ((index * RANK_SPACING, task_id) for index, task_id in enumerate(ordered_ids, 1)),
    )


def new_task_rank_key(db: sqlite3.Connection) -> int:
    first_rank = db.execute(
        "SELECT MIN(rank_key) AS rank_key FROM tasks"
    ).fetchone()["rank_key"]
    if first_rank is None:
        return RANK_SPACING
    if first_rank < SQLITE_INTEGER_MIN + RANK_SPACING:
        rebalance_task_ranks(db, rank_ordered_task_ids(db))
        first_rank = db.execute(
            "SELECT MIN(rank_key) AS rank_key FROM tasks"
        ).fetchone()["rank_key"]
    return first_rank - RANK_SPACING


class TaskService:
    """Task-domain operations shared by HTTP routes and agent tools.

    Methods deliberately do not commit. The caller owns the transaction so a
    tool invocation can combine domain changes with its own audit record.
    """

    def __init__(self, db: sqlite3.Connection, timezone_info: ZoneInfo):
        self.db = db
        self.timezone = timezone_info

    def task(self, task_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFoundError(task_id)
        return row

    def active_waiting(self, task_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM task_waiting WHERE task_id = ? AND resolved_at IS NULL",
            (task_id,),
        ).fetchone()

    def attach_active_label(
        self,
        task_id: int,
        local_day: str,
        now: datetime,
        *,
        allow_readd: bool = True,
    ) -> None:
        name = f"active:{local_day}"
        self.db.execute(
            "INSERT OR IGNORE INTO labels(name, type) VALUES (?, 'active_date')",
            (name,),
        )
        label_id = self.db.execute(
            "SELECT id FROM labels WHERE name = ?", (name,)
        ).fetchone()["id"]
        if not allow_readd and self.db.execute(
            "SELECT 1 FROM task_labels WHERE task_id = ? AND label_id = ? LIMIT 1",
            (task_id, label_id),
        ).fetchone():
            return
        self.db.execute(
            """
            INSERT OR IGNORE INTO task_labels(task_id, label_id, source, added_at)
            VALUES (?, ?, 'automatic', ?)
            """,
            (task_id, label_id, iso_utc(now)),
        )

    def record_event(
        self,
        task_id: int,
        event_type: str,
        *,
        counts_as_progress: bool = False,
        details: dict[str, Any] | None = None,
        now: datetime | None = None,
        active_for_label: bool | None = None,
    ) -> None:
        now = now or utc_now()
        local_day = now.astimezone(self.timezone).date().isoformat()
        self.db.execute(
            """
            INSERT INTO task_events(
                task_id, event_type, counts_as_progress, occurred_at_utc, local_date, details_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                event_type,
                int(counts_as_progress),
                iso_utc(now),
                local_day,
                json.dumps(details or {}),
            ),
        )
        if not counts_as_progress:
            return
        self.db.execute(
            "UPDATE tasks SET last_progress_at = ?, updated_at = ? WHERE id = ?",
            (iso_utc(now), iso_utc(now), task_id),
        )
        if active_for_label is None:
            row = self.db.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            active_for_label = bool(row and row["status"] == "in_progress")
        if active_for_label:
            self.attach_active_label(task_id, local_day, now)

    def create_task(
        self,
        *,
        title: Any,
        blocks_task_id: Any = None,
        request_id: Any = None,
        now: datetime | None = None,
    ) -> TaskCreateResult:
        normalized_title = str(title).strip()
        if not normalized_title or len(normalized_title) > 200:
            raise ValueError("Title must be between 1 and 200 characters")

        if blocks_task_id in (None, ""):
            normalized_blocks_id = None
        else:
            try:
                normalized_blocks_id = int(blocks_task_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid blocked task") from exc
            blocked_task = self.task(normalized_blocks_id)
            if blocked_task["status"] == "done":
                raise ValueError("Reopen the completed task before adding a blocker")

        normalized_request_id = _create_request_id(request_id)
        if normalized_request_id is not None:
            existing = self.db.execute(
                "SELECT id FROM tasks WHERE create_request_id = ?",
                (normalized_request_id,),
            ).fetchone()
            if existing is not None:
                return TaskCreateResult(existing["id"], created=False)

        now = now or utc_now()
        cursor = self.db.execute(
            """
            INSERT INTO tasks(rank_key, title, created_at, updated_at, create_request_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                new_task_rank_key(self.db),
                normalized_title,
                iso_utc(now),
                iso_utc(now),
                normalized_request_id,
            ),
        )
        task_id = int(cursor.lastrowid)
        self.record_event(
            task_id, "task_created", details={"status": "todo"}, now=now
        )
        if normalized_blocks_id is not None:
            self.db.execute(
                """
                INSERT INTO task_dependencies(blocked_task_id, blocker_task_id)
                VALUES (?, ?)
                """,
                (normalized_blocks_id, task_id),
            )
            self.record_event(
                normalized_blocks_id,
                "dependency_added",
                details={"blocker_task_id": task_id},
                now=now,
            )
        return TaskCreateResult(task_id, created=True)

    def _change_status(
        self, task: sqlite3.Row, new_status: str, now: datetime
    ) -> bool:
        old_status = task["status"]
        if old_status == new_status:
            return False
        self.db.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, iso_utc(now), task["id"]),
        )
        counts = old_status == "in_progress" or new_status == "in_progress"
        self.record_event(
            task["id"],
            "status_changed",
            counts_as_progress=counts,
            active_for_label=counts,
            details={"old_status": old_status, "new_status": new_status},
            now=now,
        )
        if new_status == "done":
            event_type = "dependency_resolved"
            counts_as_progress = True
        elif old_status == "done":
            event_type = "dependency_reopened"
            counts_as_progress = False
        else:
            return True
        for dependent in self.db.execute(
            "SELECT blocked_task_id FROM task_dependencies WHERE blocker_task_id = ?",
            (task["id"],),
        ).fetchall():
            self.record_event(
                dependent["blocked_task_id"],
                event_type,
                counts_as_progress=counts_as_progress,
                details={"blocker_task_id": task["id"]},
                now=now,
            )
        return True

    def resolve_active_waiting(
        self, task_id: int, now: datetime, *, reason: str
    ) -> sqlite3.Row | None:
        waiting = self.active_waiting(task_id)
        if waiting is None:
            return None
        self.db.execute(
            "UPDATE task_waiting SET resolved_at = ?, updated_at = ? WHERE id = ?",
            (iso_utc(now), iso_utc(now), waiting["id"]),
        )
        self.record_event(
            task_id,
            "waiting_resolved",
            details={"person_name": waiting["person_name"], "reason": reason},
            now=now,
        )
        return waiting

    def update_task(
        self,
        task_id: int,
        values: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> TaskUpdateResult:
        allowed = {"title", "description", "status", "due_date"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unsupported field: {sorted(unknown)[0]}")

        task = self.task(task_id)
        normalized: dict[str, Any] = {}
        if "title" in values:
            title = str(values["title"]).strip()
            if not title or len(title) > 200:
                raise ValueError("Title must be between 1 and 200 characters")
            normalized["title"] = title
        if "description" in values:
            description = str(values["description"]).strip()
            if len(description) > 5000:
                raise ValueError("Description must be at most 5000 characters")
            normalized["description"] = description
        if "status" in values:
            status = str(values["status"])
            if status not in STATUSES:
                raise ValueError("Invalid status")
            normalized["status"] = status
        if "due_date" in values:
            normalized["due_date"] = valid_due_date(values["due_date"])

        changed_fields = {
            key: value for key, value in normalized.items() if value != task[key]
        }
        if not changed_fields:
            return TaskUpdateResult(False, normalized)

        now = now or utc_now()
        new_status = changed_fields.pop("status", None)
        if new_status is not None:
            self._change_status(task, new_status, now)
            if new_status == "done":
                self.resolve_active_waiting(
                    task_id, now, reason="task_completed"
                )
        if changed_fields:
            assignments = ", ".join(f"{key} = ?" for key in changed_fields)
            self.db.execute(
                f"UPDATE tasks SET {assignments}, updated_at = ? WHERE id = ?",
                (*changed_fields.values(), iso_utc(now), task_id),
            )
            self.record_event(
                task_id,
                "task_updated",
                details={"fields": list(changed_fields)},
                now=now,
            )
        return TaskUpdateResult(True, normalized)
