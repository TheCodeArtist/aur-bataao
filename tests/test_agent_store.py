from datetime import datetime, timezone
import sqlite3

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
        assert store.message_records(session.id) == [
            {
                "message_id": 1,
                "role": "user",
                "content": "What should I work on?",
                "created_at": NOW.isoformat(timespec="seconds"),
            }
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

        with pytest.raises(ValueError, match="Finish the active"):
            store.create_run(session.id, now=NOW)
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
        consumed = store.mark_approval_consumed(approval.id, now=NOW)
        assert consumed.status == "executed"
        with pytest.raises(ValueError, match="already been decided"):
            store.decide_approval(approval.id, False, now=NOW)

        assert [event["event_type"] for event in store.events(run.id)] == [
            "run_started",
            "approval_requested",
            "run_waiting_approval",
            "approval_approved",
            "approval_consumed",
        ]


def test_sessions_can_be_moved_between_folders(app):
    with app.app_context():
        db = get_db()
        store = AgentStore(db)
        session = store.create_session(LlmProfileStore(db).get().id, now=NOW)

        folder = store.create_folder("Planning", now=NOW)
        moved = store.move_session(session.id, folder.id)
        assert moved.folder_id == folder.id
        assert store.list_folders() == [folder]
        assert store.move_session(session.id, None).folder_id is None
        store.delete_folder(folder.id)
        assert store.list_folders() == []
        with pytest.raises(AgentNotFoundError):
            store.session("missing")


def test_legacy_archived_sessions_migrate_to_archived_folder(tmp_path):
    database = tmp_path / "legacy-agent.sqlite3"
    db = sqlite3.connect(database)
    db.execute(
        """
        CREATE TABLE agent_sessions (
            id TEXT PRIMARY KEY,
            profile_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    timestamp = NOW.isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO agent_sessions VALUES (?, ?, ?, ?, ?, ?)",
        ("legacy-chat", 1, "Saved chat", "archived", timestamp, timestamp),
    )
    db.commit()
    db.close()

    migrated_app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(database),
            "USER_TIMEZONE": "Asia/Kolkata",
            "LLM_MODEL": "",
        }
    )
    with migrated_app.app_context():
        row = get_db().execute(
            """
            SELECT s.status, f.name AS folder_name
            FROM agent_sessions s
            JOIN agent_folders f ON f.id = s.folder_id
            WHERE s.id = 'legacy-chat'
            """
        ).fetchone()
        assert dict(row) == {"status": "active", "folder_name": "Archived"}


def test_application_restart_marks_running_run_as_failed(tmp_path):
    database = tmp_path / "restart.sqlite3"
    first_app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(database),
            "USER_TIMEZONE": "Asia/Kolkata",
            "LLM_MODEL": "",
        }
    )
    with first_app.app_context():
        db = get_db()
        profile = LlmProfileStore(db).create(
            {
                "name": "Local",
                "base_url": "http://localhost:1234/v1",
                "model": "local-model",
            },
            now=NOW,
        )
        session = AgentStore(db).create_session(profile.id, now=NOW)
        run = AgentStore(db).create_run(session.id, now=NOW)
        db.commit()

    restarted_app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(database),
            "USER_TIMEZONE": "Asia/Kolkata",
            "LLM_MODEL": "",
        }
    )
    with restarted_app.app_context():
        recovered = AgentStore(get_db()).run(run.id)

        assert recovered.status == "failed"
        assert recovered.error == "Agent run was interrupted by an application restart"
