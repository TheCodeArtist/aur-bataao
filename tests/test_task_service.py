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
