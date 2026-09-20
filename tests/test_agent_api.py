from pathlib import Path

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
    assert all(message["created_at"].endswith("+00:00") for message in detail["messages"])
    assert all(isinstance(message["message_id"], int) for message in detail["messages"])
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
        },
        environ={},
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


def test_agent_session_can_be_moved_between_folders(client):
    profile = create_profile(client)
    session = client.post(
        "/api/agent/sessions", json={"profile_id": profile["id"]}
    ).get_json()["session"]

    created = client.post("/api/agent/folders", json={"name": "Planning"})
    assert created.status_code == 201
    folder = created.get_json()["folder"]
    assert client.get("/api/agent/folders").get_json()["folders"] == [folder]

    moved = client.patch(
        f"/api/agent/sessions/{session['id']}", json={"folder_id": folder["id"]}
    )
    assert moved.status_code == 200
    assert moved.get_json()["session"]["folder_id"] == folder["id"]

    removed = client.delete(f"/api/agent/folders/{folder['id']}")
    assert removed.status_code == 204
    detail = client.get(f"/api/agent/sessions/{session['id']}").get_json()
    assert detail["session"]["folder_id"] is None


def test_agent_workspace_is_linked_from_the_task_app(client):
    workspace = client.get("/?view=agent")
    tasks = client.get("/")

    assert workspace.status_code == 200
    assert client.get("/agent").status_code == 404
    assert workspace.data.count(b'class="app-header"') == 1
    assert workspace.data.count(b'id="theme-toggle"') == 1
    assert b'id="profile-form"' in workspace.data
    assert b'id="approval-list"' in workspace.data
    assert b'id="width-toggle"' in workspace.data
    assert b'id="profile-model"' in workspace.data
    assert b'role="combobox"' in workspace.data
    assert b'id="model-options"' in workspace.data
    assert b'role="listbox"' in workspace.data
    assert b'id="model-discovery-status"' in workspace.data
    assert b'>Discover</button>' in workspace.data
    assert workspace.data.index(b'id="profile-api-key-env"') < workspace.data.index(b'id="profile-model"')
    assert b'<span>Endpoint supports tool calling</span>' in workspace.data
    assert b'<span>Use as default</span>' in workspace.data
    assert b'id="focus-view-link"' in workspace.data
    assert b'id="manage-view-link"' in workspace.data
    assert b'id="agent-view-link"' in workspace.data
    assert b'chat-bubble-icon' in workspace.data
    assert b'aria-label="Agent" aria-current="page"' in workspace.data
    assert workspace.data.count(b'class="app-view-link-prompt"') == 3
    assert b'class="app-view-prompt-current">Agent</span>' in workspace.data
    assert workspace.data.index(b'class="app-view-nav"') < workspace.data.index(b'class="header-preferences"')
    assert workspace.data.index(b'class="header-preferences"') < workspace.data.index(b'id="theme-toggle"')
    assert workspace.data.index(b'class="conversation-header"') < workspace.data.index(b'id="width-toggle"')
    assert b'href="/?view=agent"' in tasks.data
    assert b'id="task-workspace"' in workspace.data
    assert b'id="agent-workspace"' in workspace.data
    assert b'id="agent-workspace" class="agent-workspace app-view-panel" aria-label="Agent">' in workspace.data
    assert b'id="task-workspace" class="task-workspace app-view-panel" aria-label="Tasks" hidden inert' in workspace.data


def test_agent_workspace_styles_width_mode_and_checkbox_rows():
    static_dir = Path(__file__).parents[1] / "static"
    styles = (static_dir / "agent.css").read_text()
    controls = (static_dir / "controls.css").read_text()

    assert '.agent-layout[data-width="full"]' in styles
    assert ".agent-header" not in styles
    assert ".agent-workspace" in styles
    assert ".app-header .app-view-nav" in controls
    assert ".agent-header .app-view-nav" not in controls
    assert "transform: translateX(-50%);" in controls
    assert '.agent-layout[data-width="full"] .message-list' in styles
    assert '.agent-layout[data-width="full"] .agent-message {' in styles
    assert '.agent-layout[data-width="full"] .agent-message.assistant' in styles
    assert "width: calc(100% - 32px);" in styles
    assert "max-width: none;" in styles
    assert '.agent-layout[data-width="full"] .agent-message.user' in styles
    assert "max-width: 88%;" in styles
    assert '.agent-layout[data-width="full"] .agent-message.tool[open]' in styles
    assert '#profile-form input:not([type="checkbox"])' in styles
    assert "#profile-form input," not in styles
    check_rule = styles.split(".check {", 1)[1].split("}", 1)[0]
    checkbox_rule = styles.split('.check input[type="checkbox"] {', 1)[1].split("}", 1)[0]
    assert "display: flex;" in check_rule
    assert "align-items: center;" in check_rule
    assert "margin: 0;" in checkbox_rule
    assert "flex: 0 0 auto;" in checkbox_rule


def test_agent_messages_use_safe_markdown_rendering_for_both_roles():
    static_dir = Path(__file__).parents[1] / "static"
    script = (static_dir / "agent.js").read_text()
    markdown = (static_dir / "markdown.js").read_text()
    styles = (static_dir / "agent.css").read_text()

    assert 'import { renderMarkdown } from "./markdown.js";' in script
    assert script.count("renderMarkdown(item,") == 3
    assert "target.replaceChildren();" in markdown
    assert "target.innerHTML" not in markdown
    assert 'SAFE_LINK_PROTOCOLS = new Set(["http:", "https:", "mailto:"])' in markdown
    assert ".markdown-body pre" in styles
    assert ".markdown-body table" in styles


def test_agent_tool_calls_render_as_live_collapsible_pairs():
    static_dir = Path(__file__).parents[1] / "static"
    script = (static_dir / "agent.js").read_text()
    styles = (static_dir / "agent.css").read_text()

    assert "function createToolCall(call, runStatus)" in script
    assert "`Running ${name}" in script
    assert "`Ran ${view.name}`" in script
    assert 'responseValue.textContent = "Awaiting tool response' in script
    assert "apiWithSessionPolling" in script
    assert ".tool-details" in styles


def test_agent_lifecycle_badges_and_task_invalidation_are_shell_integrated():
    static_dir = Path(__file__).parents[1] / "static"
    agent_script = (static_dir / "agent.js").read_text()
    app_script = (static_dir / "app.js").read_text()
    controls = (static_dir / "controls.css").read_text()

    assert "function initializeAgent()" in agent_script
    assert "if (agentIsVisible()) enterAgentView();" in agent_script
    assert not agent_script.rstrip().endswith("initialize();")
    assert 'window.addEventListener("app:viewchange"' in agent_script
    assert "themeToggle" not in agent_script
    assert "normalUpdate: false" in agent_script
    assert "warningSources: new Set()" in agent_script
    assert 'agentUpdateBadge.dataset.kind = warning ? "warning" : "normal"' in agent_script
    assert "function clearAgentBadges()" in agent_script
    assert 'new CustomEvent("agent:task-mutation-start")' in agent_script
    assert 'new CustomEvent("agent:tasks-mutated")' in agent_script
    assert 'window.addEventListener("agent:tasks-mutated"' in app_script
    assert "tasksStale = true" in app_script
    assert '.agent-update-badge[data-kind="warning"]' in controls
