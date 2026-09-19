import pytest

from app import create_app, get_db
from llm_provider import CompletionResult, CompletionToolCall


class ScriptedProvider:
    def __init__(self, responses=(), models=()):
        self.responses = list(responses)
        self.models = tuple(models)
        self.requests = []

    def complete(self, messages, *, tools):
        self.requests.append({"messages": messages, "tools": tools})
        return self.responses.pop(0)

    def list_models(self):
        return self.models


def completion(content=None, *, calls=()):
    return CompletionResult(
        content=content,
        tool_calls=tuple(calls),
        finish_reason="tool_calls" if calls else "stop",
        response_id="response-api",
        model="fake-model",
        usage={},
    )


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "test.sqlite3"),
            "USER_TIMEZONE": "Asia/Kolkata",
            "LLM_MODEL": "",
        }
    )


@pytest.fixture()
def client(app):
    return app.test_client()


def create_profile(client, **overrides):
    values = {
        "name": "Local",
        "base_url": "http://localhost:1234/v1",
        "model": "local-model",
    }
    values.update(overrides)
    response = client.post("/api/llm-profiles", json=values)
    assert response.status_code == 201
    return response.get_json()["profile"]


def test_profile_api_configures_default_without_accepting_secrets(client, app):
    profile = create_profile(client, api_key_env="REMOTE_TEST_KEY")

    assert profile["is_default"] is True
    assert profile["api_key_configured"] is False
    assert "api_key" not in profile
    assert client.get("/api/llm-profiles").get_json()["profiles"] == [profile]

    rejected = client.patch(
        f"/api/llm-profiles/{profile['id']}", json={"api_key": "secret"}
    )
    assert rejected.status_code == 400
    assert "Unsupported field" in rejected.get_json()["error"]

    with app.app_context():
        columns = {
            row[1]
            for row in get_db().execute("PRAGMA table_info(llm_profiles)").fetchall()
        }
        assert "api_key" not in columns


def test_saved_profile_can_discover_endpoint_models(client, app):
    profile = create_profile(client)
    provider = ScriptedProvider(models=("model-b", "model-a"))
    app.extensions["llm_provider_factory"] = lambda config: provider

    response = client.get(f"/api/llm-profiles/{profile['id']}/models")

    assert response.status_code == 200
    assert response.get_json() == {"models": ["model-b", "model-a"]}


def test_agent_api_pauses_for_approval_then_resumes(client, app):
    profile = create_profile(client)
    session_response = client.post(
        "/api/agent/sessions",
        json={"profile_id": profile["id"], "title": "Launch planning"},
    )
    assert session_response.status_code == 201
    session = session_response.get_json()["session"]
    provider = ScriptedProvider(
        (
            completion(
                calls=(
                    CompletionToolCall(
                        "call-create", "create_task", '{"title":"Book venue"}'
                    ),
                )
            ),
            completion("Book venue was added."),
        )
    )
    app.extensions["llm_provider_factory"] = lambda config: provider

    paused = client.post(
        f"/api/agent/sessions/{session['id']}/messages",
        json={"content": "Add a task to book the venue"},
    )

    assert paused.status_code == 202
    paused_body = paused.get_json()
    approval = paused_body["pending_approvals"][0]
    assert approval["tool_name"] == "create_task"
    assert approval["arguments"] == {"title": "Book venue"}
    with app.app_context():
        assert get_db().execute("SELECT count(*) FROM tasks").fetchone()[0] == 0

    completed = client.post(
        f"/api/agent/approvals/{approval['id']}", json={"approved": True}
    )

    assert completed.status_code == 200
    completed_body = completed.get_json()
    assert completed_body["run"]["status"] == "completed"
    assert completed_body["latest_content"] == "Book venue was added."
    detail = client.get(f"/api/agent/sessions/{session['id']}").get_json()
    assert [message["role"] for message in detail["messages"]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    run_detail = client.get(
        f"/api/agent/runs/{completed_body['run']['id']}"
    ).get_json()
    assert run_detail["approvals"][0]["status"] == "executed"
    assert any(
        event["event_type"] == "approval_approved"
        for event in run_detail["events"]
    )


def test_environment_configuration_seeds_first_profile(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "seed.sqlite3"),
            "USER_TIMEZONE": "Asia/Kolkata",
            "LLM_BASE_URL": "https://llm.example.test/v1",
            "LLM_MODEL": "configured-model",
            "LLM_API_KEY_ENV": "CONFIGURED_LLM_KEY",
        }
    )

    response = app.test_client().get("/api/llm-profiles")

    assert response.status_code == 200
    profile = response.get_json()["profiles"][0]
    assert profile["api_key_configured"] is False
    assert profile["name"] == "Environment default"
    assert profile["base_url"] == "https://llm.example.test/v1"
    assert profile["model"] == "configured-model"


def test_agent_session_requires_a_configured_profile(client):
    response = client.post("/api/agent/sessions", json={})

    assert response.status_code == 404
    assert response.get_json() == {"error": "Not found"}
