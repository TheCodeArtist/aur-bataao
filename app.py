from __future__ import annotations

import json
import os
import random
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (
    Flask,
    abort,
    current_app,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from agent_runner import AgentOutcome, AgentRunner
from agent_store import AgentNotFoundError, AgentStore
from llm_profiles import LlmProfileNotFoundError, LlmProfileStore
from llm_provider import ChatCompletionsProvider

from task_service import (
    DEFAULT_FOLLOW_UP_TIME,
    RANK_SPACING,
    SQLITE_INTEGER_MIN,
    STATUS_LABELS,
    TaskNotFoundError,
    TaskService,
    iso_utc,
    parse_utc,
    rebalance_task_ranks,
    utc_now,
)

TASK_VIEWS = {"focus", "manage", "blocked"}
APP_VIEWS = TASK_VIEWS | {"agent"}
FORM_META_FIELDS = {"return_view", "expand", "task_id"}
NOTICE_MESSAGES = {
    "task-created": "Task added",
    "task-updated": "Task updated",
    "task-completed": "Task completed",
    "attachments-added": "Attachments added",
    "attachment-removed": "Attachment removed",
    "comment-added": "Comment added",
    "waiting-saved": "Waiting details saved",
    "follow-up-recorded": "Follow-up recorded",
    "waiting-resolved": "Waiting resolved",
    "label-added": "Label added",
    "label-removed": "Label removed",
    "label-deleted": "Label deleted",
    "dependency-added": "Dependency added",
    "dependency-removed": "Dependency removed",
}
MAX_RECONCILE_AGE_SECONDS = 60
PREVIEWABLE_IMAGE_TYPES = {"image/gif", "image/jpeg", "image/png", "image/webp"}
SQLITE_INTEGER_MAX = 2**63 - 1
EMPTY_STATE_HEROES = (
    ("🍻", "Clinking beer mugs"),
    ("🥂", "Clinking glasses"),
    ("🍹", "Tropical drink"),
    ("🍾", "Bottle with popping cork"),
    ("🧋", "Bubble tea"),
)


def create_app(
    test_config: dict[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Flask:
    environment = os.environ if environ is None else environ
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        DATABASE=environment.get(
            "AUR_BATAAO_DATABASE", str(Path(app.instance_path) / "tasks.sqlite3")
        ),
        USER_TIMEZONE=environment.get("AUR_BATAAO_TIMEZONE", "Asia/Kolkata"),
        ATTACHMENTS_DIR=environment.get("AUR_BATAAO_ATTACHMENTS_DIR"),
        MAX_ATTACHMENT_BYTES=10 * 1024 * 1024,
        MAX_ATTACHMENTS_PER_TASK=20,
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
        LLM_BASE_URL=environment.get(
            "AUR_BATAAO_LLM_BASE_URL", "http://127.0.0.1:1234/v1"
        ),
        LLM_MODEL=environment.get("AUR_BATAAO_LLM_MODEL", ""),
        LLM_API_KEY_ENV=environment.get("AUR_BATAAO_LLM_API_KEY_ENV")
        or (
            "AUR_BATAAO_LLM_API_KEY"
            if environment.get("AUR_BATAAO_LLM_API_KEY")
            else None
        ),
        LLM_TIMEOUT_SECONDS=_environment_number(
            environment, "AUR_BATAAO_LLM_TIMEOUT_SECONDS", 60
        ),
        LLM_SUPPORTS_TOOLS=environment.get("AUR_BATAAO_LLM_SUPPORTS_TOOLS", "1")
        != "0",
        AGENT_MAX_STEPS=_environment_integer(
            environment, "AUR_BATAAO_AGENT_MAX_STEPS", 8
        ),
    )
    if test_config:
        app.config.update(test_config)

    if not app.config["ATTACHMENTS_DIR"]:
        app.config["ATTACHMENTS_DIR"] = str(Path(app.config["DATABASE"]).parent / "attachments")

    try:
        app.config["TZINFO"] = ZoneInfo(app.config["USER_TIMEZONE"])
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"Unknown timezone: {app.config['USER_TIMEZONE']}") from exc

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    Path(app.config["ATTACHMENTS_DIR"]).mkdir(parents=True, exist_ok=True)
    app.extensions["reconcile_lock"] = threading.Lock()
    app.extensions["last_reconcile_monotonic"] = 0.0
    app.extensions["llm_provider_factory"] = ChatCompletionsProvider
    app.extensions["environment"] = environment

    app.teardown_appcontext(close_db)
    register_routes(app)

    with app.app_context():
        init_db()
        recover_interrupted_agent_runs()
        seed_default_llm_profile()
        reconcile_active_labels()
        app.extensions["last_reconcile_monotonic"] = time.monotonic()

    return app


def _environment_number(
    environment: Mapping[str, str], name: str, default: float
) -> float:
    value = environment.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc


def _environment_integer(
    environment: Mapping[str, str], name: str, default: int
) -> int:
    value = environment.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db = sqlite3.connect(current_app.config["DATABASE"])
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 5000")
        g.db = db
    return g.db


def close_db(_: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    schema = Path(current_app.root_path, "schema.sql").read_text(encoding="utf-8")
    db = get_db()
    db.executescript(schema)
    migrate_task_ranks(db)
    migrate_waiting_times(db)
    migrate_legacy_subtasks(db)
    migrate_create_request_ids(db)
    migrate_agent_run_config(db)
    migrate_agent_folders(db)
    db.commit()


def seed_default_llm_profile() -> None:
    """Seed a first profile from environment configuration, when complete."""
    model = current_app.config["LLM_MODEL"]
    if not model:
        return
    db = get_db()
    store = _llm_profile_store(db)
    if store.list():
        return
    store.create(
        {
            "name": "Environment default",
            "base_url": current_app.config["LLM_BASE_URL"],
            "model": model,
            "api_key_env": current_app.config["LLM_API_KEY_ENV"],
            "timeout_seconds": current_app.config["LLM_TIMEOUT_SECONDS"],
            "supports_tools": current_app.config["LLM_SUPPORTS_TOOLS"],
        },
        make_default=True,
    )
    db.commit()


def recover_interrupted_agent_runs() -> int:
    """Fail runs that could not finish because the application stopped."""
    db = get_db()
    store = AgentStore(db)
    run_ids = [
        row["id"]
        for row in db.execute(
            "SELECT id FROM agent_runs WHERE status = 'running'"
        ).fetchall()
    ]
    for run_id in run_ids:
        store.transition_run(
            run_id,
            "failed",
            error="Agent run was interrupted by an application restart",
        )
    if run_ids:
        db.commit()
    return len(run_ids)


def migrate_create_request_ids(db: sqlite3.Connection) -> None:
    """Add retry-safe task creation IDs to existing databases."""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)").fetchall()}
    if "create_request_id" not in columns:
        db.execute("ALTER TABLE tasks ADD COLUMN create_request_id TEXT")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_create_request_id "
        "ON tasks(create_request_id) WHERE create_request_id IS NOT NULL"
    )


def migrate_agent_run_config(db: sqlite3.Connection) -> None:
    """Snapshot complete endpoint configuration on runs created by older builds."""
    columns = {
        row["name"] for row in db.execute("PRAGMA table_info(agent_runs)").fetchall()
    }
    additions = {
        "api_key_env": "TEXT",
        "timeout_seconds": "REAL NOT NULL DEFAULT 60",
        "supports_tools": "INTEGER NOT NULL DEFAULT 1",
    }
    for name, declaration in additions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE agent_runs ADD COLUMN {name} {declaration}")
    db.execute(
        """
        UPDATE agent_runs
        SET api_key_env = COALESCE(
                api_key_env,
                (SELECT api_key_env FROM llm_profiles WHERE id = agent_runs.profile_id)
            ),
            timeout_seconds = COALESCE(
                timeout_seconds,
                (SELECT timeout_seconds FROM llm_profiles WHERE id = agent_runs.profile_id),
                60
            ),
            supports_tools = COALESCE(
                supports_tools,
                (SELECT supports_tools FROM llm_profiles WHERE id = agent_runs.profile_id),
                1
            )
        """
    )


def migrate_agent_folders(db: sqlite3.Connection) -> None:
    """Add folders and retain conversations archived by earlier builds."""
    columns = {
        row["name"] for row in db.execute("PRAGMA table_info(agent_sessions)").fetchall()
    }
    if "folder_id" not in columns:
        db.execute(
            "ALTER TABLE agent_sessions ADD COLUMN folder_id INTEGER "
            "REFERENCES agent_folders(id) ON DELETE SET NULL"
        )
    archived = db.execute(
        "SELECT 1 FROM agent_sessions WHERE status = 'archived' LIMIT 1"
    ).fetchone()
    if archived is not None:
        db.execute(
            "INSERT OR IGNORE INTO agent_folders(name, created_at) VALUES ('Archived', ?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
        )
        folder_id = db.execute(
            "SELECT id FROM agent_folders WHERE name = 'Archived' COLLATE NOCASE"
        ).fetchone()["id"]
        db.execute(
            "UPDATE agent_sessions SET folder_id = ?, status = 'active' "
            "WHERE status = 'archived'",
            (folder_id,),
        )


def _smart_ordered_task_ids(db: sqlite3.Connection) -> list[int]:
    return [
        row["id"]
        for row in db.execute(
            """
            SELECT id FROM tasks
            ORDER BY CASE status
                         WHEN 'in_progress' THEN 0
                         WHEN 'todo' THEN 2
                         ELSE 3
                     END,
                     COALESCE(due_date, '9999-12-31'), id DESC
            """
        ).fetchall()
    ]


def migrate_task_ranks(db: sqlite3.Connection) -> None:
    """Add and safely initialize persistent task ranks for existing databases."""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)").fetchall()}
    if "rank_key" not in columns:
        db.execute("ALTER TABLE tasks ADD COLUMN rank_key INTEGER")

    ranks = db.execute("SELECT rank_key FROM tasks").fetchall()
    rank_values = [row["rank_key"] for row in ranks]
    if any(value is None for value in rank_values) or len(rank_values) != len(set(rank_values)):
        rebalance_task_ranks(db, _smart_ordered_task_ids(db))

    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_rank_key ON tasks(rank_key)"
    )


def migrate_waiting_times(db: sqlite3.Connection) -> None:
    """Add optional exact reminder times without changing date-only reminders."""
    columns = {
        row["name"]
        for row in db.execute("PRAGMA table_info(task_waiting)").fetchall()
    }
    if "next_follow_up_time" not in columns:
        db.execute("ALTER TABLE task_waiting ADD COLUMN next_follow_up_time TEXT")


def migrate_legacy_subtasks(db: sqlite3.Connection) -> int:
    """Convert the former parent/child hierarchy into blocking relationships."""
    legacy_links = db.execute(
        "SELECT id, parent_task_id FROM tasks WHERE parent_task_id IS NOT NULL"
    ).fetchall()
    migrated = 0
    now = utc_now()
    for task in legacy_links:
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO task_dependencies(blocked_task_id, blocker_task_id)
            VALUES (?, ?)
            """,
            (task["parent_task_id"], task["id"]),
        )
        if cursor.rowcount:
            migrated += 1
            _record_event(
                db,
                task["parent_task_id"],
                "dependency_added",
                details={"blocker_task_id": task["id"], "migrated_from": "subtask"},
                now=now,
            )
    if legacy_links:
        db.execute("UPDATE tasks SET parent_task_id = NULL WHERE parent_task_id IS NOT NULL")
    return migrated


def local_now() -> datetime:
    return datetime.now(current_app.config["TZINFO"])


def local_today() -> date:
    return local_now().date()


def _task_service(db: sqlite3.Connection) -> TaskService:
    return TaskService(db, current_app.config["TZINFO"])


def _llm_profile_store(db: sqlite3.Connection) -> LlmProfileStore:
    return LlmProfileStore(db, environ=current_app.extensions["environment"])


def _public_llm_profile(profile) -> dict[str, Any]:
    return profile.public_dict(current_app.extensions["environment"])


def _attach_active_label(
    db: sqlite3.Connection,
    task_id: int,
    local_day: str,
    now: datetime,
    *,
    allow_readd: bool = True,
) -> None:
    _task_service(db).attach_active_label(
        task_id, local_day, now, allow_readd=allow_readd
    )


def _record_event(
    db: sqlite3.Connection,
    task_id: int,
    event_type: str,
    *,
    counts_as_progress: bool = False,
    details: dict[str, Any] | None = None,
    now: datetime | None = None,
    active_for_label: bool | None = None,
) -> None:
    _task_service(db).record_event(
        task_id,
        event_type,
        counts_as_progress=counts_as_progress,
        details=details,
        now=now,
        active_for_label=active_for_label,
    )


def _status_intervals(db: sqlite3.Connection, task: sqlite3.Row, now: datetime) -> list[tuple[datetime, datetime]]:
    events = db.execute(
        """
        SELECT occurred_at_utc, details_json
        FROM task_events
        WHERE task_id = ? AND event_type = 'status_changed'
        ORDER BY occurred_at_utc, id
        """,
        (task["id"],),
    ).fetchall()
    intervals: list[tuple[datetime, datetime]] = []
    opened: datetime | None = None
    for event in events:
        try:
            details = json.loads(event["details_json"])
        except json.JSONDecodeError:
            continue
        old_status = details.get("old_status")
        new_status = details.get("new_status")
        occurred = parse_utc(event["occurred_at_utc"])
        if new_status == "in_progress" and old_status != "in_progress":
            opened = occurred
        elif old_status == "in_progress" and new_status != "in_progress" and opened:
            intervals.append((opened, occurred))
            opened = None
    if task["status"] == "in_progress":
        opened = opened or parse_utc(task["created_at"])
        intervals.append((opened, now))
    return intervals


def _interval_touches_local_day(started: datetime, ended: datetime, local_day: date) -> bool:
    tz = current_app.config["TZINFO"]
    day_start = datetime.combine(local_day, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    day_end = (datetime.combine(local_day, datetime.min.time(), tzinfo=tz) + timedelta(days=1)).astimezone(timezone.utc)
    return started < day_end and ended > day_start


def reconcile_active_labels(now: datetime | None = None) -> int:
    """Backfill one active-date label for every local date touched by in-progress state."""
    db = get_db()
    now = now or utc_now()
    tz = current_app.config["TZINFO"]
    added_before = db.total_changes
    for task in db.execute("SELECT id, status, created_at FROM tasks").fetchall():
        for started, ended in _status_intervals(db, task, now):
            cursor = started.astimezone(tz).date()
            last_day = ended.astimezone(tz).date()
            while cursor <= last_day:
                if _interval_touches_local_day(started, ended, cursor):
                    _attach_active_label(db, task["id"], cursor.isoformat(), now, allow_readd=False)
                cursor += timedelta(days=1)
    db.commit()
    return db.total_changes - added_before


def maybe_reconcile() -> None:
    app = current_app._get_current_object()
    if time.monotonic() - app.extensions["last_reconcile_monotonic"] < MAX_RECONCILE_AGE_SECONDS:
        return
    with app.extensions["reconcile_lock"]:
        if time.monotonic() - app.extensions["last_reconcile_monotonic"] < MAX_RECONCILE_AGE_SECONDS:
            return
        reconcile_active_labels()
        app.extensions["last_reconcile_monotonic"] = time.monotonic()


def _task_or_404(db: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        from flask import abort

        abort(404)
    return row


def _active_waiting(db: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return _task_service(db).active_waiting(task_id)


def _attachment_path(stored_name: str) -> Path:
    if len(stored_name) != 32 or any(character not in "0123456789abcdef" for character in stored_name):
        raise RuntimeError("Invalid stored attachment name")
    return Path(current_app.config["ATTACHMENTS_DIR"]) / stored_name


def _display_filename(filename: str | None) -> str:
    raw_name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    clean_name = "".join(character for character in raw_name if character >= " " and character != "\x7f").strip()
    return clean_name[:255] or "attachment"


def _size_label(byte_size: int) -> str:
    if byte_size < 1024:
        return f"{byte_size} B"
    if byte_size < 1024 * 1024:
        return f"{byte_size / 1024:.1f} KB"
    return f"{byte_size / (1024 * 1024):.1f} MB"


def _serialize_attachment(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["previewable"] = item["mime_type"] in PREVIEWABLE_IMAGE_TYPES
    item["size_label"] = _size_label(item["byte_size"])
    return item


def _save_attachments(
    db: sqlite3.Connection,
    task_id: int,
    uploads: list[Any],
    now: datetime,
) -> list[Path]:
    uploads = [upload for upload in uploads if upload and upload.filename]
    if not uploads:
        return []

    existing_count = db.execute(
        "SELECT count(*) AS count FROM task_attachments WHERE task_id = ?",
        (task_id,),
    ).fetchone()["count"]
    maximum_count = current_app.config["MAX_ATTACHMENTS_PER_TASK"]
    if existing_count + len(uploads) > maximum_count:
        raise ValueError(f"A task can have at most {maximum_count} attachments")

    maximum_bytes = current_app.config["MAX_ATTACHMENT_BYTES"]
    stored_paths: list[Path] = []
    try:
        for upload in uploads:
            stored_name = uuid.uuid4().hex
            target = _attachment_path(stored_name)
            byte_size = 0
            with target.open("xb") as stored_file:
                while chunk := upload.stream.read(64 * 1024):
                    byte_size += len(chunk)
                    if byte_size > maximum_bytes:
                        raise ValueError(
                            f"{_display_filename(upload.filename)} is larger than "
                            f"{_size_label(maximum_bytes)}"
                        )
                    stored_file.write(chunk)
            stored_paths.append(target)
            if byte_size == 0:
                raise ValueError(f"{_display_filename(upload.filename)} is empty")
            mime_type = (upload.mimetype or "application/octet-stream").lower()[:127]
            db.execute(
                """
                INSERT INTO task_attachments(
                    task_id, original_name, stored_name, mime_type, byte_size, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    _display_filename(upload.filename),
                    stored_name,
                    mime_type,
                    byte_size,
                    iso_utc(now),
                ),
            )
    except Exception:
        for stored_path in stored_paths:
            stored_path.unlink(missing_ok=True)
        # The current target is created before it is appended when validation fails.
        if "target" in locals():
            target.unlink(missing_ok=True)
        raise
    return stored_paths


def _serialize_task(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item.pop("create_request_id", None)
    item["labels"] = [
        dict(label)
        for label in db.execute(
            """
            SELECT l.id, l.name, l.type, tl.source
            FROM task_labels tl JOIN labels l ON l.id = tl.label_id
            WHERE tl.task_id = ? AND tl.removed_at IS NULL
            ORDER BY l.type, l.name
            """,
            (row["id"],),
        ).fetchall()
    ]
    item["comments"] = [
        dict(comment)
        for comment in db.execute(
            "SELECT id, body, created_at FROM comments WHERE task_id = ? ORDER BY id DESC LIMIT 20",
            (row["id"],),
        ).fetchall()
    ]
    item["attachments"] = [
        _serialize_attachment(attachment)
        for attachment in db.execute(
            """
            SELECT id, task_id, original_name, stored_name, mime_type, byte_size, created_at
            FROM task_attachments WHERE task_id = ? ORDER BY id DESC
            """,
            (row["id"],),
        ).fetchall()
    ]
    item["blocked_by"] = [
        dict(dep)
        for dep in db.execute(
            """
            SELECT t.id, t.title, t.status, (t.status = 'done') AS resolved
            FROM task_dependencies d JOIN tasks t ON t.id = d.blocker_task_id
            WHERE d.blocked_task_id = ? ORDER BY t.title COLLATE NOCASE
            """,
            (row["id"],),
        ).fetchall()
    ]
    item["blocks"] = [
        dict(dep)
        for dep in db.execute(
            """
            SELECT t.id, t.title, t.status
            FROM task_dependencies d JOIN tasks t ON t.id = d.blocked_task_id
            WHERE d.blocker_task_id = ? ORDER BY t.title COLLATE NOCASE
            """,
            (row["id"],),
        ).fetchall()
    ]
    waiting_row = _active_waiting(db, row["id"])
    item["waiting"] = None
    if waiting_row is not None:
        waiting = dict(waiting_row)
        last_followed_up = (
            parse_utc(waiting["last_followed_up_at"]).astimezone(
                current_app.config["TZINFO"]
            )
            if waiting["last_followed_up_at"]
            else None
        )
        waiting["last_followed_up_on"] = (
            last_followed_up.date().isoformat() if last_followed_up else None
        )
        waiting["last_followed_up_time"] = (
            last_followed_up.strftime("%H:%M") if last_followed_up else None
        )
        waiting["effective_follow_up_time"] = (
            waiting["next_follow_up_time"] or DEFAULT_FOLLOW_UP_TIME
        )
        waiting["history"] = []
        for event in db.execute(
            """
            SELECT occurred_at_utc, local_date, details_json
            FROM task_events
            WHERE task_id = ? AND event_type = 'followed_up'
            ORDER BY occurred_at_utc DESC, id DESC
            LIMIT 5
            """,
            (row["id"],),
        ).fetchall():
            try:
                details = json.loads(event["details_json"])
            except (json.JSONDecodeError, TypeError):
                details = {}
            followed_up_at = parse_utc(event["occurred_at_utc"]).astimezone(
                current_app.config["TZINFO"]
            )
            waiting["history"].append(
                {
                    "followed_up_on": event["local_date"],
                    "followed_up_time": followed_up_at.strftime("%H:%M"),
                    "note": details.get("note", ""),
                    "next_follow_up_on": details.get("next_follow_up_on"),
                    "next_follow_up_time": details.get("next_follow_up_time"),
                }
            )
        item["waiting"] = waiting

    item["blocked"] = bool(
        row["status"] != "done"
        and (
            waiting_row is not None
            or any(not blocker["resolved"] for blocker in item["blocked_by"])
        )
    )

    now_local = local_now()
    today = now_local.date()
    next_follow_up_on = waiting_row["next_follow_up_on"] if waiting_row else None
    follow_up_deadline = None
    if next_follow_up_on:
        effective_time = waiting_row["next_follow_up_time"] or DEFAULT_FOLLOW_UP_TIME
        follow_up_deadline = datetime.combine(
            date.fromisoformat(next_follow_up_on),
            datetime.strptime(effective_time, "%H:%M").time(),
            tzinfo=current_app.config["TZINFO"],
        )
    item["follow_up_due"] = bool(
        row["status"] != "done"
        and follow_up_deadline
        and follow_up_deadline <= now_local
    )
    item["follow_up_overdue"] = bool(
        item["follow_up_due"] and date.fromisoformat(next_follow_up_on) < today
    )
    yesterday_date = today - timedelta(days=1)
    yesterday = yesterday_date.isoformat()
    item["overdue"] = bool(row["due_date"] and date.fromisoformat(row["due_date"]) < today and row["status"] != "done")
    was_in_progress = any(
        _interval_touches_local_day(started, ended, yesterday_date)
        for started, ended in _status_intervals(db, row, utc_now())
    )
    item["stalled"] = was_in_progress and not bool(
        db.execute(
            """
            SELECT 1
            FROM task_events
            WHERE task_id = ? AND local_date = ? AND counts_as_progress = 1
            LIMIT 1
            """,
            (row["id"], yesterday),
        ).fetchone()
    )
    return item


def _smart_state_order(task: dict[str, Any]) -> int:
    if task["status"] == "done":
        return 3
    if task["blocked"]:
        return 1
    return 0 if task["status"] == "in_progress" else 2


def load_tasks() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    db = get_db()
    rows = db.execute("SELECT * FROM tasks").fetchall()
    items = [_serialize_task(db, row) for row in rows]
    items.sort(
        key=lambda item: (
            0 if item["follow_up_due"] else 1,
            item["waiting"]["next_follow_up_on"] if item["follow_up_due"] else "",
            item["waiting"]["effective_follow_up_time"] if item["follow_up_due"] else "",
            _smart_state_order(item),
            item["due_date"] or "9999-12-31",
            -item["id"],
        )
    )
    choices = [
        {"id": item["id"], "title": item["title"], "status": item["status"]}
        for item in items
    ]
    return items, choices


def _json_body() -> dict[str, Any]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON object")
    return body


def _is_form_request() -> bool:
    return not request.path.startswith("/api/")


def _command_body() -> dict[str, Any]:
    if _is_form_request():
        return {
            key: value
            for key, value in request.form.items()
            if key not in FORM_META_FIELDS
        }
    return _json_body()


def _requested_view(default: str = "manage") -> str:
    view = request.form.get("return_view", default)
    return view if view in TASK_VIEWS else default


def _task_redirect(
    task_id: int,
    notice: str,
    *,
    view: str | None = None,
    expanded: bool | None = None,
    created: bool = False,
):
    destination_view = view or _requested_view()
    if destination_view not in TASK_VIEWS:
        destination_view = "manage"
    if expanded is None:
        expanded = request.form.get("expand") in {"1", "true"}
    values: dict[str, Any] = {"view": destination_view, "notice": notice}
    if expanded:
        values["expanded"] = task_id
    if created:
        values["created"] = task_id
    return redirect(f"{url_for('index', **values)}#task-{task_id}", code=303)


def _query_task_id(name: str) -> int | None:
    value = request.args.get(name)
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _positive_id(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{label} must be a positive integer")
    return parsed


def _approval_dict(approval) -> dict[str, Any]:
    return {
        "id": approval.id,
        "run_id": approval.run_id,
        "tool_call_id": approval.tool_call_id,
        "tool_name": approval.tool_name,
        "arguments": approval.arguments,
        "status": approval.status,
        "created_at": approval.created_at,
        "decided_at": approval.decided_at,
    }


def _outcome_dict(outcome: AgentOutcome) -> dict[str, Any]:
    return {
        "run": asdict(outcome.run),
        "pending_approvals": [
            _approval_dict(approval) for approval in outcome.pending_approvals
        ],
        "latest_content": outcome.latest_content,
    }


def _agent_runner(db: sqlite3.Connection) -> AgentRunner:
    return AgentRunner(
        db,
        current_app.config["TZINFO"],
        environ=current_app.extensions["environment"],
        provider_factory=current_app.extensions["llm_provider_factory"],
        max_steps=current_app.config["AGENT_MAX_STEPS"],
    )


def register_routes(app: Flask) -> None:
    @app.errorhandler(ValueError)
    def invalid_input(exc: ValueError):
        if _is_form_request():
            return redirect(
                url_for(
                    "index",
                    view=_requested_view(),
                    error=str(exc),
                ),
                code=303,
            )
        return jsonify(error=str(exc)), 400

    @app.errorhandler(TaskNotFoundError)
    @app.errorhandler(AgentNotFoundError)
    @app.errorhandler(LlmProfileNotFoundError)
    @app.errorhandler(404)
    def not_found(_exc):
        if request.path.startswith("/api/"):
            return jsonify(error="Not found"), 404
        return "Not found", 404

    @app.errorhandler(413)
    def request_too_large(_exc):
        if _is_form_request():
            return redirect(
                url_for(
                    "index",
                    view="manage",
                    error="Upload is too large; attach fewer or smaller files",
                ),
                code=303,
            )
        return jsonify(error="Upload is too large; attach fewer or smaller files"), 413

    @app.get("/")
    def index():
        maybe_reconcile()
        tasks, choices = load_tasks()
        initial_view = request.args.get("view", "focus")
        if initial_view not in APP_VIEWS:
            initial_view = "focus"
        expanded_task_id = _query_task_id("expanded")
        created_task_id = _query_task_id("created")
        notice_message = NOTICE_MESSAGES.get(request.args.get("notice", ""), "")
        error_message = request.args.get("error", "")[:500]
        focus_task = next(
            (
                task
                for task in sorted(
                    tasks,
                    key=lambda item: (
                        0 if item["follow_up_due"] else 1,
                        (
                            item["waiting"]["next_follow_up_on"]
                            if item["follow_up_due"]
                            else ""
                        ),
                        (
                            item["waiting"]["effective_follow_up_time"]
                            if item["follow_up_due"]
                            else ""
                        ),
                    ),
                )
                if task["follow_up_due"]
                or (
                    task["status"] in {"todo", "in_progress"}
                    and not task["blocked"]
                )
            ),
            None,
        )
        empty_state_emoji, empty_state_emoji_label = random.choice(EMPTY_STATE_HEROES)
        label_choices = sorted(
            {
                label["id"]: label
                for task in tasks
                for label in task["labels"]
            }.values(),
            key=lambda label: (label["type"], label["name"].casefold()),
        )
        manual_label_choices = [
            dict(label)
            for label in get_db().execute(
                """
                SELECT l.id, l.name, l.type, count(tl.id) AS task_count
                FROM labels l
                LEFT JOIN task_labels tl
                  ON tl.label_id = l.id AND tl.removed_at IS NULL
                WHERE l.type = 'manual'
                GROUP BY l.id, l.name, l.type
                ORDER BY l.name COLLATE NOCASE
                """
            ).fetchall()
        ]
        response = make_response(
            render_template(
                "index.html",
                tasks=tasks,
                focus_task_id=focus_task["id"] if focus_task else None,
                empty_state_emoji=empty_state_emoji,
                empty_state_emoji_label=empty_state_emoji_label,
                task_choices=choices,
                label_choices=label_choices,
                manual_label_choices=manual_label_choices,
                status_labels=STATUS_LABELS,
                initial_view=initial_view,
                expanded_task_id=expanded_task_id,
                created_task_id=created_task_id,
                notice_message=notice_message,
                error_message=error_message,
                new_task_request_id=uuid.uuid4().hex,
                create_blocker_request_ids={
                    task["id"]: uuid.uuid4().hex for task in tasks
                },
                timezone_name=current_app.config["USER_TIMEZONE"],
                today=local_today().isoformat(),
                default_follow_up_on=(local_today() + timedelta(days=3)).isoformat(),
                default_follow_up_time=DEFAULT_FOLLOW_UP_TIME,
                max_attachment_bytes=current_app.config["MAX_ATTACHMENT_BYTES"],
                max_attachments_per_task=current_app.config["MAX_ATTACHMENTS_PER_TASK"],
                max_upload_bytes=current_app.config["MAX_CONTENT_LENGTH"],
            )
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/tasks", endpoint="create_task_form")
    @app.post("/api/tasks")
    def create_task():
        is_multipart = request.mimetype == "multipart/form-data"
        body = request.form if is_multipart or _is_form_request() else _json_body()
        uploads = request.files.getlist("attachments") if is_multipart else []
        if "parent_task_id" in body:
            raise ValueError("Subtasks are no longer supported; create a blocking task instead")
        db = get_db()
        stored_paths: list[Path] = []
        try:
            db.execute("BEGIN IMMEDIATE")
            now = utc_now()
            result = _task_service(db).create_task(
                title=body.get("title", ""),
                blocks_task_id=body.get("blocks_task_id"),
                request_id=body.get("request_id"),
                now=now,
            )
            task_id = result.task_id
            if result.created:
                stored_paths = _save_attachments(db, task_id, uploads, now)
            db.commit()
        except Exception:
            db.rollback()
            for stored_path in stored_paths:
                stored_path.unlink(missing_ok=True)
            raise
        if _is_form_request():
            return _task_redirect(
                task_id,
                "task-created",
                view="manage",
                created=True,
            )
        status_code = 201 if result.created else 200
        return jsonify(task=_serialize_task(db, _task_or_404(db, task_id))), status_code

    @app.post(
        "/tasks/<int:task_id>/attachments",
        endpoint="add_attachments_form",
    )
    @app.post("/api/tasks/<int:task_id>/attachments")
    def add_attachments(task_id: int):
        db = get_db()
        _task_or_404(db, task_id)
        now = utc_now()
        stored_paths: list[Path] = []
        try:
            stored_paths = _save_attachments(db, task_id, request.files.getlist("attachments"), now)
            if not stored_paths:
                raise ValueError("Choose at least one file")
            _record_event(
                db,
                task_id,
                "attachments_added",
                details={"count": len(stored_paths)},
                now=now,
            )
            db.commit()
        except Exception:
            db.rollback()
            for stored_path in stored_paths:
                stored_path.unlink(missing_ok=True)
            raise
        task = _serialize_task(db, _task_or_404(db, task_id))
        if _is_form_request():
            return _task_redirect(task_id, "attachments-added", expanded=True)
        return jsonify(attachments=task["attachments"]), 201

    @app.get("/api/attachments/<int:attachment_id>")
    def get_attachment(attachment_id: int):
        attachment = get_db().execute(
            "SELECT * FROM task_attachments WHERE id = ?",
            (attachment_id,),
        ).fetchone()
        if attachment is None:
            from flask import abort

            abort(404)
        path = _attachment_path(attachment["stored_name"])
        if not path.is_file():
            from flask import abort

            abort(404)
        response = send_file(
            path,
            mimetype=attachment["mime_type"],
            as_attachment=request.args.get("download") == "1"
            or attachment["mime_type"] not in PREVIEWABLE_IMAGE_TYPES,
            download_name=attachment["original_name"],
            conditional=True,
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.post(
        "/tasks/<int:task_id>/attachments/<int:attachment_id>/remove",
        endpoint="remove_attachment_form",
    )
    @app.delete("/api/tasks/<int:task_id>/attachments/<int:attachment_id>")
    def remove_attachment(task_id: int, attachment_id: int):
        db = get_db()
        _task_or_404(db, task_id)
        attachment = db.execute(
            "SELECT * FROM task_attachments WHERE id = ? AND task_id = ?",
            (attachment_id, task_id),
        ).fetchone()
        if attachment is None:
            if _is_form_request():
                raise ValueError("Attachment not found")
            return jsonify(error="Attachment not found"), 404
        try:
            _attachment_path(attachment["stored_name"]).unlink(missing_ok=True)
        except PermissionError as exc:
            raise ValueError("Attachment is still in use; close its preview and try again") from exc
        db.execute("DELETE FROM task_attachments WHERE id = ?", (attachment_id,))
        _record_event(
            db,
            task_id,
            "attachment_removed",
            details={"attachment_id": attachment_id},
        )
        db.commit()
        if _is_form_request():
            return _task_redirect(task_id, "attachment-removed", expanded=True)
        return "", 204

    @app.post("/tasks/<int:task_id>", endpoint="update_task_form")
    @app.patch("/api/tasks/<int:task_id>")
    def update_task(task_id: int):
        body = _command_body()
        db = get_db()
        try:
            result = _task_service(db).update_task(task_id, body)
            db.commit()
        except Exception:
            db.rollback()
            raise
        if _is_form_request():
            notice = (
                "task-completed"
                if result.values.get("status") == "done"
                else "task-updated"
            )
            return _task_redirect(task_id, notice)
        return jsonify(task=_serialize_task(db, _task_or_404(db, task_id)))

    @app.patch("/api/tasks/<int:task_id>/rank")
    def rank_task(task_id: int):
        body = _json_body()
        unknown = set(body) - {"after_task_id", "before_task_id"}
        if unknown:
            raise ValueError(f"Unsupported field: {sorted(unknown)[0]}")
        if "after_task_id" not in body or "before_task_id" not in body:
            raise ValueError("Both neighboring task fields are required")

        def neighbor_id(field: str) -> int | None:
            value = body[field]
            if value is None:
                return None
            if isinstance(value, bool):
                raise ValueError(f"Invalid {field}")
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid {field}") from exc
            if parsed <= 0 or parsed == task_id:
                raise ValueError(f"Invalid {field}")
            return parsed

        after_id = neighbor_id("after_task_id")
        before_id = neighbor_id("before_task_id")
        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            _task_or_404(db, task_id)
            ordered_rows = db.execute(
                "SELECT id, rank_key FROM tasks ORDER BY rank_key, id"
            ).fetchall()
            remaining = [row for row in ordered_rows if row["id"] != task_id]
            positions = {row["id"]: index for index, row in enumerate(remaining)}

            if after_id is not None and after_id not in positions:
                raise ValueError("The preceding task no longer exists")
            if before_id is not None and before_id not in positions:
                raise ValueError("The following task no longer exists")

            if after_id is None and before_id is None:
                if remaining:
                    raise ValueError("Choose where to rank the task")
                insertion_index = 0
            elif after_id is None:
                insertion_index = positions[before_id]
                if insertion_index != 0:
                    raise ValueError("Task order changed; try again")
            elif before_id is None:
                insertion_index = positions[after_id] + 1
                if insertion_index != len(remaining):
                    raise ValueError("Task order changed; try again")
            else:
                insertion_index = positions[before_id]
                if positions[after_id] + 1 != insertion_index:
                    raise ValueError("Task order changed; try again")

            new_order = [row["id"] for row in remaining]
            new_order.insert(insertion_index, task_id)
            after_rank = remaining[insertion_index - 1]["rank_key"] if insertion_index else None
            before_rank = remaining[insertion_index]["rank_key"] if insertion_index < len(remaining) else None

            if after_rank is None and before_rank is None:
                candidate = RANK_SPACING
            elif after_rank is None:
                candidate = before_rank - RANK_SPACING
            elif before_rank is None:
                candidate = after_rank + RANK_SPACING
            else:
                candidate = (after_rank + before_rank) // 2

            rebalanced = not (
                SQLITE_INTEGER_MIN <= candidate <= SQLITE_INTEGER_MAX
                and (after_rank is None or candidate > after_rank)
                and (before_rank is None or candidate < before_rank)
            )
            if rebalanced:
                rebalance_task_ranks(db, new_order)
            else:
                db.execute(
                    "UPDATE tasks SET rank_key = ? WHERE id = ?",
                    (candidate, task_id),
                )
            db.commit()
        except Exception:
            db.rollback()
            raise

        ranks = [
            {"id": row["id"], "rank_key": str(row["rank_key"])}
            for row in db.execute(
                "SELECT id, rank_key FROM tasks ORDER BY rank_key, id"
            ).fetchall()
        ]
        return jsonify(ranks=ranks, rebalanced=rebalanced)

    @app.post(
        "/tasks/<int:task_id>/comments",
        endpoint="add_comment_form",
    )
    @app.post("/api/tasks/<int:task_id>/comments")
    def add_comment(task_id: int):
        body = _command_body()
        db = get_db()
        try:
            comment_id = _task_service(db).add_comment(
                task_id,
                body.get("body", ""),
                counts_as_progress=bool(body.get("counts_as_progress", False)),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        if _is_form_request():
            return _task_redirect(task_id, "comment-added", expanded=True)
        return jsonify(comment_id=comment_id), 201

    @app.post(
        "/tasks/<int:task_id>/waiting",
        endpoint="set_waiting_form",
    )
    @app.put("/api/tasks/<int:task_id>/waiting")
    def set_waiting(task_id: int):
        body = _command_body()
        db = get_db()
        try:
            result = _task_service(db).set_waiting(task_id, body)
            db.commit()
        except Exception:
            db.rollback()
            raise

        if _is_form_request():
            return _task_redirect(task_id, "waiting-saved", expanded=True)
        response = jsonify(task=_serialize_task(db, _task_or_404(db, task_id)))
        return (response, 201) if result.created else response

    @app.post(
        "/tasks/<int:task_id>/follow-ups",
        endpoint="record_follow_up_form",
    )
    @app.post("/api/tasks/<int:task_id>/follow-ups")
    def record_follow_up(task_id: int):
        body = _command_body()
        db = get_db()
        try:
            _task_service(db).record_follow_up(task_id, body)
            db.commit()
        except Exception:
            db.rollback()
            raise
        if _is_form_request():
            return _task_redirect(task_id, "follow-up-recorded", expanded=True)
        return jsonify(task=_serialize_task(db, _task_or_404(db, task_id))), 201

    @app.post(
        "/tasks/<int:task_id>/waiting/resolve",
        endpoint="resolve_waiting_form",
    )
    @app.post("/api/tasks/<int:task_id>/waiting/resolve")
    def resolve_waiting(task_id: int):
        body = _command_body()
        unknown = set(body)
        if unknown:
            raise ValueError(f"Unsupported field: {sorted(unknown)[0]}")

        db = get_db()
        try:
            _task_service(db).resolve_waiting(task_id)
            db.commit()
        except Exception:
            db.rollback()
            raise
        if _is_form_request():
            return _task_redirect(task_id, "waiting-resolved")
        return jsonify(task=_serialize_task(db, _task_or_404(db, task_id)))

    @app.post(
        "/tasks/<int:task_id>/labels",
        endpoint="add_label_form",
    )
    @app.post("/api/tasks/<int:task_id>/labels")
    def add_label(task_id: int):
        body = _command_body()
        db = get_db()
        try:
            _task_service(db).add_label(task_id, body.get("name", ""))
            db.commit()
        except Exception:
            db.rollback()
            raise
        if _is_form_request():
            return _task_redirect(task_id, "label-added", expanded=True)
        return jsonify(task=_serialize_task(db, _task_or_404(db, task_id))), 201

    @app.post(
        "/tasks/<int:task_id>/labels/<int:label_id>/remove",
        endpoint="remove_label_form",
    )
    @app.delete("/api/tasks/<int:task_id>/labels/<int:label_id>")
    def remove_label(task_id: int, label_id: int):
        db = get_db()
        try:
            removed = _task_service(db).remove_label(task_id, label_id)
            if removed:
                db.commit()
            else:
                db.rollback()
        except Exception:
            db.rollback()
            raise
        if not removed:
            if _is_form_request():
                raise ValueError("Active label not found")
            return jsonify(error="Active label not found"), 404
        if _is_form_request():
            return _task_redirect(task_id, "label-removed", expanded=True)
        return "", 204

    @app.post("/labels/<int:label_id>/delete", endpoint="delete_label_form")
    @app.delete("/api/labels/<int:label_id>")
    def delete_label(label_id: int):
        db = get_db()
        try:
            deleted = _task_service(db).delete_unused_label(label_id)
            if not deleted:
                abort(404)
            db.commit()
        except Exception:
            db.rollback()
            raise
        if _is_form_request():
            return redirect(
                url_for(
                    "index",
                    view=_requested_view(),
                    notice="label-deleted",
                ),
                code=303,
            )
        return "", 204

    @app.post(
        "/tasks/<int:task_id>/dependencies",
        endpoint="add_dependency_form",
    )
    @app.post("/api/tasks/<int:task_id>/dependencies")
    def add_dependency(task_id: int):
        body = _command_body()
        try:
            blocker_id = int(body.get("blocker_task_id"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Choose a blocker task") from exc
        db = get_db()
        try:
            _task_service(db).add_dependency(task_id, blocker_id)
            db.commit()
        except Exception:
            db.rollback()
            raise
        if _is_form_request():
            return _task_redirect(task_id, "dependency-added", expanded=True)
        return jsonify(task=_serialize_task(db, _task_or_404(db, task_id))), 201

    @app.post(
        "/tasks/<int:blocker_id>/blocked-tasks",
        endpoint="add_blocked_task_form",
    )
    def add_blocked_task(blocker_id: int):
        try:
            blocked_id = int(request.form.get("blocked_task_id"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Choose a blocked task") from exc
        db = get_db()
        try:
            _task_service(db).add_dependency(blocked_id, blocker_id)
            db.commit()
        except Exception:
            db.rollback()
            raise
        return _task_redirect(blocker_id, "dependency-added", expanded=True)

    @app.post(
        "/tasks/<int:task_id>/dependencies/<int:blocker_id>/remove",
        endpoint="remove_dependency_form",
    )
    @app.delete("/api/tasks/<int:task_id>/dependencies/<int:blocker_id>")
    def remove_dependency(task_id: int, blocker_id: int):
        db = get_db()
        try:
            removed = _task_service(db).remove_dependency(task_id, blocker_id)
            if removed:
                db.commit()
            else:
                db.rollback()
        except Exception:
            db.rollback()
            raise
        if not removed:
            if _is_form_request():
                raise ValueError("Dependency not found")
            return jsonify(error="Dependency not found"), 404
        if _is_form_request():
            return _task_redirect(task_id, "dependency-removed", expanded=True)
        return "", 204

    @app.post("/api/reconcile")
    def reconcile():
        changed = reconcile_active_labels()
        current_app.extensions["last_reconcile_monotonic"] = time.monotonic()
        return jsonify(changes=changed)

    @app.get("/api/llm-profiles")
    def list_llm_profiles():
        profiles = _llm_profile_store(get_db()).list()
        return jsonify(profiles=[_public_llm_profile(profile) for profile in profiles])

    @app.post("/api/llm-profiles")
    def create_llm_profile():
        body = _json_body()
        make_default = body.pop("is_default", False)
        if not isinstance(make_default, bool):
            raise ValueError("is_default must be a boolean")
        db = get_db()
        try:
            profile = _llm_profile_store(db).create(
                body, make_default=make_default
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        return jsonify(profile=_public_llm_profile(profile)), 201

    @app.patch("/api/llm-profiles/<int:profile_id>")
    def update_llm_profile(profile_id: int):
        body = _json_body()
        make_default = body.pop("is_default", None)
        if make_default is not None and not isinstance(make_default, bool):
            raise ValueError("is_default must be a boolean")
        db = get_db()
        try:
            profile = _llm_profile_store(db).update(
                profile_id, body, make_default=make_default
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        return jsonify(profile=_public_llm_profile(profile))

    @app.get("/api/llm-profiles/<int:profile_id>/models")
    def list_llm_models(profile_id: int):
        store = _llm_profile_store(get_db())
        config = store.provider_config(profile_id)
        provider = current_app.extensions["llm_provider_factory"](config)
        try:
            models = provider.list_models()
        except Exception as exc:
            message = str(exc).replace(config.api_key or "\0", "***")[:500]
            return jsonify(error=message or "Could not list models"), 502
        return jsonify(models=models)

    @app.get("/api/agent/sessions")
    def list_agent_sessions():
        sessions = AgentStore(get_db()).list_sessions()
        return jsonify(sessions=[asdict(session) for session in sessions])

    @app.get("/api/agent/folders")
    def list_agent_folders():
        folders = AgentStore(get_db()).list_folders()
        return jsonify(folders=[asdict(folder) for folder in folders])

    @app.post("/api/agent/folders")
    def create_agent_folder():
        body = _json_body()
        unknown = set(body) - {"name"}
        if unknown:
            raise ValueError(f"Unsupported field: {sorted(unknown)[0]}")
        db = get_db()
        try:
            folder = AgentStore(db).create_folder(body.get("name"))
            db.commit()
        except Exception:
            db.rollback()
            raise
        return jsonify(folder=asdict(folder)), 201

    @app.delete("/api/agent/folders/<int:folder_id>")
    def delete_agent_folder(folder_id: int):
        db = get_db()
        try:
            AgentStore(db).delete_folder(folder_id)
            db.commit()
        except Exception:
            db.rollback()
            raise
        return "", 204

    @app.post("/api/agent/sessions")
    def create_agent_session():
        body = _json_body()
        unknown = set(body) - {"profile_id", "title"}
        if unknown:
            raise ValueError(f"Unsupported field: {sorted(unknown)[0]}")
        db = get_db()
        profile_store = _llm_profile_store(db)
        profile_id = (
            profile_store.get().id
            if body.get("profile_id") is None
            else _positive_id(body["profile_id"], "profile_id")
        )
        try:
            session = AgentStore(db).create_session(
                profile_id, title=body.get("title", "New conversation")
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        return jsonify(session=asdict(session)), 201

    @app.get("/api/agent/sessions/<session_id>")
    def get_agent_session(session_id: str):
        store = AgentStore(get_db())
        session = store.session(session_id)
        return jsonify(
            session=asdict(session),
            messages=store.message_records(session_id),
            runs=[asdict(run) for run in store.runs_for_session(session_id)],
        )

    @app.patch("/api/agent/sessions/<session_id>")
    def update_agent_session(session_id: str):
        body = _json_body()
        unknown = set(body) - {"folder_id"}
        if unknown:
            raise ValueError(f"Unsupported field: {sorted(unknown)[0]}")
        folder_id = body.get("folder_id")
        if folder_id is not None:
            folder_id = _positive_id(folder_id, "folder_id")
        db = get_db()
        store = AgentStore(db)
        try:
            session = store.move_session(session_id, folder_id)
            db.commit()
        except Exception:
            db.rollback()
            raise
        return jsonify(session=asdict(session))

    @app.post("/api/agent/sessions/<session_id>/messages")
    def send_agent_message(session_id: str):
        body = _json_body()
        unknown = set(body) - {"content"}
        if unknown:
            raise ValueError(f"Unsupported field: {sorted(unknown)[0]}")
        outcome = _agent_runner(get_db()).start(session_id, body.get("content"))
        status = 202 if outcome.run.status == "waiting_approval" else 200
        response = _outcome_dict(outcome)
        if outcome.run.status == "failed":
            status = 502
            response["error"] = outcome.run.error
        return jsonify(**response), status

    @app.post("/api/agent/approvals/<approval_id>")
    def decide_agent_approval(approval_id: str):
        body = _json_body()
        unknown = set(body) - {"approved"}
        if unknown:
            raise ValueError(f"Unsupported field: {sorted(unknown)[0]}")
        outcome = _agent_runner(get_db()).decide(
            approval_id, body.get("approved")
        )
        status = 202 if outcome.run.status == "waiting_approval" else 200
        response = _outcome_dict(outcome)
        if outcome.run.status == "failed":
            status = 502
            response["error"] = outcome.run.error
        return jsonify(**response), status

    @app.post("/api/agent/runs/<run_id>/resume")
    def resume_agent_run(run_id: str):
        body = _json_body()
        if body:
            raise ValueError(f"Unsupported field: {sorted(body)[0]}")
        outcome = _agent_runner(get_db()).resume(run_id)
        status = 202 if outcome.run.status == "waiting_approval" else 200
        response = _outcome_dict(outcome)
        if outcome.run.status == "failed":
            status = 502
            response["error"] = outcome.run.error
        return jsonify(**response), status

    @app.get("/api/agent/runs/<run_id>")
    def get_agent_run(run_id: str):
        store = AgentStore(get_db())
        run = store.run(run_id)
        return jsonify(
            run=asdict(run),
            events=store.events(run_id),
            approvals=[
                _approval_dict(approval)
                for approval in store.approvals_for_run(run_id)
            ],
        )
