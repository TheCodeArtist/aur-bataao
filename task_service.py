from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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


@dataclass(frozen=True)
class WaitingUpdateResult:
    created: bool
    changed: bool


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


def _valid_follow_up_date(value: Any, today: date) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("Next follow-up must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Next follow-up must be YYYY-MM-DD") from exc
    if parsed < today:
        raise ValueError("Next follow-up cannot be in the past")
    return parsed.isoformat()


def _valid_follow_up_time(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("Follow-up time must be HH:MM")
    try:
        parsed = datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise ValueError("Follow-up time must be HH:MM") from exc
    if parsed.strftime("%H:%M") != value:
        raise ValueError("Follow-up time must be HH:MM")
    return value


def _person_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Enter who you are waiting on")
    name = " ".join(value.split())
    if not name or len(name) > 100:
        raise ValueError("Person name must be between 1 and 100 characters")
    return name


def _waiting_note(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Note must be text")
    note = value.strip()
    if len(note) > 1000:
        raise ValueError("Note must be at most 1000 characters")
    return note


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

    def add_comment(
        self,
        task_id: int,
        body: Any,
        *,
        counts_as_progress: bool = False,
        now: datetime | None = None,
    ) -> int:
        comment = str(body).strip()
        if not comment or len(comment) > 2000:
            raise ValueError("Comment must be between 1 and 2000 characters")
        self.task(task_id)
        now = now or utc_now()
        cursor = self.db.execute(
            "INSERT INTO comments(task_id, body, created_at) VALUES (?, ?, ?)",
            (task_id, comment, iso_utc(now)),
        )
        comment_id = int(cursor.lastrowid)
        self.record_event(
            task_id,
            "comment_added",
            counts_as_progress=counts_as_progress,
            details={"comment_id": comment_id},
            now=now,
        )
        return comment_id

    def set_waiting(
        self,
        task_id: int,
        values: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> WaitingUpdateResult:
        allowed = {
            "person_name",
            "note",
            "next_follow_up_on",
            "next_follow_up_time",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unsupported field: {sorted(unknown)[0]}")

        task = self.task(task_id)
        waiting = self.active_waiting(task_id)
        if task["status"] == "done":
            raise ValueError("Reopen the completed task before waiting on someone")
        if waiting is None and "person_name" not in values:
            raise ValueError("Enter who you are waiting on")

        now = now or utc_now()
        today = now.astimezone(self.timezone).date()
        person_name = _person_name(
            values.get("person_name", waiting["person_name"] if waiting else None)
        )
        note = _waiting_note(
            values.get("note", waiting["note"] if waiting else "")
        )
        if "next_follow_up_on" in values:
            next_follow_up_on = _valid_follow_up_date(
                values["next_follow_up_on"], today
            )
        elif waiting is not None:
            next_follow_up_on = waiting["next_follow_up_on"]
        else:
            next_follow_up_on = (today + timedelta(days=3)).isoformat()
        if "next_follow_up_time" in values:
            next_follow_up_time = _valid_follow_up_time(
                values["next_follow_up_time"]
            )
        elif waiting is not None:
            next_follow_up_time = waiting["next_follow_up_time"]
        else:
            next_follow_up_time = None
        if next_follow_up_on is None:
            if next_follow_up_time is not None:
                raise ValueError("Choose a follow-up date before adding a time")
            next_follow_up_time = None

        details = {
            "person_name": person_name,
            "note": note,
            "next_follow_up_on": next_follow_up_on,
            "next_follow_up_time": next_follow_up_time,
        }
        if waiting is None:
            self.db.execute(
                """
                INSERT INTO task_waiting(
                    task_id, person_name, note, started_at,
                    next_follow_up_on, next_follow_up_time, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    person_name,
                    note,
                    iso_utc(now),
                    next_follow_up_on,
                    next_follow_up_time,
                    iso_utc(now),
                ),
            )
            self.record_event(
                task_id, "waiting_started", details=details, now=now
            )
            return WaitingUpdateResult(created=True, changed=True)

        changed = (
            person_name != waiting["person_name"]
            or note != waiting["note"]
            or next_follow_up_on != waiting["next_follow_up_on"]
            or next_follow_up_time != waiting["next_follow_up_time"]
        )
        if changed:
            self.db.execute(
                """
                UPDATE task_waiting
                SET person_name = ?, note = ?, next_follow_up_on = ?,
                    next_follow_up_time = ?, updated_at = ?
                WHERE id = ? AND resolved_at IS NULL
                """,
                (
                    person_name,
                    note,
                    next_follow_up_on,
                    next_follow_up_time,
                    iso_utc(now),
                    waiting["id"],
                ),
            )
            self.record_event(
                task_id, "waiting_updated", details=details, now=now
            )
        return WaitingUpdateResult(created=False, changed=changed)

    def record_follow_up(
        self,
        task_id: int,
        values: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> None:
        allowed = {"note", "next_follow_up_on", "next_follow_up_time"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unsupported field: {sorted(unknown)[0]}")

        task = self.task(task_id)
        if task["status"] == "done":
            raise ValueError("A completed task cannot be followed up")
        waiting = self.active_waiting(task_id)
        if waiting is None:
            raise ValueError("This task is not waiting on anyone")

        now = now or utc_now()
        today = now.astimezone(self.timezone).date()
        note = _waiting_note(values.get("note"))
        next_follow_up_on = _valid_follow_up_date(
            values.get(
                "next_follow_up_on", (today + timedelta(days=3)).isoformat()
            ),
            today,
        )
        next_follow_up_time = _valid_follow_up_time(
            values["next_follow_up_time"]
            if "next_follow_up_time" in values
            else waiting["next_follow_up_time"]
        )
        if next_follow_up_on is None and next_follow_up_time is not None:
            raise ValueError("Choose a follow-up date before adding a time")

        self.db.execute(
            """
            UPDATE task_waiting
            SET last_followed_up_at = ?, next_follow_up_on = ?,
                next_follow_up_time = ?, updated_at = ?
            WHERE id = ? AND resolved_at IS NULL
            """,
            (
                iso_utc(now),
                next_follow_up_on,
                next_follow_up_time,
                iso_utc(now),
                waiting["id"],
            ),
        )
        self.record_event(
            task_id,
            "followed_up",
            counts_as_progress=True,
            details={
                "person_name": waiting["person_name"],
                "note": note,
                "next_follow_up_on": next_follow_up_on,
                "next_follow_up_time": next_follow_up_time,
            },
            now=now,
        )

    def resolve_waiting(
        self,
        task_id: int,
        *,
        reason: str = "resolved",
        now: datetime | None = None,
    ) -> None:
        self.task(task_id)
        now = now or utc_now()
        if self.resolve_active_waiting(task_id, now, reason=reason) is None:
            raise ValueError("This task is not waiting on anyone")

    def add_label(
        self, task_id: int, name: Any, *, now: datetime | None = None
    ) -> int:
        normalized_name = str(name).strip().lower()
        if not normalized_name or len(normalized_name) > 32:
            raise ValueError("Label must be between 1 and 32 characters")
        if normalized_name.startswith("active:"):
            raise ValueError("The active: prefix is reserved")
        self.task(task_id)
        now = now or utc_now()
        self.db.execute(
            "INSERT OR IGNORE INTO labels(name, type) VALUES (?, 'manual')",
            (normalized_name,),
        )
        label = self.db.execute(
            "SELECT id, type FROM labels WHERE name = ?", (normalized_name,)
        ).fetchone()
        if label["type"] != "manual":
            raise ValueError("That label name is reserved")
        self.db.execute(
            """
            INSERT OR IGNORE INTO task_labels(task_id, label_id, source, added_at)
            VALUES (?, ?, 'user', ?)
            """,
            (task_id, label["id"], iso_utc(now)),
        )
        self.record_event(
            task_id,
            "label_added",
            details={"label_id": label["id"]},
            now=now,
        )
        return int(label["id"])

    def remove_label(
        self, task_id: int, label_id: int, *, now: datetime | None = None
    ) -> bool:
        self.task(task_id)
        now = now or utc_now()
        cursor = self.db.execute(
            """
            UPDATE task_labels SET removed_at = ?
            WHERE task_id = ? AND label_id = ? AND removed_at IS NULL
            """,
            (iso_utc(now), task_id, label_id),
        )
        if cursor.rowcount == 0:
            return False
        self.record_event(
            task_id,
            "label_removed",
            details={"label_id": label_id},
            now=now,
        )
        return True

    def delete_unused_label(self, label_id: int) -> bool:
        cursor = self.db.execute(
            """
            DELETE FROM labels
            WHERE id = ?
              AND type = 'manual'
              AND NOT EXISTS (
                  SELECT 1 FROM task_labels
                  WHERE label_id = labels.id AND removed_at IS NULL
              )
            """,
            (label_id,),
        )
        if cursor.rowcount:
            return True
        label = self.db.execute(
            "SELECT type FROM labels WHERE id = ?", (label_id,)
        ).fetchone()
        if label is None:
            return False
        if label["type"] != "manual":
            raise ValueError("Automatic labels cannot be deleted")
        raise ValueError("Remove this label from every task before deleting it")

    def _would_create_dependency_cycle(
        self, blocked_id: int, blocker_id: int
    ) -> bool:
        row = self.db.execute(
            """
            WITH RECURSIVE chain(task_id) AS (
                SELECT ?
                UNION
                SELECT d.blocker_task_id
                FROM task_dependencies d JOIN chain c ON d.blocked_task_id = c.task_id
            )
            SELECT 1 FROM chain WHERE task_id = ? LIMIT 1
            """,
            (blocker_id, blocked_id),
        ).fetchone()
        return row is not None

    def add_dependency(
        self,
        blocked_id: int,
        blocker_id: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        if blocker_id == blocked_id:
            raise ValueError("A task cannot block itself")
        blocked_task = self.task(blocked_id)
        blocker = self.task(blocker_id)
        if blocked_task["status"] == "done":
            raise ValueError("Reopen the completed task before adding a blocker")
        if blocker["status"] == "done":
            raise ValueError("A completed task cannot be an active blocker")
        if self._would_create_dependency_cycle(blocked_id, blocker_id):
            raise ValueError("That dependency would create a cycle")
        cursor = self.db.execute(
            """
            INSERT OR IGNORE INTO task_dependencies(blocked_task_id, blocker_task_id)
            VALUES (?, ?)
            """,
            (blocked_id, blocker_id),
        )
        if cursor.rowcount == 0:
            return False
        self.record_event(
            blocked_id,
            "dependency_added",
            details={"blocker_task_id": blocker_id},
            now=now,
        )
        return True

    def remove_dependency(
        self,
        blocked_id: int,
        blocker_id: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        self.task(blocked_id)
        blocker = self.task(blocker_id)
        cursor = self.db.execute(
            "DELETE FROM task_dependencies WHERE blocked_task_id = ? AND blocker_task_id = ?",
            (blocked_id, blocker_id),
        )
        if cursor.rowcount == 0:
            return False
        self.record_event(
            blocked_id,
            "dependency_removed",
            counts_as_progress=blocker["status"] != "done",
            details={"blocker_task_id": blocker_id},
            now=now,
        )
        return True
