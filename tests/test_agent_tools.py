from datetime import datetime, timezone

import pytest

from agent_tools import TaskToolRegistry, ToolNotFoundError
from app import create_app, get_db
from task_service import TaskService


NOW = datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc)


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "test.sqlite3"),
            "USER_TIMEZONE": "Asia/Kolkata",
        }
    )


def test_registry_exposes_unique_openai_function_tools_and_approval_policy(app):
    with app.app_context():
        registry = TaskToolRegistry(get_db(), app.config["TZINFO"])

        tools = registry.openai_tools()
        names = [tool["function"]["name"] for tool in tools]

        assert len(names) == len(set(names))
        assert {"list_tasks", "get_task", "create_task", "update_task"} <= set(names)
        assert registry.requires_approval("list_tasks") is False
        assert registry.requires_approval("get_task") is False
        assert registry.requires_approval("create_task") is True
        assert registry.requires_approval("resolve_waiting") is True
        assert all(
            tool["function"]["parameters"]["additionalProperties"] is False
            for tool in tools
        )


def test_read_tools_return_compact_and_detailed_task_context(app):
    with app.app_context():
        db = get_db()
        service = TaskService(db, app.config["TZINFO"])
        task = service.create_task(title="Write launch plan", now=NOW)
        service.add_comment(task.task_id, "Outlined milestones", now=NOW)
        service.add_label(task.task_id, "Launch", now=NOW)
        registry = TaskToolRegistry(db, app.config["TZINFO"])

        listed = registry.execute("list_tasks", {"status": "todo"}, now=NOW)
        detail = registry.execute("get_task", {"task_id": task.task_id}, now=NOW)

        assert listed == [
            {
                "id": task.task_id,
                "title": "Write launch plan",
                "description": "",
                "status": "todo",
                "due_date": None,
                "waiting_on": None,
                "unresolved_blockers": 0,
                "updated_at": "2026-09-20T10:30:00+00:00",
            }
        ]
        assert detail["comments"][0]["body"] == "Outlined milestones"
        assert detail["labels"][0]["name"] == "launch"


def test_write_tools_use_task_service_and_leave_transaction_to_caller(app):
    with app.app_context():
        db = get_db()
        registry = TaskToolRegistry(db, app.config["TZINFO"])

        created = registry.execute(
            "create_task",
            {
                "title": "Prepare demo",
                "description": "Draft a walkthrough",
                "due_date": "2026-09-25",
            },
            now=NOW,
        )
        updated = registry.execute(
            "update_task",
            {"task_id": created["id"], "status": "in_progress"},
            now=NOW,
        )
        comment = registry.execute(
            "add_comment",
            {"task_id": created["id"], "body": "Started the outline"},
            now=NOW,
        )

        assert updated["status"] == "in_progress"
        assert comment["comment_id"] > 0
        assert db.in_transaction is True
        db.rollback()
        assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_tool_boundary_rejects_unknown_tools_and_arguments(app):
    with app.app_context():
        registry = TaskToolRegistry(get_db(), app.config["TZINFO"])

        with pytest.raises(ToolNotFoundError, match="Unknown tool"):
            registry.execute("run_shell", {})
        with pytest.raises(ValueError, match="Unsupported tool argument"):
            registry.execute("list_tasks", {"secret": True})
        with pytest.raises(ValueError, match="positive integer"):
            registry.execute("get_task", {"task_id": True})
        with pytest.raises(ValueError, match="title must be"):
            registry.execute("create_task", {})
        with pytest.raises(ValueError, match="counts_as_progress must be"):
            registry.execute(
                "add_comment",
                {"task_id": 1, "body": "Progress", "counts_as_progress": "yes"},
            )
