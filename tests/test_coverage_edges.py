from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

import pytest

from agent_runner import AgentRunner, _message_text, _safe_error, _tool_arguments
from agent_store import AgentNotFoundError, AgentStore, _validated_usage
from agent_tools import TaskToolRegistry
from app import get_db
from llm_profiles import LlmProfileNotFoundError, LlmProfileStore
from llm_provider import (
    ChatCompletionsProvider,
    CompletionResult,
    CompletionToolCall,
    LlmConfig,
    LlmConfigurationError,
    LlmProtocolError,
    _parse_usage,
)
from task_service import (
    SQLITE_INTEGER_MIN,
    TaskService,
    _create_request_id,
    _person_name,
    _valid_follow_up_date,
    _valid_follow_up_time,
    _waiting_note,
    new_task_rank_key,
    valid_due_date,
)


NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def create_profile(db, **overrides):
    values = {
        "name": "Test",
        "base_url": "http://localhost:1234/v1",
        "model": "fake-model",
    }
    values.update(overrides)
    profile = LlmProfileStore(db, environ={}).create(values, now=NOW)
    db.commit()
    return profile


def create_session(db):
    profile = create_profile(db)
    session = AgentStore(db).create_session(profile.id, now=NOW)
    db.commit()
    return session


class Provider:
    def __init__(self, responses):
        self.responses = list(responses)

    def complete(self, _messages, *, tools):
        return self.responses.pop(0)


def completion(content=None, calls=()):
    return CompletionResult(content, tuple(calls), "stop", None, None, {})


def test_agent_runner_validation_and_failure_edges(app):
    with app.app_context():
        db = get_db()
        session = create_session(db)
        with pytest.raises(ValueError, match="max_steps"):
            AgentRunner(db, app.config["TZINFO"], max_steps=0)

        runner = AgentRunner(db, app.config["TZINFO"], environ={})
        with pytest.raises(ValueError, match="boolean"):
            runner.decide("unused", 1)
        with pytest.raises(ValueError, match="Message"):
            runner.start(session.id, " ")

        provider = Provider([completion()])
        runner = AgentRunner(
            db,
            app.config["TZINFO"],
            environ={},
            provider_factory=lambda _: provider,
        )
        assert runner.start(session.id, "empty response", now=NOW).run.error == (
            "The model returned neither text nor tool calls"
        )

        session = AgentStore(db).create_session(create_profile(db, name="Second").id, now=NOW)
        db.commit()
        runner = AgentRunner(
            db,
            app.config["TZINFO"],
            environ={},
            provider_factory=lambda _: (_ for _ in ()).throw(RuntimeError("factory")),
        )
        assert runner.start(session.id, "factory failure", now=NOW).run.error == "factory"


def test_agent_runner_tool_errors_and_step_limit(app):
    with app.app_context():
        db = get_db()
        session = create_session(db)
        calls = (
            CompletionToolCall("bad-json", "list_tasks", "{"),
            CompletionToolCall("not-object", "list_tasks", "[]"),
            CompletionToolCall("unknown", "missing_tool", "{}"),
            CompletionToolCall("missing-task", "get_task", '{"task_id":999}'),
        )
        provider = Provider([completion(calls=calls), completion("handled")])
        runner = AgentRunner(
            db,
            app.config["TZINFO"],
            environ={},
            provider_factory=lambda _: provider,
        )
        outcome = runner.start(session.id, "exercise errors", now=NOW)
        assert outcome.run.status == "completed"
        messages = AgentStore(db).messages(session.id)
        errors = [message["content"] for message in messages if message["role"] == "tool"]
        assert len(errors) == 4
        assert any("valid JSON" in error for error in errors)
        assert any("Unknown tool" in error for error in errors)

        session = AgentStore(db).create_session(
            LlmProfileStore(db, environ={}).get().id, now=NOW
        )
        db.commit()
        provider = Provider([completion(calls=(CompletionToolCall("read", "list_tasks", "{}"),))])
        runner = AgentRunner(
            db,
            app.config["TZINFO"],
            environ={},
            provider_factory=lambda _: provider,
            max_steps=1,
        )
        assert "step limit" in runner.start(session.id, "loop", now=NOW).run.error


def test_agent_runner_resume_and_helper_edges(app, monkeypatch):
    with app.app_context():
        db = get_db()
        session = create_session(db)
        store = AgentStore(db)
        run = store.create_run(session.id, now=NOW)
        store.transition_run(run.id, "completed", now=NOW)
        db.commit()
        runner = AgentRunner(db, app.config["TZINFO"], environ={})
        with pytest.raises(ValueError, match="not waiting"):
            runner.resume(run.id)

        profile = LlmProfileStore(db, environ={}).get()
        LlmProfileStore(db, environ={}).update(profile.id, {"api_key_env": "KEY"}, now=NOW)
        db.commit()
        session = store.create_session(profile.id, now=NOW)
        db.commit()
        failed = AgentRunner(db, app.config["TZINFO"], environ={}).start(session.id, "key", now=NOW)
        assert "KEY" in failed.run.error

        monkeypatch.setenv("KEY", "secret")
        runner = AgentRunner(db, app.config["TZINFO"], environ=None)
        assert runner._run_config(store.run(failed.run.id)).api_key == "secret"

    for value in (None, "", " " * 3, "x" * 8001):
        with pytest.raises(ValueError):
            _message_text(value)
    assert _tool_arguments(CompletionToolCall("1", "x", "[]"))[1]
    assert _safe_error(RuntimeError()) == "RuntimeError"
    assert _safe_error(RuntimeError("secret"), "secret") == "***"


def test_agent_runner_preserves_terminal_state_during_races(app):
    with app.app_context():
        db = get_db()
        profile = create_profile(db)
        store = AgentStore(db)
        runner = AgentRunner(db, app.config["TZINFO"], environ={})

        session = store.create_session(profile.id, now=NOW)
        run = store.create_run(session.id, now=NOW)
        approval = store.request_approval(
            run.id,
            tool_call_id="cancelled-call",
            tool_name="create_task",
            arguments={"title": "Never created"},
            now=NOW,
        )
        store.transition_run(run.id, "cancelled", now=NOW)
        db.commit()
        with pytest.raises(ValueError, match="not waiting"):
            runner.decide(approval.id, True, now=NOW)

        session = store.create_session(profile.id, now=NOW)
        run = store.create_run(session.id, now=NOW)
        approval = store.request_approval(
            run.id,
            tool_call_id="consumed-call",
            tool_name="create_task",
            arguments={"title": "Already consumed"},
            now=NOW,
        )
        store.decide_approval(approval.id, True, now=NOW)
        store.mark_approval_consumed(approval.id, now=NOW)
        db.commit()
        with pytest.raises(ValueError, match="no decided approvals"):
            runner.resume(run.id, now=NOW)

        session = store.create_session(profile.id, now=NOW)

        def cancelling_factory(_config):
            active = store.runs_for_session(session.id)[0]
            store.transition_run(active.id, "cancelled", now=NOW)
            db.commit()
            return Provider([])

        outcome = AgentRunner(
            db,
            app.config["TZINFO"],
            environ={},
            provider_factory=cancelling_factory,
        ).start(session.id, "Cancel before completion", now=NOW)
        assert outcome.run.status == "cancelled"

        session = store.create_session(profile.id, now=NOW)

        def cancelling_failure(_config):
            active = store.runs_for_session(session.id)[0]
            store.transition_run(active.id, "cancelled", now=NOW)
            db.commit()
            raise RuntimeError("late provider failure")

        outcome = AgentRunner(
            db,
            app.config["TZINFO"],
            environ={},
            provider_factory=cancelling_failure,
        ).start(session.id, "Preserve cancellation", now=NOW)
        assert outcome.run.status == "cancelled"
        assert outcome.run.error is None


def test_agent_store_validation_and_not_found_edges(app):
    with app.app_context():
        db = get_db()
        profile = create_profile(db)
        store = AgentStore(db)
        session = store.create_session(profile.id, title="  Session  ", now=NOW)
        assert store.list_sessions() == [session]
        with pytest.raises(AgentNotFoundError):
            store.folder(999)
        with pytest.raises(AgentNotFoundError):
            store.run("missing")
        with pytest.raises(AgentNotFoundError):
            store.approval("missing")

        folder = store.create_folder("Folder", now=NOW)
        with pytest.raises(ValueError, match="Folder name"):
            store.create_folder(" ", now=NOW)
        with pytest.raises(ValueError, match="already exists"):
            store.create_folder("Folder", now=NOW)
        with pytest.raises(ValueError, match="role"):
            store.append_message(session.id, {"role": "invalid"}, now=NOW)
        with pytest.raises(ValueError, match="serializable"):
            store.append_message(session.id, {"role": "user", "content": {1}}, now=NOW)

        other = store.create_session(profile.id, now=NOW)
        run = store.create_run(session.id, now=NOW)
        with pytest.raises(ValueError, match="decided tool call"):
            approval = store.request_approval(
                run.id,
                tool_call_id="pending",
                tool_name="create_task",
                arguments={"title": "Pending"},
                now=NOW,
            )
            store.mark_approval_consumed(approval.id, now=NOW)
        store.transition_run(run.id, "cancelled", now=NOW)

        db.execute(
            "UPDATE agent_sessions SET status = 'archived' WHERE id = ?",
            (other.id,),
        )
        with pytest.raises(ValueError, match="archived"):
            store.create_run(other.id, now=NOW)

        session = store.create_session(profile.id, now=NOW)
        run = store.create_run(session.id, now=NOW)
        with pytest.raises(ValueError, match="does not belong"):
            store.append_message(other.id, {"role": "user", "content": "x"}, run_id=run.id)
        store.transition_run(run.id, "completed", now=NOW)
        with pytest.raises(ValueError, match="Only failed"):
            new_run = store.create_run(session.id, now=NOW)
            store.transition_run(new_run.id, "completed", error="no", now=NOW)
        store.transition_run(new_run.id, "failed", error="done", now=NOW)
        with pytest.raises(ValueError, match="active run"):
            store.add_usage(new_run.id, {})
        with pytest.raises(ValueError, match="active run"):
            store.request_approval(new_run.id, tool_call_id="1", tool_name="x", arguments={})
        db.commit()
        assert folder in store.list_folders()


@pytest.mark.parametrize(
    "usage",
    [{"other": 1}, {"prompt_tokens": -1}, {"prompt_tokens": True}, {"total_tokens": "1"}],
)
def test_usage_validation_rejects_bad_values(usage):
    with pytest.raises(ValueError):
        _validated_usage(usage)


def test_profile_duplicate_and_validation_edges(app):
    with app.app_context():
        db = get_db()
        store = LlmProfileStore(db, environ={})
        first = create_profile(db)
        assert store.provider_config(first.id).api_key is None
        with pytest.raises(ValueError, match="already in use"):
            create_profile(db)
        second = create_profile(db, name="Second")
        with pytest.raises(ValueError, match="already in use"):
            store.update(second.id, {"name": first.name}, now=NOW)
        with pytest.raises(LlmProfileNotFoundError):
            store.get(999)

        invalid = [
            {"unknown": True},
            {"name": ""},
            {"model": ""},
            {"supports_tools": "yes"},
        ]
        for values in invalid:
            with pytest.raises(ValueError):
                store.update(first.id, values, now=NOW)

        with pytest.raises(ValueError, match="Unsupported field"):
            store.create({"unknown": True}, now=NOW)


def test_profile_store_preserves_unexpected_integrity_errors(app):
    with app.app_context():
        db = get_db()
        store = LlmProfileStore(db, environ={})
        profile = create_profile(db)

        db.execute(
            """
            CREATE TRIGGER reject_profile_insert
            BEFORE INSERT ON llm_profiles
            BEGIN
                SELECT RAISE(ABORT, 'synthetic insert rejection');
            END
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="synthetic insert rejection"):
            store.create(
                {
                    "name": "Rejected",
                    "base_url": "http://localhost:1234/v1",
                    "model": "model",
                },
                now=NOW,
            )
        db.execute("DROP TRIGGER reject_profile_insert")

        db.execute(
            """
            CREATE TRIGGER reject_profile_update
            BEFORE UPDATE ON llm_profiles
            BEGIN
                SELECT RAISE(ABORT, 'synthetic update rejection');
            END
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="synthetic update rejection"):
            store.update(profile.id, {"model": "rejected-model"}, now=NOW)
        assert store.get(profile.id).model == "fake-model"


@pytest.mark.parametrize(
    "values",
    [
        {"base_url": "http://localhost/v1", "model": ""},
        {"base_url": "http://localhost/v1", "model": "x", "api_key": 1},
        {"base_url": "http://localhost/v1", "model": "x", "timeout_seconds": True},
        {"base_url": "http://localhost/v1", "model": "x", "timeout_seconds": 0},
        {"base_url": "http://localhost/v1", "model": "x", "timeout_seconds": 601},
    ],
)
def test_llm_config_validation_edges(values):
    with pytest.raises(LlmConfigurationError):
        LlmConfig(**values)


def test_provider_protocol_edges():
    class Completions:
        response = None

        def create(self, **_kwargs):
            return self.response

    completions = Completions()
    client = type("Client", (), {"chat": type("Chat", (), {"completions": completions})()})()
    provider = ChatCompletionsProvider(LlmConfig("http://localhost/v1", "model"), client=client)
    with pytest.raises(ValueError, match="one message"):
        provider.complete([])

    for response, match in [
        ({"choices": []}, "choice"),
        ({"choices": [{}]}, "message"),
        ({"choices": [{"message": {"content": 5}}]}, "content"),
        (
            {"choices": [{"message": {"content": None, "tool_calls": [{"id": "x"}]}}]},
            "tool call",
        ),
    ]:
        completions.response = response
        with pytest.raises(LlmProtocolError, match=match):
            provider.complete([{"role": "user", "content": "x"}])

    assert _parse_usage(
        {"prompt_tokens": True, "completion_tokens": "2", "total_tokens": None}
    ) == {}


def test_all_agent_tool_mutations_and_validation(app):
    with app.app_context():
        db = get_db()
        registry = TaskToolRegistry(db, app.config["TZINFO"])
        with pytest.raises(ValueError, match="must be an object"):
            registry.execute("list_tasks", [])
        first = registry.execute("create_task", {"title": "First", "description": "Body"}, now=NOW)
        second = registry.execute("create_task", {"title": "Second"}, now=NOW)
        task_id = first["id"]
        assert registry.execute("add_comment", {"task_id": task_id, "body": "Progress"}, now=NOW)
        assert registry.execute("set_waiting", {"task_id": task_id, "person_name": "Ada"}, now=NOW)["created"]
        assert registry.execute("record_follow_up", {"task_id": task_id}, now=NOW)["recorded"]
        assert registry.execute("resolve_waiting", {"task_id": task_id}, now=NOW)["resolved"]
        label = registry.execute("add_label", {"task_id": task_id, "name": "Launch"}, now=NOW)
        assert registry.execute("remove_label", {"task_id": task_id, "label_id": label["label_id"]}, now=NOW)["removed"]
        dependency = {"blocked_task_id": task_id, "blocker_task_id": second["id"]}
        assert registry.execute("add_dependency", dependency, now=NOW)["created"]
        assert registry.execute("remove_dependency", dependency, now=NOW)["removed"]

        with pytest.raises(ValueError, match="status"):
            registry.execute("list_tasks", {"status": "blocked"})
        with pytest.raises(ValueError, match="Limit"):
            registry.execute("list_tasks", {"limit": True})
        with pytest.raises(ValueError, match="description"):
            registry.execute("create_task", {"title": "Bad", "description": 1})
        with pytest.raises(ValueError, match="field"):
            registry.execute("update_task", {"task_id": task_id})
        with pytest.raises(ValueError, match="must be text"):
            registry.execute("update_task", {"task_id": task_id, "status": 1})


@pytest.mark.parametrize(
    ("function", "value"),
    [
        (valid_due_date, 1),
        (valid_due_date, "bad"),
        (_valid_follow_up_time, 1),
        (_valid_follow_up_time, "9:00"),
        (_valid_follow_up_time, "25:00"),
        (_person_name, None),
        (_person_name, " "),
        (_waiting_note, 1),
        (_waiting_note, "x" * 1001),
        (_create_request_id, "bad"),
    ],
)
def test_task_value_validation_edges(function, value):
    with pytest.raises(ValueError):
        function(value)


def test_follow_up_date_and_rank_rebalance_edges(app):
    with pytest.raises(ValueError):
        _valid_follow_up_date(1, date(2026, 9, 20))
    with pytest.raises(ValueError):
        _valid_follow_up_date("bad", date(2026, 9, 20))
    with pytest.raises(ValueError, match="past"):
        _valid_follow_up_date("2026-09-19", date(2026, 9, 20))

    with app.app_context():
        db = get_db()
        service = TaskService(db, app.config["TZINFO"])
        task = service.create_task(title="Rank", now=NOW)
        db.execute("UPDATE tasks SET rank_key = ? WHERE id = ?", (SQLITE_INTEGER_MIN + 1, task.task_id))
        assert new_task_rank_key(db) == 0


def test_task_service_state_transition_and_conflict_edges(app):
    assert valid_due_date(None) is None

    with app.app_context():
        db = get_db()
        service = TaskService(db, app.config["TZINFO"])
        blocked = service.create_task(title="Blocked", now=NOW).task_id
        blocker = service.create_task(title="Blocker", now=NOW).task_id

        with pytest.raises(ValueError, match="Invalid blocked task"):
            service.create_task(title="Bad blocker", blocks_task_id="not-an-id", now=NOW)

        service.update_task(blocked, {"status": "done"}, now=NOW)
        with pytest.raises(ValueError, match="Reopen"):
            service.create_task(title="Too late", blocks_task_id=blocked, now=NOW)

        blocker_row = service.task(blocker)
        assert service._change_status(blocker_row, "todo", NOW) is False
        service.update_task(blocked, {"status": "todo"}, now=NOW)
        assert service.add_dependency(blocked, blocker, now=NOW) is True
        service.update_task(blocker, {"status": "done"}, now=NOW)
        service.update_task(blocker, {"status": "todo"}, now=NOW)

        with pytest.raises(ValueError, match="Unsupported field"):
            service.update_task(blocked, {"unknown": True}, now=NOW)
        with pytest.raises(ValueError, match="Title"):
            service.update_task(blocked, {"title": " "}, now=NOW)
        with pytest.raises(ValueError, match="Description"):
            service.update_task(blocked, {"description": "x" * 5001}, now=NOW)
        unchanged = service.update_task(blocked, {"title": "Blocked"}, now=NOW)
        assert unchanged.changed is False
        with pytest.raises(ValueError, match="Comment"):
            service.add_comment(blocked, "x" * 2001, now=NOW)

        waiting_task = service.create_task(title="Waiting", now=NOW).task_id
        with pytest.raises(ValueError, match="who"):
            service.set_waiting(waiting_task, {}, now=NOW)
        with pytest.raises(ValueError, match="date"):
            service.set_waiting(
                waiting_task,
                {
                    "person_name": "Alex",
                    "next_follow_up_on": None,
                    "next_follow_up_time": "09:30",
                },
                now=NOW,
            )
        service.set_waiting(
            waiting_task,
            {
                "person_name": "Alex",
                "next_follow_up_on": "2026-09-21",
                "next_follow_up_time": "09:30",
            },
            now=NOW,
        )
        with pytest.raises(ValueError, match="Unsupported field"):
            service.record_follow_up(waiting_task, {"unknown": True}, now=NOW)
        with pytest.raises(ValueError, match="date"):
            service.record_follow_up(
                waiting_task, {"next_follow_up_on": None}, now=NOW
            )

        idle_task = service.create_task(title="Idle", now=NOW).task_id
        with pytest.raises(ValueError, match="not waiting"):
            service.record_follow_up(idle_task, {}, now=NOW)
        with pytest.raises(ValueError, match="not waiting"):
            service.resolve_waiting(idle_task, now=NOW)

        service.update_task(waiting_task, {"status": "done"}, now=NOW)
        with pytest.raises(ValueError, match="completed"):
            service.record_follow_up(waiting_task, {}, now=NOW)

        with pytest.raises(ValueError, match="reserved"):
            service.add_label(idle_task, "active:2026-09-20", now=NOW)
        db.execute("INSERT INTO labels(name, type) VALUES ('system-name', 'active_date')")
        with pytest.raises(ValueError, match="reserved"):
            service.add_label(idle_task, "system-name", now=NOW)
        assert service.remove_label(idle_task, 999, now=NOW) is False

        assert service.add_dependency(idle_task, blocker, now=NOW) is True
        assert service.add_dependency(idle_task, blocker, now=NOW) is False
        assert service.remove_dependency(idle_task, blocked, now=NOW) is False
