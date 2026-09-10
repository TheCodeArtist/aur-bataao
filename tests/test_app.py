import sqlite3
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pytest

from app import RANK_SPACING, create_app, get_db, init_db, reconcile_active_labels


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

    assert "rank_key" in columns
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
    assert b"Visible task" in response.data
    assert b"task-list" in response.data
    assert b'new-task-dialog' in response.data
    assert b'task-composer' in response.data
    assert b'id="sort-control"' in response.data
    assert b'class="rank-handle"' in response.data
    assert b'data-rank-key=' in response.data
    assert b">Suno...</button>" in response.data
    assert b">Save task</button>" in response.data
    assert b'Counts as progress' in response.data
    assert b'record-progress' not in response.data
    assert b'+ Progress' not in response.data
    task_main = page.split('<div class="task-main">', 1)[1].split("</div>", 1)[0]
    assert task_main.index('class="rank-handle"') < task_main.index(
        'class="badge rank-badge"'
    ) < task_main.index('class="icon toggle-details"')


def test_index_count_excludes_done_tasks(client):
    create_task(client, "Still active")
    completed = create_task(client, "Already complete")
    response = client.patch(
        f"/api/tasks/{completed['id']}", json={"status": "done"}
    )
    assert response.status_code == 200

    response = client.get("/")

    assert response.status_code == 200
    assert b'id="active-task-count" class="muted">1 task \xc2\xb7 Asia/Kolkata</span>' in response.data


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


def test_status_filter_defaults_to_everything_except_done(client):
    response = client.get("/")

    assert response.status_code == 200
    assert b'value="todo" data-filter-value checked' in response.data
    assert b'value="in_progress" data-filter-value checked' in response.data
    assert b'value="blocked" data-filter-value checked' in response.data
    assert b'value="done" data-filter-value>' in response.data


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
