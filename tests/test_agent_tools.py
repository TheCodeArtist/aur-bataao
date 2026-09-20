from datetime import datetime, timezone

import pytest

from agent_tools import TaskToolRegistry, ToolNotFoundError
from app import get_db
from task_service import TaskService


NOW = datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc)


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
                "next_follow_up_on": None,
                "next_follow_up_time": None,
                "unresolved_blockers": 0,
                "blocking_tasks": 0,
                "updated_at": "2026-09-20T10:30:00+00:00",
            }
        ]
        assert detail["comments"][0]["body"] == "Outlined milestones"
        assert detail["labels"][0]["name"] == "launch"


def test_read_tools_page_filter_search_and_traverse_both_dependency_directions(app):
    with app.app_context():
        db = get_db()
        service = TaskService(db, app.config["TZINFO"])
        blocker = service.create_task(title="Approve rollout", now=NOW)
        blocked = service.create_task(title="Ship release", now=NOW)
        waiting = service.create_task(title="Vendor response", now=NOW)
        other = service.create_task(title="Write retrospective", now=NOW)
        service.update_task(
            blocker.task_id, {"due_date": "2026-09-19"}, now=NOW
        )
        service.update_task(
            blocked.task_id,
            {
                "description": "Deployment plan with a canary phase",
                "due_date": "2026-09-24",
            },
            now=NOW,
        )
        service.update_task(
            other.task_id, {"due_date": "2026-10-01"}, now=NOW
        )
        service.add_comment(blocked.task_id, "Document the rollback switch", now=NOW)
        service.add_label(blocked.task_id, "Launch", now=NOW)
        service.add_dependency(blocked.task_id, blocker.task_id, now=NOW)
        service.set_waiting(
            waiting.task_id,
            {
                "person_name": "Ada",
                "next_follow_up_on": "2026-09-20",
                "next_follow_up_time": "15:59",
            },
            now=NOW,
        )
        registry = TaskToolRegistry(db, app.config["TZINFO"])

        assert [
            task["id"]
            for task in registry.execute(
                "list_tasks", {"limit": 2, "offset": 1}, now=NOW
            )
        ] == [waiting.task_id, blocked.task_id]
        assert _listed_ids(registry, {"label": "LAUNCH"}) == [blocked.task_id]
        assert set(_listed_ids(registry, {"blocked": True})) == {
            blocked.task_id,
            waiting.task_id,
        }
        assert set(_listed_ids(registry, {"blocked": False})) == {
            blocker.task_id,
            other.task_id,
        }
        assert _listed_ids(registry, {"blocking": True}) == [blocker.task_id]
        assert set(_listed_ids(registry, {"blocking": False})) == {
            blocked.task_id,
            waiting.task_id,
            other.task_id,
        }
        assert _listed_ids(registry, {"follow_up_overdue": True}) == [
            waiting.task_id
        ]
        assert set(_listed_ids(registry, {"follow_up_overdue": False})) == {
            blocker.task_id,
            blocked.task_id,
            other.task_id,
        }
        assert _listed_ids(registry, {"overdue": True}) == [blocker.task_id]
        assert set(_listed_ids(registry, {"overdue": False})) == {
            blocked.task_id,
            waiting.task_id,
            other.task_id,
        }
        assert _listed_ids(
            registry,
            {"due_from": "2026-09-23", "due_through": "2026-09-30"},
        ) == [blocked.task_id]
        assert _listed_ids(registry, {"search": "APPROVE"}) == [blocker.task_id]
        assert _listed_ids(registry, {"search": "canary"}) == [blocked.task_id]
        assert _listed_ids(registry, {"search": "ROLLBACK"}) == [blocked.task_id]
        assert _listed_ids(
            registry,
            {
                "status": "todo",
                "label": "launch",
                "blocked": True,
                "due_through": "2026-09-24",
                "search": "release",
            },
        ) == [blocked.task_id]

        blocker_summary = registry.execute(
            "list_tasks", {"blocking": True}, now=NOW
        )[0]
        waiting_summary = registry.execute(
            "list_tasks", {"follow_up_overdue": True}, now=NOW
        )[0]
        blocker_detail = registry.execute(
            "get_task", {"task_id": blocker.task_id}, now=NOW
        )
        blocked_detail = registry.execute(
            "get_task", {"task_id": blocked.task_id}, now=NOW
        )

        assert blocker_summary["blocking_tasks"] == 1
        assert waiting_summary["next_follow_up_on"] == "2026-09-20"
        assert waiting_summary["next_follow_up_time"] == "15:59"
        assert blocker_detail["dependents"] == [
            {"id": blocked.task_id, "title": "Ship release", "status": "todo"}
        ]
        assert blocked_detail["blockers"] == [
            {"id": blocker.task_id, "title": "Approve rollout", "status": "todo"}
        ]

        assert registry.execute(
            "list_tasks", {"follow_up_overdue": True}
        )[0]["id"] == waiting.task_id


def _listed_ids(
    registry: TaskToolRegistry, arguments: dict[str, object]
) -> list[int]:
    return [task["id"] for task in registry.execute("list_tasks", arguments, now=NOW)]


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
        with pytest.raises(ValueError, match="Offset must be"):
            registry.execute("list_tasks", {"offset": True})
        with pytest.raises(ValueError, match="label must be"):
            registry.execute("list_tasks", {"label": " "})
        with pytest.raises(ValueError, match="blocked must be"):
            registry.execute("list_tasks", {"blocked": "yes"})
        with pytest.raises(ValueError, match="blocking must be"):
            registry.execute("list_tasks", {"blocking": 1})
        with pytest.raises(ValueError, match="follow_up_overdue must be"):
            registry.execute("list_tasks", {"follow_up_overdue": None})
        with pytest.raises(ValueError, match="overdue must be"):
            registry.execute("list_tasks", {"overdue": "yes"})
        with pytest.raises(ValueError, match="due_from must be"):
            registry.execute("list_tasks", {"due_from": 20260920})
        with pytest.raises(ValueError, match="due_through must be"):
            registry.execute("list_tasks", {"due_through": "20 September"})
        with pytest.raises(ValueError, match="must not be after"):
            registry.execute(
                "list_tasks",
                {"due_from": "2026-09-21", "due_through": "2026-09-20"},
            )
        with pytest.raises(ValueError, match="search must be"):
            registry.execute("list_tasks", {"search": []})
