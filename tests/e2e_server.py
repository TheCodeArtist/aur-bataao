from __future__ import annotations

import tempfile
from pathlib import Path

from flask import jsonify
from waitress import serve

from agent_store import AgentStore
from app import create_app, get_db
from llm_profiles import LlmProfileStore
from llm_provider import CompletionResult, CompletionToolCall


class BrowserTestProvider:
    """Deterministic provider used only by the isolated browser-test server."""

    def complete(self, messages, *, tools):
        del tools
        if messages and messages[-1]["role"] == "tool":
            return CompletionResult(
                "The browser-agent task was added.", (), "stop", 2, 3, {}
            )
        user_message = next(
            (
                message.get("content", "")
                for message in reversed(messages)
                if message.get("role") == "user"
            ),
            "",
        )
        if "create" in user_message.lower():
            return CompletionResult(
                None,
                (
                    CompletionToolCall(
                        "browser-create",
                        "create_task",
                        '{"title":"Browser agent task"}',
                    ),
                ),
                "tool_calls",
                2,
                1,
                {},
            )
        return CompletionResult(
            "The browser test agent is ready.", (), "stop", 2, 3, {}
        )

    def list_models(self):
        return ["browser-test-model"]


runtime = tempfile.TemporaryDirectory(prefix="aur-bataao-e2e-")
runtime_path = Path(runtime.name)
attachments_path = runtime_path / "attachments"
app = create_app(
    {
        "TESTING": True,
        "DATABASE": str(runtime_path / "browser.sqlite3"),
        "ATTACHMENTS_DIR": str(attachments_path),
        "USER_TIMEZONE": "Asia/Kolkata",
        "LLM_MODEL": "",
    },
    environ={},
)
app.extensions["llm_provider_factory"] = lambda _config: BrowserTestProvider()


def reset_state() -> None:
    db = get_db()
    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")
    tables = [
        row["name"]
        for row in db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    for table in tables:
        db.execute(f'DELETE FROM "{table}"')
    db.execute("DELETE FROM sqlite_sequence")
    db.commit()
    db.execute("PRAGMA foreign_keys = ON")
    for path in attachments_path.iterdir():
        if path.is_file():
            path.unlink()
    LlmProfileStore(db, environ={}).create(
        {
            "name": "Browser test endpoint",
            "base_url": "http://browser.invalid/v1",
            "model": "browser-test-model",
            "supports_tools": True,
        },
        make_default=True,
    )
    db.commit()


@app.post("/__e2e__/reset")
def reset_for_browser_test():
    reset_state()
    return jsonify(ok=True)


@app.post("/__e2e__/profiles/clear")
def clear_profiles_for_browser_test():
    db = get_db()
    db.execute("DELETE FROM llm_profiles")
    db.commit()
    return jsonify(ok=True)


@app.post("/__e2e__/agent/interrupted")
def seed_interrupted_agent_run():
    db = get_db()
    profile = LlmProfileStore(db, environ={}).get()
    store = AgentStore(db)
    session = store.create_session(profile.id, title="Interrupted approval")
    run = store.create_run(session.id)
    store.append_message(
        session.id,
        {"role": "user", "content": "Create the recovered browser task"},
        run_id=run.id,
    )
    store.append_message(
        session.id,
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "resume-create",
                    "type": "function",
                    "function": {
                        "name": "create_task",
                        "arguments": '{"title":"Recovered browser task"}',
                    },
                }
            ],
        },
        run_id=run.id,
    )
    approval = store.request_approval(
        run.id,
        tool_call_id="resume-create",
        tool_name="create_task",
        arguments={"title": "Recovered browser task"},
    )
    store.decide_approval(approval.id, True)
    db.commit()
    return jsonify(session_id=session.id, run_id=run.id)


@app.post("/__e2e__/agent/messages")
def seed_agent_messages():
    db = get_db()
    profile = LlmProfileStore(db, environ={}).get()
    store = AgentStore(db)
    session = store.create_session(profile.id, title="Message rendering")
    run = store.create_run(session.id)
    messages = [
        {"role": "user", "content": "Show message variants"},
        {
            "role": "assistant",
            "content": "Rendered **assistant** message",
            "tool_calls": [
                {
                    "id": "paired-call",
                    "type": "function",
                    "function": {"name": "list_tasks", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "paired-call", "content": '{"tasks":[]}'},
        {"role": "tool", "tool_call_id": "orphan-call", "content": "orphan result"},
        {"role": "assistant", "content": "   "},
    ]
    for message in messages:
        store.append_message(session.id, message, run_id=run.id)
    store.transition_run(run.id, "completed")
    db.commit()
    return jsonify(session_id=session.id)


with app.app_context():
    reset_state()


if __name__ == "__main__":
    serve(app, host="127.0.0.1", port=4173, threads=16)
