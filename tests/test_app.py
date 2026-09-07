import json
from datetime import datetime, timezone

import pytest

from app import create_app, get_db, reconcile_active_labels


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "test.sqlite3"),
            "USER_TIMEZONE": "Asia/Kolkata",
        }
    )


@pytest.fixture()
def client(app):
    return app.test_client()


def create_task(client, title, parent_task_id=None):
    payload = {"title": title}
    if parent_task_id is not None:
        payload["parent_task_id"] = parent_task_id
    response = client.post("/api/tasks", json=payload)
    assert response.status_code == 201
    return response.get_json()["task"]


def test_task_subtask_and_progress_label(client):
    parent = create_task(client, "Ship compact MVP")
    child = create_task(client, "Write tests", parent["id"])

    response = client.patch(f"/api/tasks/{child['id']}", json={"status": "in_progress"})
    assert response.status_code == 200
    task = response.get_json()["task"]
    assert task["status"] == "in_progress"
    assert any(label["name"].startswith("active:") for label in task["labels"])
    assert task["last_progress_at"] is not None


def test_comments_are_only_progress_when_requested(client, app):
    task = create_task(client, "Investigate")
    client.post(f"/api/tasks/{task['id']}/comments", json={"body": "A note"})
    client.post(
        f"/api/tasks/{task['id']}/comments",
        json={"body": "A real step", "counts_as_progress": True},
    )

    with app.app_context():
        events = get_db().execute(
            "SELECT counts_as_progress FROM task_events WHERE task_id = ? AND event_type = 'comment_added' ORDER BY id",
            (task["id"],),
        ).fetchall()
        assert [row["counts_as_progress"] for row in events] == [0, 1]


def test_dependency_cycles_are_rejected(client):
    first = create_task(client, "First")
    second = create_task(client, "Second")
    third = create_task(client, "Third")
    assert client.post(f"/api/tasks/{first['id']}/dependencies", json={"blocker_task_id": second["id"]}).status_code == 201
    assert client.post(f"/api/tasks/{second['id']}/dependencies", json={"blocker_task_id": third["id"]}).status_code == 201

    response = client.post(f"/api/tasks/{third['id']}/dependencies", json={"blocker_task_id": first["id"]})
    assert response.status_code == 400
    assert "cycle" in response.get_json()["error"]


def test_completing_blocker_records_progress_for_dependent(client, app):
    blocked = create_task(client, "Blocked work")
    blocker = create_task(client, "Prerequisite")
    client.post(f"/api/tasks/{blocked['id']}/dependencies", json={"blocker_task_id": blocker["id"]})
    client.patch(f"/api/tasks/{blocker['id']}", json={"status": "done"})

    with app.app_context():
        event = get_db().execute(
            "SELECT * FROM task_events WHERE task_id = ? AND event_type = 'dependency_resolved'",
            (blocked["id"],),
        ).fetchone()
        assert event is not None
        assert event["counts_as_progress"] == 1


def test_reconciliation_backfills_each_in_progress_day(client, app):
    task = create_task(client, "Long-running task")
    client.patch(f"/api/tasks/{task['id']}", json={"status": "in_progress"})

    with app.app_context():
        db = get_db()
        event = db.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND event_type = 'status_changed'",
            (task["id"],),
        ).fetchone()
        db.execute(
            "UPDATE task_events SET occurred_at_utc = ?, local_date = ? WHERE id = ?",
            ("2026-09-05T18:00:00+00:00", "2026-09-05", event["id"]),
        )
        db.commit()
        reconcile_active_labels(datetime(2026, 9, 7, 6, 0, tzinfo=timezone.utc))
        labels = db.execute(
            """
            SELECT l.name FROM task_labels tl JOIN labels l ON l.id = tl.label_id
            WHERE tl.task_id = ? AND tl.removed_at IS NULL AND l.type = 'active_date'
            """,
            (task["id"],),
        ).fetchall()
        names = {row["name"] for row in labels}
        assert {"active:2026-09-05", "active:2026-09-06", "active:2026-09-07"} <= names


def test_overdue_is_derived(client):
    task = create_task(client, "Late task")
    response = client.patch(f"/api/tasks/{task['id']}", json={"due_date": "2000-01-01"})
    assert response.get_json()["task"]["overdue"] is True


def test_removed_automatic_label_waits_for_later_progress(client, app):
    task = create_task(client, "Respect label override")
    response = client.patch(f"/api/tasks/{task['id']}", json={"status": "in_progress"})
    active_label = next(label for label in response.get_json()["task"]["labels"] if label["type"] == "active_date")
    assert client.delete(f"/api/tasks/{task['id']}/labels/{active_label['id']}").status_code == 204

    with app.app_context():
        reconcile_active_labels()
        active_count = get_db().execute(
            """
            SELECT count(*) AS count
            FROM task_labels tl JOIN labels l ON l.id = tl.label_id
            WHERE tl.task_id = ? AND tl.removed_at IS NULL AND l.type = 'active_date'
            """,
            (task["id"],),
        ).fetchone()["count"]
        assert active_count == 0

    response = client.post(f"/api/tasks/{task['id']}/progress", json={})
    assert any(label["type"] == "active_date" for label in response.get_json()["task"]["labels"])


def test_index_renders_compact_task_ui(client):
    create_task(client, "Visible task")
    response = client.get("/")
    assert response.status_code == 200
    assert b"Visible task" in response.data
    assert b"task-list" in response.data
