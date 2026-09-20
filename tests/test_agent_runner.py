import json
import sqlite3
from datetime import datetime, timezone

import pytest

from agent_runner import AgentRunner
from agent_store import AgentStore
from app import create_app, get_db
from llm_profiles import LlmProfileStore
from llm_provider import CompletionResult, CompletionToolCall
from task_service import TaskService


NOW = datetime(2026, 9, 20, 11, 0, tzinfo=timezone.utc)


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, messages, *, tools):
        self.requests.append({"messages": messages, "tools": tools})
        return self.responses.pop(0)


def completion(content=None, *, calls=(), usage=None):
    return CompletionResult(
        content=content,
        tool_calls=tuple(calls),
        finish_reason="tool_calls" if calls else "stop",
        response_id="response-1",
        model="fake-model",
        usage=usage or {},
    )


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "test.sqlite3"),
            "USER_TIMEZONE": "Asia/Kolkata",
        }
    )
    with app.app_context():
        LlmProfileStore(get_db(), environ={}).create(
            {
                "name": "Test",
                "base_url": "http://localhost:1234/v1",
                "model": "fake-model",
            },
            now=NOW,
        )
        get_db().commit()
    return app


def create_session(db):
    profile = LlmProfileStore(db, environ={}).get()
    session = AgentStore(db).create_session(profile.id, now=NOW)
    db.commit()
    return session


def test_completes_plain_conversation_and_accounts_usage(app):
    with app.app_context():
        db = get_db()
        session = create_session(db)
        provider = FakeProvider(
            [
                completion(
                    "Start with the launch plan.",
                    usage={
                        "prompt_tokens": 11,
                        "completion_tokens": 6,
                        "total_tokens": 17,
                    },
                )
            ]
        )
        runner = AgentRunner(
            db,
            app.config["TZINFO"],
            environ={},
            provider_factory=lambda _: provider,
        )

        outcome = runner.start(session.id, "What should I do?", now=NOW)

        assert outcome.run.status == "completed"
        assert outcome.run.total_tokens == 17
        assert outcome.latest_content == "Start with the launch plan."
        assert AgentStore(db).messages(session.id) == [
            {"role": "user", "content": "What should I do?"},
            {"role": "assistant", "content": "Start with the launch plan."},
        ]
        assert provider.requests[0]["messages"][0]["role"] == "system"


def test_executes_read_tool_and_returns_result_to_model(app):
    with app.app_context():
        db = get_db()
        TaskService(db, app.config["TZINFO"]).create_task(
            title="Prepare launch", now=NOW
        )
        session = create_session(db)
        provider = FakeProvider(
            [
                completion(
                    calls=[
                        CompletionToolCall("call-1", "list_tasks", '{"status":"todo"}')
                    ]
                ),
                completion("Prepare launch is next."),
            ]
        )
        runner = AgentRunner(
            db,
            app.config["TZINFO"],
            environ={},
            provider_factory=lambda _: provider,
        )

        outcome = runner.start(session.id, "What is next?", now=NOW)

        assert outcome.run.status == "completed"
        assert len(provider.requests) == 2
        tool_message = provider.requests[1]["messages"][-1]
        tool_result = json.loads(tool_message["content"])
        assert tool_message["role"] == "tool"
        assert tool_result["ok"] is True
        assert tool_result["result"][0]["title"] == "Prepare launch"


def test_commits_tool_calls_and_each_response_while_run_is_active(app):
    with app.app_context():
        db = get_db()
        session = create_session(db)
        provider = FakeProvider(
            [
                completion(
                    calls=[
                        CompletionToolCall("call-1", "list_tasks", "{}"),
                        CompletionToolCall("call-2", "list_tasks", "{}"),
                    ]
                ),
                completion("Finished both lookups."),
            ]
        )
        runner = AgentRunner(
            db,
            app.config["TZINFO"],
            environ={},
            provider_factory=lambda _: provider,
        )
        observed_roles = []
        execute = runner.tools.execute

        def observing_execute(name, arguments, *, now=None):
            with sqlite3.connect(app.config["DATABASE"]) as observer:
                rows = observer.execute(
                    "SELECT role FROM agent_messages WHERE session_id = ? ORDER BY id",
                    (session.id,),
                ).fetchall()
            observed_roles.append([row[0] for row in rows])
            return execute(name, arguments, now=now)

        runner.tools.execute = observing_execute

        outcome = runner.start(session.id, "Check twice", now=NOW)

        assert outcome.run.status == "completed"
        assert observed_roles == [
            ["user", "assistant"],
            ["user", "assistant", "tool"],
        ]


def test_endpoint_without_tool_support_still_provides_chat(app):
    with app.app_context():
        db = get_db()
        profile = LlmProfileStore(db, environ={}).get()
        LlmProfileStore(db, environ={}).update(
            profile.id, {"supports_tools": False}, now=NOW
        )
        db.commit()
        session = create_session(db)
        provider = FakeProvider([completion("I can still help you think it through.")])
        runner = AgentRunner(
            db,
            app.config["TZINFO"],
            environ={},
            provider_factory=lambda _: provider,
        )

        outcome = runner.start(session.id, "Help me plan", now=NOW)

        assert outcome.run.status == "completed"
        assert provider.requests[0]["tools"] == ()


def test_mutation_pauses_then_approval_executes_and_resumes(app):
    with app.app_context():
        db = get_db()
        session = create_session(db)
        provider = FakeProvider(
            [
                completion(
                    calls=[
                        CompletionToolCall(
                            "call-create", "create_task", '{"title":"Book venue"}'
                        )
                    ]
                ),
                completion("Book venue was added."),
            ]
        )
        runner = AgentRunner(
            db,
            app.config["TZINFO"],
            environ={},
            provider_factory=lambda _: provider,
        )

        paused = runner.start(session.id, "Add a task to book the venue", now=NOW)

        assert paused.run.status == "waiting_approval"
        assert len(paused.pending_approvals) == 1
        assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0

        completed = runner.decide(paused.pending_approvals[0].id, True, now=NOW)

        assert completed.run.status == "completed"
        assert db.execute("SELECT title FROM tasks").fetchone()["title"] == "Book venue"
        tool_result = json.loads(provider.requests[1]["messages"][-1]["content"])
        assert tool_result["ok"] is True


def test_parallel_mutations_wait_for_every_decision_before_execution(app):
    with app.app_context():
        db = get_db()
        session = create_session(db)
        provider = FakeProvider(
            [
                completion(
                    calls=[
                        CompletionToolCall(
                            "call-one", "create_task", '{"title":"First task"}'
                        ),
                        CompletionToolCall(
                            "call-two", "create_task", '{"title":"Second task"}'
                        ),
                    ]
                ),
                completion("Both tasks were added."),
            ]
        )
        runner = AgentRunner(
            db,
            app.config["TZINFO"],
            environ={},
            provider_factory=lambda _: provider,
        )

        paused = runner.start(session.id, "Add both tasks", now=NOW)

        assert len(paused.pending_approvals) == 2
        still_waiting = runner.decide(
            paused.pending_approvals[0].id, True, now=NOW
        )
        assert still_waiting.run.status == "waiting_approval"
        assert len(still_waiting.pending_approvals) == 1
        assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0

        completed = runner.decide(
            still_waiting.pending_approvals[0].id, True, now=NOW
        )

        assert completed.run.status == "completed"
        assert [
            row["title"]
            for row in db.execute("SELECT title FROM tasks ORDER BY id").fetchall()
        ] == ["First task", "Second task"]


def test_paused_run_keeps_its_endpoint_snapshot_when_profile_changes(app):
    with app.app_context():
        db = get_db()
        session = create_session(db)
        provider = FakeProvider(
            [
                completion(
                    calls=[
                        CompletionToolCall(
                            "call-create", "create_task", '{"title":"Stable config"}'
                        )
                    ]
                ),
                completion("Added with the original run configuration."),
            ]
        )
        configs = []

        def provider_factory(config):
            configs.append(config)
            return provider

        runner = AgentRunner(
            db,
            app.config["TZINFO"],
            environ={},
            provider_factory=provider_factory,
        )
        paused = runner.start(session.id, "Add it", now=NOW)
        profile = LlmProfileStore(db, environ={}).get()
        LlmProfileStore(db, environ={}).update(
            profile.id,
            {
                "base_url": "https://changed.example.test/v1",
                "model": "changed-model",
            },
            now=NOW,
        )
        db.commit()

        runner.decide(paused.pending_approvals[0].id, True, now=NOW)

        assert [config.base_url for config in configs] == [
            "http://localhost:1234/v1",
            "http://localhost:1234/v1",
        ]
        assert [config.model for config in configs] == ["fake-model", "fake-model"]


def test_rejection_returns_tool_result_without_mutating(app):
    with app.app_context():
        db = get_db()
        session = create_session(db)
        provider = FakeProvider(
            [
                completion(
                    calls=[
                        CompletionToolCall(
                            "call-create", "create_task", '{"title":"Delete nothing"}'
                        )
                    ]
                ),
                completion("Okay, I did not add it."),
            ]
        )
        runner = AgentRunner(
            db,
            app.config["TZINFO"],
            environ={},
            provider_factory=lambda _: provider,
        )
        paused = runner.start(session.id, "Add a task", now=NOW)

        outcome = runner.decide(paused.pending_approvals[0].id, False, now=NOW)

        assert outcome.run.status == "completed"
        assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        result = json.loads(provider.requests[1]["messages"][-1]["content"])
        assert result == {"ok": False, "error": "User rejected this change"}


def test_resume_consumes_a_decision_saved_before_interruption(app):
    with app.app_context():
        db = get_db()
        session = create_session(db)
        provider = FakeProvider(
            [
                completion(
                    calls=[
                        CompletionToolCall(
                            "call-create", "create_task", '{"title":"Recovered task"}'
                        )
                    ]
                ),
                completion("Recovered task was added."),
            ]
        )
        runner = AgentRunner(
            db,
            app.config["TZINFO"],
            environ={},
            provider_factory=lambda _: provider,
        )
        paused = runner.start(session.id, "Add a task", now=NOW)
        approval = paused.pending_approvals[0]
        AgentStore(db).decide_approval(approval.id, True, now=NOW)
        db.commit()

        outcome = runner.resume(paused.run.id, now=NOW)

        assert outcome.run.status == "completed"
        assert db.execute("SELECT title FROM tasks").fetchone()["title"] == "Recovered task"


def test_provider_failure_is_persisted_without_exposing_secret(app):
    class FailingProvider:
        def complete(self, messages, *, tools):
            raise RuntimeError("request failed with secret-value")

    with app.app_context():
        db = get_db()
        profile = LlmProfileStore(db, environ={}).get()
        LlmProfileStore(db, environ={}).update(
            profile.id, {"api_key_env": "TEST_KEY"}, now=NOW
        )
        db.commit()
        session = create_session(db)
        runner = AgentRunner(
            db,
            app.config["TZINFO"],
            environ={"TEST_KEY": "secret-value"},
            provider_factory=lambda _: FailingProvider(),
        )

        outcome = runner.start(session.id, "Help", now=NOW)

        assert outcome.run.status == "failed"
        assert outcome.run.error == "request failed with ***"
