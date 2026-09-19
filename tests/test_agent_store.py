from datetime import datetime, timezone

import pytest

from agent_store import AgentNotFoundError, AgentStore
from app import create_app, get_db
from llm_profiles import LlmProfileStore


NOW = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)


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
        LlmProfileStore(get_db()).create(
            {
                "name": "Local",
                "base_url": "http://localhost:1234/v1",
                "model": "local-model",
            },
            now=NOW,
        )
        get_db().commit()
    return app


def test_persists_conversation_run_snapshot_and_audit_events(app):
    with app.app_context():
        db = get_db()
        profile = LlmProfileStore(db).get()
        store = AgentStore(db)
        session = store.create_session(profile.id, title="Plan my day", now=NOW)
        run = store.create_run(session.id, now=NOW)
        store.append_message(
            session.id,
            {"role": "user", "content": "What should I work on?"},
            run_id=run.id,
            now=NOW,
        )
        store.record_event(run.id, "llm_response", {"tool_calls": 0}, now=NOW)
        completed = store.transition_run(
            run.id,
            "completed",
            usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            now=NOW,
        )
        db.commit()

        assert store.messages(session.id) == [
            {"role": "user", "content": "What should I work on?"}
        ]
        assert completed.model == "local-model"
        assert completed.base_url == "http://localhost:1234/v1"
        assert completed.total_tokens == 14
        assert [event["event_type"] for event in store.events(run.id)] == [
            "run_started",
            "llm_response",
            "run_completed",
        ]


def test_run_state_machine_rejects_invalid_transitions(app):
    with app.app_context():
        db = get_db()
        store = AgentStore(db)
        session = store.create_session(LlmProfileStore(db).get().id, now=NOW)
        run = store.create_run(session.id, now=NOW)

        with pytest.raises(ValueError, match="Failed runs require"):
            store.transition_run(run.id, "failed", now=NOW)
        store.transition_run(run.id, "completed", now=NOW)
        with pytest.raises(ValueError, match="Cannot transition"):
            store.transition_run(run.id, "running", now=NOW)


def test_approval_survives_pause_and_records_decision(app):
    with app.app_context():
        db = get_db()
        store = AgentStore(db)
        session = store.create_session(LlmProfileStore(db).get().id, now=NOW)
        run = store.create_run(session.id, now=NOW)

        approval = store.request_approval(
            run.id,
            tool_call_id="call-1",
            tool_name="complete_task",
            arguments={"task_id": 7},
            now=NOW,
        )
        db.commit()

        assert store.run(run.id).status == "waiting_approval"
        assert store.approval(approval.id).arguments == {"task_id": 7}
        decided = store.decide_approval(approval.id, True, now=NOW)
        assert decided.status == "approved"
        with pytest.raises(ValueError, match="already been decided"):
            store.decide_approval(approval.id, False, now=NOW)

        assert [event["event_type"] for event in store.events(run.id)] == [
            "run_started",
            "approval_requested",
            "run_waiting_approval",
            "approval_approved",
        ]


def test_session_archive_and_missing_records_are_explicit(app):
    with app.app_context():
        db = get_db()
        store = AgentStore(db)
        session = store.create_session(LlmProfileStore(db).get().id, now=NOW)

        archived = store.archive_session(session.id, now=NOW)

        assert archived.status == "archived"
        assert store.list_sessions() == []
        assert store.list_sessions(include_archived=True) == [archived]
        with pytest.raises(ValueError, match="archived"):
            store.create_run(session.id, now=NOW)
        with pytest.raises(AgentNotFoundError):
            store.session("missing")
