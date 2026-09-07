from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, current_app, g, jsonify, render_template, request


STATUSES = {"todo", "in_progress", "blocked", "done"}
MAX_RECONCILE_AGE_SECONDS = 60


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        DATABASE=os.getenv("AUR_BATAAO_DATABASE", str(Path(app.instance_path) / "tasks.sqlite3")),
        USER_TIMEZONE=os.getenv("AUR_BATAAO_TIMEZONE", "Asia/Kolkata"),
    )
    if test_config:
        app.config.update(test_config)

    try:
        app.config["TZINFO"] = ZoneInfo(app.config["USER_TIMEZONE"])
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"Unknown timezone: {app.config['USER_TIMEZONE']}") from exc

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    app.extensions["reconcile_lock"] = threading.Lock()
    app.extensions["last_reconcile_monotonic"] = 0.0

    app.teardown_appcontext(close_db)
    register_routes(app)

    with app.app_context():
        init_db()
        reconcile_active_labels()
        app.extensions["last_reconcile_monotonic"] = time.monotonic()

    return app


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
    get_db().executescript(schema)
    get_db().commit()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def local_date_for(value: datetime) -> str:
    return value.astimezone(current_app.config["TZINFO"]).date().isoformat()


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _attach_active_label(
    db: sqlite3.Connection,
    task_id: int,
    local_day: str,
    now: datetime,
    *,
    allow_readd: bool = True,
) -> None:
    name = f"active:{local_day}"
    db.execute("INSERT OR IGNORE INTO labels(name, type) VALUES (?, 'active_date')", (name,))
    label_id = db.execute("SELECT id FROM labels WHERE name = ?", (name,)).fetchone()["id"]
    if not allow_readd and db.execute(
        "SELECT 1 FROM task_labels WHERE task_id = ? AND label_id = ? LIMIT 1",
        (task_id, label_id),
    ).fetchone():
        return
    db.execute(
        """
        INSERT OR IGNORE INTO task_labels(task_id, label_id, source, added_at)
        VALUES (?, ?, 'automatic', ?)
        """,
        (task_id, label_id, iso_utc(now)),
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
    now = now or utc_now()
    local_day = local_date_for(now)
    db.execute(
        """
        INSERT INTO task_events(
            task_id, event_type, counts_as_progress, occurred_at_utc, local_date, details_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (task_id, event_type, int(counts_as_progress), iso_utc(now), local_day, json.dumps(details or {})),
    )
    if counts_as_progress:
        db.execute(
            "UPDATE tasks SET last_progress_at = ?, updated_at = ? WHERE id = ?",
            (iso_utc(now), iso_utc(now), task_id),
        )
        if active_for_label is None:
            row = db.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            active_for_label = bool(row and row["status"] == "in_progress")
        if active_for_label:
            _attach_active_label(db, task_id, local_day, now)


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


def _serialize_task(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
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
    today = datetime.now(current_app.config["TZINFO"]).date()
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


def load_tasks() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    db = get_db()
    rows = db.execute(
        """
        SELECT * FROM tasks
        ORDER BY CASE status WHEN 'in_progress' THEN 0 WHEN 'blocked' THEN 1 WHEN 'todo' THEN 2 ELSE 3 END,
                 COALESCE(due_date, '9999-12-31'), id DESC
        """
    ).fetchall()
    items = [_serialize_task(db, row) for row in rows]
    by_parent: dict[int | None, list[dict[str, Any]]] = {}
    for item in items:
        by_parent.setdefault(item["parent_task_id"], []).append(item)

    ordered: list[dict[str, Any]] = []

    def visit(parent_id: int | None, depth: int) -> None:
        for item in by_parent.get(parent_id, []):
            item["depth"] = depth
            ordered.append(item)
            visit(item["id"], depth + 1)

    visit(None, 0)
    # Keep malformed/orphaned rows visible if foreign-key checks were ever bypassed.
    seen = {item["id"] for item in ordered}
    for item in items:
        if item["id"] not in seen:
            item["depth"] = 0
            ordered.append(item)
    choices = [{"id": item["id"], "title": item["title"]} for item in items]
    return ordered, choices


def _json_body() -> dict[str, Any]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON object")
    return body


def _valid_due_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("Due date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("Due date must be YYYY-MM-DD") from exc


def _would_create_dependency_cycle(db: sqlite3.Connection, blocked_id: int, blocker_id: int) -> bool:
    row = db.execute(
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


def register_routes(app: Flask) -> None:
    @app.errorhandler(ValueError)
    def invalid_input(exc: ValueError):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(404)
    def not_found(_exc):
        if request.path.startswith("/api/"):
            return jsonify(error="Not found"), 404
        return "Not found", 404

    @app.get("/")
    def index():
        maybe_reconcile()
        tasks, choices = load_tasks()
        return render_template(
            "index.html",
            tasks=tasks,
            task_choices=choices,
            statuses=("todo", "in_progress", "blocked", "done"),
            timezone_name=current_app.config["USER_TIMEZONE"],
            today=datetime.now(current_app.config["TZINFO"]).date().isoformat(),
        )

    @app.post("/api/tasks")
    def create_task():
        body = _json_body()
        title = str(body.get("title", "")).strip()
        if not title or len(title) > 200:
            raise ValueError("Title must be between 1 and 200 characters")
        parent_id = body.get("parent_task_id")
        if parent_id is not None:
            try:
                parent_id = int(parent_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid parent task") from exc
        db = get_db()
        if parent_id is not None:
            _task_or_404(db, parent_id)
        now = utc_now()
        cursor = db.execute(
            """
            INSERT INTO tasks(parent_task_id, title, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (parent_id, title, iso_utc(now), iso_utc(now)),
        )
        task_id = cursor.lastrowid
        _record_event(db, task_id, "task_created", details={"status": "todo"}, now=now)
        db.commit()
        return jsonify(task=_serialize_task(db, _task_or_404(db, task_id))), 201

    @app.patch("/api/tasks/<int:task_id>")
    def update_task(task_id: int):
        body = _json_body()
        allowed = {"title", "description", "status", "due_date"}
        unknown = set(body) - allowed
        if unknown:
            raise ValueError(f"Unsupported field: {sorted(unknown)[0]}")
        db = get_db()
        task = _task_or_404(db, task_id)
        changes: dict[str, Any] = {}
        if "title" in body:
            title = str(body["title"]).strip()
            if not title or len(title) > 200:
                raise ValueError("Title must be between 1 and 200 characters")
            changes["title"] = title
        if "description" in body:
            description = str(body["description"]).strip()
            if len(description) > 5000:
                raise ValueError("Description must be at most 5000 characters")
            changes["description"] = description
        if "status" in body:
            status = str(body["status"])
            if status not in STATUSES:
                raise ValueError("Invalid status")
            changes["status"] = status
        if "due_date" in body:
            changes["due_date"] = _valid_due_date(body["due_date"])

        now = utc_now()
        changed_fields = {key: value for key, value in changes.items() if value != task[key]}
        if changed_fields:
            assignments = ", ".join(f"{key} = ?" for key in changed_fields)
            db.execute(
                f"UPDATE tasks SET {assignments}, updated_at = ? WHERE id = ?",
                (*changed_fields.values(), iso_utc(now), task_id),
            )
            if "status" in changed_fields:
                old_status = task["status"]
                new_status = changed_fields["status"]
                counts = old_status == "in_progress" or new_status == "in_progress"
                _record_event(
                    db,
                    task_id,
                    "status_changed",
                    counts_as_progress=counts,
                    active_for_label=counts,
                    details={"old_status": old_status, "new_status": new_status},
                    now=now,
                )
                if new_status == "done" and old_status != "done":
                    dependents = db.execute(
                        "SELECT blocked_task_id FROM task_dependencies WHERE blocker_task_id = ?",
                        (task_id,),
                    ).fetchall()
                    for dependent in dependents:
                        _record_event(
                            db,
                            dependent["blocked_task_id"],
                            "dependency_resolved",
                            counts_as_progress=True,
                            details={"blocker_task_id": task_id},
                            now=now,
                        )
                elif old_status == "done" and new_status != "done":
                    dependents = db.execute(
                        "SELECT blocked_task_id FROM task_dependencies WHERE blocker_task_id = ?",
                        (task_id,),
                    ).fetchall()
                    for dependent in dependents:
                        _record_event(
                            db,
                            dependent["blocked_task_id"],
                            "dependency_reopened",
                            details={"blocker_task_id": task_id},
                            now=now,
                        )
            non_status = [key for key in changed_fields if key != "status"]
            if non_status:
                _record_event(db, task_id, "task_updated", details={"fields": non_status}, now=now)
            db.commit()
        return jsonify(task=_serialize_task(db, _task_or_404(db, task_id)))

    @app.post("/api/tasks/<int:task_id>/comments")
    def add_comment(task_id: int):
        body = _json_body()
        comment = str(body.get("body", "")).strip()
        if not comment or len(comment) > 2000:
            raise ValueError("Comment must be between 1 and 2000 characters")
        counts = bool(body.get("counts_as_progress", False))
        db = get_db()
        _task_or_404(db, task_id)
        now = utc_now()
        cursor = db.execute(
            "INSERT INTO comments(task_id, body, created_at) VALUES (?, ?, ?)",
            (task_id, comment, iso_utc(now)),
        )
        _record_event(
            db,
            task_id,
            "comment_added",
            counts_as_progress=counts,
            details={"comment_id": cursor.lastrowid},
            now=now,
        )
        db.commit()
        return jsonify(comment_id=cursor.lastrowid), 201

    @app.post("/api/tasks/<int:task_id>/labels")
    def add_label(task_id: int):
        body = _json_body()
        name = str(body.get("name", "")).strip().lower()
        if not name or len(name) > 32:
            raise ValueError("Label must be between 1 and 32 characters")
        if name.startswith("active:"):
            raise ValueError("The active: prefix is reserved")
        db = get_db()
        _task_or_404(db, task_id)
        now = utc_now()
        db.execute("INSERT OR IGNORE INTO labels(name, type) VALUES (?, 'manual')", (name,))
        label = db.execute("SELECT id, type FROM labels WHERE name = ?", (name,)).fetchone()
        if label["type"] != "manual":
            raise ValueError("That label name is reserved")
        db.execute(
            """
            INSERT OR IGNORE INTO task_labels(task_id, label_id, source, added_at)
            VALUES (?, ?, 'user', ?)
            """,
            (task_id, label["id"], iso_utc(now)),
        )
        _record_event(db, task_id, "label_added", details={"label_id": label["id"]}, now=now)
        db.commit()
        return jsonify(task=_serialize_task(db, _task_or_404(db, task_id))), 201

    @app.delete("/api/tasks/<int:task_id>/labels/<int:label_id>")
    def remove_label(task_id: int, label_id: int):
        db = get_db()
        _task_or_404(db, task_id)
        now = utc_now()
        cursor = db.execute(
            """
            UPDATE task_labels SET removed_at = ?
            WHERE task_id = ? AND label_id = ? AND removed_at IS NULL
            """,
            (iso_utc(now), task_id, label_id),
        )
        if cursor.rowcount == 0:
            return jsonify(error="Active label not found"), 404
        _record_event(db, task_id, "label_removed", details={"label_id": label_id}, now=now)
        db.commit()
        return "", 204

    @app.post("/api/tasks/<int:task_id>/dependencies")
    def add_dependency(task_id: int):
        body = _json_body()
        try:
            blocker_id = int(body.get("blocker_task_id"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Choose a blocker task") from exc
        if blocker_id == task_id:
            raise ValueError("A task cannot block itself")
        db = get_db()
        _task_or_404(db, task_id)
        _task_or_404(db, blocker_id)
        if _would_create_dependency_cycle(db, task_id, blocker_id):
            raise ValueError("That dependency would create a cycle")
        now = utc_now()
        cursor = db.execute(
            "INSERT OR IGNORE INTO task_dependencies(blocked_task_id, blocker_task_id) VALUES (?, ?)",
            (task_id, blocker_id),
        )
        if cursor.rowcount:
            _record_event(db, task_id, "dependency_added", details={"blocker_task_id": blocker_id}, now=now)
        db.commit()
        return jsonify(task=_serialize_task(db, _task_or_404(db, task_id))), 201

    @app.delete("/api/tasks/<int:task_id>/dependencies/<int:blocker_id>")
    def remove_dependency(task_id: int, blocker_id: int):
        db = get_db()
        _task_or_404(db, task_id)
        blocker = _task_or_404(db, blocker_id)
        cursor = db.execute(
            "DELETE FROM task_dependencies WHERE blocked_task_id = ? AND blocker_task_id = ?",
            (task_id, blocker_id),
        )
        if cursor.rowcount == 0:
            return jsonify(error="Dependency not found"), 404
        _record_event(
            db,
            task_id,
            "dependency_removed",
            counts_as_progress=blocker["status"] != "done",
            details={"blocker_task_id": blocker_id},
        )
        db.commit()
        return "", 204

    @app.post("/api/reconcile")
    def reconcile():
        changed = reconcile_active_labels()
        current_app.extensions["last_reconcile_monotonic"] = time.monotonic()
        return jsonify(changes=changed)


def main() -> None:
    from waitress import serve

    host = os.getenv("AUR_BATAAO_HOST", "127.0.0.1")
    port = int(os.getenv("AUR_BATAAO_PORT", "8080"))
    serve(create_app(), host=host, port=port, threads=4)


if __name__ == "__main__":
    main()
