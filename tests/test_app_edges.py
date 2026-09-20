from __future__ import annotations

from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

import pytest

import app as app_module
from app import create_app, get_db, maybe_reconcile


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
    assert client.post(
        f"/api/agent/sessions/{session['id']}/messages", json={"extra": 1}
    ).status_code == 400
    assert client.post("/api/agent/approvals/missing", json={"extra": 1}).status_code == 400
    assert client.post("/api/agent/runs/missing/resume", json={"extra": 1}).status_code == 400
    assert client.get("/api/agent/runs/missing").status_code == 404
    assert client.delete(f"/api/agent/folders/{folder['id']}").status_code == 204


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
