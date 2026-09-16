import sqlite3
import re
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pytest
import start
import app as app_module

from app import (
    DEFAULT_FOLLOW_UP_TIME,
    EMPTY_STATE_HEROES,
    RANK_SPACING,
    create_app,
    get_db,
    init_db,
    reconcile_active_labels,
)


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


def create_task(client, title):
    response = client.post("/api/tasks", json={"title": title})
    assert response.status_code == 201
    return response.get_json()["task"]


def test_task_progress_label(client):
    task = create_task(client, "Write tests")

    response = client.patch(f"/api/tasks/{task['id']}", json={"status": "in_progress"})
    assert response.status_code == 200
    task = response.get_json()["task"]
    assert task["status"] == "in_progress"
    assert any(label["name"].startswith("active:") for label in task["labels"])
    assert task["last_progress_at"] is not None


def test_create_blocker_creates_task_and_relationship_atomically(client, app):
    blocked = create_task(client, "Ship compact MVP")

    response = client.post(
        "/api/tasks",
        json={"title": "Write tests", "blocks_task_id": blocked["id"]},
    )

    assert response.status_code == 201
    blocker = response.get_json()["task"]
    assert blocker["parent_task_id"] is None
    assert [task["id"] for task in blocker["blocks"]] == [blocked["id"]]

    with app.app_context():
        relationship = get_db().execute(
            "SELECT blocked_task_id, blocker_task_id FROM task_dependencies"
        ).fetchone()
        assert (relationship["blocked_task_id"], relationship["blocker_task_id"]) == (
            blocked["id"],
            blocker["id"],
        )


def test_browser_task_creation_redirects_to_visible_authoritative_row(client):
    response = client.post(
        "/tasks",
        data={
            "title": "Visible after redirect",
            "request_id": "11111111-1111-4111-8111-111111111111",
        },
    )

    assert response.status_code == 303
    assert response.headers["Location"].endswith(
        "/?view=manage&notice=task-created&created=1#task-1"
    )

    page = client.get(response.headers["Location"])
    markup = page.get_data(as_text=True)
    assert page.headers["Cache-Control"] == "no-store"
    assert '<body class="manage-mode has-actionable"' in markup
    assert 'id="task-1"' in markup
    assert "Visible after redirect" in markup
    assert "is-newly-created" in markup
    assert 'data-notice="Task added"' in markup


def test_browser_task_creation_retry_is_idempotent(client, app):
    submission = {
        "title": "Create exactly once",
        "request_id": "22222222-2222-4222-8222-222222222222",
    }

    first = client.post("/tasks", data=submission)
    retry = client.post("/tasks", data=submission)

    assert first.status_code == retry.status_code == 303
    assert first.headers["Location"] == retry.headers["Location"]
    with app.app_context():
        rows = get_db().execute(
            "SELECT id, title, create_request_id FROM tasks"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        (1, "Create exactly once", "22222222222242228222222222222222")
    ]


def test_browser_structural_commands_redirect_to_committed_state(client):
    blocked = create_task(client, "Blocked task")
    blocker = create_task(client, "Blocker")

    dependency = client.post(
        f"/tasks/{blocked['id']}/dependencies",
        data={
            "blocker_task_id": blocker["id"],
            "return_view": "manage",
            "expand": "true",
        },
    )
    assert dependency.status_code == 303
    assert f"expanded={blocked['id']}" in dependency.headers["Location"]
    dependency_page = client.get(dependency.headers["Location"]).get_data(as_text=True)
    blocked_markup = dependency_page.split(
        f'id="task-{blocked["id"]}"', 1
    )[1].split("</article>", 1)[0]
    assert "Blocker" in blocked_markup
    assert f'id="task-details-{blocked["id"]}" class="task-details">' in blocked_markup

    comment = client.post(
        f"/tasks/{blocked['id']}/comments",
        data={"body": "Confirmed in the redirected page", "expand": "true"},
    )
    assert comment.status_code == 303
    comment_page = client.get(comment.headers["Location"]).get_data(as_text=True)
    assert "Confirmed in the redirected page" in comment_page


def test_browser_form_validation_redirects_without_partial_task(client, app):
    response = client.post(
        "/tasks",
        data={
            "title": "   ",
            "request_id": "33333333-3333-4333-8333-333333333333",
        },
    )

    assert response.status_code == 303
    assert "error=Title+must+be+between+1+and+200+characters" in response.headers["Location"]
    with app.app_context():
        assert get_db().execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_legacy_subtasks_migrate_to_dependencies(client, app):
    parent = create_task(client, "Former parent")
    child = create_task(client, "Former child")

    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE tasks SET parent_task_id = ? WHERE id = ?",
            (parent["id"], child["id"]),
        )
        db.commit()

        init_db()
        init_db()

        migrated_child = db.execute(
            "SELECT parent_task_id FROM tasks WHERE id = ?", (child["id"],)
        ).fetchone()
        relationship_count = db.execute(
            """
            SELECT count(*) FROM task_dependencies
            WHERE blocked_task_id = ? AND blocker_task_id = ?
            """,
            (parent["id"], child["id"]),
        ).fetchone()[0]
        assert migrated_child["parent_task_id"] is None
        assert relationship_count == 1


def test_existing_database_gets_rank_column_and_preserves_smart_order(tmp_path):
    database_path = tmp_path / "legacy.sqlite3"
    legacy_db = sqlite3.connect(database_path)
    legacy_db.execute(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'todo',
            due_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_progress_at TEXT
        )
        """
    )
    legacy_db.execute(
        """
        CREATE TABLE task_waiting (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            person_name TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            last_followed_up_at TEXT,
            next_follow_up_on TEXT,
            updated_at TEXT NOT NULL,
            resolved_at TEXT
        )
        """
    )
    timestamp = "2026-09-10T00:00:00+00:00"
    legacy_db.executemany(
        """
        INSERT INTO tasks(title, status, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        [
            ("Older todo", "todo", timestamp, timestamp),
            ("In progress", "in_progress", timestamp, timestamp),
            ("Newer todo", "todo", timestamp, timestamp),
        ],
    )
    legacy_db.commit()
    legacy_db.close()

    migrated_app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(database_path),
            "USER_TIMEZONE": "Asia/Kolkata",
        }
    )
    with migrated_app.app_context():
        db = get_db()
        columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
        ranked_titles = [
            row["title"]
            for row in db.execute("SELECT title FROM tasks ORDER BY rank_key")
        ]
        ranks = [row["rank_key"] for row in db.execute("SELECT rank_key FROM tasks")]
        waiting_table = db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'task_waiting'"
        ).fetchone()
        waiting_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(task_waiting)")
        }

    assert "rank_key" in columns
    assert "create_request_id" in columns
    assert waiting_table is not None
    assert "next_follow_up_time" in waiting_columns
    assert ranked_titles == ["In progress", "Newer todo", "Older todo"]
    assert len(ranks) == len(set(ranks))
    assert all(rank is not None for rank in ranks)


def test_new_tasks_receive_unique_top_ranks(client):
    first = create_task(client, "First")
    second = create_task(client, "Second")

    assert second["rank_key"] < first["rank_key"]
    assert first["rank_key"] - second["rank_key"] == RANK_SPACING


def test_rank_move_changes_only_moved_task_when_gap_exists(client, app):
    first = create_task(client, "First")
    second = create_task(client, "Second")
    third = create_task(client, "Third")
    with app.app_context():
        before = {
            row["id"]: row["rank_key"]
            for row in get_db().execute("SELECT id, rank_key FROM tasks")
        }

    response = client.patch(
        f"/api/tasks/{first['id']}/rank",
        json={"after_task_id": third["id"], "before_task_id": second["id"]},
    )

    assert response.status_code == 200
    result = response.get_json()
    assert result["rebalanced"] is False
    assert [rank["id"] for rank in result["ranks"]] == [
        third["id"],
        first["id"],
        second["id"],
    ]
    with app.app_context():
        after = {
            row["id"]: row["rank_key"]
            for row in get_db().execute("SELECT id, rank_key FROM tasks")
        }
    assert after[third["id"]] == before[third["id"]]
    assert after[second["id"]] == before[second["id"]]
    assert after[third["id"]] < after[first["id"]] < after[second["id"]]


def test_rank_move_rebalances_when_neighbor_gap_is_exhausted(client, app):
    first = create_task(client, "First")
    second = create_task(client, "Second")
    third = create_task(client, "Third")
    with app.app_context():
        db = get_db()
        db.execute("UPDATE tasks SET rank_key = NULL")
        db.executemany(
            "UPDATE tasks SET rank_key = ? WHERE id = ?",
            [(1, first["id"]), (2, second["id"]), (3, third["id"])],
        )
        db.commit()

    response = client.patch(
        f"/api/tasks/{third['id']}/rank",
        json={"after_task_id": first["id"], "before_task_id": second["id"]},
    )

    assert response.status_code == 200
    result = response.get_json()
    assert result["rebalanced"] is True
    assert [rank["id"] for rank in result["ranks"]] == [
        first["id"],
        third["id"],
        second["id"],
    ]
    assert [int(rank["rank_key"]) for rank in result["ranks"]] == [
        RANK_SPACING,
        RANK_SPACING * 2,
        RANK_SPACING * 3,
    ]


def test_rank_move_rejects_stale_non_adjacent_neighbors(client, app):
    first = create_task(client, "First")
    second = create_task(client, "Second")
    third = create_task(client, "Third")
    with app.app_context():
        original = [
            row["id"]
            for row in get_db().execute("SELECT id FROM tasks ORDER BY rank_key")
        ]

    response = client.patch(
        f"/api/tasks/{first['id']}/rank",
        json={"after_task_id": third["id"], "before_task_id": None},
    )

    assert response.status_code == 400
    assert "order changed" in response.get_json()["error"].lower()
    with app.app_context():
        current = [
            row["id"]
            for row in get_db().execute("SELECT id FROM tasks ORDER BY rank_key")
        ]
    assert current == original


def test_create_task_rejects_legacy_parent_relationship(client, app):
    parent = create_task(client, "Former parent")

    response = client.post(
        "/api/tasks",
        json={"title": "Former child", "parent_task_id": parent["id"]},
    )

    assert response.status_code == 400
    assert "no longer supported" in response.get_json()["error"]
    with app.app_context():
        assert get_db().execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


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


def test_dependency_can_be_added_and_removed_from_either_task_direction(client, app):
    current = create_task(client, "Current task")
    blocker = create_task(client, "Blocking task")
    dependent = create_task(client, "Dependent task")

    blocked_by_response = client.post(
        f"/api/tasks/{current['id']}/dependencies",
        json={"blocker_task_id": blocker["id"]},
    )
    blocks_response = client.post(
        f"/api/tasks/{dependent['id']}/dependencies",
        json={"blocker_task_id": current["id"]},
    )

    assert blocked_by_response.status_code == 201
    assert blocked_by_response.get_json()["task"]["status"] == "todo"
    assert blocked_by_response.get_json()["task"]["blocked"] is True
    assert [task["id"] for task in blocked_by_response.get_json()["task"]["blocked_by"]] == [blocker["id"]]
    assert blocks_response.status_code == 201

    with app.app_context():
        relationships = get_db().execute(
            "SELECT blocked_task_id, blocker_task_id FROM task_dependencies ORDER BY blocked_task_id"
        ).fetchall()
        assert [(row["blocked_task_id"], row["blocker_task_id"]) for row in relationships] == [
            (current["id"], blocker["id"]),
            (dependent["id"], current["id"]),
        ]

    assert client.delete(
        f"/api/tasks/{current['id']}/dependencies/{blocker['id']}"
    ).status_code == 204
    assert client.delete(
        f"/api/tasks/{dependent['id']}/dependencies/{current['id']}"
    ).status_code == 204

    with app.app_context():
        assert get_db().execute("SELECT count(*) FROM task_dependencies").fetchone()[0] == 0


def test_index_renders_editable_relationships_in_both_directions(client):
    current = create_task(client, "Current task")
    blocker = create_task(client, "Blocking task")
    dependent = create_task(client, "Dependent task")
    client.post(
        f"/api/tasks/{current['id']}/dependencies",
        json={"blocker_task_id": blocker["id"]},
    )
    client.post(
        f"/api/tasks/{dependent['id']}/dependencies",
        json={"blocker_task_id": current["id"]},
    )

    page = client.get("/").data

    assert b'add-blocker-form' in page
    assert b'add-blocked-task-form' in page
    assert b'create-blocker-form' in page
    assert b'add-subtask-form' not in page
    assert "Dependent task — To do".encode() in page
    assert f'data-blocked-task-id="{current["id"]}"'.encode() in page
    assert f'data-blocker-task-id="{blocker["id"]}"'.encode() in page
    assert f'data-blocked-task-id="{dependent["id"]}"'.encode() in page
    assert f'data-blocker-task-id="{current["id"]}"'.encode() in page


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


def test_waiting_on_person_blocks_task_and_can_be_edited(client, app):
    task = create_task(client, "Get budget approved")

    response = client.put(
        f"/api/tasks/{task['id']}/waiting",
        json={
            "person_name": "  Ravi   Kumar ",
            "note": "Needs finance sign-off",
            "next_follow_up_on": "2099-01-10",
        },
    )

    assert response.status_code == 201
    waiting_task = response.get_json()["task"]
    assert waiting_task["status"] == "todo"
    assert waiting_task["blocked"] is True
    assert waiting_task["waiting"]["person_name"] == "Ravi Kumar"
    assert waiting_task["waiting"]["note"] == "Needs finance sign-off"
    assert waiting_task["waiting"]["next_follow_up_on"] == "2099-01-10"
    assert waiting_task["waiting"]["next_follow_up_time"] is None
    assert waiting_task["waiting"]["effective_follow_up_time"] == DEFAULT_FOLLOW_UP_TIME
    assert waiting_task["follow_up_due"] is False

    response = client.put(
        f"/api/tasks/{task['id']}/waiting",
        json={"person_name": "Meera", "note": "", "next_follow_up_on": None},
    )

    assert response.status_code == 200
    assert response.get_json()["task"]["waiting"]["person_name"] == "Meera"
    assert response.get_json()["task"]["waiting"]["next_follow_up_on"] is None
    with app.app_context():
        db = get_db()
        assert db.execute(
            "SELECT count(*) FROM task_waiting WHERE task_id = ? AND resolved_at IS NULL",
            (task["id"],),
        ).fetchone()[0] == 1
        event_types = [
            row["event_type"]
            for row in db.execute(
                "SELECT event_type FROM task_events WHERE task_id = ? ORDER BY id",
                (task["id"],),
            )
        ]
        assert "waiting_started" in event_types
        assert "waiting_updated" in event_types
        assert "status_changed" not in event_types


def test_follow_up_records_last_contact_history_and_next_reminder(client, app):
    task = create_task(client, "Get legal review")
    client.put(
        f"/api/tasks/{task['id']}/waiting",
        json={"person_name": "Anika", "next_follow_up_on": "2099-01-10"},
    )

    response = client.post(
        f"/api/tasks/{task['id']}/follow-ups",
        json={
            "note": "Sent a message",
            "next_follow_up_on": "2099-01-13",
            "next_follow_up_time": "11:45",
        },
    )

    assert response.status_code == 201
    waiting = response.get_json()["task"]["waiting"]
    assert waiting["last_followed_up_at"] is not None
    assert waiting["last_followed_up_on"] is not None
    assert waiting["last_followed_up_time"] is not None
    assert waiting["next_follow_up_on"] == "2099-01-13"
    assert waiting["next_follow_up_time"] == "11:45"
    assert waiting["history"] == [
        {
            "followed_up_on": waiting["last_followed_up_on"],
            "followed_up_time": waiting["last_followed_up_time"],
            "note": "Sent a message",
            "next_follow_up_on": "2099-01-13",
            "next_follow_up_time": "11:45",
        }
    ]
    with app.app_context():
        event = get_db().execute(
            "SELECT counts_as_progress FROM task_events WHERE task_id = ? AND event_type = 'followed_up'",
            (task["id"],),
        ).fetchone()
        assert event["counts_as_progress"] == 1


def test_exact_follow_up_time_is_optional_and_validated_atomically(client):
    task = create_task(client, "Get design feedback")
    response = client.put(
        f"/api/tasks/{task['id']}/waiting",
        json={
            "person_name": "Neha",
            "next_follow_up_on": "2099-01-10",
            "next_follow_up_time": "14:30",
        },
    )

    assert response.status_code == 201
    waiting = response.get_json()["task"]["waiting"]
    assert waiting["next_follow_up_time"] == "14:30"
    assert waiting["effective_follow_up_time"] == "14:30"

    invalid = client.put(
        f"/api/tasks/{task['id']}/waiting",
        json={
            "person_name": "Neha",
            "next_follow_up_on": "2099-01-10",
            "next_follow_up_time": "24:00",
        },
    )

    assert invalid.status_code == 400
    assert "HH:MM" in invalid.get_json()["error"]
    unchanged = client.put(
        f"/api/tasks/{task['id']}/waiting",
        json={"person_name": "Neha"},
    ).get_json()["task"]["waiting"]
    assert unchanged["next_follow_up_time"] == "14:30"


def test_date_only_follow_up_becomes_due_at_nine_am(client, app, monkeypatch):
    task = create_task(client, "Get morning confirmation")
    client.put(
        f"/api/tasks/{task['id']}/waiting",
        json={"person_name": "Arun", "next_follow_up_on": "2099-01-10"},
    )
    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE task_waiting SET next_follow_up_on = '2026-09-15' WHERE task_id = ?",
            (task["id"],),
        )
        db.commit()

    monkeypatch.setattr(
        app_module,
        "local_now",
        lambda: datetime(2026, 9, 15, 8, 59, tzinfo=app.config["TZINFO"]),
    )
    before_nine = client.get("/").get_data(as_text=True)
    before_markup = before_nine.split(f'data-task-id="{task["id"]}"', 1)[1].split(
        "</article>", 1
    )[0]
    assert 'data-follow-up-time="09:00"' in before_markup
    assert 'data-follow-up-due="false"' in before_markup
    assert "is-focus-task" not in before_nine

    monkeypatch.setattr(
        app_module,
        "local_now",
        lambda: datetime(2026, 9, 15, 9, 0, tzinfo=app.config["TZINFO"]),
    )
    at_nine = client.get("/").get_data(as_text=True)
    at_nine_markup = at_nine.split(f'data-task-id="{task["id"]}"', 1)[1].split(
        "</article>", 1
    )[0]
    assert 'data-follow-up-due="true"' in at_nine_markup
    assert "is-focus-task" in at_nine


def test_exact_follow_up_time_controls_when_reminder_becomes_due(client, app, monkeypatch):
    task = create_task(client, "Get afternoon confirmation")
    client.put(
        f"/api/tasks/{task['id']}/waiting",
        json={
            "person_name": "Arun",
            "next_follow_up_on": "2099-01-10",
            "next_follow_up_time": "14:30",
        },
    )
    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE task_waiting SET next_follow_up_on = '2026-09-15' WHERE task_id = ?",
            (task["id"],),
        )
        db.commit()

    monkeypatch.setattr(
        app_module,
        "local_now",
        lambda: datetime(2026, 9, 15, 14, 29, tzinfo=app.config["TZINFO"]),
    )
    assert 'data-follow-up-due="false"' in client.get("/").get_data(as_text=True)

    monkeypatch.setattr(
        app_module,
        "local_now",
        lambda: datetime(2026, 9, 15, 14, 30, tzinfo=app.config["TZINFO"]),
    )
    assert 'data-follow-up-due="true"' in client.get("/").get_data(as_text=True)


def test_due_follow_up_is_prioritized_as_the_next_action(client, app):
    moving = create_task(client, "Already moving")
    client.patch(f"/api/tasks/{moving['id']}", json={"status": "in_progress"})
    waiting = create_task(client, "Get launch approval")
    client.put(
        f"/api/tasks/{waiting['id']}/waiting",
        json={"person_name": "Ravi", "next_follow_up_on": "2099-01-10"},
    )
    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE task_waiting SET next_follow_up_on = '2000-01-01' WHERE task_id = ?",
            (waiting["id"],),
        )
        db.commit()

    page = client.get("/").get_data(as_text=True)
    waiting_markup = page.split(f'data-task-id="{waiting["id"]}"', 1)[1].split(
        "</article>", 1
    )[0]

    assert f'class="task-card task-row status-todo is-blocked is-focus-task"\n        data-task-id="{waiting["id"]}"' in page
    assert 'data-follow-up-due="true"' in waiting_markup
    assert 'data-actionable="true"' in waiting_markup
    assert 'class="focus-task-title focus-only">Follow up with Ravi</div>' in waiting_markup
    assert 'class="focus-task-context focus-only">About: Get launch approval</div>' in waiting_markup
    assert 'class="primary followed-up"' in waiting_markup
    assert "follow-up overdue" in waiting_markup


def test_resolving_wait_preserves_workflow_status_and_history(client, app):
    task = create_task(client, "Get a decision")
    client.patch(f"/api/tasks/{task['id']}", json={"status": "in_progress"})
    client.put(
        f"/api/tasks/{task['id']}/waiting",
        json={"person_name": "Sam", "next_follow_up_on": "2099-01-10"},
    )

    response = client.post(f"/api/tasks/{task['id']}/waiting/resolve", json={})

    assert response.status_code == 200
    resolved = response.get_json()["task"]
    assert resolved["status"] == "in_progress"
    assert resolved["waiting"] is None
    assert resolved["blocked"] is False
    assert resolved["follow_up_due"] is False
    with app.app_context():
        waiting = get_db().execute(
            "SELECT resolved_at FROM task_waiting WHERE task_id = ?", (task["id"],)
        ).fetchone()
        assert waiting["resolved_at"] is not None


def test_resolving_person_wait_preserves_other_blocking_reasons(client):
    task = create_task(client, "Ship launch")
    blocker = create_task(client, "Approve launch")
    client.patch(f"/api/tasks/{task['id']}", json={"status": "in_progress"})
    client.post(
        f"/api/tasks/{task['id']}/dependencies",
        json={"blocker_task_id": blocker["id"]},
    )
    client.put(
        f"/api/tasks/{task['id']}/waiting",
        json={"person_name": "Ravi", "next_follow_up_on": None},
    )

    response = client.post(f"/api/tasks/{task['id']}/waiting/resolve", json={})
    resolved = response.get_json()["task"]

    assert response.status_code == 200
    assert resolved["status"] == "in_progress"
    assert resolved["waiting"] is None
    assert resolved["blocked"] is True
    assert [item["id"] for item in resolved["blocked_by"] if not item["resolved"]] == [
        blocker["id"]
    ]


def test_blocked_is_not_a_valid_workflow_status(client):
    task = create_task(client, "Use explicit blockers")

    response = client.patch(f"/api/tasks/{task['id']}", json={"status": "blocked"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid status"


def test_completed_tasks_cannot_gain_new_blocking_reasons(client):
    completed = create_task(client, "Already complete")
    active = create_task(client, "Still active")
    client.patch(f"/api/tasks/{completed['id']}", json={"status": "done"})

    wait_response = client.put(
        f"/api/tasks/{completed['id']}/waiting",
        json={"person_name": "Ravi", "next_follow_up_on": None},
    )
    blocked_task_response = client.post(
        f"/api/tasks/{completed['id']}/dependencies",
        json={"blocker_task_id": active["id"]},
    )
    completed_blocker_response = client.post(
        f"/api/tasks/{active['id']}/dependencies",
        json={"blocker_task_id": completed["id"]},
    )

    assert wait_response.status_code == 400
    assert blocked_task_response.status_code == 400
    assert completed_blocker_response.status_code == 400


def test_workflow_changes_preserve_waiting_until_completion(client, app):
    task = create_task(client, "Get an answer")
    invalid = client.put(
        f"/api/tasks/{task['id']}/waiting",
        json={"person_name": "Sam", "next_follow_up_on": "2000-01-01"},
    )
    assert invalid.status_code == 400
    assert "past" in invalid.get_json()["error"]
    with app.app_context():
        db = get_db()
        assert db.execute("SELECT count(*) FROM task_waiting").fetchone()[0] == 0
        assert db.execute(
            "SELECT status FROM tasks WHERE id = ?", (task["id"],)
        ).fetchone()["status"] == "todo"

    client.put(
        f"/api/tasks/{task['id']}/waiting",
        json={"person_name": "Sam", "next_follow_up_on": None},
    )
    response = client.patch(f"/api/tasks/{task['id']}", json={"status": "in_progress"})

    assert response.status_code == 200
    assert response.get_json()["task"]["status"] == "in_progress"
    assert response.get_json()["task"]["waiting"] is not None
    assert response.get_json()["task"]["blocked"] is True

    response = client.patch(f"/api/tasks/{task['id']}", json={"status": "done"})
    assert response.status_code == 200
    assert response.get_json()["task"]["waiting"] is None
    assert response.get_json()["task"]["blocked"] is False
    with app.app_context():
        assert get_db().execute(
            "SELECT resolved_at FROM task_waiting WHERE task_id = ?", (task["id"],)
        ).fetchone()["resolved_at"] is not None


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

    response = client.post(
        f"/api/tasks/{task['id']}/comments",
        json={"body": "Made progress", "counts_as_progress": True},
    )
    assert response.status_code == 201
    with app.app_context():
        active_count = get_db().execute(
            """
            SELECT count(*) AS count
            FROM task_labels tl JOIN labels l ON l.id = tl.label_id
            WHERE tl.task_id = ? AND tl.removed_at IS NULL AND l.type = 'active_date'
            """,
            (task["id"],),
        ).fetchone()["count"]
        assert active_count == 1


def test_index_renders_compact_task_ui(client):
    create_task(client, "Visible task")
    response = client.get("/")
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '<link rel="icon" type="image/png" href="/static/aur-bataao-icon.png">' in page
    assert b"Visible task" in response.data
    assert b"task-list" in response.data
    assert b'new-task-dialog' in response.data
    assert b'task-composer' in response.data
    assert b'id="sort-control"' in response.data
    assert b'class="rank-handle"' in response.data
    assert b'data-rank-key=' in response.data
    assert b'id="view-toggle"' in response.data
    assert b'<span class="view-toggle-prompt" aria-hidden="true">Switch Mode to</span>' in response.data
    assert b'<span class="view-toggle-label">View all tasks</span>' in response.data
    assert b'id="view-all-tasks"' not in response.data
    assert b'id="back-to-aur-bataao"' not in response.data
    assert b'class="toggle-chevron"' in response.data
    assert b">Add a Task...</button>" in response.data
    assert b">Save task</button>" in response.data
    assert b'Counts as progress' in response.data
    assert b'record-progress' not in response.data
    assert b'+ Progress' not in response.data
    assert 'class="task-card task-row status-' in page
    assert 'class="task-leading"' in page
    assert 'class="task-controls"' in page
    assert page.count('class="task-detail-column"') == 2
    detail_columns = page.split('<div class="task-detail-column">')
    left_column, right_column = detail_columns[1:3]
    assert (
        left_column.index("Description")
        < left_column.index("Labels")
        < left_column.index("Relationships")
    )
    assert "Attachments" not in left_column
    assert right_column.index("Attachments") < right_column.index("Comments")
    assert "Relationships" not in right_column
    task_main = page.split('<header class="task-main">', 1)[1].split("</header>", 1)[0]
    assert task_main.index('class="rank-handle"') < task_main.index(
        'class="badge rank-badge"'
    ) < task_main.index('class="icon toggle-details"')


def test_stylesheet_supports_nvidia_light_and_dark_themes():
    theme = (Path(__file__).parents[1] / "static" / "theme.css").read_text(
        encoding="utf-8"
    )
    controls = (Path(__file__).parents[1] / "static" / "controls.css").read_text(
        encoding="utf-8"
    )

    assert "--accent: #76b900;" in theme
    assert theme.count("--accent-text: #ffffff;") == 1
    assert "--accent-text: #111111;" in theme
    assert "color-scheme: light;" in theme
    assert ':root[data-theme="dark"]' in theme
    assert "color-scheme: dark;" in theme
    assert "color: var(--accent-text);" in controls


def test_stylesheet_uses_subtle_functional_motifs():
    theme = (Path(__file__).parents[1] / "static" / "theme.css").read_text(
        encoding="utf-8"
    )
    layout = (Path(__file__).parents[1] / "static" / "app.css").read_text(
        encoding="utf-8"
    )

    wave = (Path(__file__).parents[1] / "static" / "wave-motif.svg").read_text(
        encoding="utf-8"
    )

    light_theme = theme.split(':root[data-theme="dark"] {', 1)[0]
    assert "--motif-opacity: 0.05;" in light_theme
    dark_theme = theme.split(':root[data-theme="dark"] {', 1)[1]
    assert "--motif-opacity: 0.14;" in dark_theme
    assert 'viewBox="0 0 4800 240"' in wave
    assert wave.count("<path") == 12
    assert all(f'id="wave-{variant}"' in wave for variant in "abcd")
    assert ".task-card::before" in layout
    task_motif = layout.split(".task-card::before {", 1)[1].split("}", 1)[0]
    assert "left: -6%;" in task_motif
    assert "width: 112%;" in task_motif
    assert ".task-card:nth-child(4n + 2)::before" in layout
    assert ".task-card:nth-child(4n + 3)::before" in layout
    assert ".task-card:nth-child(4n)::before" in layout
    assert "body.focus-mode .task-card.is-focus-task::after" in layout
    assert ".relationship-list li::before" in layout
    assert ".comments li" in layout
    assert layout.count('\n  mask: url("wave-motif.svg")') == 3
    assert layout.count('\n  -webkit-mask: url("wave-motif.svg")') == 3
    assert "mask-position: 33.333% center;" in layout
    assert "mask-position: 66.667% center;" in layout
    assert "mask-position: right center;" in layout


def test_python_launcher_owns_and_cleans_up_server():
    launcher = (Path(__file__).parents[1] / "start.py").read_text(encoding="utf-8")

    assert "class InstanceLock" in launcher
    assert "class ParentWatcher" in launcher
    assert "class WindowsConsoleCloseHandler" in launcher
    assert "class WindowsJob" in launcher
    assert "kill_on_job_close = 0x00002000" in launcher
    assert "signal.SIGINT" in launcher
    assert "signal.SIGTERM" in launcher
    assert 'sys.argv[1:] == ["--stop"]' in launcher
    assert "server.task_dispatcher.shutdown" in launcher
    assert "wasyncore.close_all" in launcher


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES"])
def test_python_launcher_accepts_restart_confirmation(monkeypatch, answer):
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)

    assert start.confirm_restart() is True


@pytest.mark.parametrize("answer", ["", "n", "no", "later"])
def test_python_launcher_skips_restart_by_default(monkeypatch, capsys, answer):
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)

    assert start.confirm_restart() is False
    assert "existing instance will continue running" in capsys.readouterr().out


def test_python_launcher_removes_stale_runtime_files(monkeypatch, tmp_path):
    lock_path = tmp_path / "aur-bataao.lock"
    pid_path = tmp_path / "aur-bataao.pid"
    stop_path = tmp_path / "aur-bataao.stop"
    ready_path = tmp_path / "aur-bataao-worker-stale.ready"
    worker_stop_path = tmp_path / "aur-bataao-worker-stale.stop"
    pid_path.write_text("999999\n", encoding="utf-8")
    stop_path.write_text("stop\n", encoding="utf-8")
    ready_path.write_text("stale\n", encoding="utf-8")
    worker_stop_path.write_text("stop\n", encoding="utf-8")
    monkeypatch.setattr(start, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(start, "LOCK_FILE", lock_path)
    monkeypatch.setattr(start, "PID_FILE", pid_path)
    monkeypatch.setattr(start, "STOP_FILE", stop_path)

    assert start.instance_is_running() is False
    assert lock_path.exists()
    assert not pid_path.exists()
    assert not stop_path.exists()
    assert not ready_path.exists()
    assert not worker_stop_path.exists()


def test_python_launcher_detects_held_kernel_lock(monkeypatch, tmp_path):
    lock_path = tmp_path / "aur-bataao.lock"
    pid_path = tmp_path / "aur-bataao.pid"
    monkeypatch.setattr(start, "LOCK_FILE", lock_path)
    monkeypatch.setattr(start, "PID_FILE", pid_path)

    with start.InstanceLock(lock_path, pid_path):
        assert start.instance_is_running() is True


def test_stylesheet_uses_compact_borderless_task_controls_and_responsive_date_width():
    theme = (Path(__file__).parents[1] / "static" / "theme.css").read_text(
        encoding="utf-8"
    )
    controls = (Path(__file__).parents[1] / "static" / "controls.css").read_text(
        encoding="utf-8"
    )
    layout = (Path(__file__).parents[1] / "static" / "app.css").read_text(
        encoding="utf-8"
    )

    assert "--control-height: 36px;" in theme
    assert "--date-input-width: 150px;" in theme
    assert "min-height: var(--control-height);" in controls
    assert ".status-select, .due-date" in layout
    assert 'grid-template-areas: "leading summary badges controls";' in layout
    assert 'grid-template-columns: 116px var(--date-input-width);' in layout
    assert '"leading summary summary"' in layout
    assert '". badges controls"' in layout
    assert '.toggle-details[aria-expanded="true"] .toggle-chevron' in layout
    assert '.icon[aria-expanded="true"]' not in layout
    assert ".date-control" not in controls
    assert ".date-picker-button" not in controls
    assert ".date-picker-icon" not in controls
    assert "calendar-picker-indicator" not in controls
    assert "calendar.svg" not in controls
    row_controls = layout.split(".status-select, .due-date {", 1)[1].split("}", 1)[0]
    assert "width: 100%;" in row_controls
    assert "min-height: 30px;" in row_controls
    assert "height: 30px;" in row_controls
    assert "padding-block: 0;" in row_controls
    assert "border-color: transparent;" in row_controls
    assert "background: transparent;" in row_controls
    assert ".status-select:is(:hover, :focus), .due-date:is(:hover, :focus)" in layout
    highlighted_controls = layout.split(
        ".status-select:is(:hover, :focus), .due-date:is(:hover, :focus) {", 1
    )[1].split("}", 1)[0]
    assert "border-color: var(--line);" in highlighted_controls
    assert "background: var(--panel);" in highlighted_controls
    task_card_layout = layout.split("/* Task cards */", 1)[1]
    task_main = task_card_layout.split(".task-main {", 1)[1].split("}", 1)[0]
    task_title = task_card_layout.split("\n.task-title {", 1)[1].split("}", 1)[0]
    badges = task_card_layout.split("\n.badges {", 1)[1].split("}", 1)[0]
    task_labels = task_card_layout.split("\n.task-label-list {", 1)[1].split("}", 1)[0]
    assert "padding: 2px 8px 2px 4px;" in task_main
    assert "height: 54px;" in task_main
    assert "flex-wrap: nowrap;" in badges
    assert "overflow: hidden;" in badges
    assert "flex-wrap: nowrap;" in task_labels
    assert "overflow: hidden;" in task_labels
    assert "min-height: 26px;" in task_title
    assert "height: 26px;" in task_title
    assert "--task-row-padding-block" not in theme


def test_toolbar_has_no_panel_background():
    layout = (Path(__file__).parents[1] / "static" / "app.css").read_text(
        encoding="utf-8"
    )

    toolbar = layout.split(".toolbar {", 1)[1].split("}", 1)[0]
    assert "background: transparent;" in toolbar
    assert "background: var(--surface-muted);" not in toolbar


def test_collapsed_and_expanded_labels_share_compact_height():
    layout = (Path(__file__).parents[1] / "static" / "app.css").read_text(
        encoding="utf-8"
    )

    chip_rule = layout.split(".label-chip {", 1)[1].split("}", 1)[0]
    remove_rule = layout.split(".label-chip > button {", 1)[1].split("}", 1)[0]
    assert "height: 19px;" in chip_rule
    assert "min-height: 19px;" in chip_rule
    assert "min-height: 15px;" in remove_rule


def test_expanded_task_sections_use_compact_dividers():
    layout = (Path(__file__).parents[1] / "static" / "app.css").read_text(
        encoding="utf-8"
    )

    section_rule = layout.split(
        ".task-detail-column > .section-label {", 1
    )[1].split("}", 1)[0]
    first_section_rule = layout.split(
        ".task-detail-column > .section-label.first {", 1
    )[1].split("}", 1)[0]
    assert "border-top: 1px solid var(--line-subtle);" in section_rule
    assert "padding-top: 10px;" in section_rule
    assert "border-top: 0;" in first_section_rule


def test_waiting_form_layout_is_compact_and_progressively_discloses_time(client):
    task = create_task(client, "Await review")
    page = client.get("/").get_data(as_text=True)
    task_markup = page.split(f'data-task-id="{task["id"]}"', 1)[1].split(
        "</article>", 1
    )[0]
    layout = (Path(__file__).parents[1] / "static" / "app.css").read_text(
        encoding="utf-8"
    )

    assert "Track who you’re waiting on and when to follow up." in task_markup
    assert 'class="exact-time-field" hidden' in task_markup
    assert '<button type="submit">Mark as waiting</button>' in task_markup
    assert ".exact-time-field[hidden] { display: none !important; }" in layout
    waiting_fields = layout.split(".waiting-fields {", 1)[1].split("}", 1)[0]
    assert "align-items: start;" in waiting_fields
    waiting_input = layout.split(".waiting-form input {", 1)[1].split("}", 1)[0]
    assert "height: calc(var(--control-height) + 2px);" in waiting_input


def test_index_renders_accessible_theme_toggle(client):
    page = client.get("/").get_data(as_text=True)

    assert 'href="/static/theme.css"' in page
    assert 'href="/static/controls.css"' in page
    assert 'href="/static/app.css"' in page
    assert 'id="theme-toggle"' in page
    assert 'role="switch"' in page
    assert 'aria-checked="false"' in page
    assert 'class="theme-icon theme-icon-sun"' in page
    assert 'class="theme-icon theme-icon-moon"' in page

    theme = (Path(__file__).parents[1] / "static" / "theme.css").read_text(
        encoding="utf-8"
    )
    controls = (Path(__file__).parents[1] / "static" / "controls.css").read_text(
        encoding="utf-8"
    )
    toggle_thumb = controls.split(".theme-toggle::before {", 1)[1].split("}", 1)[0]
    assert "background: var(--theme-toggle-thumb);" in toggle_thumb
    assert "background: var(--accent);" not in toggle_thumb
    assert "--theme-toggle-height: 28px;" in theme
    assert "--theme-toggle-width: 54px;" in theme
    assert "--theme-toggle-thumb-size: 24px;" in theme
    assert "--theme-toggle-travel: 26px;" in theme
    assert "*, *::before, *::after { box-sizing: border-box; }" in controls
    assert "border: 0;" in controls.split(".theme-toggle {", 1)[1].split("}", 1)[0]


def test_index_renders_native_date_control(client):
    create_task(client, "Task without labels")
    page = client.get("/").get_data(as_text=True)

    assert 'class="due-date" data-field="due_date" type="date"' in page
    assert 'class="date-control due-date-control"' not in page
    assert 'class="date-picker-button"' not in page
    assert 'class="date-picker-icon"' not in page


def test_index_defaults_to_one_actionable_task(client):
    older_todo = create_task(client, "Older todo")
    blocked = create_task(client, "Blocked by dependency")
    blocker = create_task(client, "Dependency")
    in_progress = create_task(client, "Already moving")
    client.patch(
        f"/api/tasks/{in_progress['id']}", json={"status": "in_progress"}
    )
    client.post(
        f"/api/tasks/{blocked['id']}/dependencies",
        json={"blocker_task_id": blocker["id"]},
    )

    page = client.get("/").get_data(as_text=True)

    assert '<body class="focus-mode has-actionable"' in page
    assert (
        f'class="task-card task-row status-in_progress is-focus-task"\n'
        f'        data-task-id="{in_progress["id"]}"'
    ) in page
    assert page.count(" is-focus-task") == 1
    assert f'data-task-id="{older_todo["id"]}"' in page
    assert 'class="focus-task-actions focus-only"' in page
    assert 'class="focus-task-title focus-only">Already moving</div>' in page
    assert 'class="disclosure-chevron" viewBox="0 0 20 20"' in page
    assert 'aria-controls="task-details-' in page
    assert 'class="focus-title-editor focus-only" data-field="title"' in page
    assert ">Kuch Aur Bataao</button>" in page
    focused_article = page.split(
        f'data-task-id="{in_progress["id"]}"', 1
    )[1].split("</article>", 1)[0]
    assert "Kuch Aur Bataao" not in focused_article
    assert 'id="focus-next-action" class="focus-next-action focus-only"' in page
    assert 'id="aur-bataao-button"' in page
    focus_actions = focused_article.split(
        '<footer class="focus-task-actions focus-only">', 1
    )[1].split("</footer>", 1)[0]
    assert "Update / comment" not in focus_actions
    assert focus_actions.index('class="focus-toggle-details"') < focus_actions.index(
        'class="primary complete-focus-task"'
    )
    assert "Can’t do now" not in page


def test_index_excludes_dependency_blocked_task_from_focus(client):
    blocked = create_task(client, "Waiting task")
    blocker = create_task(client, "Do this first")
    client.post(
        f"/api/tasks/{blocked['id']}/dependencies",
        json={"blocker_task_id": blocker["id"]},
    )

    page = client.get("/").get_data(as_text=True)
    blocked_markup = page.split(f'data-task-id="{blocked["id"]}"', 1)[1].split(
        "</article>", 1
    )[0]

    assert f'data-task-id="{blocker["id"]}"' in page
    assert 'data-actionable="false"' in blocked_markup
    assert 'data-blocked="true"' in blocked_markup


def test_index_shows_english_empty_state_when_nothing_is_actionable(client):
    blocked = create_task(client, "Waiting")
    client.put(
        f"/api/tasks/{blocked['id']}/waiting",
        json={"person_name": "Ravi", "next_follow_up_on": None},
    )

    page = client.get("/").get_data(as_text=True)

    assert '<body class="focus-mode focus-empty"' in page
    assert re.search(
        r'<div class="focus-empty-hero" role="img" aria-label="(?:Clinking beer mugs|Clinking glasses|Tropical drink|Bottle with popping cork|Bubble tea)">(?:🍻|🥂|🍹|🍾|🧋)</div>',
        page,
    )
    assert '<h2>No actionable tasks</h2>' in page
    assert '<p>There is nothing ready to work on right now.</p>' in page
    assert 'id="view-blocked-tasks"' in page
    assert '<button type="button" class="primary open-task-dialog">Add a Task...</button>' in page
    assert "Suno" not in page
    assert "Aap Bataao" not in page
    assert 'id="focus-next-action" class="focus-next-action focus-only" hidden' in page
    assert "is-focus-task" not in page


def test_empty_state_hero_pool_contains_all_approved_native_emoji():
    assert EMPTY_STATE_HEROES == (
        ("🍻", "Clinking beer mugs"),
        ("🥂", "Clinking glasses"),
        ("🍹", "Tropical drink"),
        ("🍾", "Bottle with popping cork"),
        ("🧋", "Bubble tea"),
    )


def test_index_title_summary_shows_only_nonzero_counts_with_grammar(client):
    first = create_task(client, "First active task")
    second = create_task(client, "Second active task")
    create_task(client, "To do task")
    assert client.patch(
        f"/api/tasks/{first['id']}", json={"status": "in_progress"}
    ).status_code == 200
    assert client.patch(
        f"/api/tasks/{second['id']}", json={"status": "in_progress"}
    ).status_code == 200

    response = client.get("/")

    assert response.status_code == 200
    assert b'id="active-task-count" class="muted">2 in progress</span>' in response.data
    assert b"Asia/Kolkata" not in response.data.split(b"active-task-count", 1)[1].split(b"</span>", 1)[0]


def test_index_title_summary_is_hidden_when_all_counts_are_zero(client):
    create_task(client, "To do task")

    response = client.get("/")

    assert response.status_code == 200
    assert b'id="active-task-count" class="muted" hidden></span>' in response.data


def test_index_title_summary_uses_follow_up_grammar(client, app):
    first = create_task(client, "First follow-up")
    second = create_task(client, "Second follow-up")
    for task in (first, second):
        assert client.put(
            f"/api/tasks/{task['id']}/waiting",
            json={"person_name": "Ravi", "next_follow_up_on": "2099-01-10"},
        ).status_code == 201

    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE task_waiting SET next_follow_up_on = '2000-01-01' WHERE task_id = ?",
            (first["id"],),
        )
        db.commit()

    response = client.get("/")
    assert b'id="active-task-count" class="muted">1 needs follow-up</span>' in response.data

    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE task_waiting SET next_follow_up_on = '2000-01-01' WHERE task_id = ?",
            (second["id"],),
        )
        db.commit()

    response = client.get("/")
    assert b'id="active-task-count" class="muted">2 need follow-up</span>' in response.data


def test_index_shows_task_labels_in_collapsed_view_and_filter(client):
    task = create_task(client, "Plan release")
    assert client.post(
        f"/api/tasks/{task['id']}/labels", json={"name": "launch"}
    ).status_code == 201

    response = client.get("/")

    assert response.status_code == 200
    assert b'id="label-filter"' in response.data
    assert b'value="1" data-filter-value checked' in response.data
    assert b'<span>launch</span>' in response.data
    assert b'class="task-label-list"' in response.data
    assert b'class="label-chip task-label-filter manual"' in response.data
    assert b'data-label-ids="1"' in response.data


def test_state_filter_defaults_to_everything_except_done(client):
    response = client.get("/")

    assert response.status_code == 200
    assert b'value="todo" data-filter-value checked' in response.data
    assert b'value="in_progress" data-filter-value checked' in response.data
    assert b'value="blocked" data-filter-value checked' in response.data
    assert b'value="done" data-filter-value>' in response.data
    assert b'data-all-label="All states"' in response.data


def test_label_picker_lists_saved_labels_and_checks_task_assignments(client):
    task = create_task(client, "Plan release")
    other = create_task(client, "Other work")
    selected = client.post(
        f"/api/tasks/{task['id']}/labels", json={"name": "launch"}
    ).get_json()["task"]["labels"][0]
    unused = client.post(
        f"/api/tasks/{other['id']}/labels", json={"name": "someday"}
    ).get_json()["task"]["labels"][0]
    assert client.delete(
        f"/api/tasks/{other['id']}/labels/{unused['id']}"
    ).status_code == 204

    page = client.get("/").get_data(as_text=True)
    task_markup = page.split(f'data-task-id="{task["id"]}"', 1)[1].split(
        "</article>", 1
    )[0]

    assert 'class="multi-select task-label-picker"' in task_markup
    assert f'value="{selected["id"]}"' in task_markup
    assert f'value="{unused["id"]}"' in task_markup
    selected_option = task_markup.split(f'value="{selected["id"]}"', 1)[1].split(
        ">", 1
    )[0]
    unused_option = task_markup.split(f'value="{unused["id"]}"', 1)[1].split(
        ">", 1
    )[0]
    assert "checked" in selected_option
    assert "checked" not in unused_option
    assert "Create or add label…" in task_markup


def test_create_task_with_attachment_and_remove_it(client):
    image_bytes = b"fake-png-content"
    response = client.post(
        "/api/tasks",
        data={
            "title": "Task with a screenshot",
            "attachments": (BytesIO(image_bytes), "../../screenshot.png", "image/png"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 201
    task = response.get_json()["task"]
    assert len(task["attachments"]) == 1
    attachment = task["attachments"][0]
    assert attachment["original_name"] == "screenshot.png"
    assert attachment["previewable"] is True

    preview = client.get(f"/api/attachments/{attachment['id']}")
    assert preview.status_code == 200
    assert preview.data == image_bytes
    assert preview.mimetype == "image/png"
    assert preview.headers["X-Content-Type-Options"] == "nosniff"
    preview.close()

    download = client.get(f"/api/attachments/{attachment['id']}?download=1")
    assert "attachment" in download.headers["Content-Disposition"]
    download.close()

    removed = client.delete(f"/api/tasks/{task['id']}/attachments/{attachment['id']}")
    assert removed.status_code == 204
    assert client.get(f"/api/attachments/{attachment['id']}").status_code == 404


def test_add_attachment_to_existing_task(client):
    task = create_task(client, "Attach later")
    response = client.post(
        f"/api/tasks/{task['id']}/attachments",
        data={"attachments": (BytesIO(b"notes"), "notes.txt", "text/plain")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 201
    attachments = response.get_json()["attachments"]
    assert len(attachments) == 1
    assert attachments[0]["original_name"] == "notes.txt"
    assert attachments[0]["previewable"] is False


def test_oversized_attachment_does_not_create_partial_task(client, app):
    app.config["MAX_ATTACHMENT_BYTES"] = 4
    response = client.post(
        "/api/tasks",
        data={
            "title": "Should roll back",
            "attachments": (BytesIO(b"12345"), "large.txt", "text/plain"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert "larger than" in response.get_json()["error"]

    with app.app_context():
        assert get_db().execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    assert not any(Path(app.config["ATTACHMENTS_DIR"]).iterdir())
