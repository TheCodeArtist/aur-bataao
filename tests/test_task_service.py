from datetime import datetime, timezone

import pytest

from app import create_app, get_db
from task_service import TaskNotFoundError, TaskService


NOW = datetime(2026, 9, 20, 8, 30, tzinfo=timezone.utc)


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "test.sqlite3"),
            "USER_TIMEZONE": "Asia/Kolkata",
        }
    )


def test_service_create_and_update_use_the_task_domain(app):
    with app.app_context():
        db = get_db()
        service = TaskService(db, app.config["TZINFO"])

        blocked = service.create_task(title="Ship release", now=NOW)
        blocker = service.create_task(
            title="Finish verification",
            blocks_task_id=blocked.task_id,
            now=NOW,
        )
        update = service.update_task(
            blocker.task_id,
            {"status": "in_progress", "description": "Run the complete suite"},
            now=NOW,
        )
        db.commit()

        assert blocked.created is True
        assert blocker.created is True
        assert update.changed is True
        task = service.task(blocker.task_id)
        assert task["status"] == "in_progress"
        assert task["description"] == "Run the complete suite"
        assert db.execute(
            "SELECT 1 FROM task_dependencies WHERE blocked_task_id = ? AND blocker_task_id = ?",
            (blocked.task_id, blocker.task_id),
        ).fetchone()
        assert db.execute(
            """
            SELECT 1
            FROM task_labels tl JOIN labels l ON l.id = tl.label_id
            WHERE tl.task_id = ? AND l.name = 'active:2026-09-20'
            """,
            (blocker.task_id,),
        ).fetchone()


def test_service_leaves_transaction_control_to_the_caller(app):
    with app.app_context():
        db = get_db()
        service = TaskService(db, app.config["TZINFO"])

        service.create_task(title="Temporary task", now=NOW)
        assert db.in_transaction is True
        db.rollback()

        assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_service_creation_is_idempotent_by_request_id(app):
    with app.app_context():
        db = get_db()
        service = TaskService(db, app.config["TZINFO"])
        request_id = "11111111-1111-4111-8111-111111111111"

        first = service.create_task(
            title="Created once", request_id=request_id, now=NOW
        )
        second = service.create_task(
            title="Ignored retry title", request_id=request_id, now=NOW
        )
        db.commit()

        assert first.created is True
        assert second.created is False
        assert second.task_id == first.task_id
        assert db.execute("SELECT title FROM tasks").fetchone()["title"] == "Created once"


def test_service_reports_missing_tasks_without_flask_coupling(app):
    with app.app_context():
        service = TaskService(get_db(), app.config["TZINFO"])

        with pytest.raises(TaskNotFoundError):
            service.update_task(999, {"title": "Missing"}, now=NOW)


def test_service_exposes_agent_ready_task_actions(app):
    with app.app_context():
        db = get_db()
        service = TaskService(db, app.config["TZINFO"])
        task = service.create_task(title="Prepare launch", now=NOW)
        blocker = service.create_task(title="Approve launch", now=NOW)

        comment_id = service.add_comment(
            task.task_id,
            "Drafted the rollout plan",
            counts_as_progress=True,
            now=NOW,
        )
        waiting = service.set_waiting(
            task.task_id,
            {"person_name": "  Ravi   Kumar  ", "note": "Finance review"},
            now=NOW,
        )
        label_id = service.add_label(task.task_id, "Launch", now=NOW)
        assert service.add_dependency(task.task_id, blocker.task_id, now=NOW)
        service.record_follow_up(
            task.task_id,
            {"note": "Sent the revised plan", "next_follow_up_on": "2026-09-24"},
            now=NOW,
        )
        assert service.remove_label(task.task_id, label_id, now=NOW)
        assert service.remove_dependency(task.task_id, blocker.task_id, now=NOW)
        service.resolve_waiting(task.task_id, now=NOW)
        db.commit()

        assert comment_id > 0
        assert waiting.created is True
        assert service.active_waiting(task.task_id) is None
        assert db.execute(
            "SELECT last_progress_at FROM tasks WHERE id = ?", (task.task_id,)
        ).fetchone()["last_progress_at"] is not None
        event_types = [
            row["event_type"]
            for row in db.execute(
                "SELECT event_type FROM task_events WHERE task_id = ? ORDER BY id",
                (task.task_id,),
            ).fetchall()
        ]
        assert event_types == [
            "task_created",
            "comment_added",
            "waiting_started",
            "label_added",
            "dependency_added",
            "followed_up",
            "label_removed",
            "dependency_removed",
            "waiting_resolved",
        ]


def test_service_validates_tool_shaped_inputs_before_writes(app):
    with app.app_context():
        db = get_db()
        service = TaskService(db, app.config["TZINFO"])
        task = service.create_task(title="Safe task", now=NOW)

        with pytest.raises(ValueError, match="Unsupported field"):
            service.set_waiting(
                task.task_id, {"person_name": "Ravi", "unexpected": True}, now=NOW
            )
        with pytest.raises(ValueError, match="cannot block itself"):
            service.add_dependency(task.task_id, task.task_id, now=NOW)

        assert service.active_waiting(task.task_id) is None
        assert db.execute("SELECT count(*) FROM task_dependencies").fetchone()[0] == 0
