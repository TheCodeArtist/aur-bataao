from __future__ import annotations

import sqlite3
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app as app_module
from agent_runner import AgentOutcome
from agent_store import AgentRun
from app import (
    create_app,
    get_db,
    load_tasks,
    maybe_reconcile,
    migrate_legacy_subtasks,
    reconcile_active_labels,
)


def create_task(client, title="Task"):
    response = client.post("/api/tasks", json={"title": title})
    assert response.status_code == 201
    return response.get_json()["task"]


def create_profile(client, **overrides):
    values = {
        "name": "Local",
        "base_url": "http://localhost:1234/v1",
        "model": "model",
    }
    values.update(overrides)
    response = client.post("/api/llm-profiles", json=values)
    assert response.status_code == 201
    return response.get_json()["profile"]


def test_app_factory_uses_injected_environment_and_validates_it(tmp_path):
    database = tmp_path / "environment.sqlite3"
    app = create_app(
        {"TESTING": True},
        environ={
            "AUR_BATAAO_DATABASE": str(database),
            "AUR_BATAAO_TIMEZONE": "UTC",
            "AUR_BATAAO_LLM_MODEL": "configured",
            "AUR_BATAAO_LLM_API_KEY": "secret",
            "AUR_BATAAO_LLM_TIMEOUT_SECONDS": "30.5",
            "AUR_BATAAO_AGENT_MAX_STEPS": "4",
            "AUR_BATAAO_LLM_SUPPORTS_TOOLS": "0",
        },
    )
    assert app.config["DATABASE"] == str(database)
    assert app.config["LLM_API_KEY_ENV"] == "AUR_BATAAO_LLM_API_KEY"
    assert app.config["LLM_TIMEOUT_SECONDS"] == 30.5
    assert app.config["AGENT_MAX_STEPS"] == 4
    with app.app_context():
        assert len(app_module.LlmProfileStore(get_db()).list()) == 1
    profile = app.test_client().get("/api/llm-profiles").get_json()["profiles"][0]
    assert profile["api_key_configured"] is True

    for environment, match in [
        ({"AUR_BATAAO_LLM_TIMEOUT_SECONDS": "bad"}, "must be a number"),
        ({"AUR_BATAAO_AGENT_MAX_STEPS": "bad"}, "must be an integer"),
        ({"AUR_BATAAO_TIMEZONE": "Not/AZone"}, "Unknown timezone"),
    ]:
        with pytest.raises(RuntimeError, match=match):
            create_app({"TESTING": True, "DATABASE": str(tmp_path / f"{len(match)}.db")}, environ=environment)


def test_app_factory_preserves_explicit_paths_and_existing_profile(tmp_path):
    database = tmp_path / "explicit.sqlite3"
    attachments = tmp_path / "explicit-attachments"
    environment = {
        "AUR_BATAAO_DATABASE": str(database),
        "AUR_BATAAO_ATTACHMENTS_DIR": str(attachments),
        "AUR_BATAAO_LLM_MODEL": "first-model",
    }

    first = create_app({}, environ=environment)
    assert first.config["ATTACHMENTS_DIR"] == str(attachments)

    restarted = create_app(
        {
            "TESTING": True,
            "DATABASE": str(database),
            "ATTACHMENTS_DIR": str(attachments),
            "LLM_MODEL": "replacement-model",
        },
        environ={},
    )
    with restarted.app_context():
        profiles = app_module.LlmProfileStore(get_db(), environ={}).list()
        assert [profile.model for profile in profiles] == ["first-model"]


def test_migrates_legacy_agent_run_configuration_columns(tmp_path):
    database = tmp_path / "legacy-agent-runs.sqlite3"
    db = sqlite3.connect(database)
    db.execute(
        """
        CREATE TABLE agent_runs (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            profile_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            base_url TEXT NOT NULL,
            model TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            error TEXT,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    db.commit()
    db.close()

    migrated = create_app(
        {
            "TESTING": True,
            "DATABASE": str(database),
            "USER_TIMEZONE": "Asia/Kolkata",
            "LLM_MODEL": "",
        },
        environ={},
    )
    with migrated.app_context():
        columns = {
            row["name"] for row in get_db().execute("PRAGMA table_info(agent_runs)")
        }
    assert {"api_key_env", "timeout_seconds", "supports_tools"} <= columns


def test_helper_and_reconciliation_edges(app, monkeypatch):
    with app.app_context():
        assert app_module._display_filename("C:\\unsafe\x00name.txt") == "unsafename.txt"
        assert app_module._display_filename("\x00\x7f") == "attachment"
        assert app_module._size_label(100) == "100 B"
        assert app_module._size_label(2048) == "2.0 KB"
        assert app_module._size_label(2 * 1024 * 1024) == "2.0 MB"
        with pytest.raises(RuntimeError, match="Invalid stored"):
            app_module._attachment_path("wrong")

        app.extensions["last_reconcile_monotonic"] = 0
        values = iter([1000, 1000, 1001])
        monkeypatch.setattr(app_module.time, "monotonic", lambda: next(values))
        reconcile = Mock()
        monkeypatch.setattr(app_module, "reconcile_active_labels", reconcile)
        maybe_reconcile()
        reconcile.assert_called_once()


def test_reconciliation_handles_races_and_corrupt_events(client, app, monkeypatch):
    corrupt = create_task(client, "Corrupt history")
    active = create_task(client, "Active interval")
    client.patch(f"/api/tasks/{active['id']}", json={"status": "in_progress"})

    with app.app_context():
        db = get_db()
        db.execute(
            """
            INSERT INTO task_events(
                task_id, event_type, occurred_at_utc, local_date,
                counts_as_progress, details_json
            ) VALUES (?, 'status_changed', ?, ?, 0, ?)
            """,
            (
                corrupt["id"],
                "2026-09-20T10:00:00+00:00",
                "2026-09-20",
                "not-json",
            ),
        )
        db.commit()
        monkeypatch.setattr(
            app_module, "_interval_touches_local_day", lambda *_args: False
        )
        reconcile_active_labels()

        app.extensions["last_reconcile_monotonic"] = 0

        class RacingLock:
            def __enter__(self):
                app.extensions["last_reconcile_monotonic"] = 1000

            def __exit__(self, *_args):
                return False

        app.extensions["reconcile_lock"] = RacingLock()
        monkeypatch.setattr(app_module.time, "monotonic", lambda: 1000)
        reconcile = Mock()
        monkeypatch.setattr(app_module, "reconcile_active_labels", reconcile)
        maybe_reconcile()
        reconcile.assert_not_called()


def test_legacy_dependency_migration_is_idempotent(client, app):
    blocked = create_task(client, "Legacy parent")
    blocker = create_task(client, "Existing child")
    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE tasks SET parent_task_id = ? WHERE id = ?",
            (blocked["id"], blocker["id"]),
        )
        db.execute(
            """
            INSERT INTO task_dependencies(blocked_task_id, blocker_task_id)
            VALUES (?, ?)
            """,
            (blocked["id"], blocker["id"]),
        )
        assert migrate_legacy_subtasks(db) == 0
        assert db.execute(
            "SELECT parent_task_id FROM tasks WHERE id = ?", (blocker["id"],)
        ).fetchone()["parent_task_id"] is None


def test_error_handlers_and_invalid_index_queries(client):
    assert client.get("/missing").status_code == 404
    assert client.get("/api/missing").get_json() == {"error": "Not found"}
    response = client.get("/?view=invalid&expanded=bad&created=-1")
    assert response.status_code == 200
    assert b"Tasks" in response.data

    response = client.post("/tasks", data={"title": ""})
    assert response.status_code == 303
    assert "error=" in response.headers["Location"]


def test_attachment_error_and_download_paths(client, app, monkeypatch):
    task = create_task(client)
    response = client.post(f"/api/tasks/{task['id']}/attachments", data={})
    assert response.status_code == 400

    app.config["MAX_ATTACHMENT_BYTES"] = 2
    response = client.post(
        f"/api/tasks/{task['id']}/attachments",
        data={"attachments": (BytesIO(b"abc"), "big.txt")},
    )
    assert response.status_code == 400
    assert list(Path(app.config["ATTACHMENTS_DIR"]).iterdir()) == []

    app.config["MAX_ATTACHMENT_BYTES"] = 100
    response = client.post(
        f"/api/tasks/{task['id']}/attachments",
        data={"attachments": (BytesIO(b""), "empty.txt")},
    )
    assert response.status_code == 400

    response = client.post(
        f"/api/tasks/{task['id']}/attachments",
        data={"attachments": (BytesIO(b"data"), "file.txt")},
    )
    attachment = response.get_json()["attachments"][0]
    stored = next(Path(app.config["ATTACHMENTS_DIR"]).iterdir())
    stored.unlink()
    assert client.get(f"/api/attachments/{attachment['id']}").status_code == 404
    assert client.get("/api/attachments/999").status_code == 404

    response = client.delete(f"/api/tasks/{task['id']}/attachments/999")
    assert response.status_code == 404
    response = client.post(f"/tasks/{task['id']}/attachments/999/remove", data={})
    assert response.status_code == 303


def test_attachment_cleanup_before_and_after_persistence(client, app, monkeypatch):
    task = create_task(client, "Cleanup attachment")
    with app.app_context():
        upload = SimpleNamespace(
            filename="never-created.txt",
            mimetype="text/plain",
            stream=BytesIO(b"content"),
        )
        monkeypatch.setattr(
            app_module.uuid, "uuid4", Mock(side_effect=RuntimeError("uuid failed"))
        )
        with pytest.raises(RuntimeError, match="uuid failed"):
            app_module._save_attachments(
                get_db(), task["id"], [upload], app_module.utc_now()
            )
        assert list(Path(app.config["ATTACHMENTS_DIR"]).iterdir()) == []

    monkeypatch.undo()

    def fail_after_file_write(*_args, **_kwargs):
        raise ValueError("event persistence failed")

    monkeypatch.setattr(app_module, "_record_event", fail_after_file_write)
    response = client.post(
        f"/api/tasks/{task['id']}/attachments",
        data={"attachments": (BytesIO(b"saved then rolled back"), "rollback.txt")},
    )
    assert response.status_code == 400
    assert list(Path(app.config["ATTACHMENTS_DIR"]).iterdir()) == []
    with app.app_context():
        assert get_db().execute(
            "SELECT count(*) FROM task_attachments WHERE task_id = ?", (task["id"],)
        ).fetchone()[0] == 0


def test_task_creation_cleans_files_when_commit_fails(client, app, monkeypatch):
    connection = sqlite3.connect(app.config["DATABASE"])
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    class FailingCommitConnection:
        def __getattr__(self, name):
            return getattr(connection, name)

        def commit(self):
            raise RuntimeError("synthetic commit failure")

    monkeypatch.setattr(app_module, "get_db", lambda: FailingCommitConnection())
    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        client.post(
            "/api/tasks",
            data={
                "title": "Rollback task",
                "attachments": (BytesIO(b"temporary"), "temporary.txt"),
            },
        )

    assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    assert list(Path(app.config["ATTACHMENTS_DIR"]).iterdir()) == []
    connection.close()


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/api/tasks/1/comments", {"body": ""}),
        ("put", "/api/tasks/1/waiting", {"person_name": ""}),
        ("post", "/api/tasks/1/follow-ups", {}),
        ("post", "/api/tasks/1/waiting/resolve", {"extra": True}),
        ("post", "/api/tasks/1/labels", {"name": ""}),
        ("post", "/api/tasks/1/dependencies", {"blocker_task_id": "bad"}),
    ],
)
def test_task_command_rollbacks(client, method, path, payload):
    create_task(client)
    response = getattr(client, method)(path, json=payload)
    assert response.status_code == 400


def test_label_and_dependency_missing_api_and_form_paths(client):
    first = create_task(client, "First")
    second = create_task(client, "Second")
    assert client.delete(f"/api/tasks/{first['id']}/labels/999").status_code == 404
    assert client.post(f"/tasks/{first['id']}/labels/999/remove", data={}).status_code == 303
    assert client.delete(f"/api/tasks/{first['id']}/dependencies/{second['id']}").status_code == 404
    assert client.post(
        f"/tasks/{first['id']}/dependencies/{second['id']}/remove", data={}
    ).status_code == 303
    assert client.post(f"/tasks/{first['id']}/blocked-tasks", data={}).status_code == 303


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"after_task_id": None, "before_task_id": None, "extra": 1},
        {"after_task_id": None},
        {"after_task_id": True, "before_task_id": None},
        {"after_task_id": "bad", "before_task_id": None},
        {"after_task_id": 1, "before_task_id": None},
    ],
)
def test_rank_validation_paths(client, payload):
    task = create_task(client)
    response = client.patch(f"/api/tasks/{task['id']}/rank", json=payload)
    assert response.status_code == 400


def test_rank_stale_neighbor_paths(client):
    tasks = [create_task(client, value) for value in ("One", "Two", "Three")]
    moving, middle, last = tasks
    cases = [
        {"after_task_id": 999, "before_task_id": last["id"]},
        {"after_task_id": middle["id"], "before_task_id": 999},
        {"after_task_id": None, "before_task_id": middle["id"]},
        {"after_task_id": last["id"], "before_task_id": None},
        {"after_task_id": middle["id"], "before_task_id": last["id"]},
    ]
    for payload in cases:
        assert client.patch(f"/api/tasks/{moving['id']}/rank", json=payload).status_code == 400


def test_missing_task_and_ambiguous_rank_paths(client):
    first = create_task(client, "First")
    create_task(client, "Second")
    response = client.patch(
        f"/api/tasks/{first['id']}/rank",
        json={"after_task_id": None, "before_task_id": None},
    )
    assert response.status_code == 400
    assert "Choose where" in response.get_json()["error"]

    response = client.patch(
        "/api/tasks/999/rank",
        json={"after_task_id": None, "before_task_id": None},
    )
    assert response.status_code == 404


def test_profile_and_model_error_routes(client, app):
    assert client.post("/api/llm-profiles", json={"is_default": "yes"}).status_code == 400
    profile = create_profile(client)
    assert client.patch(
        f"/api/llm-profiles/{profile['id']}", json={"is_default": "yes"}
    ).status_code == 400
    assert client.patch(
        f"/api/llm-profiles/{profile['id']}", json={"unknown": True}
    ).status_code == 400

    class FailingProvider:
        def __init__(self, _config):
            pass

        def list_models(self):
            raise RuntimeError("model discovery failed")

    app.extensions["llm_provider_factory"] = FailingProvider
    response = client.get(f"/api/llm-profiles/{profile['id']}/models")
    assert response.status_code == 502
    assert response.get_json()["error"] == "model discovery failed"


def test_agent_folder_and_session_validation_routes(client):
    profile = create_profile(client)
    assert client.post("/api/agent/folders", json={"name": "x", "extra": 1}).status_code == 400
    folder = client.post("/api/agent/folders", json={"name": "Folder"}).get_json()["folder"]
    assert client.post("/api/agent/folders", json={"name": "Folder"}).status_code == 400
    assert client.delete("/api/agent/folders/999").status_code == 404

    assert client.post("/api/agent/sessions", json={"extra": 1}).status_code == 400
    session = client.post(
        "/api/agent/sessions", json={"profile_id": profile["id"], "title": "Session"}
    ).get_json()["session"]
    assert client.patch(
        f"/api/agent/sessions/{session['id']}", json={"extra": 1}
    ).status_code == 400
    assert client.patch(
        f"/api/agent/sessions/{session['id']}", json={"folder_id": True}
    ).status_code == 400
    assert client.patch(
        f"/api/agent/sessions/{session['id']}", json={"folder_id": folder["id"]}
    ).status_code == 200
    assert client.patch(
        f"/api/agent/sessions/{session['id']}", json={"folder_id": None}
    ).status_code == 200
    assert client.patch(
        f"/api/agent/sessions/{session['id']}", json={"folder_id": 999}
    ).status_code == 404
    assert client.post(
        f"/api/agent/sessions/{session['id']}/messages", json={"extra": 1}
    ).status_code == 400
    assert client.post("/api/agent/approvals/missing", json={"extra": 1}).status_code == 400
    assert client.post("/api/agent/runs/missing/resume", json={"extra": 1}).status_code == 400
    assert client.get("/api/agent/runs/missing").status_code == 404
    assert client.delete(f"/api/agent/folders/{folder['id']}").status_code == 204


def test_mutation_routes_roll_back_domain_failures(client):
    task = create_task(client, "Not waiting")
    assert client.post(f"/api/tasks/{task['id']}/waiting/resolve", json={}).status_code == 400
    assert client.delete("/api/tasks/999/labels/1").status_code == 404
    assert client.delete("/api/tasks/999/dependencies/1").status_code == 404
    assert client.post(
        f"/tasks/{task['id']}/blocked-tasks",
        data={"blocked_task_id": 999},
    ).status_code == 404

    assert client.post(
        "/api/llm-profiles",
        json={
            "name": "",
            "base_url": "http://localhost:1234/v1",
            "model": "model",
        },
    ).status_code == 400
    profile = create_profile(client)
    assert client.post(
        "/api/agent/sessions",
        json={"profile_id": profile["id"], "title": ""},
    ).status_code == 400


def test_agent_failure_outcomes_map_to_bad_gateway(client, monkeypatch):
    failed_run = AgentRun(
        id="failed-run",
        session_id="session",
        profile_id=1,
        status="failed",
        base_url="http://localhost:1234/v1",
        model="model",
        api_key_env=None,
        timeout_seconds=60,
        supports_tools=True,
        started_at="2026-09-20T10:00:00+00:00",
        completed_at="2026-09-20T10:00:01+00:00",
        error="synthetic failure",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
    )
    outcome = AgentOutcome(failed_run, (), None)

    class FailingRunner:
        def start(self, *_args, **_kwargs):
            return outcome

        def decide(self, *_args, **_kwargs):
            return outcome

        def resume(self, *_args, **_kwargs):
            return outcome

    monkeypatch.setattr(app_module, "_agent_runner", lambda _db: FailingRunner())
    for response in (
        client.post("/api/agent/sessions/session/messages", json={"content": "go"}),
        client.post("/api/agent/approvals/approval", json={"approved": True}),
        client.post("/api/agent/runs/run/resume", json={}),
    ):
        assert response.status_code == 502
        assert response.get_json()["error"] == "synthetic failure"

    completed = AgentOutcome(
        replace(failed_run, status="completed", error=None), (), "recovered"
    )

    class CompletedRunner:
        def resume(self, *_args, **_kwargs):
            return completed

    monkeypatch.setattr(app_module, "_agent_runner", lambda _db: CompletedRunner())
    response = client.post("/api/agent/runs/run/resume", json={})
    assert response.status_code == 200
    assert response.get_json()["latest_content"] == "recovered"


def test_browser_mutation_success_routes(client, app):
    task = create_task(client, "Browser task")
    task_id = task["id"]

    response = client.post(
        f"/tasks/{task_id}/attachments",
        data={"attachments": (BytesIO(b"hello"), "note.txt")},
    )
    assert response.status_code == 303
    assert "notice=attachments-added" in response.headers["Location"]
    with app.app_context():
        attachment_id = get_db().execute(
            "SELECT id FROM task_attachments WHERE task_id = ?", (task_id,)
        ).fetchone()["id"]
    response = client.post(
        f"/tasks/{task_id}/attachments/{attachment_id}/remove", data={}
    )
    assert response.status_code == 303
    assert "notice=attachment-removed" in response.headers["Location"]

    response = client.post(
        f"/tasks/{task_id}", data={"status": "done", "return_view": "invalid"}
    )
    assert response.status_code == 303
    assert "notice=task-completed" in response.headers["Location"]
    assert client.post(f"/tasks/{task_id}", data={"status": "todo"}).status_code == 303

    response = client.post(
        f"/tasks/{task_id}/waiting",
        data={"person_name": "Alex", "next_follow_up_on": "2099-01-01"},
    )
    assert response.status_code == 303
    assert "notice=waiting-saved" in response.headers["Location"]
    response = client.post(
        f"/tasks/{task_id}/follow-ups",
        data={"note": "Checked", "next_follow_up_on": "2099-01-02"},
    )
    assert response.status_code == 303
    assert "notice=follow-up-recorded" in response.headers["Location"]
    response = client.post(f"/tasks/{task_id}/waiting/resolve", data={})
    assert response.status_code == 303
    assert "notice=waiting-resolved" in response.headers["Location"]

    response = client.post(f"/tasks/{task_id}/labels", data={"name": "browser"})
    assert response.status_code == 303
    with app.app_context():
        label_id = get_db().execute(
            "SELECT id FROM labels WHERE name = 'browser'"
        ).fetchone()["id"]
    assert client.post(
        f"/tasks/{task_id}/labels/{label_id}/remove", data={}
    ).status_code == 303

    blocker = create_task(client, "Blocker")
    assert client.post(
        f"/tasks/{task_id}/dependencies",
        data={"blocker_task_id": blocker["id"]},
    ).status_code == 303
    assert client.post(
        f"/tasks/{task_id}/dependencies/{blocker['id']}/remove", data={}
    ).status_code == 303
    blocked = create_task(client, "Blocked")
    assert client.post(
        f"/tasks/{blocker['id']}/blocked-tasks",
        data={"blocked_task_id": blocked["id"]},
    ).status_code == 303
    assert client.post("/api/reconcile").status_code == 200


def test_payload_size_attachment_count_and_permission_errors(client, app, monkeypatch):
    task = create_task(client)
    app.config["MAX_ATTACHMENTS_PER_TASK"] = 0
    response = client.post(
        f"/api/tasks/{task['id']}/attachments",
        data={"attachments": (BytesIO(b"x"), "one.txt")},
    )
    assert response.status_code == 400
    assert "at most 0" in response.get_json()["error"]

    app.config["MAX_ATTACHMENTS_PER_TASK"] = 20
    response = client.post(
        f"/api/tasks/{task['id']}/attachments",
        data={"attachments": (BytesIO(b"data"), "locked.txt")},
    )
    attachment = response.get_json()["attachments"][0]
    original_unlink = app_module.Path.unlink

    def locked_unlink(path, *args, **kwargs):
        if path.name == attachment["stored_name"]:
            raise PermissionError("locked")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(app_module.Path, "unlink", locked_unlink)
    response = client.delete(
        f"/api/tasks/{task['id']}/attachments/{attachment['id']}"
    )
    assert response.status_code == 400
    assert "still in use" in response.get_json()["error"]

    app.config["MAX_CONTENT_LENGTH"] = 1
    assert client.post("/api/tasks", json={"title": "too large"}).status_code == 413
    response = client.post("/tasks", data={"title": "too large"})
    assert response.status_code == 303
    assert "Upload+is+too+large" in response.headers["Location"]


def test_rank_singleton_and_boundary_moves(client):
    singleton = create_task(client, "Only")
    response = client.patch(
        f"/api/tasks/{singleton['id']}/rank",
        json={"after_task_id": None, "before_task_id": None},
    )
    assert response.status_code == 200

    second = create_task(client, "Second")
    third = create_task(client, "Third")
    response = client.patch(
        f"/api/tasks/{singleton['id']}/rank",
        json={"after_task_id": None, "before_task_id": third["id"]},
    )
    assert response.status_code == 200
    response = client.patch(
        f"/api/tasks/{singleton['id']}/rank",
        json={"after_task_id": second["id"], "before_task_id": None},
    )
    assert response.status_code == 200


def test_json_and_positive_identifier_validation(client):
    assert client.post("/api/tasks", json=[]).status_code == 400
    assert client.post(
        "/api/agent/sessions", json={"profile_id": "not-an-id"}
    ).status_code == 400
    assert client.post(
        "/api/agent/sessions", json={"profile_id": 0}
    ).status_code == 400


def test_profile_discovery_and_listing_routes(client, app):
    profile = create_profile(client)
    response = client.patch(
        f"/api/llm-profiles/{profile['id']}", json={"model": "updated-model"}
    )
    assert response.status_code == 200
    assert response.get_json()["profile"]["model"] == "updated-model"
    assert client.get("/api/llm-profiles").status_code == 200
    assert client.get("/api/agent/folders").status_code == 200
    assert client.get("/api/agent/sessions").status_code == 200

    provider = Mock()
    provider.list_models.return_value = ["updated-model", "other"]
    app.extensions["llm_provider_factory"] = lambda _config: provider
    response = client.get(f"/api/llm-profiles/{profile['id']}/models")
    assert response.get_json() == {"models": ["updated-model", "other"]}


def test_corrupt_follow_up_history_and_redirect_fallback(client, app):
    task = create_task(client, "Waiting history")
    client.put(
        f"/api/tasks/{task['id']}/waiting",
        json={"person_name": "Ravi", "next_follow_up_on": None},
    )
    with app.app_context():
        db = get_db()
        db.execute(
            """
            INSERT INTO task_events(
                task_id, event_type, occurred_at_utc, local_date,
                counts_as_progress, details_json
            ) VALUES (?, 'followed_up', ?, ?, 1, ?)
            """,
            (
                task["id"],
                "2026-09-20T10:00:00+00:00",
                "2026-09-20",
                "not-json",
            ),
        )
        db.commit()
        items, _choices = load_tasks()
        assert items[0]["waiting"]["history"][0]["note"] == ""
        assert app_module._smart_state_order({"status": "done", "blocked": False}) == 3

    with app.test_request_context("/tasks/1", method="POST"):
        response = app_module._task_redirect(
            task["id"], "task-updated", view="unexpected"
        )
        assert "view=manage" in response.headers["Location"]
