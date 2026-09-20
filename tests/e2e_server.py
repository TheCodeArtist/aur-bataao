from __future__ import annotations

import tempfile
from pathlib import Path

from flask import jsonify
from waitress import serve

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


with app.app_context():
    reset_state()


if __name__ == "__main__":
    serve(app, host="127.0.0.1", port=4173, threads=16)
